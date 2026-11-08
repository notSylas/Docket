"""Tiered indexing: persisted priority queue, runner, yielding, status,
eligibility at 100k versions, migration up/down."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text

from docket.core.config import Settings
from docket.core.db.engine import get_engine, get_session_factory
from docket.core.db.models import Chunk, EvidenceVersion, SourceStatus, VersionStatus
from docket.infra.index.base import ChunkRecord
from docket.infra.index.fts_index import FtsIndexWriter
from docket.infra.index.vector_index import LanceIndexWriter
from docket.infra.inference.gateway import FakeInferenceGateway
from docket.infra.retrieval.hybrid import fts_search, vector_search
from docket.interfaces.cli.tui.yield_gate import YieldGate
from docket.services.ingestion.queue import DEAD, DONE, QUEUED, RUNNING, IngestQueue, QueuePolicy
from docket.services.ingestion.tiered import TieredIndexer, status_line, tier_status
from docket.services.sources.manager import SourceManager

from conftest import _write_docx
from test_hybrid_retrieval import _insert_metadata
from test_page_images_optional import _pipeline
from test_scale_step1a import _bulk_clone_versions, _rec
from test_tiered import BODY, CountingParser

REPO = Path(__file__).resolve().parents[3]
DAY = 86400.0


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def _queue(engine, clock=None, **policy):
    return IngestQueue(get_session_factory(engine), QueuePolicy(**policy), clock or Clock())


def _source(engine, tmp_path):
    sf = get_session_factory(engine)
    folder = tmp_path / "docs"
    folder.mkdir(exist_ok=True)
    return sf, SourceManager(sf).register_source(folder), folder


def _touch(path, age_days, size=10, now=1_000_000.0):
    path.write_bytes(b"x" * size)
    t = now - age_days * DAY
    os.utime(path, (t, t))


# -- priority ordering ---------------------------------------------------------


def test_claim_order_touched_then_recent_then_recency_then_size(migrated_sqlite_engine, tmp_path):
    engine = migrated_sqlite_engine
    sf, source, folder = _source(engine, tmp_path)
    (folder / "proj").mkdir()
    spec = {  # name: (age in days, size)
        "old_small.txt": (400, 5), "old_big.txt": (400, 500), "old_older.txt": (900, 1),
        "new_big.txt": (1, 900), "new_small.txt": (1, 3), "week.txt": (6, 40),
        "proj/touched_old.txt": (800, 700),
    }
    for name, (age, size) in spec.items():
        _touch(folder / name, age, size)
    q = _queue(engine)
    q.enqueue(source.id, [folder / n for n in spec], tiers=(1,), touched=[folder / "proj"])
    order = []
    while (job := q.claim(1)) is not None:
        order.append(str(job.path.relative_to(folder)))
        q.complete(job)
    assert order == [
        "proj/touched_old.txt",            # user-touched folder first
        "new_small.txt", "new_big.txt",    # recent, same day: size ascending
        "week.txt",                        # recent, older day
        "old_small.txt", "old_big.txt",    # not recent: day bucket 400, size ascending
        "old_older.txt",
    ]


def test_boost_persists_and_reorders_queued_jobs(migrated_sqlite_engine, tmp_path):
    engine = migrated_sqlite_engine
    sf, source, folder = _source(engine, tmp_path)
    (folder / "hot").mkdir()
    _touch(folder / "a.txt", 1)
    _touch(folder / "hot" / "z.txt", 900, 999)
    q = _queue(engine)
    q.enqueue(source.id, [folder / "a.txt", folder / "hot" / "z.txt"], tiers=(1,))
    assert q.peek(1, 1) == [folder / "a.txt"]
    assert q.boost(source.id, folder / "hot") == 1
    assert _queue(engine).peek(1, 1) == [folder / "hot" / "z.txt"]  # a fresh queue object sees it


# -- checkpoints, retry, dead letters -------------------------------------------------


def test_resume_after_kill_recovers_running_rows_and_never_repeats_done(migrated_sqlite_engine, tmp_path):
    engine = migrated_sqlite_engine
    sf, source, folder = _source(engine, tmp_path)
    for i in range(4):
        _touch(folder / f"f{i}.txt", 1, 10 + i)
    clock = Clock()
    q = _queue(engine, clock)
    q.enqueue(source.id, [folder / f"f{i}.txt" for i in range(4)], tiers=(1,))
    done = q.claim(1)
    q.complete(done)
    victim = q.claim(1)            # claimed, then the process is killed -9
    assert q.counts()[1][RUNNING] == 1
    q2 = _queue(engine, clock)     # new process
    assert q2.recover() == 1
    counts = q2.counts()[1]
    assert counts[RUNNING] == 0 and counts[DONE] == 1 and counts[QUEUED] == 3
    claimed = []
    while (j := q2.claim(1)) is not None:
        claimed.append(j.path)
        q2.complete(j)
    assert done.path not in claimed and victim.path in claimed and len(claimed) == 3
    # re-enqueue of an unchanged tree adds nothing and does not reopen done rows
    out = q2.enqueue(source.id, [folder / f"f{i}.txt" for i in range(4)], tiers=(1,))
    assert out == {"new": 0, "reset": 0, "kept": 4} and q2.claim(1) is None


def test_retry_backoff_then_dead_letter_and_poison_file_cannot_loop(migrated_sqlite_engine, tmp_path):
    engine = migrated_sqlite_engine
    sf, source, folder = _source(engine, tmp_path)
    _touch(folder / "bad.txt", 1)
    clock = Clock()
    q = _queue(engine, clock, max_attempts=3, backoff_base_s=30)
    q.enqueue(source.id, [folder / "bad.txt"], tiers=(1,))
    j = q.claim(1)
    assert q.fail(j, "boom") == QUEUED
    assert q.claim(1) is None                      # backing off (30 s)
    clock.t += 31
    j = q.claim(1)
    assert j.attempts == 1 and q.fail(j, "boom") == QUEUED
    clock.t += 59
    assert q.claim(1) is None                      # second backoff is 60 s
    clock.t += 2
    j = q.claim(1)
    assert q.fail(j, "boom again") == DEAD
    clock.t += 10_000
    assert q.claim(1) is None and q.counts()[1][DEAD] == 1
    assert q.dead_letters()[0][2] == "boom again"
    assert q.retry_dead() == 1 and q.claim(1) is not None
    # a file that kills the process every time: each recovery burns an attempt
    q.recover()
    q.claim(1)
    q.recover()
    q.claim(1)
    q.recover()
    assert q.counts()[1][DEAD] == 1


def test_changed_file_resets_dead_and_done_rows(migrated_sqlite_engine, tmp_path):
    engine = migrated_sqlite_engine
    sf, source, folder = _source(engine, tmp_path)
    p = folder / "a.txt"
    _touch(p, 1, 10)
    q = _queue(engine, max_attempts=1)
    q.enqueue(source.id, [p], tiers=(1, 2))
    q.fail(q.claim(1), "x")
    q.complete(q.claim(2))
    assert q.counts()[1][DEAD] == 1 and q.counts()[2][DONE] == 1
    _touch(p, 0, 25)
    assert q.enqueue(source.id, [p], tiers=(1, 2))["reset"] == 2
    assert q.counts()[1][QUEUED] == 1 and q.counts()[2][QUEUED] == 1


# -- runner ---------------------------------------------------------------------------


def _indexer_env(engine, tmp_path, n_docx=6):
    parser = CountingParser()
    sf, pipeline = _pipeline(engine, tmp_path, parser, Settings())
    folder = tmp_path / "docs"
    folder.mkdir()
    source = SourceManager(sf).register_source(folder)
    for i in range(n_docx):
        _write_docx(folder / f"doc{i}.docx", f"Title {i}", f"unique{i}marker " + BODY)
    (folder / "n.txt").write_text("plain wombat note " * 40)
    return sf, pipeline, parser, source, folder


def test_tier1_only_then_deepen_each_file_processed_once(migrated_sqlite_engine, tmp_path):
    engine = migrated_sqlite_engine
    sf, pipeline, parser, source, folder = _indexer_env(engine, tmp_path)
    queue = IngestQueue(sf)
    indexer = TieredIndexer(pipeline=pipeline, queue=queue)
    assert indexer.enqueue_source(source.id)["new"] == 14
    s1 = indexer.run(tier2=False)
    assert (s1.tier1_done, s1.tier2_done) == (7, 0) and s1.first_searchable_s is not None
    assert parser.calls == [] and pipeline._gateway.embed_calls == []
    assert fts_search(engine, "unique3marker", 3)                      # searchable now
    st = tier_status(engine, queue)
    assert st["versions"]["keyword_only"] == 7 and st["queue"]["tier2"]["queued"] == 7
    assert "7 keyword-only" in status_line(st)
    s2 = indexer.run()
    assert s2.tier2_done == 7 and sorted(parser.calls) == sorted(f"doc{i}.docx" for i in range(6))
    st = tier_status(engine, queue)
    assert st["versions"]["ready"] == 7 and st["versions"]["keyword_only"] == 0
    assert st["coverage"] is None and st["queue"]["tier2"]["done"] == 7
    # a third run does nothing at all
    n_embeds = len(pipeline._gateway.embed_calls)
    s3 = indexer.run()
    assert (s3.tier1_done, s3.tier2_done) == (0, 0) and len(pipeline._gateway.embed_calls) == n_embeds


def test_kill_and_resume_does_no_repeated_work(migrated_sqlite_engine, tmp_path):
    engine = migrated_sqlite_engine
    sf, pipeline, parser, source, folder = _indexer_env(engine, tmp_path)
    queue = IngestQueue(sf)
    seen = {"n": 0}

    def stop_after_three():
        return seen["n"] >= 3

    def progress(kind, job, detail):
        if kind == "done":
            seen["n"] += 1

    first = TieredIndexer(pipeline=pipeline, queue=queue, should_stop=stop_after_three, progress=progress)
    first.enqueue_source(source.id)
    s = first.run()
    assert s.stopped and seen["n"] == 3
    # simulate kill -9 mid-file: a job left running with no live worker
    j = queue.claim(1)
    assert j is not None and queue.counts()[1][RUNNING] == 1
    second = TieredIndexer(pipeline=pipeline, queue=IngestQueue(sf))
    second.run()
    counts = IngestQueue(sf).counts()
    assert counts[1][DONE] == 7 and counts[2][DONE] == 7 and counts[1][RUNNING] == 0
    assert sorted(parser.calls) == sorted(f"doc{i}.docx" for i in range(6))   # parsed exactly once each
    with sf() as s:
        assert {v.status for v in s.execute(select(EvidenceVersion)).scalars()} == {VersionStatus.READY}


def test_failing_file_is_retried_then_dead_lettered_and_stays_keyword_searchable(
    migrated_sqlite_engine, tmp_path
):
    engine = migrated_sqlite_engine
    sf, pipeline, parser, source, folder = _indexer_env(engine, tmp_path, n_docx=2)
    clock = Clock()
    queue = IngestQueue(sf, QueuePolicy(max_attempts=2, backoff_base_s=30.0), clock)
    indexer = TieredIndexer(pipeline=pipeline, queue=queue)
    indexer.enqueue_source(source.id)
    indexer.run(tier2=False)
    real = parser.parse

    def parse(source_id, path):
        if path.name == "doc1.docx":
            raise RuntimeError("cannot parse")
        return real(source_id, path)

    parser.parse = parse
    s = indexer.run()
    assert s.dead == 0 and s.retried == 1
    assert indexer.run().tier2_done == 0           # still backing off: nothing claimable
    clock.t += 31
    s = indexer.run()
    assert s.dead == 1
    counts = queue.counts()
    assert counts[2][DEAD] == 1 and counts[2][DONE] == 2 - 1 + 1  # doc0 + n.txt
    status = {Path(v.file_path).name: v.status for v in sf().execute(select(EvidenceVersion)).scalars()}
    assert status["doc1.docx"] == VersionStatus.TEXT_READY and status["doc0.docx"] == VersionStatus.READY
    assert fts_search(engine, "unique1marker", 3)
    assert "gave up" in status_line(tier_status(engine, queue))


def test_vanished_file_is_marked_done_and_deindexed(migrated_sqlite_engine, tmp_path):
    engine = migrated_sqlite_engine
    sf, pipeline, parser, source, folder = _indexer_env(engine, tmp_path, n_docx=2)
    queue = IngestQueue(sf)
    indexer = TieredIndexer(pipeline=pipeline, queue=queue)
    indexer.enqueue_source(source.id)
    indexer.run(tier2=False)
    (folder / "doc0.docx").unlink()
    indexer.run()
    assert queue.counts()[2][DEAD] == 0
    assert not fts_search(engine, "unique0marker", 3)


# -- yielding ----------------------------------------------------------------------------


def test_pause_hook_runs_between_files_and_between_embedding_batches(migrated_sqlite_engine, tmp_path):
    engine = migrated_sqlite_engine
    sf, pipeline, parser, source, folder = _indexer_env(engine, tmp_path, n_docx=1)
    pipeline._index_manager._embed_batch_size = 2
    _write_docx(folder / "doc0.docx", "Big", BODY * 12)
    calls = []
    queue = IngestQueue(sf)
    indexer = TieredIndexer(pipeline=pipeline, queue=queue, pause=lambda: calls.append(1))
    indexer.enqueue_source(source.id)
    indexer.run(tier2=False)
    between_files = len(calls)
    assert between_files >= 2                        # at least once per file
    calls.clear()
    indexer.run()
    n_chunks = len(pipeline._gateway.embed_calls)
    batches = -(-len(pipeline._gateway.embed_calls) // 2)
    assert n_chunks > 4 and len(calls) >= batches    # + once per file, before every embedding batch


def test_yield_gate_holds_deep_indexing_until_the_question_is_done(migrated_sqlite_engine, tmp_path):
    engine = migrated_sqlite_engine
    sf, pipeline, parser, source, folder = _indexer_env(engine, tmp_path, n_docx=3)
    gate = YieldGate()
    stop = threading.Event()
    queue = IngestQueue(sf)
    indexer = TieredIndexer(
        pipeline=pipeline, queue=queue,
        pause=lambda: gate.wait_clear(stop.is_set, poll=0.01), should_stop=stop.is_set,
    )
    indexer.enqueue_source(source.id)
    gate.hold()                                      # a question is being answered
    t = threading.Thread(target=indexer.run, daemon=True)
    t.start()
    time.sleep(0.5)
    assert queue.counts()[1][DONE] == 0 and t.is_alive()   # nothing started while held
    gate.release()
    t.join(timeout=60)
    assert not t.is_alive() and queue.counts()[2][DONE] == 4


# -- status ---------------------------------------------------------------------------------


def test_status_reports_counts_eta_and_coverage(migrated_sqlite_engine, tmp_path):
    engine = migrated_sqlite_engine
    sf, source, folder = _source(engine, tmp_path)
    clock = Clock()
    q = _queue(engine, clock)
    for i in range(10):
        _touch(folder / f"f{i}.txt", 1, 10 + i)
    q.enqueue(source.id, [folder / f"f{i}.txt" for i in range(10)], tiers=(1,))
    for _ in range(4):                     # four jobs, 2 s each => 0.5 files/s
        j = q.claim(1)
        clock.t += 2
        q.complete(j)
    st = tier_status(engine, q)
    assert st["queue"]["tier1"] == {"queued": 6, "running": 0, "done": 4, "dead": 0}
    assert st["rate_files_per_s"]["tier1"] == pytest.approx(0.5, rel=0.01)
    assert st["eta_s"]["tier1"] == pytest.approx(12.0, rel=0.05)
    assert st["eta_s"]["tier2"] == 0.0
    assert "quick pass: 6 left, ETA 12s" in status_line(st)


# -- eligibility at scale ------------------------------------------------------------------------


def test_100k_versions_text_ready_is_keyword_only_and_nothing_leaks(
    migrated_sqlite_engine, tmp_path, monkeypatch
):
    monkeypatch.setenv("DOCKET_ELIGIBILITY_MODE", "scalable")
    engine = migrated_sqlite_engine
    gateway = FakeInferenceGateway()
    query = "needle query"
    good = [_rec(f"chk_ok{i}", f"ev_ok{i}", f"other {i}") for i in range(5)]
    _insert_metadata(engine, good)
    # a TEXT_READY file: keyword-eligible, vector-ineligible
    prov = [_rec(f"chk_tr{i}", "ev_tr", query, source_id="src_tr") for i in range(3)]
    _insert_metadata(engine, prov, version_status=VersionStatus.TEXT_READY)
    with engine.begin() as conn:
        conn.execute(text("UPDATE chunks SET provisional = 1 WHERE source_id = 'src_tr'"))
    bad = {
        "sup": (VersionStatus.SUPERSEDED, SourceStatus.ACTIVE),
        "fail": (VersionStatus.FAILED, SourceStatus.ACTIVE),
        "pend": (VersionStatus.PENDING, SourceStatus.ACTIVE),
        "rev": (VersionStatus.READY, SourceStatus.REVOKED),
        "revtr": (VersionStatus.TEXT_READY, SourceStatus.REVOKED),
        "tomb": (VersionStatus.READY, SourceStatus.TOMBSTONED),
    }
    bad_records = []
    for name, (vs, ss) in bad.items():
        recs = [_rec(f"chk_{name}{i}", f"ev_{name}", query, source_id=f"src_{name}") for i in range(3)]
        _insert_metadata(engine, recs, status=ss, version_status=vs)
        bad_records += recs
    _bulk_clone_versions(engine, "ev_ok0", 100_000, "ev_fake")
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM evidence_versions")).scalar_one() > 100_000

    fts = FtsIndexWriter(engine)
    fts.upsert(good + prov + bad_records)
    writer = LanceIndexWriter(tmp_path / "lancedb")
    # worst case: vectors for EVERY row exist (the TEXT_READY ones sit exactly on the query)
    writer.upsert(bad_records + prov, [gateway.embed(r.text) for r in bad_records + prov])
    writer.upsert(good, [gateway.embed(r.text) for r in good])

    t0 = time.perf_counter()
    vec = vector_search(writer.table, engine, gateway, query, top_k=4)
    assert time.perf_counter() - t0 < 5
    assert len(vec) == 4 and all(c.startswith("chk_ok") for c in vec)       # READY only
    monkeypatch.setenv("DOCKET_ELIGIBILITY_MODE", "legacy")
    assert vector_search(writer.table, engine, gateway, query, top_k=4) == vec

    kw = fts_search(engine, "needle", 50)
    assert set(kw) == {f"chk_tr{i}" for i in range(3)}                       # TEXT_READY yes, all else no
    scoped = fts_search(engine, "needle", 50, scope_version_ids=["ev_tr", "ev_sup", "ev_revtr"])
    assert set(scoped) == {f"chk_tr{i}" for i in range(3)}


# -- migration -------------------------------------------------------------------------------------


def _cfg(path):
    cfg = Config(str(REPO / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO / "src" / "docket" / "core" / "db" / "migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    return cfg


def test_migration_0016_up_down_on_a_populated_copy(tmp_path):
    path = tmp_path / "lib.sqlite3"
    cfg = _cfg(path)
    command.upgrade(cfg, "head")
    engine = get_engine(path)
    sf = get_session_factory(engine)
    rec = ChunkRecord(
        chunk_id="chk_old", source_id="src_1", evidence_version_id="ev_1", evidence_unit_id="eu_ev_1",
        chunk_recipe_id="rcp_1", ordinal=0, heading=None, text="legacy ready text", content_hash="h",
    )
    _insert_metadata(engine, [rec])
    FtsIndexWriter(engine).upsert([rec])
    with engine.connect() as conn:
        cols = {r[1] for r in conn.execute(text("PRAGMA table_info(chunks)"))}
        tables = {r[0] for r in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))}
        assert "provisional" in cols and {"ingest_queue", "chunk_aliases"} <= tables
        assert conn.execute(text("SELECT provisional FROM chunks WHERE id='chk_old'")).scalar_one() == 0
    # add a TEXT_READY version with a provisional chunk, then go back down
    prov = ChunkRecord(
        chunk_id="chk_prov", source_id="src_2", evidence_version_id="ev_2", evidence_unit_id="eu_ev_2",
        chunk_recipe_id="rcp_1", ordinal=0, heading=None, text="provisional text", content_hash="h2",
    )
    _insert_metadata(engine, [prov], version_status=VersionStatus.TEXT_READY)
    with engine.begin() as conn:
        conn.execute(text("UPDATE chunks SET provisional = 1 WHERE id='chk_prov'"))
    FtsIndexWriter(engine).upsert([prov])
    engine.dispose()

    command.downgrade(cfg, "e5b8d2f07a31")
    engine = get_engine(path)
    with engine.connect() as conn:
        tables = {r[0] for r in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))}
        cols = {r[1] for r in conn.execute(text("PRAGMA table_info(chunks)"))}
        assert "ingest_queue" not in tables and "provisional" not in cols
        assert {r[0] for r in conn.execute(text("SELECT id FROM chunks"))} == {"chk_old"}
        assert {r[0] for r in conn.execute(text("SELECT chunk_id FROM fts_chunks"))} == {"chk_old"}
        assert {r[0] for r in conn.execute(text("SELECT chunk_id FROM fts_chunk_map"))} == {"chk_old"}
        assert conn.execute(text("SELECT status FROM evidence_versions WHERE id='ev_2'")).scalar_one() == "PENDING"
        assert conn.execute(text("SELECT status FROM evidence_versions WHERE id='ev_1'")).scalar_one() == "READY"
    engine.dispose()
    command.upgrade(cfg, "head")                      # and back up again


# -- CLI -----------------------------------------------------------------------------------------


def test_cli_tier1_only_then_deepen_and_status(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from docket.interfaces.cli import main as cli_main
    from docket.interfaces.cli.context import AppContext
    from pool_fakes import FakeParser

    data = tmp_path / "data"
    docs = tmp_path / "docs"
    docs.mkdir()
    for i in range(3):
        _write_docx(docs / f"d{i}.docx", f"T{i}", f"cli{i}marker " + BODY)
    context = AppContext.for_testing(data_dir=data, gateway=FakeInferenceGateway(), parser=FakeParser())
    monkeypatch.setattr(cli_main, "build_context", lambda: context)
    source = context.source_manager.register_source(docs)
    runner = CliRunner()

    r = runner.invoke(cli_main.app, ["ingest", source.id, "--tier1-only"])
    assert r.exit_code == 0, r.output
    assert "quick pass: 3 file(s)" in r.output and "full index: 0 file(s)" in r.output
    assert "3 keyword-only" in r.output and fts_search(context.engine, "cli1marker", 3)
    assert context.gateway.embed_calls == []

    r = runner.invoke(cli_main.app, ["status"])
    assert r.exit_code == 0 and "0 of 3 files fully indexed" in r.output and "full index: 3 left" in r.output

    r = runner.invoke(cli_main.app, ["ingest", source.id, "--deepen", "--prioritize", str(docs / "d2.docx")])
    assert r.exit_code == 0, r.output
    assert "full index: 3 file(s)" in r.output
    r = runner.invoke(cli_main.app, ["status"])
    assert "3 of 3 files fully indexed" in r.output and "keyword-only" not in r.output

    bad = runner.invoke(cli_main.app, ["ingest", source.id, "--deepen", "--tier1-only"])
    assert bad.exit_code == 1


# -- reindex / coverage statement ---------------------------------------------------------------------


def test_reindex_keeps_keyword_only_chunks_searchable_and_vectorless(migrated_sqlite_engine, tmp_path):
    from docket.infra.index.reindex import reindex
    from test_tiered import _chunk_ids, _env, _tier1, _tier2, _vector_ids

    engine = migrated_sqlite_engine
    sf, pipeline, parser, source, folder, _ = _env(engine, tmp_path)
    _write_docx(folder / "full.docx", "F", "fully indexed gizmo " * 40)
    _write_docx(folder / "quick.docx", "Q", "keyword only zebracorn " * 40)
    _tier2(pipeline, source, folder / "full.docx")
    _tier1(pipeline, source, folder / "quick.docx")
    reindex(
        engine=engine, db_path=tmp_path / "lancedb", gateway=pipeline._gateway,
        manifest_path=tmp_path / "manifest.json", embed_model="fake",
    )
    assert fts_search(engine, "zebracorn", 3) and fts_search(engine, "gizmo", 3)
    with sf() as s:
        prov = {c for (c,) in s.execute(select(Chunk.id).where(Chunk.provisional.is_(True)))}
    assert prov and not (_vector_ids(pipeline) & prov) and _vector_ids(pipeline)


def test_answer_coverage_statement_when_some_files_are_keyword_only(migrated_sqlite_engine, tmp_path):
    from types import SimpleNamespace

    from docket.services.ingestion.coverage import coverage_statement, library_coverage
    from docket.services.query.service import Citation, QueryResult, QueryService
    from test_tiered import _env, _tier1, _tier2

    engine = migrated_sqlite_engine
    sf, pipeline, parser, source, folder, _ = _env(engine, tmp_path)
    _write_docx(folder / "full.docx", "F", "fully indexed gizmo " * 40)
    _write_docx(folder / "quick.docx", "Q", "keyword only zebracorn " * 40)
    _tier2(pipeline, source, folder / "full.docx")
    assert library_coverage(engine) is None            # fully indexed: no statement
    _tier1(pipeline, source, folder / "quick.docx")
    cov = library_coverage(engine)
    assert (cov["indexed"], cov["total"], cov["keyword_only"]) == (1, 2, 1)
    assert coverage_statement(cov) == "1 of 2 files fully indexed; the other 1 are keyword-only so far."
    with sf() as s:
        prov_id = s.execute(select(Chunk.id).where(Chunk.provisional.is_(True))).scalars().first()
        full_id = s.execute(select(Chunk.id).where(Chunk.provisional.is_(False))).scalars().first()
    me = SimpleNamespace(_engine=engine)

    def result(chunk_id):
        return QueryResult(
            question="q", answer="a", citations=[Citation(citation_label="l", chunk_id=chunk_id, source_display_name="x")],
            abstained=False, validation_warnings=[], mode="fast",
        )

    on_prov = QueryService._tier_coverage(me, result(prov_id))
    assert on_prov["index_coverage"]["used_provisional"] is True
    assert on_prov["validation_warnings"] == ["1 of 2 files fully indexed; the other 1 are keyword-only so far."]
    on_full = QueryService._tier_coverage(me, result(full_id))
    assert on_full["index_coverage"]["used_provisional"] is False and "validation_warnings" not in on_full


# -- lifecycle interactions ------------------------------------------------------------------------------


def test_one_pass_ingest_finishes_keyword_only_files_and_revoked_sources_are_not_claimed(
    migrated_sqlite_engine, tmp_path
):
    from test_tiered import _chunk_ids, _env, _tier1, assert_consistent

    engine = migrated_sqlite_engine
    sf, pipeline, parser, source, folder, queue = _env(engine, tmp_path)
    _write_docx(folder / "a.docx", "A", BODY)
    (folder / "b.txt").write_text("wombat " * 100)
    for n in ("a.docx", "b.txt"):
        _tier1(pipeline, source, folder / n)
    queue.enqueue(source.id, [folder / "a.docx"], tiers=(2,))
    SourceManager(sf).deactivate_source(source.id)
    assert queue.claim(2) is None                    # revoked: background work does not start
    assert not fts_search(engine, "wombat", 3)       # and keyword-only chunks do not leak
    with sf() as s:
        from docket.core.db.models import Source

        s.get(Source, source.id).status = SourceStatus.ACTIVE
        s.commit()
    r = pipeline.run_ingestion_for_source(source.id)  # a plain one-pass run completes the upgrade
    assert r.status == "succeeded"
    with sf() as s:
        assert {v.status for v in s.execute(select(EvidenceVersion)).scalars()} == {VersionStatus.READY}
    assert_consistent(engine, sf, pipeline)


def test_claim_is_an_index_walk_not_a_sort_of_the_queue(migrated_sqlite_engine, tmp_path):
    engine = migrated_sqlite_engine
    sf, source, folder = _source(engine, tmp_path)
    rows = [
        dict(id=f"q{i}", s=source.id, fp=f"/lib/f{i}.pdf", tier=2, prio=2 if i % 7 else 1,
             mtime=1_000_000.0 - (i % 900) * DAY, day=int((1_000_000.0 - (i % 900) * DAY) // DAY),
             size=(i * 7919) % 100_000)
        for i in range(100_000)
    ]
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO ingest_queue (id, source_id, file_path, tier, state, prio_class, mtime, "
                "recency_day, byte_size, attempts, not_before, enqueued_at) VALUES "
                "(:id, :s, :fp, :tier, 'queued', :prio, :mtime, :day, :size, 0, 0, 0)"
            ),
            rows,
        )
        plan = " ".join(
            str(r) for r in conn.execute(
                text("EXPLAIN QUERY PLAN SELECT id FROM ingest_queue WHERE tier = 2 AND state = 'queued' "
                     "AND not_before <= 1000000 ORDER BY prio_class, recency_day DESC, byte_size, mtime DESC, id LIMIT 1")
            )
        )
    assert "ix_ingest_queue_claim" in plan and "TEMP B-TREE" not in plan
    q = _queue(engine)
    t0 = time.perf_counter()
    for _ in range(300):
        job = q.claim(2)
        q.complete(job)
    assert time.perf_counter() - t0 < 5
