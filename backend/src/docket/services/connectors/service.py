"""Connector accounts: consent, sync into managed .eml files, disconnect."""
from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from sqlalchemy import select

from docket.core.db.models import ConnectorAccount
from docket.core.writer_lock import lock_for_sessions
from docket.services.connectors.base import Connector, ConnectorError, ConsentRequired
from docket.services.connectors.gmail_imap import DEFAULT_CONFIG as GMAIL_DEFAULTS, GmailImapConnector

PROVIDERS: dict[str, Callable[..., Connector]] = {"gmail": GmailImapConnector}
PROVIDER_DEFAULTS = {"gmail": GMAIL_DEFAULTS}

CONSENT_TEXT = (
    "This is a network-using component. Docket will connect to {host} and pull your mail "
    "READ-ONLY to this machine (nothing is sent, deleted, moved or marked read), store it "
    "under {where}, and index it locally. Queries and chat stay offline; the network is used "
    "only while syncing. Attachments are {attachments}."
)


@dataclass
class SyncResult:
    added: int = 0
    removed: int = 0
    skipped: int = 0
    truncated: bool = False


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._") or "folder"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class ConnectorService:
    def __init__(self, session_factory, settings, source_manager, *, connector_factory=None, purge: Callable[[str], object] | None = None):
        self.sessions, self.settings, self.sources = session_factory, settings, source_manager
        self._factory = connector_factory or (lambda provider, account, config: PROVIDERS[provider](account, config))
        self._purge = purge
        self.lock = lock_for_sessions(session_factory)

    def mail_dir(self, account_id: str) -> Path:
        return Path(self.settings.data_dir) / "connectors" / account_id / "mail"

    def consent_text(self, provider: str, config: dict) -> str:
        host = PROVIDERS[provider]("probe@example.com").allowed_hosts[0]
        return CONSENT_TEXT.format(host=host, where=Path(self.settings.data_dir) / "connectors",
                                   attachments="indexed" if config.get("include_attachments") else "NOT stored")

    def _config(self, provider: str, overrides: dict) -> dict:
        return {**PROVIDER_DEFAULTS[provider], **{k: v for k, v in overrides.items() if v is not None}}

    def add_account(self, provider: str, account: str, *, overrides: dict | None = None, consent: bool = False) -> ConnectorAccount:
        if provider not in PROVIDERS:
            raise ConnectorError(f"Unknown provider: {provider}")
        if not consent:
            raise ConsentRequired("Consent is required before connecting a mail account.")
        config = self._config(provider, overrides or {})
        connector = self._factory(provider, account, config)
        connector.validate_config()
        with self.sessions() as db:
            if db.execute(select(ConnectorAccount).where(ConnectorAccount.provider == provider,
                          ConnectorAccount.account_label == account)).first():
                raise ConnectorError("This account is already connected.")
            row = ConnectorAccount(provider=provider, account_label=account, config_json=json.dumps(config),
                                   consent_at=_now(), status="active")
            db.add(row)
            db.flush()
            account_id = row.id
            db.commit()
        folder = self.mail_dir(account_id)
        folder.mkdir(parents=True, exist_ok=True)
        source = self.sources.register_source(folder, display_name=f"{account} ({provider})")
        with self.sessions() as db:
            row = db.get(ConnectorAccount, account_id)
            row.source_id = source.id
            try:
                row.credential_ref = connector.authenticate()
            except ConnectorError:
                row.credential_ref = None  # secret may be exported later
            db.commit()
            db.refresh(row)
            db.expunge(row)
            return row

    def list_accounts(self) -> list[ConnectorAccount]:
        with self.sessions() as db:
            rows = list(db.execute(select(ConnectorAccount).order_by(ConnectorAccount.consent_at)).scalars())
            db.expunge_all()
            return rows

    def get(self, ref: str) -> ConnectorAccount:
        rows = self.list_accounts()
        hits = [r for r in rows if ref in (r.id, r.account_label) or r.id.startswith(ref)]
        if len(hits) != 1:
            raise ConnectorError(f"No unique connector matches {ref!r}.")
        return hits[0]

    def sync(self, ref: str) -> SyncResult:
        row = self.get(ref)
        if row.status != "active":
            raise ConnectorError("This connector is disconnected.")
        config = json.loads(row.config_json)
        connector = self._factory(row.provider, row.account_label, config)
        result = SyncResult()
        base = self.mail_dir(row.id)
        with self.lock.hold("syncing a mail connector"):
            connector.validate_config()
            connector.authenticate()
            try:
                batch = connector.list_changes(json.loads(row.cursor_json) if row.cursor_json else None)
                for change in batch:
                    target = base / _safe(change.folder) / f"{_safe(change.remote_id)}.eml"
                    if change.deleted:
                        if target.exists():
                            target.unlink()
                            result.removed += 1
                            try:
                                target.parent.rmdir()
                            except OSError:
                                pass
                    elif change.raw is not None:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        tmp = target.with_suffix(".part")
                        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                        with os.fdopen(fd, "wb") as handle:
                            handle.write(change.raw)
                        os.replace(tmp, target)
                        result.added += 1
            finally:
                connector.disconnect()
            result.skipped, result.truncated = batch.skipped, batch.truncated
            with self.sessions() as db:
                stored = db.get(ConnectorAccount, row.id)
                stored.cursor_json = json.dumps(batch.cursor)
                stored.last_sync_at = _now()
                db.commit()
        return result

    def sync_all(self) -> dict[str, SyncResult | ConnectorError]:
        out = {}
        for row in self.list_accounts():
            if row.status == "active":
                try:
                    out[row.account_label] = self.sync(row.id)
                except ConnectorError as exc:
                    out[row.account_label] = exc
        return out

    def remove(self, ref: str, *, purge: bool = False) -> None:
        row = self.get(ref)
        with self.lock.hold("disconnecting a mail connector"):
            if row.source_id and row.status == "active":
                self.sources.deactivate_source(row.source_id, reason="connector_disconnected")
            with self.sessions() as db:
                stored = db.get(ConnectorAccount, row.id)
                stored.status = "disconnected"
                stored.cursor_json = None
                db.commit()
            if purge:
                if row.source_id and self._purge:
                    self._purge(row.source_id)
                shutil.rmtree(Path(self.settings.data_dir) / "connectors" / row.id, ignore_errors=True)
