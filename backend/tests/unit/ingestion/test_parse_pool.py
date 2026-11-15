"""Step 1b-C: process-based parse pool (timeouts, crashes, ordering, identical results)."""

from __future__ import annotations

import time

import pytest
from sqlalchemy import select

from docket.core.config import Settings
from docket.core.db.models import Chunk, EvidenceVersion
from docket.infra.parsing.docling_wrapper import ParseError
from docket.services.ingestion.parse_pool import ParsePool, default_parse_workers
from conftest import _write_docx
from pool_fakes import FakeParser, make_fast, make_slow
from test_page_images_optional import _pipeline, _setup


@pytest.fixture
def pool():
    p = ParsePool(make_fast, workers=2, timeout_s=3)
    yield p
    p.close()


def _docx(tmp_path, name, body):
    path = tmp_path / name
    _write_docx(path, "H", body)
    return path


def test_default_workers_bounds():
    assert 1 <= default_parse_workers() <= 4


def test_results_match_direct_parse(pool, tmp_path):
    paths = [_docx(tmp_path, f"f{i}.docx", f"body {i}") for i in range(6)]
    for p in paths:
        pool.submit("s", p)
    direct = FakeParser()
    for p in paths:
        got = pool.take("s", p)
        want = direct.parse("s", p)
        assert (got.text, got.source_path, got.parser_name) == (want.text, want.source_path, want.parser_name)
    assert pool.take("s", paths[0]) is None  # consumed
    assert pool.take("s", tmp_path / "never.docx") is None  # never submitted


def test_timeout_kills_worker_and_pool_recovers(tmp_path):
    p = ParsePool(make_fast, workers=1, timeout_s=2)
    try:
        hang, ok = _docx(tmp_path, "h.docx", "HANG"), _docx(tmp_path, "o.docx", "fine")
        t0 = time.time()
        p.submit("s", hang)
        p.submit("s", ok)
        with pytest.raises(ParseError, match="timed out"):
            p.take("s", hang)
        assert time.time() - t0 < 30
        assert p.take("s", ok).text.endswith("fine")  # fresh worker served the next file
    finally:
        p.close()


def test_worker_killed_mid_file_fails_only_that_file(pool, tmp_path):
    crash, ok1, ok2 = (_docx(tmp_path, n, b) for n, b in (("c.docx", "CRASH"), ("a.docx", "one"), ("b.docx", "two")))
    for f in (ok1, crash, ok2):
        pool.submit("s", f)
    assert pool.take("s", ok1).text.endswith("one")
    with pytest.raises(ParseError, match="died"):
        pool.take("s", crash)
    assert pool.take("s", ok2).text.endswith("two")


def test_parser_exception_is_parse_error(pool, tmp_path):
    bad = _docx(tmp_path, "r.docx", "RAISE")
    pool.submit("s", bad)
    with pytest.raises(ParseError, match="boom"):
        pool.take("s", bad)


def test_parallel_is_faster_than_sequential(tmp_path):
    paths = [_docx(tmp_path, f"p{i}.docx", f"x{i}") for i in range(8)]

    def run(workers):
        p = ParsePool(make_slow, workers=workers, timeout_s=30)
        try:
            p.submit("s", paths[0]); p.take("s", paths[0])  # warm worker startup
            t0 = time.time()
            for f in paths[1:]:
                p.submit("s", f)
            for f in paths[1:]:
                p.take("s", f)
            return time.time() - t0
        finally:
            p.close()

    assert run(4) < run(1) * 0.75


def _snapshot(sf):
    with sf() as s:
        vs = s.execute(select(EvidenceVersion.file_path, EvidenceVersion.status)).all()
        cs = s.execute(
            select(EvidenceVersion.file_path, Chunk.ordinal, Chunk.text).join(
                EvidenceVersion, Chunk.evidence_version_id == EvidenceVersion.id
            )
        ).all()
    return sorted((v[0].rsplit("/", 1)[1], str(v[1])) for v in vs), sorted(
        (c[0].rsplit("/", 1)[1], c[1], c[2]) for c in cs
    )


def test_pipeline_parallel_equals_sequential_and_survives_failures(tmp_path):
    from alembic import command
    from tests.unit.conftest import _alembic_config
    from docket.core.db.engine import get_engine

    outcomes = []
    for mode in ("seq", "pool"):
        work = tmp_path / mode
        work.mkdir()
        sqlite = work / "d.sqlite3"
        command.upgrade(_alembic_config(sqlite), "head")
        engine = get_engine(sqlite)
        sf, pipeline = _pipeline(engine, work, FakeParser(), Settings(parse_timeout_s=3))
        source = _setup(work, sf)  # writes docs/a.docx
        folder = work / "docs"
        for i in range(5):
            _write_docx(folder / f"d{i}.docx", "H", f"alpha beta gamma {i} " * 20)
        _write_docx(folder / "z_raise.docx", "H", "RAISE")
        _write_docx(folder / "z_crash.docx", "H", "CRASH")
        _write_docx(folder / "z_hang.docx", "H", "HANG")
        if mode == "pool":
            pipeline._parse_pool = ParsePool(make_fast, workers=3, timeout_s=3)
        try:
            if mode == "seq":
                # the sequential path would exit/hang on the crash/hang fakes: drop them
                (folder / "z_crash.docx").unlink()
                (folder / "z_hang.docx").unlink()
            result = pipeline.run_ingestion_for_source(source.id)
        finally:
            if pipeline._parse_pool is not None:
                pipeline._parse_pool.close()
        statuses = {r.path.name: r.status for r in result.file_results}
        outcomes.append((statuses, _snapshot(sf)))
        engine.dispose()
    seq, pooled = outcomes
    assert pooled[0]["z_raise.docx"] == "failed" and pooled[0]["z_crash.docx"] == "failed"
    assert pooled[0]["z_hang.docx"] == "failed"
    common = {k: v for k, v in pooled[0].items() if k in seq[0]}
    assert common == seq[0]
    # identical chunks for every file both modes could ingest
    seq_chunks = [c for c in seq[1][1]]
    pool_chunks = [c for c in pooled[1][1] if not c[0].startswith("z_")]
    assert seq_chunks == pool_chunks


# -- idle shutdown / lifecycle ------------------------------------------------

import multiprocessing  # noqa: E402


def _children():
    return [c for c in multiprocessing.active_children() if c.is_alive()]


def _wait(pred, limit=15.0):
    end = time.time() + limit
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.1)
    return pred()


def test_idle_shutdown_then_lazy_restart(tmp_path):
    p = ParsePool(make_fast, workers=2, timeout_s=5, idle_s=0.5)
    try:
        a, b = _docx(tmp_path, "a.docx", "one"), _docx(tmp_path, "b.docx", "two")
        p.submit("s", a)
        assert p.take("s", a).text.endswith("one")
        assert _children()
        assert _wait(lambda: not _children())  # released after idle
        p.submit("s", b)
        assert p.take("s", b).text.endswith("two")  # restarted lazily
    finally:
        p.close()
    assert not _children()


def test_idle_zero_means_never(tmp_path):
    p = ParsePool(make_fast, workers=1, timeout_s=5, idle_s=0)
    try:
        a = _docx(tmp_path, "a.docx", "one")
        p.submit("s", a)
        p.take("s", a)
        time.sleep(1.5)
        assert _children()
    finally:
        p.close()


def test_shutdown_workers_end_of_run_and_reuse(tmp_path):
    p = ParsePool(make_fast, workers=2, timeout_s=5, idle_s=0)
    try:
        a, b = _docx(tmp_path, "a.docx", "one"), _docx(tmp_path, "b.docx", "two")
        p.submit("s", a)
        p.take("s", a)
        p.shutdown_workers()
        assert not _children()
        p.submit("s", b)
        assert p.take("s", b).text.endswith("two")
    finally:
        p.close()
    assert not _children()


def test_close_idempotent(tmp_path):
    p = ParsePool(make_fast, workers=1, timeout_s=5)
    a = _docx(tmp_path, "a.docx", "one")
    p.submit("s", a)
    p.take("s", a)
    p.close()
    p.close()
    assert not _children()


def test_close_while_worker_busy(tmp_path):
    p = ParsePool(make_fast, workers=1, timeout_s=60, idle_s=0, grace_s=1)
    hang = _docx(tmp_path, "h.docx", "HANG")
    p.submit("s", hang)
    assert _wait(lambda: bool(_children()))
    time.sleep(1.0)  # let the worker pick up the file
    t0 = time.time()
    p.close()
    assert time.time() - t0 < 20
    assert not _children()
