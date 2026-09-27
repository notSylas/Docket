"""Shared fixtures for the eval-harness tests."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from docket.eval.schema import RecordedChunk, RecordedCitation, RunRecord
from docket.infra.inference.gateway import FakeInferenceGateway
from docket.infra.parsing.docling_wrapper import ParsedDocument
from docket.services.query.prompts import ABSTENTION_PHRASE

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "eval"

_BLOCK_RE = re.compile(r"(\[[^\[\]]*#[^\[\]]*\])\n(.*?)(?=\n\n\[|\n\nQuestion: )", re.S)


class PlainTextParser:
    """Stand-in for `DoclingParser`: the fixture 'pdfs' are plain text."""

    parser_name = "plain"
    parser_version = "1"

    def parse(self, source_id: str, path: Path) -> ParsedDocument:
        return ParsedDocument(
            text=path.read_text(encoding="utf-8"),
            source_path=path,
            parser_name=self.parser_name,
            parser_version=self.parser_version,
        )


class ScriptedGateway(FakeInferenceGateway):
    """Fake gateway answering by question keyword; cites the context block that
    contains the given needle. `script` maps a question substring to
    `(needle, text)`; text may use `{tag}` for the citation tag. Unmatched
    questions get the abstention phrase."""

    def __init__(self, script: dict[str, tuple[str, str]], on_generate=None):
        super().__init__()
        self.script = script
        self.on_generate = on_generate
        self.last_generate_meta = {"prompt_eval_count": 500}

    def generate(self, *, system: str, prompt: str, **opts) -> str:
        super().generate(system=system, prompt=prompt, **opts)
        if self.on_generate:
            self.on_generate()
        question = prompt.rsplit("Question: ", 1)[1].split("\n\nAnswer:")[0]
        for key, (needle, text) in self.script.items():
            if key in question:
                tag = next((t for t, body in _BLOCK_RE.findall(prompt) if needle in body), "")
                return text.format(tag=tag)
        return ABSTENTION_PHRASE


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture
def make_record():
    def _make(question_id="q", repeat=0, answer="", chunks=(), cited=(), prompt=None, **kw):
        """`chunks` are texts (ids c0, c1, ...); `cited` are indexes into chunks."""
        retrieved = [
            RecordedChunk(chunk_id=f"c{i}", text=t, citation_label=f"[f #c{i}]", source_display_name="f")
            for i, t in enumerate(chunks)
        ]
        citations = [
            RecordedCitation(citation_label=f"[f #c{i}]", chunk_id=f"c{i}", source_display_name="f")
            for i in cited
        ]
        return RunRecord(
            question_id=question_id,
            repeat=repeat,
            answer=answer,
            retrieved=retrieved,
            citations=citations,
            prompt=prompt,
            **kw,
        )

    return _make
