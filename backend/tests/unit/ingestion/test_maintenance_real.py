"""CP5 on the real pipeline: failures that clear (ignore a file, retry one
file), narrowing an existing source, and a real plan/run/plan round trip.
Real openpyxl/Docling parsing, fake embedding gateway, scratch dirs only."""

from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import select

from conftest import _write_xlsx
from docket.core.db.models import EvidenceVersion, IngestionJob, VersionStatus
from docket.services.ingestion.failure import plain_failure_reason
from docket.services.sources.maintenance import LibraryMaintenance
from docket.services.sources.prune import SourcePruneService


def _maintenance(env) -> LibraryMaintenance:
    prune = SourcePruneService(
        env.session_factory, env.pipeline._evidence_manager, env.pipeline._index_manager
    )
    return LibraryMaintenance(
        session_factory=env.session_factory,
        source_manager=env.source_manager,
        prune_service=prune,
        pipeline=env.pipeline,
        seconds_per_file=lambda: 1.0,
    )


def _versions(env) -> dict[str, EvidenceVersion]:
    with env.session_factory() as session:
        rows = session.execute(select(EvidenceVersion)).scalars().all()
        return {
            Path(v.file_path).name + ":" + v.status.name: v for v in rows
        }


def _need_attention(env) -> int:
    """FAILED, non-superseded versions: what the Sources panel counts."""
    return sum(1 for key in _versions(env) if key.endswith(":FAILED"))


def _bad(path: Path) -> None:
    path.write_bytes(b"PK\x03\x04this is not a workbook")


# -- failures that clear ---------------------------------------------------------


def test_failed_file_records_a_reason_and_ignoring_it_reaches_zero(env):
    _write_xlsx(env.folder / "good.xlsx", "S", ["n", "q"], [["a", 1]])
    _bad(env.folder / "bad.xlsx")
    result = env.pipeline.run_ingestion_for_source(env.source.id)
    assert result.files_failed == 1
    failed = _versions(env)["bad.xlsx:FAILED"]
    assert failed.failure_reason  # stored for the Sources panel
    assert plain_failure_reason(failed.failure_reason, "bad.xlsx")  # and mappable
    assert _need_attention(env) == 1

    m = _maintenance(env)
    assert m.ignore_file(env.source.id, "bad.xlsx") == 1
    assert _need_attention(env) == 0
    assert _versions(env)["bad.xlsx:SUPERSEDED"]
    rules = json.loads(env.source_manager.get_source(env.source.id).rules_json)
    assert rules["ignored_files"] == ["bad.xlsx"]
    assert (env.folder / "bad.xlsx").exists()  # the user's file is never touched

    # a later run leaves it alone and nothing is listed again
    again = env.pipeline.run_ingestion_for_source(env.source.id)
    assert [r.path.name for r in again.file_results] == ["good.xlsx"]
    assert _need_attention(env) == 0
    assert m.ignore_file(env.source.id, "bad.xlsx") == 0  # idempotent


def test_retry_single_file_clears_the_failure_when_it_now_reads(env):
    _write_xlsx(env.folder / "good.xlsx", "S", ["n", "q"], [["a", 1]])
    _bad(env.folder / "fix.xlsx")
    env.pipeline.run_ingestion_for_source(env.source.id)
    assert _need_attention(env) == 1
    jobs_before = _job_count(env)

    _write_xlsx(env.folder / "fix.xlsx", "S", ["n", "q"], [["fixed", 2]])
    job = _maintenance(env).retry_file(env.source.id, "fix.xlsx")
    assert job.status == "succeeded" and job.files_processed == 1
    assert [(r.path.name, r.status) for r in job.file_results] == [("fix.xlsx", "ingested")]
    assert _need_attention(env) == 0
    ready = [k for k in _versions(env) if k.startswith("fix.xlsx") and k.endswith("READY")]
    assert ready
    assert _versions(env)[ready[0]].failure_reason is None
    assert _job_count(env) == jobs_before + 1
    # only that one file was processed: good.xlsx was not re-read
    assert job.files_processed == 1


def test_retry_that_still_fails_keeps_it_listed_with_a_reason(env):
    _bad(env.folder / "still.xlsx")
    env.pipeline.run_ingestion_for_source(env.source.id)
    job = _maintenance(env).retry_file(env.source.id, "still.xlsx")
    assert job.status == "failed" and job.files_failed == 1
    assert _need_attention(env) == 1


def test_retry_refuses_paths_outside_the_source_and_missing_files(env, tmp_path):
    outside = tmp_path / "elsewhere.xlsx"
    _write_xlsx(outside, "S", ["n"], [[1]])
    m = _maintenance(env)
    job = env.pipeline.run_ingestion_for_paths(env.source.id, [outside, env.folder / "nope.xlsx"])
    assert job.files_failed == 2 and job.status == "failed"
    assert "not inside" in job.file_results[0].error
    assert "not found" in job.file_results[1].error
    assert not _versions(env)  # nothing was ingested
    assert m.plan().empty


def _job_count(env) -> int:
    with env.session_factory() as session:
        return len(session.execute(select(IngestionJob)).scalars().all())


# -- narrow ------------------------------------------------------------------------


def _tree(env) -> None:
    for rel, val in [("root.xlsx", 0), ("a/x.xlsx", 1), ("b/y.xlsx", 2), ("c/z.xlsx", 3)]:
        p = env.folder / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        _write_xlsx(p, "S", ["n", "q"], [["v", val]])


def test_narrow_saves_anchored_excludes_then_prunes_without_reparsing(env):
    _tree(env)
    env.pipeline.run_ingestion_for_source(env.source.id)
    assert sum(k.endswith("READY") for k in _versions(env)) == 4
    m = _maintenance(env)

    parsed: list[str] = []
    real_parse = env.pipeline._parser.parse
    env.pipeline._parser.parse = lambda *a, **k: parsed.append("parse") or real_parse(*a, **k)

    removed = m.narrow(env.source.id, ["a"])
    rules = json.loads(env.source_manager.get_source(env.source.id).rules_json)
    assert sorted(rules["exclude"]) == ["/b/", "/c/"]
    assert removed == 2
    states = {k for k in _versions(env)}
    assert {"root.xlsx:READY", "x.xlsx:READY", "y.xlsx:SUPERSEDED", "z.xlsx:SUPERSEDED"} <= states
    assert parsed == []  # nothing was re-parsed
    assert (env.folder / "b" / "y.xlsx").exists()
    assert m.plan().empty  # nothing left to prune

    # widening again lets a folder back in; the next read picks it up
    m.narrow(env.source.id, ["a", "b"])
    rules = json.loads(env.source_manager.get_source(env.source.id).rules_json)
    assert rules["exclude"] == ["/c/"]
    result = env.pipeline.run_ingestion_for_source(env.source.id)
    assert {r.path.name for r in result.file_results} == {"root.xlsx", "x.xlsx", "y.xlsx"}


def test_narrow_keeps_other_rules_and_escapes_names(env):
    (env.folder / "odd[1]").mkdir()
    (env.folder / "plain").mkdir()
    env.source_manager.set_rules(
        env.source.id, {"exclude": ["drafts/"], "include": [], "ignored_files": ["keep/me.pdf"]}
    )
    env.source_manager.narrow_source(env.source.id, ["plain"])
    rules = json.loads(env.source_manager.get_source(env.source.id).rules_json)
    assert "drafts/" in rules["exclude"] and "/odd[[]1]/" in rules["exclude"]
    assert rules["ignored_files"] == ["keep/me.pdf"]
    assert "/plain/" not in rules["exclude"]


# -- a real plan / run / plan round trip ----------------------------------------------


def test_real_plan_prunes_after_rules_change_and_is_then_empty(env):
    _tree(env)
    env.pipeline.run_ingestion_for_source(env.source.id)
    env.source_manager.set_rules(env.source.id, {"exclude": ["/b/"], "include": [], "ignored_files": []})
    m = _maintenance(env)
    plan = m.plan()
    assert [s.kind for s in plan.steps] == ["prune"]
    assert plan.steps[0].count == 1 and plan.steps[0].detail == ("b/y.xlsx",)
    result = m.run(plan)
    assert result.ok and result.pruned == 1
    assert m.plan().empty
    assert (env.folder / "b" / "y.xlsx").exists()
