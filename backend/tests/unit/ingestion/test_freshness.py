"""CP6 on the real pipeline: change detection by path/size/mtime, the shared
path-level ingest (new, changed, deleted), and the safety rules around an
unreachable or emptied root. Real openpyxl parsing, fake embedding gateway,
scratch dirs only; no Ollama, no waiting on a watcher.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from sqlalchemy import select

from conftest import _write_xlsx
from docket.core.db.models import EvidenceVersion, IngestionJob, SourceStatus, VersionStatus
from docket.services.sources.freshness import catch_up


def _book(path: Path, value: object = 1) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_xlsx(path, "S", ["name", "qty"], [["row", value]])
    return path


def _catch_up(env, **kw):
    return catch_up(env.source.id, session_factory=env.session_factory, **kw)


def _names(paths) -> list[str]:
    return sorted(Path(p).name for p in paths)


def _versions(env, name: str) -> list[EvidenceVersion]:
    with env.session_factory() as session:
        rows = session.execute(select(EvidenceVersion)).scalars().all()
    return [v for v in rows if Path(v.file_path).name == name]


def _index_ids(env) -> set[str]:
    return env.fts.chunk_ids_for_source(env.source.id)


def _settled(env) -> None:
    """Index everything once so later changes are measured against it."""
    env.pipeline.run_ingestion_for_source(env.source.id)


# -- detection -------------------------------------------------------------------


def test_new_changed_deleted_and_renamed_are_told_apart(env):
    keep, edit, gone, ren = (
        _book(env.folder / "keep.xlsx"),
        _book(env.folder / "edit.xlsx", 1),
        _book(env.folder / "gone.xlsx"),
        _book(env.folder / "ren.xlsx"),
    )
    _settled(env)
    assert _catch_up(env).empty  # nothing changed since indexing

    _book(env.folder / "added.xlsx")
    _write_xlsx(edit, "S", ["name", "qty"], [["row", 99], ["more", 100]])  # new size
    gone.unlink()
    ren.rename(env.folder / "renamed.xlsx")

    cs = _catch_up(env)
    assert _names(cs.new) == ["added.xlsx", "renamed.xlsx"]
    assert _names(cs.changed) == ["edit.xlsx"]
    assert _names(cs.deleted) == ["gone.xlsx", "ren.xlsx"]  # a rename is a delete plus a new file
    assert cs.checked >= 4 and not cs.incomplete and not cs.unreachable
    assert keep.exists()


def test_a_touched_but_identical_file_costs_one_hash_and_no_parse(env, monkeypatch):
    f = _book(env.folder / "same.xlsx")
    _settled(env)
    assert _versions(env, "same.xlsx")[0].source_mtime is not None
    os.utime(f, (f.stat().st_atime, f.stat().st_mtime + 100))

    cs = _catch_up(env)
    assert _names(cs.changed) == ["same.xlsx"]  # mtime differs: a candidate

    parsed: list[Path] = []
    real = env.pipeline._xlsx_parser.parse
    monkeypatch.setattr(
        env.pipeline._xlsx_parser, "parse", lambda *a, **k: (parsed.append(a[-1]), real(*a, **k))[1]
    )
    job = env.pipeline.run_ingestion_for_paths(env.source.id, cs.paths, missing="skip")
    assert [r.status for r in job.file_results] == ["unchanged"]
    assert parsed == []  # hashed, never re-parsed
    assert len(_versions(env, "same.xlsx")) == 1
    # the new mtime is recorded, so the next look is free
    assert _catch_up(env).empty


def test_ignored_paths_never_show_up_or_get_ingested(env):
    _book(env.folder / "real.xlsx")
    _book(env.folder / "node_modules" / "dep.xlsx")
    _book(env.folder / ".hidden" / "h.xlsx")
    _book(env.folder / "drafts" / "d.xlsx")
    (env.folder / ".docketignore").write_text("drafts/\n")
    env.source_manager.set_rules(env.source.id, {"exclude": ["/archive/"]})
    _book(env.folder / "archive" / "old.xlsx")

    cs = _catch_up(env)
    assert _names(cs.new) == ["real.xlsx"]

    ignored = [
        env.folder / "node_modules" / "dep.xlsx",
        env.folder / "drafts" / "d.xlsx",
        env.folder / "archive" / "old.xlsx",
    ]
    job = env.pipeline.run_ingestion_for_paths(env.source.id, ignored, missing="skip")
    assert [r.status for r in job.file_results] == ["skipped"] * 3
    assert job.files_failed == 0 and job.status == "succeeded"
    assert _versions(env, "dep.xlsx") == [] and _versions(env, "old.xlsx") == []


def test_unsupported_and_not_real_documents_are_skipped_not_failed(env):
    (env.folder / "notes.bin").write_text("plain")
    fake = env.folder / "fake.xlsx"
    fake.write_bytes(b"this is not a zip at all")
    job = env.pipeline.run_ingestion_for_paths(env.source.id, [env.folder / "notes.bin", fake], missing="skip")
    assert [r.status for r in job.file_results] == ["skipped", "skipped"]
    assert _catch_up(env).empty  # discovery agrees: neither is a candidate


# -- the shared path-level ingest ---------------------------------------------------


def test_new_and_changed_paths_go_through_the_normal_per_file_path(env):
    a = _book(env.folder / "a.xlsx", 1)
    _book(env.folder / "b.xlsx", 2)
    job = env.pipeline.run_ingestion_for_paths(env.source.id, [a, env.folder / "b.xlsx"], missing="skip")
    assert job.status == "succeeded"
    assert [r.status for r in job.file_results] == ["ingested", "ingested"]
    before = _index_ids(env)
    assert before

    _write_xlsx(a, "S", ["name", "qty"], [["row", 7], ["row2", 8], ["row3", 9]])
    job = env.pipeline.run_ingestion_for_paths(env.source.id, [a], missing="skip")
    assert [r.status for r in job.file_results] == ["ingested"]
    after = _index_ids(env)
    assert after != before  # a's old chunks left, new ones arrived; b's stayed
    states = sorted(v.status.name for v in _versions(env, "a.xlsx"))
    assert states == ["READY", "SUPERSEDED"]


def test_a_deleted_file_is_superseded_and_leaves_the_index(env):
    keep, gone = _book(env.folder / "keep.xlsx", 1), _book(env.folder / "gone.xlsx", 2)
    _settled(env)
    gone_version = next(v for v in _versions(env, "gone.xlsx") if v.status == VersionStatus.READY)
    with_both = _index_ids(env)

    gone.unlink()
    job = env.pipeline.run_ingestion_for_paths(env.source.id, [gone], missing="skip")
    assert [r.status for r in job.file_results] == ["deleted"]
    assert job.status == "succeeded"
    versions = _versions(env, "gone.xlsx")
    assert [v.status for v in versions] == [VersionStatus.SUPERSEDED]
    assert versions[0].id == gone_version.id
    remaining = _index_ids(env)
    assert remaining and remaining < with_both  # only keep.xlsx's chunks are searchable
    assert _versions(env, "keep.xlsx")[0].status == VersionStatus.READY and keep.exists()
    assert _catch_up(env).empty


def test_repeated_runs_are_idempotent(env):
    a = _book(env.folder / "a.xlsx")
    gone = _book(env.folder / "gone.xlsx")
    _settled(env)
    gone.unlink()
    first = env.pipeline.run_ingestion_for_paths(env.source.id, [a, gone], missing="skip")
    snapshot = (_index_ids(env), len(_versions(env, "a.xlsx")), len(_versions(env, "gone.xlsx")))
    second = env.pipeline.run_ingestion_for_paths(env.source.id, [a, gone], missing="skip")
    third = env.pipeline.run_ingestion_for_paths(env.source.id, [a, gone], missing="skip")
    assert [r.status for r in first.file_results] == ["unchanged", "deleted"]
    assert [r.status for r in second.file_results] == ["unchanged", "skipped"]
    assert [r.status for r in third.file_results] == ["unchanged", "skipped"]
    assert (_index_ids(env), len(_versions(env, "a.xlsx")), len(_versions(env, "gone.xlsx"))) == snapshot
    assert _catch_up(env).empty and _catch_up(env).empty


def test_one_job_row_per_batch_with_stats(env):
    a, b = _book(env.folder / "a.xlsx"), _book(env.folder / "b.xlsx")
    _settled(env)
    with env.session_factory() as session:
        before = len(session.execute(select(IngestionJob)).scalars().all())
    b.unlink()
    _write_xlsx(a, "S", ["name", "qty"], [["row", 5], ["r2", 6]])
    env.pipeline.run_ingestion_for_paths(env.source.id, [a, b, env.folder / "never.xlsx"], missing="skip")
    with env.session_factory() as session:
        jobs = session.execute(select(IngestionJob)).scalars().all()
    assert len(jobs) == before + 1
    stats = json.loads(jobs[-1].stats_json)
    assert stats["files_processed"] == 3 and stats["files_deleted"] == 1
    assert stats["files_skipped"] == 1 and stats["files_failed"] == 0 and stats["chunks_written"] > 0
    assert jobs[-1].finished_at is not None


def test_missing_path_with_nothing_indexed_is_an_error_only_when_asked(env):
    ghost = env.folder / "ghost.xlsx"
    err = env.pipeline.run_ingestion_for_paths(env.source.id, [ghost])  # Retry's default
    assert err.files_failed == 1 and "not found" in err.file_results[0].error
    ok = env.pipeline.run_ingestion_for_paths(env.source.id, [ghost], missing="skip")
    assert [r.status for r in ok.file_results] == ["skipped"] and ok.status == "succeeded"


# -- safety --------------------------------------------------------------------------


def test_an_unreachable_root_deletes_nothing(env, tmp_path):
    _book(env.folder / "a.xlsx")
    _book(env.folder / "b.xlsx")
    _settled(env)
    away = tmp_path / "away"
    env.folder.rename(away)  # an unmounted drive, in miniature

    cs = _catch_up(env)
    assert cs.unreachable and cs.empty
    job = env.pipeline.run_ingestion_for_paths(
        env.source.id, [env.folder / "a.xlsx", env.folder / "b.xlsx"], missing="skip"
    )
    assert job.status == "failed" and job.files_processed == 0
    ready = [v for name in ("a.xlsx", "b.xlsx") for v in _versions(env, name)]
    assert [v.status for v in ready] == [VersionStatus.READY] * 2
    assert env.source_manager.get_source(env.source.id).status == SourceStatus.MISSING

    away.rename(env.folder)  # it comes back: the source recovers, nothing was lost
    job = env.pipeline.run_ingestion_for_paths(env.source.id, [env.folder / "a.xlsx"], missing="skip")
    assert [r.status for r in job.file_results] == ["unchanged"]
    assert env.source_manager.get_source(env.source.id).status == SourceStatus.ACTIVE


def test_a_root_that_turns_up_empty_holds_deletions_back(env):
    _book(env.folder / "a.xlsx")
    _book(env.folder / "b.xlsx")
    _settled(env)
    for child in env.folder.iterdir():
        child.unlink()
    cs = _catch_up(env)
    assert cs.deleted == [] and cs.held_back == 2


# -- bounded walks ---------------------------------------------------------------------


def test_a_bounded_walk_reports_incomplete_and_resumes(env):
    expected = set()
    for d in ("a", "b", "c", "d"):
        for i in range(3):
            expected.add(_book(env.folder / d / f"f{i}.xlsx", i).name + d)
    seen: set[str] = set()
    cursor = None
    passes = 0
    first = _catch_up(env, max_entries=4, start_after=None)
    assert first.incomplete and first.resume_from is not None
    while True:
        passes += 1
        assert passes < 30
        cs = _catch_up(env, max_entries=4, start_after=cursor)
        seen |= {p.name + p.parent.name for p in cs.new}
        if not cs.incomplete:
            break
        assert cs.resume_from != cursor or passes > 20  # it makes progress
        cursor = cs.resume_from
    assert seen == expected and passes > 1


def test_known_files_outside_the_scanned_part_are_still_checked_by_stat(env):
    files = [_book(env.folder / d / "f.xlsx") for d in ("a", "b", "c")]
    _settled(env)
    files[2].unlink()
    _write_xlsx(files[1], "S", ["name", "qty"], [["x", 1], ["y", 2]])
    cs = _catch_up(env, max_entries=1)
    assert cs.incomplete
    assert _names(cs.changed) == ["f.xlsx"] and cs.changed[0].parent.name == "b"
    assert [p.parent.name for p in cs.deleted] == ["c"]


def test_deleting_a_whole_subfolder_is_noticed(env):
    _book(env.folder / "keep.xlsx")
    _book(env.folder / "sub" / "x.xlsx")
    _book(env.folder / "sub" / "y.xlsx")
    _settled(env)
    shutil.rmtree(env.folder / "sub")
    cs = _catch_up(env)
    assert _names(cs.deleted) == ["x.xlsx", "y.xlsx"]
    job = env.pipeline.run_ingestion_for_paths(env.source.id, cs.paths, missing="skip")
    assert [r.status for r in job.file_results] == ["deleted", "deleted"]
