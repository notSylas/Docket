"""Bounded email snapshots. No attachment decoding, remote content or mailbox writes."""
from __future__ import annotations

import hashlib
import json
import mailbox
import re
import tempfile
from dataclasses import dataclass
from email import policy
from email.header import decode_header
from email.parser import BytesParser
from pathlib import Path

from docket.infra.parsing.chunker import ChunkDraft, EvidenceUnitDraft
from docket.infra.parsing.text_family import DecodedText, ParsedText, TextBlock, _VisibleHTML, chunk_text, decode_text

MAIL_EXTENSIONS = {".eml", ".mbox"}


class MailLimitError(ValueError):
    pass


def _decode(data, charset=None):
    if charset:
        try:
            return DecodedText(data.decode(charset), charset)
        except (LookupError, UnicodeError):
            value = decode_text(data)
            return DecodedText(value.text, value.encoding, True)
    return decode_text(data)


def _header(value):
    pieces, encodings, lossy = [], [], False
    try:
        parts = decode_header(value)
    except (LookupError, ValueError):
        parts, lossy = [(value, None)], True
    for data, charset in parts:
        value = _decode(data, charset) if isinstance(data, bytes) else DecodedText(data, "unicode")
        text = value.text.encode("utf-8", "replace").decode("utf-8")
        lossy |= value.lossy or text != value.text
        pieces.append(text)
        encodings.append(value.encoding)
    return DecodedText(" ".join("".join(pieces).split()), ",".join(encodings), lossy)


@dataclass
class MailMessage:
    text: str
    locator: dict
    heading: str | None


@dataclass
class ParsedMail:
    messages: list[MailMessage]
    notice: str | None = None


def _message(raw, *, container, index, offset, end):
    msg = BytesParser(policy=policy.compat32).parsebytes(raw)
    headers = {name: _header(str(msg.get(name, ""))) for name in ("From", "To", "Date", "Subject")}
    attachments = []

    def visit(part, part_id="1"):
        filename, kind = part.get_filename(), part.get_content_type()
        if part.get_content_disposition() == "attachment" or filename or kind == "message/rfc822":
            attachments.append({"mime_part": part_id, "filename": _header(filename or "").text, "content_type": kind})
            return []
        if part.is_multipart():
            choices = [visit(child, f"{part_id}.{n}") for n, child in enumerate(part.get_payload(), 1)]
            choices = [c for c in choices if c]
            if part.get_content_subtype() == "alternative":
                return next((c for c in choices if any(p[1] == "text/plain" for p in c)), choices[0] if choices else [])
            return [p for c in choices for p in c]
        if kind not in {"text/plain", "text/html"}:
            attachments.append({"mime_part": part_id, "filename": "", "content_type": kind})
            return []
        decoded = _decode(part.get_payload(decode=True) or b"", part.get_content_charset())
        if part.defects:
            decoded = DecodedText(decoded.text, decoded.encoding, True)
        text = decoded.text
        if kind == "text/html":
            html = _VisibleHTML()
            html.feed(text)
            html.close()
            html.flush()
            text = "\n".join(b.text for b in html.blocks)
        return [(part_id, kind, decoded, text)]

    bodies = visit(msg)
    text = "".join(f"{name}: {value.text}\n" for name, value in headers.items()) + "\n"
    regions = []
    for part_id, kind, decoded, body in bodies:
        if regions:
            text += "\n"
        start = len(text)
        text += body
        regions.append({"mime_part": part_id, "content_type": kind, "char_start": start, "char_end": len(text),
                        "encoding": decoded.encoding, "lossy": decoded.lossy,
                        "body_line_start": 1, "body_line_end": max(1, len(body.splitlines()))})
    locator = {"container": container, "message_index": index, "byte_offset": offset, "byte_end": end,
               "message_sha256": hashlib.sha256(raw).hexdigest(), "message_id": _header(str(msg.get("Message-ID", ""))).text,
               "headers": {k.lower(): v.text for k, v in headers.items()},
               "header_decoding": {k.lower(): {"encoding": v.encoding, "lossy": v.lossy} for k, v in headers.items()},
               "attachments": attachments, "body_parts": regions, "decoded_text_version": 1,
               "lossy": bool(msg.defects) or any(v.lossy for v in headers.values()) or any(p["lossy"] for p in regions)}
    return MailMessage(text, locator, headers["Subject"].text or None)


class MailParser:
    parser_name, parser_version = "stdlib-mail", "1"

    def __init__(self, *, max_bytes=64 * 1024 * 1024, max_messages=1000):
        if max_bytes < 1 or max_messages < 1:
            raise ValueError("Email size and message limits must be positive.")
        self.max_bytes, self.max_messages = max_bytes, max_messages

    def snapshot(self, path):
        with Path(path).open("rb") as stream:
            data = stream.read(self.max_bytes + 1)
        if len(data) > self.max_bytes:
            raise MailLimitError(f"Mailbox exceeds the {self.max_bytes}-byte limit. Export a smaller copy.")
        return data

    def parse(self, source_id, path, *, snapshot=None):
        path = Path(path)
        data = self.snapshot(path) if snapshot is None else snapshot
        if len(data) > self.max_bytes:
            raise MailLimitError(f"Mailbox exceeds the {self.max_bytes}-byte limit. Export a smaller copy.")
        if path.suffix.lower() == ".eml":
            return ParsedMail([_message(data, container=path.name, index=1, offset=0, end=len(data))])
        offsets = [m.start() for m in re.finditer(br"(?m)^From ", data)]
        if data.strip() and not offsets:
            raise ValueError("Not a valid mbox snapshot. Export messages as .eml or mbox.")
        offsets.append(len(data))
        messages = []
        # The only disk copy is the complete snapshot, never extracted attachment bytes.
        with tempfile.TemporaryDirectory(prefix="docket-mail-") as root:
            copied = Path(root) / "snapshot.mbox"
            copied.write_bytes(data)
            box = mailbox.mbox(copied, create=False)
            try:
                for index, key in enumerate(box.iterkeys(), 1):
                    if index > self.max_messages:
                        break
                    with box.get_file(key) as stream:
                        msg = _message(stream.read(), container=path.name, index=index,
                                       offset=offsets[index - 1], end=offsets[index])
                    msg.locator["message_sha256"] = hashlib.sha256(data[offsets[index - 1]:offsets[index]]).hexdigest()
                    messages.append(msg)
            finally:
                box.close()
        total = len(offsets) - 1
        notice = (f"Partial mailbox: read the first {len(messages)} of {total} messages. "
                  "Export smaller copies or increase the email message limit.") if total > self.max_messages else None
        return ParsedMail(messages, notice)


def chunk_mail(parsed, *, counter=None, max_tokens=None):
    units, chunks = [], []
    for index, msg in enumerate(parsed.messages):
        _, pieces = chunk_text(ParsedText(DecodedText(msg.text, "mime"),
            [TextBlock(msg.text, {"heading_path": []}, "mail_message")]), counter=counter, max_tokens=max_tokens)
        position, locators = 0, []
        for piece in pieces:
            start = msg.text.index(piece.text, position)
            end = position = start + len(piece.text)
            locator = {"ordinal": len(chunks), "char_start": start, "char_end": end, "body_parts": []}
            for region in msg.locator["body_parts"]:
                lo, hi = max(start, region["char_start"]), min(end, region["char_end"])
                if lo < hi:
                    line = msg.text[region["char_start"]:lo].count("\n") + 1
                    locator["body_parts"].append({"mime_part": region["mime_part"], "line_start": line,
                        "line_end": line + msg.text[lo:hi].rstrip("\r\n").count("\n")})
            locators.append(locator)
            chunks.append(ChunkDraft(index, len(chunks), msg.heading, piece.text, piece.content_hash))
        units.append(EvidenceUnitDraft(index, msg.heading, msg.text, hashlib.sha256(msg.text.encode()).hexdigest(),
            unit_kind="mail_message", locator_json=json.dumps({**msg.locator, "chunks": locators, "partial_notice": parsed.notice})))
    return units, chunks
