"""Legacy Office ingestion (.doc/.xls/.ppt/.rtf) and the skipped-files report,
with a fake soffice runner (no LibreOffice required)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from conftest import _write_docx, _write_pptx, _write_xlsx
from docket.core.db.models import Chunk, EvidenceVersion, VersionStatus
from docket.infra.parsing.legacy_office import (
    ConversionTimeout,
    LegacyConverter,
)
from docket.services.ingestion.skips import latest_report, summary_line

CALLS: list[str] = []


def _fake_runner(cmd, timeout_s):
    fmt = cmd[cmd.index("--convert-to") + 1]
    outdir = Path(cmd[cmd.index("--outdir") + 1])
    src = Path(cmd[-1])
    CALLS.append(src.name)
    target = outdir / f"{src.stem}.{fmt}"
    if src.read_bytes().startswith(b"BAD"):
        return 1  # no output
    if src.read_bytes().startswith(b"SLOW"):
        raise ConversionTimeout("LibreOffice conversion timed out after 120 s")
    if fmt == "docx":
        _write_docx(target, "Legacy heading", "Legacy body about walruses.")
    elif fmt == "xlsx":
        _write_xlsx(target, "S", ["name", "qty"], [["widget", 3]])
    else:
        _write_pptx(target, slide_text="Legacy deck about otters")
    return 0


@pytest.fixture
def lenv(env: SimpleNamespace) -> SimpleNamespace:
    CALLS.clear()
    conv = LegacyConverter(runner=_fake_runner)
    conv._resolved, conv._resolved_done = "/fake/soffice", True
    env.pipeline._legacy = conv
    return env


def _version(env, name):
    with env.session_factory() as s:
        return s.execute(select(EvidenceVersion).where(EvidenceVersion.file_path == str(env.folder / name))).scalar_one()


@pytest.mark.parametrize("name,needle", [
    ("old.doc", "walruses"), ("old.rtf", "walruses"), ("old.xls", "widget"), ("old.ppt", "otters"),
])
def test_legacy_file_ingests_and_cites_the_original(lenv, name, needle) -> None:
    original = lenv.folder / name
    original.write_bytes(b"legacy-binary-" + name.encode())
    result = lenv.pipeline.run_ingestion_for_source(lenv.source.id)
    [fr] = result.file_results
    assert fr.status == "ingested" and fr.chunks_written > 0
    v = _version(lenv, name)
    assert v.status == VersionStatus.READY
    assert Path(v.file_path) == original  # the original path, never the temp copy
    with lenv.session_factory() as s:
        chunks = s.execute(select(Chunk).where(Chunk.evidence_version_id == v.id)).scalars().all()
        assert any(needle in c.text for c in chunks)
        assert all(c.file_path == str(original) for c in chunks if hasattr(c, "file_path"))
    # content hash is the hash of the original bytes
    import hashlib
    assert v.content_hash.endswith(hashlib.sha256(original.read_bytes()).hexdigest()[:8]) or \
        hashlib.sha256(original.read_bytes()).hexdigest() in (v.content_hash,)


def test_unchanged_legacy_file_is_not_converted_again(lenv) -> None:
    (lenv.folder / "a.doc").write_bytes(b"legacy")
    lenv.pipeline.run_ingestion_for_source(lenv.source.id)
    assert CALLS == ["a.doc"]
    second = lenv.pipeline.run_ingestion_for_source(lenv.source.id)
    assert second.file_results[0].status == "unchanged"
    assert CALLS == ["a.doc"]


def test_conversion_failure_and_timeout_are_failed_with_reasons(lenv) -> None:
    (lenv.folder / "bad.doc").write_bytes(b"BAD")
    (lenv.folder / "slow.ppt").write_bytes(b"SLOW")
    result = lenv.pipeline.run_ingestion_for_source(lenv.source.id)
    by = {r.path.name: r for r in result.file_results}
    assert by["bad.doc"].status == "failed" and by["bad.doc"].skip_reason == "conversion_failed"
    assert by["slow.ppt"].status == "failed" and by["slow.ppt"].skip_reason == "conversion_timeout"
    assert result.skipped_by_reason == {"conversion_failed": 1, "conversion_timeout": 1}


def test_no_libreoffice_skips_not_fails(env) -> None:
    (env.folder / "a.doc").write_bytes(b"legacy")
    result = env.pipeline.run_ingestion_for_source(env.source.id)
    assert result.files_failed == 0 and result.status == "succeeded"
    assert result.file_results[0].status == "skipped"
    assert result.skipped_by_reason == {"converter_missing": 1}
    with env.session_factory() as s:
        assert s.execute(select(EvidenceVersion)).scalars().all() == []


def test_skipped_report_covers_unsupported_and_persists(env) -> None:
    (env.folder / "mail.msg").write_bytes(b"m")
    (env.folder / "pack.zip").write_bytes(b"PK")
    (env.folder / "scan.png").write_bytes(b"png")
    (env.folder / "weird.xyz").write_bytes(b"?")
    (env.folder / "macro.xlsm").write_bytes(b"x")
    (env.folder / "fake.pdf").write_bytes(b"not a pdf")
    (env.folder / "ok.docx").parent.mkdir(exist_ok=True)
    _write_docx(env.folder / "ok.docx", "H", "fine")
    result = env.pipeline.run_ingestion_for_source(env.source.id)
    assert result.skipped_by_reason == {
        "unsupported_type": 4,  # msg, zip, png, xlsm
        "invalid_file": 1,
        "unsupported_other": 1,  # weird.xyz is only counted
    }
    names = sorted((s.path, s.reason) for s in result.skipped_files)
    assert ("mail.msg", "unsupported_type") in names and ("fake.pdf", "invalid_file") in names
    assert "weird.xyz" not in [n for n, _ in names]

    report = latest_report(env.session_factory, env.source.id, env.folder)
    assert report is not None
    assert report.counts == result.skipped_by_reason
    assert {f.path for f in report.files} == {"mail.msg", "pack.zip", "scan.png", "macro.xlsm", "fake.pdf"}
    line = summary_line(report.counts)
    assert line.startswith("Not indexed: 6 files")
    assert "ok.docx" not in line


def test_report_has_no_contents_and_empty_when_all_indexed(env) -> None:
    _write_docx(env.folder / "ok.docx", "H", "secret body text")
    result = env.pipeline.run_ingestion_for_source(env.source.id)
    assert result.skipped_by_reason == {} and result.skipped_files == []
    report = latest_report(env.session_factory, env.source.id, env.folder)
    assert report.counts == {} and report.files == []
    assert summary_line(report.counts) is None


# -- CLI surface ---------------------------------------------------------------


def test_ignore_notices_include_skip_summary() -> None:
    from docket.interfaces.cli.quiet import ignore_notices

    result = SimpleNamespace(
        files_skipped=2, invalid_skipped=1, file_results=[],
        skipped_by_reason={"unsupported_type": 3, "converter_missing": 1, "ignored": 2, "invalid_file": 1},
    )
    notices = ignore_notices(result, SimpleNamespace(count=lambda _s: 0), "src")
    line = next(n for n in notices if n.startswith("Not indexed"))
    assert "4 files" in line and "docket sources skipped" in line
    assert "ignored" not in line.split("(")[1].lower().replace("an ignore", "")


def test_sources_skipped_command_lists_reasons_without_contents(tmp_path, monkeypatch) -> None:
    from typer.testing import CliRunner

    from docket.interfaces.cli import main as cli_main
    from docket.interfaces.cli.context import AppContext

    ctx = AppContext.for_testing(data_dir=tmp_path / "data")
    monkeypatch.setattr(cli_main, "build_context", lambda: ctx)
    folder = tmp_path / "lib"
    folder.mkdir()
    (folder / "mail.msg").write_bytes(b"TOP SECRET CONTENT")
    (folder / "x.weird").write_bytes(b"?")
    src = ctx.source_manager.register_source(folder)
    runner = CliRunner()

    before = runner.invoke(cli_main.app, ["sources", "skipped", src.id])
    assert before.exit_code == 0 and "No skipped-files report yet" in before.output

    ctx.pipeline.run_ingestion_for_source(src.id)
    out = runner.invoke(cli_main.app, ["sources", "skipped", src.id])
    ctx.engine.dispose()
    assert out.exit_code == 0, out.output
    assert "2 not indexed" in out.output
    assert "mail.msg  [unsupported_type]" in out.output
    assert "Other file types Docket does not read" in out.output
    assert "TOP SECRET" not in out.output
