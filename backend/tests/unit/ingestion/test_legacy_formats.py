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
