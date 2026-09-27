"""End-to-end CLI test: `docket sources add` -> `docket ingest` ->
`docket query`, against a real Ollama server (the one place in this
checkpoint real Ollama is used -- everywhere else uses
`FakeInferenceGateway`). Replaces the validation spike's manual,
CLI-driven verification (`spike/ingest.py` + `spike/query.py`, run by hand)
with an automated one.

Marked `integration` (requires a running Ollama with `qwen3:14b` and
`qwen3-embedding:0.6b` pulled -- see `tests/conftest.py`, which auto-skips
this whole file if Ollama isn't reachable).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from docket.interfaces.cli.main import app

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[3]
SAMPLE_DOCX = REPO_ROOT / "Docs" / "01_Work_Intelligence_PRD_v1.0.docx"

runner = CliRunner()


@pytest.mark.skipif(not SAMPLE_DOCX.exists(), reason="sample docx not present in Docs/")
def test_cli_add_ingest_query_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir = tmp_path / "docket_data"
    monkeypatch.setenv("DOCKET_DATA_DIR", str(data_dir))

    source_folder = tmp_path / "source_docs"
    source_folder.mkdir()
    shutil.copy(SAMPLE_DOCX, source_folder / SAMPLE_DOCX.name)

    add_result = runner.invoke(app, ["sources", "add", str(source_folder)])
    assert add_result.exit_code == 0, add_result.stdout
    source_id = add_result.stdout.strip()
    assert source_id

    list_result = runner.invoke(app, ["sources", "list"])
    assert list_result.exit_code == 0
    assert source_id in list_result.stdout
    assert str(source_folder) in list_result.stdout

    ingest_result = runner.invoke(app, ["ingest", source_id])
    assert ingest_result.exit_code == 0, ingest_result.stdout
    assert "status=succeeded" in ingest_result.stdout
    assert "files_failed=0" in ingest_result.stdout

    query_result = runner.invoke(
        app, ["query", "What is Local-First Work Intelligence System?"]
    )
    assert query_result.exit_code == 0, query_result.stdout
    output = query_result.stdout

    # A real, grounded answer: mentions the product's own defining language...
    assert "evidence" in output.lower()
    # ...and is actually cited back to the ingested document, using the
    # centralized citation format from `docket.infra.retrieval.resolver`
    # ("[filename #chunk_id_prefix]").
    assert SAMPLE_DOCX.name in output
    assert "Citations:" in output
