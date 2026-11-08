"""Gmail over IMAP (app password), strictly read-only.

Safety by construction: the only IMAP object the sync code touches is
`ReadOnlyImap`, which exposes login/list/select(readonly)/response/uid/logout
and, through uid(), only SEARCH and FETCH of BODY.PEEK (never BODY[], which
would set \\Seen). There is no STORE, EXPUNGE, COPY, APPEND, DELETE or RENAME.
"""
from __future__ import annotations

import email
import imaplib
import logging
import re
from datetime import date, timedelta
from email import policy
from typing import Callable

from docket.services.connectors.base import (
    Change,
    ChangeBatch,
    ConnectorError,
    resolve_env_secret,
    scrub,
    secret_env_name,
)

log = logging.getLogger(__name__)

HOST = "imap.gmail.com"
PORT = 993
ENV_PREFIX = "DOCKET_GMAIL_APP_PASSWORD_"
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_EXCLUDED_ATTRS = ("\\junk", "\\trash", "\\drafts")
_EXCLUDED_NAMES = re.compile(r"(^|/)(spam|trash|bin|drafts|junk)$", re.I)
_UNSAFE_FETCH = re.compile(r"BODY\[|\bRFC822(?![.\w])|\bBODY\.PEEK\[\]<", re.I)

DEFAULT_CONFIG = {
    "folders": ["INBOX", "Sent"],
    "months": 12,
    "include_attachments": False,
    "max_message_bytes": 10 * 1024 * 1024,
    "max_messages_per_sync": 2000,
}


class ReadOnlyImap:
    """Whitelist wrapper around an imaplib connection."""

    def __init__(self, conn):
        self._conn = conn

    def login(self, user, password):
        return self._conn.login(user, password)

    def list(self):
        return self._conn.list()

    def select(self, mailbox):
        return self._conn.select(mailbox, readonly=True)

    def response(self, code):
        return self._conn.response(code)

    def uid(self, command, *args):
        name = command.upper()
        if name == "SEARCH":
            return self._conn.uid("SEARCH", *args)
        if name == "FETCH":
            spec = " ".join(str(a) for a in args[1:])
            if _UNSAFE_FETCH.search(spec):
                raise ConnectorError("Refusing a FETCH that could mark mail as read.")
            return self._conn.uid("FETCH", *args)
        raise ConnectorError(f"IMAP command {name} is not allowed (read-only connector).")

    def logout(self):
        return self._conn.logout()


def imap_date(day: date) -> str:
    return f"{day.day:02d}-{_MONTHS[day.month - 1]}-{day.year}"


def strip_attachments(raw: bytes) -> bytes:
    """Replace attachment payloads with a one-line placeholder; keep headers/body."""
    try:
        msg = email.message_from_bytes(raw, policy=policy.default)
        changed = False
        for part in msg.walk():
            if part.is_multipart():
                continue
            disposition = (part.get_content_disposition() or "").lower()
            is_att = disposition == "attachment" or (
                part.get_filename() and part.get_content_maintype() not in ("text",)) or (
                part.get_content_maintype() == "message")
            if not is_att:
                continue
            name = part.get_filename() or "unnamed"
            ctype = part.get_content_type()
            size = len(part.get_payload(decode=True) or b"") if part.get_content_maintype() != "message" else 0
            for header in list(part.keys()):
                del part[header]
            part.set_content(f"[attachment not stored: {name}, {ctype}, {size} bytes]")
            changed = True
        return msg.as_bytes() if changed else raw
    except Exception:  # noqa: BLE001 -- never store an unstripped payload on parse failure
        return b"Subject: (message could not be parsed; attachments not stored)\r\n\r\n"


class GmailImapConnector:
    provider = "gmail_imap"
    allowed_hosts = (HOST,)

    def __init__(self, account: str, config: dict | None = None, *, secret_resolver: Callable[[], str | None] | None = None,
                 imap_factory: Callable[..., object] | None = None, today: Callable[[], date] = date.today):
        self.account = account
        self.config = {**DEFAULT_CONFIG, **(config or {})}
        self.env_name = secret_env_name(ENV_PREFIX, account)
        self._resolver = secret_resolver or (lambda: resolve_env_secret(self.env_name))
        self._factory = imap_factory or (lambda host, port: imaplib.IMAP4_SSL(host, port))
        self._today = today
        self._conn: ReadOnlyImap | None = None
        self._secret_seen = ""

    # -- protocol -----------------------------------------------------------
    def validate_config(self) -> None:
        c = self.config
        if "@" not in self.account:
            raise ConnectorError("Account must be an email address.")
        if not c["folders"] or not all(isinstance(f, str) and f.strip() for f in c["folders"]):
            raise ConnectorError("At least one folder is required.")
        for key in ("months", "max_message_bytes", "max_messages_per_sync"):
            if not isinstance(c[key], int) or c[key] <= 0:
                raise ConnectorError(f"{key} must be a positive integer.")
        for folder in c["folders"]:
            if _EXCLUDED_NAMES.search(folder.strip()):
                raise ConnectorError(f"Folder {folder!r} (spam, trash or drafts) is excluded.")

    def authenticate(self) -> str:
        if not self._resolver():
            raise ConnectorError(f"No app password found. Export {self.env_name} (see: docket connectors guide gmail).")
        return f"env:{self.env_name}"

    def disconnect(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.logout()
            except Exception:  # noqa: BLE001
                pass

    def list_changes(self, since_cursor: dict | None) -> ChangeBatch:
        batch = ChangeBatch(changes=iter(()))
        batch.changes = self._generate(since_cursor or {}, batch)
        return batch

    # -- internals ----------------------------------------------------------
    def _connect(self) -> ReadOnlyImap:
        password = (self._resolver() or "").strip()
        self._secret_seen = password
        if not password:
            raise ConnectorError("No app password available.")
        try:
            raw = self._factory(HOST, PORT)
            conn = ReadOnlyImap(raw)
            conn.login(self.account, "".join(password.split()))
        except Exception as exc:  # noqa: BLE001 -- never echo server text unscrubbed
            raise ConnectorError("Gmail sign-in failed: " + scrub(exc, (password, self.account))) from None
        self._conn = conn
        return conn

    def _resolve_folders(self, conn: ReadOnlyImap) -> list[str]:
        status, lines = conn.list()
        names: dict[str, str] = {}
        sent = None
        for line in lines if status == "OK" else []:
            text = line.decode("utf-8", "replace") if isinstance(line, bytes) else str(line)
            m = re.match(r'\((?P<attrs>[^)]*)\)\s+(?:"[^"]*"|NIL)\s+(?P<name>.+)$', text)
            if not m:
                continue
            name = m["name"].strip().strip('"')
            attrs = m["attrs"].lower()
            names[name] = attrs
            if "\\sent" in attrs:
                sent = name
        out = []
        for want in self.config["folders"]:
            want = want.strip()
            name = (sent or "[Gmail]/Sent Mail") if want.lower() in ("sent", "sent mail") else want
            attrs = names.get(name, "")
            if any(a in attrs for a in _EXCLUDED_ATTRS) or _EXCLUDED_NAMES.search(name):
                raise ConnectorError(f"Folder {name!r} (spam, trash or drafts) is excluded.")
            if name not in out:
                out.append(name)
        return out

    def _uids(self, conn, *criteria) -> set[int]:
        status, data = conn.uid("SEARCH", None, *criteria)
        if status != "OK":
            raise ConnectorError("Folder search failed.")
        return {int(x) for x in b" ".join(d for d in data if d).split()}

    def _generate(self, cursor: dict, batch: ChangeBatch):
        try:
            conn = self._connect()
            folders = self._resolve_folders(conn)
            state: dict[str, dict] = {}
            budget = int(self.config["max_messages_per_sync"])
            since = imap_date(self._today() - timedelta(days=30 * int(self.config["months"])))
            for folder in folders:
                old = (cursor.get("folders") or {}).get(folder) or {}
                current = {"uidvalidity": old.get("uidvalidity"), "last_uid": int(old.get("last_uid", 0)),
                           "uids": dict(old.get("uids") or {})}
                state[folder] = current
                status, _ = conn.select(f'"{folder}"')
                if status != "OK":
                    raise ConnectorError(f"Folder {folder!r} could not be opened.")
                validity = self._uidvalidity(conn)
                if current["uidvalidity"] is not None and current["uidvalidity"] != validity:
                    for rid in set(current["uids"].values()):
                        yield Change(rid, folder, deleted=True)
                    current.update(last_uid=0, uids={})
                current["uidvalidity"] = validity
                everything = self._uids(conn, "ALL")
                for uid, rid in list(current["uids"].items()):
                    if int(uid) not in everything:
                        yield Change(rid, folder, deleted=True)
                        del current["uids"][uid]
                recent = self._uids(conn, "SINCE", since)
                todo = sorted(u for u in recent if u > current["last_uid"])
                done_all = True
                for uid in todo:
                    if budget <= 0:
                        done_all = False
                        batch.truncated = True
                        break
                    rid, raw = self._fetch(conn, uid, validity)
                    current["last_uid"] = uid
                    if raw is None:
                        batch.skipped += 1
                        continue
                    budget -= 1
                    current["uids"][str(uid)] = rid
                    yield Change(rid, folder, raw=raw)
                if done_all and everything:
                    current["last_uid"] = max(current["last_uid"], max(everything))
            batch.cursor = {"folders": state}
        except ConnectorError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ConnectorError("Gmail sync failed: " + scrub(exc, (self._secret_seen, self.account))) from None
        finally:
            self.disconnect()

    @staticmethod
    def _uidvalidity(conn) -> int:
        _, data = conn.response("UIDVALIDITY")
        try:
            return int(data[0])
        except (TypeError, ValueError, IndexError):
            raise ConnectorError("Server did not report UIDVALIDITY.") from None

    def _fetch(self, conn, uid: int, validity: int):
        status, data = conn.uid("FETCH", str(uid), "(X-GM-MSGID RFC822.SIZE)")
        meta = b" ".join(p if isinstance(p, bytes) else b"" for d in data or [] for p in (d if isinstance(d, tuple) else (d,)))
        gm = re.search(rb"X-GM-MSGID (\d+)", meta)
        size = re.search(rb"RFC822\.SIZE (\d+)", meta)
        rid = f"gm{int(gm[1]):x}" if gm else f"u{validity}-{uid}"
        if status != "OK" or not size:
            return rid, None
        if int(size[1]) > int(self.config["max_message_bytes"]):
            log.info("skipped oversized message uid=%s size=%s", uid, int(size[1]))
            return rid, None
        status, data = conn.uid("FETCH", str(uid), "(BODY.PEEK[])")
        raw = next((d[1] for d in data or [] if isinstance(d, tuple) and isinstance(d[1], (bytes, bytearray))), None)
        if status != "OK" or raw is None:
            return rid, None
        raw = bytes(raw)
        if not self.config["include_attachments"]:
            raw = strip_attachments(raw)
        return rid, raw
