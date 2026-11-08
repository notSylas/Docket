"""Read-only mail connector tests against an in-memory fake imaplib server."""
from __future__ import annotations

import imaplib
import json
import re
import socket
from datetime import date
from email.message import EmailMessage
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect
from typer.testing import CliRunner

from docket.core.db.models import ConnectorAccount, SourceStatus
from docket.infra.parsing.mail_family import MAIL_EXTENSIONS
from docket.interfaces.cli.context import AppContext
from docket.services.connectors.base import ConnectorError, ConsentRequired, scrub
from docket.services.connectors.gmail_imap import GmailImapConnector, ReadOnlyImap, strip_attachments
from docket.services.connectors.service import ConnectorService

PASSWORD = "abcdwxyzabcdwxyz"


def make_eml(subject="Hello", body="Budget approved.", attachment=False) -> bytes:
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = "a@example.com", "me@gmail.com", subject
    m.set_content(body)
    if attachment:
        m.add_attachment(b"SECRETPAYLOAD" * 10, maintype="application", subtype="pdf", filename="x.pdf")
    return m.as_bytes()


class FakeServer:
    """Folders: name -> {"validity": int, "msgs": {uid: (gm_id, bytes)}}."""

    def __init__(self):
        self.folders = {"INBOX": {"validity": 1, "msgs": {}}, "[Gmail]/Sent Mail": {"validity": 7, "msgs": {}},
                        "[Gmail]/Spam": {"validity": 3, "msgs": {}}}
        self.commands: list[str] = []
        self.fail_login_with: str | None = None

    def factory(self, host, port):
        assert (host, port) == ("imap.gmail.com", 993)
        return FakeConn(self)


class FakeConn:
    def __init__(self, server):
        self.s, self.sel = server, None

    def login(self, user, pw):
        self.s.commands.append("LOGIN")
        if self.s.fail_login_with is not None:
            raise imaplib.IMAP4.error(self.s.fail_login_with.format(pw=pw, user=user))
        return "OK", [b"ok"]

    def list(self):
        return "OK", [b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren \\Sent) "/" "[Gmail]/Sent Mail"',
                      b'(\\HasNoChildren \\Junk) "/" "[Gmail]/Spam"']

    def select(self, mailbox, readonly=False):
        self.s.commands.append(f"SELECT readonly={readonly}")
        assert readonly is True
        self.sel = mailbox.strip('"')
        return "OK", [b"1"]

    def response(self, code):
        return code, [str(self.s.folders[self.sel]["validity"]).encode()]

    def uid(self, command, *args):
        self.s.commands.append(f"UID {command} {' '.join(map(str, args))}")
        msgs = self.s.folders[self.sel]["msgs"]
        if command == "SEARCH":
            return "OK", [" ".join(str(u) for u in sorted(msgs)).encode()]
        uid = int(args[0])
        if "BODY.PEEK[]" in args[1]:
            return "OK", [(f"{uid} (UID {uid} BODY[] {{{len(msgs[uid][1])}}}".encode(), msgs[uid][1]), b")"]
        gm, raw = msgs[uid]
        return "OK", [f"{uid} (X-GM-MSGID {gm} RFC822.SIZE {len(raw)} UID {uid})".encode()]

    def logout(self):
        self.s.commands.append("LOGOUT")
        return "BYE", [b""]


@pytest.fixture
def server():
    return FakeServer()


@pytest.fixture
def ctx(tmp_path):
    c = AppContext.for_testing(data_dir=tmp_path / "data")
    yield c
    c.engine.dispose()


def service_for(ctx, server, **extra):
    def factory(provider, account, config):
        return GmailImapConnector(account, config, secret_resolver=lambda: PASSWORD,
                                  imap_factory=server.factory, today=lambda: date(2026, 11, 1), **extra)
    return ConnectorService(ctx.session_factory, ctx.settings, ctx.source_manager, connector_factory=factory,
                            purge=lambda sid: None)


def add(service, **overrides):
    return service.add_account("gmail", "me@gmail.com", overrides=overrides, consent=True)


def emls(ctx, row):
    return sorted(p for p in (Path(ctx.settings.data_dir) / "connectors" / row.id / "mail").rglob("*") if p.is_file())


def test_first_sync_then_incremental_by_uid(ctx, server):
    server.folders["INBOX"]["msgs"] = {1: (101, make_eml("one")), 2: (102, make_eml("two"))}
    server.folders["[Gmail]/Sent Mail"]["msgs"] = {1: (201, make_eml("sent"))}
    svc = service_for(ctx, server)
    row = add(svc)
    r1 = svc.sync(row.id)
    assert r1.added == 3 and r1.removed == 0
    files = emls(ctx, row)
    assert {p.suffix for p in files} <= MAIL_EXTENSIONS
    assert {p.name for p in files} == {"gm65.eml", "gm66.eml", "gmc9.eml"}
    server.commands.clear()
    server.folders["INBOX"]["msgs"][3] = (103, make_eml("three"))
    r2 = svc.sync(row.id)
    assert r2.added == 1
    fetches = [c for c in server.commands if "FETCH" in c]
    assert all(c.startswith("UID FETCH 3 ") for c in fetches)  # only the new UID is fetched
    assert svc.get(row.id).last_sync_at is not None


def test_uidvalidity_reset_rescans(ctx, server):
    server.folders["INBOX"]["msgs"] = {1: (101, make_eml("one"))}
    svc = service_for(ctx, server)
    row = add(svc)
    svc.sync(row.id)
    server.folders["INBOX"]["validity"] = 99
    server.folders["INBOX"]["msgs"] = {1: (555, make_eml("renumbered"))}
    r = svc.sync(row.id)
    assert r.added == 1 and r.removed == 1
    assert [p.name for p in emls(ctx, row)] == ["gm22b.eml"]


def test_deletion_propagates(ctx, server):
    server.folders["INBOX"]["msgs"] = {1: (101, make_eml()), 2: (102, make_eml("b"))}
    svc = service_for(ctx, server)
    row = add(svc)
    svc.sync(row.id)
    del server.folders["INBOX"]["msgs"][1]
    r = svc.sync(row.id)
    assert r.removed == 1
    assert [p.name for p in emls(ctx, row)] == ["gm66.eml"]


def test_size_cap_and_message_cap(ctx, server):
    big = make_eml(body="x" * 5000)
    server.folders["INBOX"]["msgs"] = {1: (1, big), 2: (2, make_eml("ok")), 3: (3, make_eml("ok3"))}
    svc = service_for(ctx, server)
    row = add(svc, max_message_bytes=2000, max_messages_per_sync=1)
    r = svc.sync(row.id)
    assert r.skipped == 1 and r.added == 1 and r.truncated
    r = svc.sync(row.id)  # remainder on the next run
    assert r.added == 1 and not r.truncated
    assert len(emls(ctx, row)) == 2


def test_attachments_stripped_by_default_kept_when_enabled(ctx, server):
    server.folders["INBOX"]["msgs"] = {1: (1, make_eml(body="visible body", attachment=True))}
    svc = service_for(ctx, server)
    row = add(svc)
    svc.sync(row.id)
    text = emls(ctx, row)[0].read_bytes()
    assert b"visible body" in text and b"SECRETPAYLOAD" not in text and b"U0VDUkVUUEFZTE9BR" not in text
    assert b"attachment not stored" in text
    assert b"U0VDUkVUUEFZTE9BR" in strip_attachments.__globals__["email"].message_from_bytes(
        server.folders["INBOX"]["msgs"][1][1]).as_bytes()  # sanity: raw has it
    svc2 = service_for(ctx, server)
    server.folders["INBOX"]["msgs"] = {1: (1, make_eml(body="visible body", attachment=True))}
    cfg = json.loads(svc.get(row.id).config_json)
    assert cfg["include_attachments"] is False
    kept = GmailImapConnector("me@gmail.com", {**cfg, "include_attachments": True}, secret_resolver=lambda: PASSWORD,
                              imap_factory=server.factory, today=lambda: date(2026, 11, 1))
    batch = kept.list_changes(None)
    assert any(b"application/pdf" in c.raw for c in batch if c.raw)
    del svc2


def test_connection_is_strictly_read_only(ctx, server):
    server.folders["INBOX"]["msgs"] = {1: (1, make_eml())}
    svc = service_for(ctx, server)
    row = add(svc)
    svc.sync(row.id)
    cmds = "\n".join(server.commands)
    assert "SELECT readonly=True" in cmds and "readonly=False" not in cmds
    assert "BODY.PEEK[]" in cmds
    for banned in ("STORE", "EXPUNGE", "COPY", "APPEND", "DELETE", "MOVE", "\\Seen"):
        assert banned not in cmds
    assert "Spam" not in cmds  # never opened
    guarded = ReadOnlyImap(FakeConn(server))
    for bad in (("STORE", "1", "+FLAGS", "\\Seen"), ("COPY", "1", "X"), ("EXPUNGE",), ("FETCH", "1", "(BODY[])"),
                ("FETCH", "1", "(RFC822)")):
        with pytest.raises(ConnectorError):
            guarded.uid(*bad)
    assert not hasattr(guarded, "store") and not hasattr(guarded, "expunge")


def test_spam_trash_drafts_rejected():
    for folder in ("[Gmail]/Spam", "[Gmail]/Trash", "[Gmail]/Drafts"):
        with pytest.raises(ConnectorError):
            GmailImapConnector("me@gmail.com", {"folders": [folder]}).validate_config()


def test_failed_login_error_contains_no_secret(ctx, server):
    server.fail_login_with = "[AUTHENTICATIONFAILED] bad password {pw} for {user}"
    svc = service_for(ctx, server)
    row = add(svc)
    with pytest.raises(ConnectorError) as err:
        svc.sync(row.id)
    text = str(err.value)
    assert PASSWORD not in text and "me@gmail.com" not in text and "sign-in failed" in text
    assert PASSWORD not in scrub(f"x {PASSWORD}", (PASSWORD,))


def test_password_never_in_logs_or_db(ctx, server, caplog):
    import logging
    caplog.set_level(logging.DEBUG)
    server.folders["INBOX"]["msgs"] = {1: (1, make_eml("Private subject", "Private body"))}
    svc = service_for(ctx, server)
    row = add(svc)
    svc.sync(row.id)
    assert PASSWORD not in caplog.text and "Private body" not in caplog.text
    assert PASSWORD not in json.dumps(row.config_json) and PASSWORD not in (row.credential_ref or "")
    raw_db = Path(ctx.settings.sqlite_path).read_bytes()
    assert PASSWORD.encode() not in raw_db
    assert row.credential_ref == "env:DOCKET_GMAIL_APP_PASSWORD_ME_GMAIL_COM"


def test_consent_required(ctx, server):
    svc = service_for(ctx, server)
    with pytest.raises(ConsentRequired):
        svc.add_account("gmail", "me@gmail.com")
    assert svc.list_accounts() == []
    assert "READ-ONLY" in svc.consent_text("gmail", {}) and "network" in svc.consent_text("gmail", {})


def test_remove_disconnects_source_and_purge_deletes_files(ctx, server):
    server.folders["INBOX"]["msgs"] = {1: (1, make_eml())}
    purged = []
    svc = service_for(ctx, server)
    svc._purge = purged.append
    row = add(svc)
    svc.sync(row.id)
    svc.remove(row.id)
    assert ctx.source_manager.get_source(row.source_id).status == SourceStatus.REVOKED
    assert svc.get(row.id).status == "disconnected" and emls(ctx, row)
    with pytest.raises(ConnectorError):
        svc.sync(row.id)
    svc.remove(row.id, purge=True)
    assert purged == [row.source_id] and not emls(ctx, row)


def test_sync_holds_writer_lock(ctx, server, monkeypatch):
    held = []
    svc = service_for(ctx, server)
    row = add(svc)
    real = svc.lock.hold

    from contextlib import contextmanager

    @contextmanager
    def spy(op):
        held.append(op)
        with real(op):
            yield
    monkeypatch.setattr(svc.lock, "hold", spy)
    svc.sync(row.id)
    assert held == ["syncing a mail connector"]


def test_synced_mail_is_indexed_by_existing_reader(ctx, server):
    from docket.infra.parsing.mail_family import MailParser  # existing reader, unchanged
    server.folders["INBOX"]["msgs"] = {1: (1, make_eml("Q3", "The committee approved the budget."))}
    svc = service_for(ctx, server)
    row = add(svc)
    svc.sync(row.id)
    parsed = MailParser().parse(row.source_id, emls(ctx, row)[0])
    assert any("committee approved" in m.text for m in parsed.messages)


def test_migration_0014_upgrades_empty_db_and_roundtrips(tmp_path):
    from tests.unit.conftest import _alembic_config
    path = tmp_path / "d.sqlite3"
    cfg = _alembic_config(path)
    command.upgrade(cfg, "head")
    engine = create_engine(f"sqlite:///{path}")
    cols = {c["name"] for c in inspect(engine).get_columns("connector_accounts")}
    assert {"id", "provider", "account_label", "config_json", "cursor_json", "last_sync_at", "consent_at", "status"} <= cols
    engine.dispose()
    command.downgrade(cfg, "c9f2a4b56d03")
    engine = create_engine(f"sqlite:///{path}")
    assert "connector_accounts" not in inspect(engine).get_table_names()
    engine.dispose()


def test_cli_wiring(tmp_path, monkeypatch):
    from docket.interfaces.cli.main import app
    monkeypatch.setenv("DOCKET_DATA_DIR", str(tmp_path / "data"))
    runner = CliRunner()
    assert "2-Step Verification" in runner.invoke(app, ["connectors", "guide", "gmail"]).output
    assert "app password" in runner.invoke(app, ["connectors", "add", "--help"]).output
    declined = runner.invoke(app, ["connectors", "add", "gmail", "--account", "me@gmail.com"], input="n\n")
    assert declined.exit_code == 1 and "read-only" in declined.output.lower().replace("READ-ONLY".lower(), "read-only")
    assert "No connectors" in runner.invoke(app, ["connectors", "list"]).output
    ok = runner.invoke(app, ["connectors", "add", "gmail", "--account", "me@gmail.com", "--months", "3",
                             "--folders", "INBOX", "--yes"])
    assert ok.exit_code == 0, ok.output
    listing = runner.invoke(app, ["connectors", "list"]).output
    assert "me@gmail.com" in listing and "never" in listing
    cid = listing.split()[0]
    monkeypatch.delenv("DOCKET_GMAIL_APP_PASSWORD_ME_GMAIL_COM", raising=False)
    synced = runner.invoke(app, ["connectors", "sync", cid])
    assert synced.exit_code == 1 and "No app password" in synced.output  # fails before any network
    assert runner.invoke(app, ["connectors", "remove", cid]).exit_code == 0
    doctor = runner.invoke(app, ["doctor"]).output
    assert "Mail connectors" in doctor


def test_doctor_reports_allowed_connector_host(tmp_path, ctx, server):
    from docket.services.doctor import connector_check
    assert connector_check(ctx.settings.sqlite_path).status == "OK"
    add(service_for(ctx, server))
    check = connector_check(ctx.settings.sqlite_path)
    assert "imap.gmail.com" in check.detail and "only during sync" in check.detail


def test_ingest_and_query_paths_make_no_network_calls(tmp_path, ctx, server, monkeypatch):
    """After sync, indexing/reading the synced mail opens no sockets at all."""
    from sqlalchemy import select
    from docket.core.db.models import Chunk
    from docket.infra.inference.gateway import FakeInferenceGateway
    server.folders["INBOX"]["msgs"] = {1: (1, make_eml("Q3", "The committee approved the revised budget on Monday. " * 5))}
    svc = service_for(ctx, server)
    row = add(svc)
    svc.sync(row.id)
    attempts = []
    original = socket.socket.connect
    monkeypatch.setattr(socket.socket, "connect", lambda self, addr: attempts.append(addr) or (_ for _ in ()).throw(AssertionError("network")))
    gateway = FakeInferenceGateway()
    c2 = AppContext.for_testing(data_dir=ctx.settings.data_dir, gateway=gateway,
                                parser=type("P", (), {"parser_name": "x", "parser_version": "1"})())
    try:
        result = c2.pipeline.run_ingestion_for_source(row.source_id)
        assert result.files_failed == 0
        with c2.session_factory() as db:
            assert db.execute(select(Chunk.id)).first() is not None
    finally:
        c2.engine.dispose()
    assert attempts == []
    assert original  # keep reference


def test_model_has_no_secret_column():
    cols = {c.name for c in ConnectorAccount.__table__.columns}
    assert not any("secret" in c or "password" in c for c in cols)
