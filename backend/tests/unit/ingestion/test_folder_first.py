"""Folder-first foundation (CP1): stored per-source rules, invalid files
skipped at discovery, failure reason / mtime recorded, prune of dead FAILED
versions."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import select

from conftest import _write_xlsx
from docket.core.db.models import EvidenceVersion, Source, VersionStatus
from docket.interfaces.cli.quiet import ignore_notices
from docket.services.ingestion.ignore import (
    IgnoreRules,
    discover,
    dump_rules_json,
    invalid_summary,
)
from docket.services.sources.prune import SourcePruneService

EXT = {".pdf", ".docx", ".xlsx", ".txt"}


def _touch(root: Path, rel: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.suffix == ".pdf":
        p.write_bytes(b"%PDF-1.4\n")
    elif p.suffix in (".docx", ".xlsx"):
        p.write_bytes(b"PK\x03\x04")
    else:
        p.write_bytes(b"x")
    return p


def _names(report, root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in report.files)


def _statuses(env) -> dict[str, VersionStatus]:
    with env.session_factory() as session:
        rows = session.execute(select(EvidenceVersion)).scalars().all()
        return {Path(v.file_path).name: v.status for v in rows}


# -- stored rules ------------------------------------------------------------


def test_stored_exclude_and_ignored_files(tmp_path):
    for rel in ["keep.pdf", "drafts/a.pdf", "work/x/b.pdf", "work/y/c.pdf", "work/y/d.pdf"]:
        _touch(tmp_path, rel)
    rules = {"version": 1, "exclude": ["drafts/"], "include": [], "ignored_files": ["work/y/d.pdf"]}
    report = discover(tmp_path, extensions=EXT, extra_rules=json.dumps(rules))
    assert _names(report, tmp_path) == ["keep.pdf", "work/x/b.pdf", "work/y/c.pdf"]
    assert report.dirs_skipped == 1


def test_stored_include_keeps_only_matching_paths(tmp_path):
    for rel in ["top.pdf", "a/one.pdf", "a/deep/two.pdf", "b/three.pdf", "c/four.pdf"]:
        _touch(tmp_path, rel)
    rules = {"include": ["a", "c/four.pdf"]}
    report = discover(tmp_path, extensions=EXT, extra_rules=rules)
    assert _names(report, tmp_path) == ["a/deep/two.pdf", "a/one.pdf", "c/four.pdf"]
    ignore = IgnoreRules.for_root(tmp_path, rules)
    assert ignore.path_ignored(Path("b/three.pdf"))
    assert ignore.path_ignored(Path("top.pdf"))
    assert not ignore.path_ignored(Path("a/deep/two.pdf"))


def test_stored_rules_extend_docketignore_and_tolerate_garbage(tmp_path):
    (tmp_path / ".docketignore").write_text("scratch.pdf\n")
    for rel in ["scratch.pdf", "old/x.pdf", "ok.pdf"]:
        _touch(tmp_path, rel)
    report = discover(tmp_path, extensions=EXT, extra_rules='{"exclude": ["old/"]}')
    assert _names(report, tmp_path) == ["ok.pdf"]
    for bad in ("not json", "[1, 2]", '{"exclude": "old/"}', None):
        again = discover(tmp_path, extensions=EXT, extra_rules=bad)
        assert _names(again, tmp_path) == ["ok.pdf", "old/x.pdf"]


def test_dump_rules_json_round_trip():
    assert dump_rules_json({}) is None
    text = dump_rules_json({"exclude": ["a/"], "ignored_files": ["b.pdf"]})
    assert json.loads(text) == {
        "version": 1, "exclude": ["a/"], "include": [], "ignored_files": ["b.pdf"],
    }


# -- invalid files -----------------------------------------------------------


def test_invalid_signature_files_skipped_at_discovery(tmp_path):
    _touch(tmp_path, "real.pdf")
    _touch(tmp_path, "sub/real.docx")
    (tmp_path / "fake.pdf").write_text("plain text pretending to be a pdf")
    (tmp_path / "sub" / "fake.xlsx").write_bytes(b"not a zip")
    (tmp_path / "notes.txt").write_text("text has no signature to check")
    report = discover(tmp_path, extensions=EXT)
    assert _names(report, tmp_path) == ["notes.txt", "real.pdf", "sub/real.docx"]
    assert report.invalid_skipped == 2
    assert report.files_skipped == 0


def test_invalid_summary_and_notices():
    assert invalid_summary(0) is None
    assert "1 file that is not a real document" in invalid_summary(1)
    assert "3 files that are not real documents" in invalid_summary(3)
    notices = ignore_notices(SimpleNamespace(files_skipped=2, invalid_skipped=1), None, "src_1")
    assert len(notices) == 2
    assert "Skipped 2 files in ignored folders" in notices[0]
    assert "not a real document" in notices[1]


# -- pipeline + prune --------------------------------------------------------


def test_pipeline_applies_stored_rules_and_records_mtime(env):
    _write_xlsx(env.folder / "keep.xlsx", "S", ["n", "q"], [["apples", 1]])
    _write_xlsx(env.folder / "skip.xlsx", "S", ["n", "q"], [["pears", 2]])
    with env.session_factory() as session:
        session.get(Source, env.source.id).rules_json = dump_rules_json(
            {"ignored_files": ["skip.xlsx"]}
        )
        session.commit()
    result = env.pipeline.run_ingestion_for_source(env.source.id)
    assert [r.path.name for r in result.file_results] == ["keep.xlsx"]
    assert result.files_skipped == 1
    with env.session_factory() as session:
        v = session.execute(select(EvidenceVersion)).scalar_one()
        assert v.status == VersionStatus.READY
        assert v.source_mtime == (env.folder / "keep.xlsx").stat().st_mtime
        assert v.failure_reason is None


def test_pipeline_counts_invalid_and_does_not_create_failed_versions(env):
    _write_xlsx(env.folder / "keep.xlsx", "S", ["n", "q"], [["apples", 1]])
    (env.folder / "fake.pdf").write_text("plain text, not a pdf")
    result = env.pipeline.run_ingestion_for_source(env.source.id)
    assert result.invalid_skipped == 1
    assert result.files_failed == 0
    assert _statuses(env) == {"keep.xlsx": VersionStatus.READY}


def test_failed_version_of_invalid_file_is_prunable(env):
    bad = env.folder / "fake.pdf"
    bad.write_text("plain text, not a pdf")  # what older runs recorded as FAILED
    em = env.pipeline._evidence_manager
    version = em.ingest_file(env.source.id, bad, parser_name="docling", parser_version="1")
    em.mark_version_status(version.id, VersionStatus.FAILED, failure_reason="not a valid PDF file")
    prune = SourcePruneService(env.session_factory, em, env.pipeline._index_manager)

    found = prune.find(env.source.id)
    assert [(c.relative_path.as_posix(), c.status) for c in found] == [("fake.pdf", "failed")]
    prune.apply(env.source.id)
    assert _statuses(env) == {"fake.pdf": VersionStatus.SUPERSEDED}
    assert bad.exists()


def test_valid_failed_file_is_not_pruned_as_invalid(env):
    good = env.folder / "real.pdf"
    good.write_bytes(b"%PDF-1.4\nbroken body")
    em = env.pipeline._evidence_manager
    version = em.ingest_file(env.source.id, good, parser_name="docling", parser_version="1")
    em.mark_version_status(version.id, VersionStatus.FAILED, failure_reason="parser crashed")
    prune = SourcePruneService(env.session_factory, em, env.pipeline._index_manager)
    assert prune.find(env.source.id) == []


def test_stored_rules_make_indexed_files_prunable(env):
    _write_xlsx(env.folder / "keep.xlsx", "S", ["n", "q"], [["apples", 1]])
    env.pipeline.run_ingestion_for_source(env.source.id)
    prune = SourcePruneService(
        env.session_factory, env.pipeline._evidence_manager, env.pipeline._index_manager
    )
    assert prune.find(env.source.id) == []
    with env.session_factory() as session:
        session.get(Source, env.source.id).rules_json = '{"version": 1, "exclude": ["keep.xlsx"]}'
        session.commit()
    assert [c.relative_path.as_posix() for c in prune.find(env.source.id)] == ["keep.xlsx"]


def test_failure_reason_cleared_when_retry_succeeds(env):
    em = env.pipeline._evidence_manager
    f = env.folder / "real.pdf"
    f.write_bytes(b"%PDF-1.4\nx")
    version = em.ingest_file(env.source.id, f, parser_name="p", parser_version="1")
    em.mark_version_status(version.id, VersionStatus.FAILED, failure_reason="boom")
    with env.session_factory() as session:
        assert session.get(EvidenceVersion, version.id).failure_reason == "boom"
    em.mark_version_status(version.id, VersionStatus.READY)
    with env.session_factory() as session:
        assert session.get(EvidenceVersion, version.id).failure_reason is None
