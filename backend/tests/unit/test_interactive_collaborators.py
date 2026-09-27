"""Standalone unit tests for the interactive-session collaborator modules
(`errors`, `source_commands`, `ingestion_ui`, `query_flow`), each exercised
directly against a minimal fake `session` -- no `run_session(...)` REPL loop
involved. Proves the Phase-4 split bought real testability, not just
file-size relief off `session.py`.
"""

from __future__ import annotations

from types import SimpleNamespace

from docket.cli.interactive import errors, ingestion_ui, query_flow, source_commands
from docket.ingestion.pipeline import FileIngestResult, IngestionJobResult
from docket.infra.inference.gateway import InferenceUnavailableError, ModelNotFoundError


class FakeSession:
    """Just enough of `_Session`'s surface for one collaborator at a time."""

    def __init__(self, **extra):
        self.said: list[str] = []
        self.errored: list[str] = []
        self.__dict__.update(extra)

    def say(self, message: str = "", **kw) -> None:
        self.said.append(message)

    def error(self, message: str) -> None:
        self.errored.append(message)

    def sync_state(self) -> None:
        pass


# -- errors.py ----------------------------------------------------------------


def test_handle_error_translates_inference_unavailable():
    session = FakeSession(context=SimpleNamespace(settings=SimpleNamespace(gen_model="m")))
    errors.handle_error(session, InferenceUnavailableError("boom"))
    assert "Can't reach Ollama" in session.errored[0]
    assert "boom" in session.said[0]


def test_handle_error_extracts_model_name():
    session = FakeSession(context=SimpleNamespace(settings=SimpleNamespace(gen_model="fallback")))
    errors.handle_error(session, ModelNotFoundError("Model 'qwen3:14b' is not available"))
    assert "Model not found: qwen3:14b" in session.errored[0]
    assert "ollama pull qwen3:14b" in session.errored[0]


# -- source_commands.py --------------------------------------------------------


def test_cmd_sources_empty_says_hint():
    session = FakeSession(
        context=SimpleNamespace(source_manager=SimpleNamespace(list_sources=lambda: []))
    )
    source_commands.cmd_sources(session, "")
    assert session.said == ["No sources registered. Use /add <folder>."]


def test_register_reports_existing_source():
    existing = SimpleNamespace(id="src1", path="/tmp/docs")
    session = FakeSession(
        context=SimpleNamespace(
            source_manager=SimpleNamespace(list_sources=lambda: [existing])
        ),
        state=SimpleNamespace(refresh_sources=lambda mgr: None),
    )
    from pathlib import Path

    result = source_commands._register(session, Path("/tmp/docs"))
    assert result == "src1"
    assert session.said == ["Already registered: src1"]


# -- ingestion_ui.py ------------------------------------------------------------


def test_summarize_reports_empty_source_friendly():
    session = FakeSession(
        context=SimpleNamespace(
            source_manager=SimpleNamespace(
                list_sources=lambda: [SimpleNamespace(id="src1", path="/tmp/empty")]
            )
        )
    )
    result = IngestionJobResult(
        source_id="src1", job_id="j1", status="failed", files_processed=0,
        files_failed=0, file_results=[],
    )
    ingestion_ui._summarize(session, "src1", result)
    assert "No supported files found in /tmp/empty" in session.said[0]


def test_summarize_counts_by_status():
    session = FakeSession(context=SimpleNamespace())
    files = [
        FileIngestResult(path="a.pdf", status="ingested", chunks_written=2),
        FileIngestResult(path="b.pdf", status="unchanged"),
        FileIngestResult(path="c.pdf", status="failed", error="bad"),
    ]
    result = IngestionJobResult(
        source_id="src1", job_id="j1", status="partial", files_processed=3,
        files_failed=1, file_results=files,
    )
    ingestion_ui._summarize(session, "src1", result)
    assert session.said == ["src1: 1 ingested, 1 unchanged, 1 failed — 2 chunks written"]


# -- query_flow.py --------------------------------------------------------------


def test_cmd_mode_reports_default_auto():
    session = FakeSession(mode=None)
    query_flow.cmd_mode(session, "")
    assert session.said == ["Mode: auto"]


def test_cmd_mode_rejects_unknown_value():
    session = FakeSession(mode=None)
    query_flow.cmd_mode(session, "bogus")
    assert session.said == ["Usage: /mode [auto|fast|agent]"]
    assert session.mode is None


def test_set_citations_updates_state_snapshot():
    session = FakeSession(state=SimpleNamespace())
    citation = SimpleNamespace(source_display_name="doc.md")
    query_flow._set_citations(session, [citation], "q?")
    assert session.last_citations == [citation]
    assert session.last_question == "q?"
    assert session.state.citations == ((1, "doc.md"),)
