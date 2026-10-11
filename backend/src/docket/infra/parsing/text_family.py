"""Offline, string-only readers. Locators describe the decoded representation.

Original bytes remain in the evidence store. Decoding metadata is repeated in
each unit locator so historical citations do not depend on a mutable sidecar.
CSV record numbers include the header record and never count physical lines.
"""
from __future__ import annotations

import codecs
import csv
import hashlib
import io
import json
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

from docket.core.config import settings
from docket.infra.parsing.chunker import ChunkDraft, EvidenceUnitDraft
from docket.infra.parsing.tokens import get_token_counter

TEXT_EXTENSIONS = {".txt", ".md", ".csv", ".html", ".htm"}
DECODED_TEXT_VERSION = 1


@dataclass(frozen=True)
class DecodedText:
    text: str
    encoding: str
    lossy: bool = False


def decode_text(data: bytes) -> DecodedText:
    """Deterministic BOM/UTF detection; fallback is explicitly uncertain.

    No statistical charset claim is made: cp1252 is a best-effort fallback,
    marked lossy even when its byte mapping happens to be reversible.
    """
    for bom, encoding in ((codecs.BOM_UTF32_LE, "utf-32"),
                          (codecs.BOM_UTF32_BE, "utf-32"),
                          (codecs.BOM_UTF8, "utf-8-sig"),
                          (codecs.BOM_UTF16_LE, "utf-16"),
                          (codecs.BOM_UTF16_BE, "utf-16")):
        if data.startswith(bom):
            try:
                return DecodedText(data.decode(encoding), encoding)
            except UnicodeError:
                return DecodedText(data.decode(encoding, errors="replace"), encoding, True)
    sample = data[:4096]
    if len(sample) >= 4 and b"\x00" in sample:
        even, odd = sample[::2], sample[1::2]
        encoding = None
        if odd.count(0) / len(odd) > .6 and even.count(0) / len(even) < .1:
            encoding = "utf-16-le"
        elif even.count(0) / len(even) > .6 and odd.count(0) / len(odd) < .1:
            encoding = "utf-16-be"
        if encoding:
            try:
                return DecodedText(data.decode(encoding), encoding)
            except UnicodeError:
                return DecodedText(data.decode(encoding, errors="replace"), encoding, True)
    try:
        return DecodedText(data.decode("utf-8"), "utf-8")
    except UnicodeError:
        return DecodedText(data.decode("cp1252", errors="replace"), "cp1252-fallback", True)


def is_text_bytes(data: bytes) -> bool:
    if data.startswith((b"%PDF-", b"PK\x03\x04", b"\x7fELF", b"\x89PNG", b"\xff\xd8\xff")):
        return False
    text = decode_text(data).text
    if not text:
        return True
    controls = sum(ord(c) < 32 and c not in "\t\n\r\f" for c in text)
    return "\x00" not in text and controls / len(text) < .01


@dataclass
class TextBlock:
    text: str
    locator: dict
    kind: str


@dataclass
class ParsedText:
    decoded: DecodedText
    blocks: list[TextBlock]


def _lines(text: str, markdown: bool) -> list[TextBlock]:
    blocks, stack = [], []
    current, start = [], 1

    def flush(end):
        if current and "".join(current).strip():
            blocks.append(TextBlock("".join(current), {
                "line_start": start, "line_end": end,
                "heading_path": [h for _, h in stack],
            }, "text_lines"))
        current.clear()

    # Heading detection does not interpret fenced code as headings.
    fence = None
    lines = text.splitlines(keepends=True)
    for number, line in enumerate(lines, 1):
        marker = re.match(r"^\s{0,3}(`{3,}|~{3,})", line) if markdown else None
        heading = re.match(r"^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$", line) if markdown and not fence else None
        if heading:
            flush(number - 1)
            start = number
            level = len(heading[1])
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, heading[2]))
        if not current:
            start = number
        current.append(line)
        if marker:
            if fence and marker[1][0] == fence[0] and len(marker[1]) >= len(fence):
                fence = None
            elif not fence:
                fence = marker[1]
    flush(len(lines))
    return blocks


def _csv_blocks(text: str, *, has_header: bool | None = None) -> list[TextBlock]:
    try:
        dialect = csv.Sniffer().sniff(text[:65536], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(io.StringIO(text, newline=""), dialect, strict=True))
    if not rows:
        return []
    if has_header is None:
        try:
            has_header = csv.Sniffer().has_header(text[:65536])
        except csv.Error:
            has_header = False
        # All-text, rectangular CSV is ambiguous: use named first-row
        # columns, recording the decision. Callers can explicitly opt out.
        if not has_header and len(rows) > 1:
            has_header = all(c.strip() and not re.fullmatch(r"[+\-\d., ]+", c) for c in rows[0])
    headers = rows[0] if has_header else []
    blocks = []
    for record, row in enumerate(rows, 1):
        for column, value in enumerate(row, 1):
            if not value.strip():
                continue
            name = headers[column - 1] if column <= len(headers) and headers[column - 1] else f"Column {column}"
            blocks.append(TextBlock(value, {
                "record": record, "column": column, "column_name": name,
                "delimiter": dialect.delimiter, "has_header": bool(has_header),
                "is_header": bool(has_header and record == 1),
                "heading_path": [f"Record {record}", name],
            }, "csv_cell"))
    return blocks


class _VisibleHTML(HTMLParser):
    _blocked = {"script", "style", "iframe", "object", "embed", "template", "noscript", "svg", "head", "form"}
    _void = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
    _blocks = {"p", "div", "section", "article", "li", "tr", "td", "th", "pre", "blockquote", "header", "footer", "main"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.blocks, self.stack, self.suppressed = [], [], [], []
        self.heading = None

    def flush(self):
        text = re.sub(r"\s+", " ", "".join(self.parts)).strip()
        self.parts.clear()
        if text:
            if self.heading:
                level = int(self.heading[1])
                while self.stack and self.stack[-1][0] >= level:
                    self.stack.pop()
                self.stack.append((level, text))
            self.blocks.append(TextBlock(text, {
                "block": len(self.blocks) + 1, "heading_path": [h for _, h in self.stack],
            }, "html_block"))

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        hidden = "hidden" in attrs or attrs.get("aria-hidden", "").lower() == "true"
        hidden |= bool(re.search(r"(?:display\s*:\s*none|visibility\s*:\s*hidden)", attrs.get("style", ""), re.I))
        if self.suppressed or tag in self._blocked or hidden:
            if tag not in self._void:
                self.suppressed.append(tag)
            return
        if tag in self._blocks or re.fullmatch(r"h[1-6]", tag):
            self.flush()
        if re.fullmatch(r"h[1-6]", tag):
            self.heading = tag
        if tag == "br":
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if self.suppressed:
            if tag in self.suppressed:
                del self.suppressed[self.suppressed.index(tag):]
            return
        if tag in self._blocks or re.fullmatch(r"h[1-6]", tag):
            self.flush()
        if tag == self.heading:
            self.heading = None

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self._void:
            self.handle_endtag(tag)

    def handle_data(self, data):
        if not self.suppressed:
            self.parts.append(data)


class TextFamilyParser:
    parser_name = "offline-text"
    parser_version = "1"

    def parse(self, source_id: str, path: Path, *, csv_has_header: bool | None = None) -> ParsedText:
        from docket.infra.parsing.filecheck import check_file_signature

        check_file_signature(path)
        decoded = decode_text(path.read_bytes())
        suffix = path.suffix.lower()
        if suffix == ".csv":
            blocks = _csv_blocks(decoded.text, has_header=csv_has_header)
        elif suffix in {".html", ".htm"}:
            parser = _VisibleHTML()
            parser.feed(decoded.text)
            parser.close()
            parser.flush()
            blocks = parser.blocks
        else:
            blocks = _lines(decoded.text, suffix == ".md")
        for block in blocks:
            block.locator.update(encoding=decoded.encoding, lossy=decoded.lossy,
                                 decoded_text_version=DECODED_TEXT_VERSION)
        return ParsedText(decoded, blocks)


def chunk_text(parsed: ParsedText, *, counter=None, max_tokens: int | None = None):
    """Cut exact substrings, including whitespace, rather than normalizing quotes."""
    counter = counter or get_token_counter()
    cap = max_tokens or settings.chunk_max_tokens
    units, chunks = [], []
    for block in parsed.blocks:
        start = 0
        while start < len(block.text):
            end = len(block.text)
            if counter.count(block.text[start:end]) > cap:
                low, high = start + 1, end
                while low < high:
                    mid = (low + high + 1) // 2
                    if counter.count(block.text[start:mid]) <= cap:
                        low = mid
                    else:
                        high = mid - 1
                end = low
                # Prefer a line/word boundary, preserving it in this slice.
                boundary = max(block.text.rfind("\n", start, end), block.text.rfind(" ", start, end))
                if boundary > start:
                    end = boundary + 1
            text = block.text[start:end]
            if text.strip():
                locator = dict(block.locator, char_start=start, char_end=end)
                if "line_start" in locator:
                    locator["line_start"] += block.text[:start].count("\n")
                    locator["line_end"] = locator["line_start"] + text.rstrip("\r\n").count("\n")
                digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
                trail = locator["heading_path"]
                heading = trail[-1] if trail else None
                index = len(units)
                units.append(EvidenceUnitDraft(index, heading, text, digest,
                             unit_kind=block.kind, locator_json=json.dumps(locator, sort_keys=True)))
                chunks.append(ChunkDraft(index, index, heading, text, digest))
            start = end
    return units, chunks
