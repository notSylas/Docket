"""Importable (spawn-safe) fake parsers for the parse-pool tests."""

from __future__ import annotations

import os
import time
from pathlib import Path

import docx

from docket.infra.parsing.docling_wrapper import ParsedDocument, ParseError


class FakeParser:
    parser_name = "fake-docling"
    parser_version = "1"

    def __init__(self, delay: float = 0.0) -> None:
        self.delay = delay

    def parse(self, source_id: str, path: Path) -> ParsedDocument:
        text = "\n".join(p.text for p in docx.Document(str(path)).paragraphs)
        if "HANG" in text:
            time.sleep(120)
        if "CRASH" in text:
            os._exit(7)
        if "RAISE" in text:
            raise ParseError(source_id, path, ValueError("boom"))
        time.sleep(self.delay)
        return ParsedDocument(
            text=text,
            source_path=path,
            parser_name=self.parser_name,
            parser_version=self.parser_version,
            text_with_page_markers=text,
        )


def make_fast():
    return FakeParser()


def make_slow():
    return FakeParser(delay=0.5)
