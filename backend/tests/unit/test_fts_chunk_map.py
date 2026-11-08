"""Step 1b: rowid-mapped FTS writes (`fts_chunk_map`), migration 0015, lance append path."""

from __future__ import annotations

import random

from alembic import command
from sqlalchemy import text

from docket.core.db.engine import get_engine
from docket.infra.index.base import ChunkRecord
from docket.infra.index.fts_index import FtsIndexWriter, ensure_fts_chunk_map
from tests.unit.conftest import _alembic_config


def _rec(cid, body, ver="ev_1", src="src_1"):
    return ChunkRecord(
        chunk_id=cid, source_id=src, evidence_version_id=ver, evidence_unit_id="eu",
        chunk_recipe_id="r", ordinal=0, heading=None, text=body, content_hash="h" + cid,
    )


def _corpus(n=60, seed=3):
    rng = random.Random(seed)
    words = ["alpha", "beta", "gamma", "delta", "journey", "journeys", "invoice", "lease", "term"]
    return [
        _rec(f"c{i:03d}", " ".join(rng.choice(words) for _ in range(12)), ver=f"ev_{i // 6}", src=f"s{i // 30}")
        for i in range(n)
    ]


def _legacy_write(engine, records):
    """The pre-0015 writer's behaviour: delete by chunk_id scan, then insert."""
    with engine.begin() as conn:
        for r in records:
            conn.execute(text("DELETE FROM fts_chunks WHERE chunk_id = :c"), {"c": r.chunk_id})
            conn.execute(
                text("INSERT INTO fts_chunks (chunk_id, text, evidence_version_id, source_id) VALUES (:c,:t,:v,:s)"),
                {"c": r.chunk_id, "t": r.text, "v": r.evidence_version_id, "s": r.source_id},
            )


def _ranked(engine, q):
    with engine.connect() as c:
        return [
            r[0]
            for r in c.execute(
                text("SELECT chunk_id FROM fts_chunks WHERE fts_chunks MATCH :q ORDER BY bm25(fts_chunks), chunk_id"),
                {"q": q},
            )
        ]


def test_identical_match_sets_and_topk_vs_legacy(tmp_path):
    new_engine = get_engine(tmp_path / "new.sqlite3")
    old_engine = get_engine(tmp_path / "old.sqlite3")
    for p, eng in (("new", new_engine), ("old", old_engine)):
        command.upgrade(_alembic_config(tmp_path / f"{p}.sqlite3"), "head")
    recs = _corpus()
    FtsIndexWriter(new_engine).upsert(recs)
    FtsIndexWriter(new_engine).upsert(recs[:10])  # re-ingest of the same ids
    _legacy_write(old_engine, recs)
    _legacy_write(old_engine, recs[:10])
    for q in ("journey", "alpha beta", "lease AND term", "invoice OR gamma", "delta"):
        assert _ranked(new_engine, q) == _ranked(old_engine, q), q
    with new_engine.connect() as c:
        assert c.execute(text("SELECT COUNT(*) FROM fts_chunks")).scalar() == len(recs)
        assert c.execute(text("SELECT COUNT(*) FROM fts_chunk_map")).scalar() == len(recs)


def test_delete_paths_keep_map_in_sync(migrated_sqlite_engine):
    w = FtsIndexWriter(migrated_sqlite_engine)
    recs = _corpus()
    w.upsert(recs)
    w.delete(["c000", "c001", "nope"])
    assert w.existing_chunk_ids(["c000", "c001", "c002"]) == {"c002"}
    w.delete_by_version("ev_1")
    w.delete_by_source("s1")
    remaining = {r.chunk_id for r in recs if r.evidence_version_id != "ev_1" and r.source_id != "s1"} - {"c000", "c001"}
    assert w.chunk_ids_for_source("s0") == {i for i in remaining if i < "c030"}
    with migrated_sqlite_engine.connect() as c:
        fts = {r[0] for r in c.execute(text("SELECT chunk_id FROM fts_chunks"))}
        mp = {r[0] for r in c.execute(text("SELECT chunk_id FROM fts_chunk_map"))}
    assert fts == mp == remaining


def test_lazy_map_creation_and_backfill(tmp_path):
    eng = get_engine(tmp_path / "x.sqlite3")
    command.upgrade(_alembic_config(tmp_path / "x.sqlite3"), "head")
    _legacy_write(eng, _corpus(20))
    with eng.begin() as c:
        c.execute(text("DROP TABLE fts_chunk_map"))
    ensure_fts_chunk_map(eng)
    w = FtsIndexWriter(eng)
    assert len(w.existing_chunk_ids([f"c{i:03d}" for i in range(20)])) == 20


def test_migration_0015_rebuilds_map_for_existing_library_and_roundtrips(tmp_path):
    path = tmp_path / "m.sqlite3"
    cfg = _alembic_config(path)
    command.upgrade(cfg, "d1a7c3e58b14")
    eng = get_engine(path)
    recs = _corpus(25)
    _legacy_write(eng, recs)
    # a duplicate row for one chunk (never written by the app, but must not break the backfill)
    _legacy_write(eng, [])
    with eng.begin() as c:
        c.execute(text("INSERT INTO fts_chunks (chunk_id, text, evidence_version_id, source_id) VALUES ('c000','dup','ev_0','s0')"))
    before = _ranked(eng, "alpha OR beta OR journey")
    eng.dispose()
    command.upgrade(cfg, "head")
    eng = get_engine(path)
    w = FtsIndexWriter(eng)
    assert len(w.existing_chunk_ids([r.chunk_id for r in recs])) == 25
    assert set(_ranked(eng, "alpha OR beta OR journey")) <= set(before)
    w.delete_by_version("ev_0")
    assert w.existing_chunk_ids(["c000", "c005"]) == set()
    eng.dispose()
    command.downgrade(cfg, "d1a7c3e58b14")
    eng = get_engine(path)
    with eng.connect() as c:
        assert c.execute(text("SELECT name FROM sqlite_master WHERE name='fts_chunk_map'")).first() is None
    eng.dispose()


def test_lance_append_and_reingest_paths(tmp_path):
    from docket.infra.index.vector_index import LanceIndexWriter

    lw = LanceIndexWriter(tmp_path / "lance")
    recs = [_rec(f"c{i}", f"t{i}") for i in range(10)]
    emb = [[float(i), 0.0, 1.0, 0.5] for i in range(10)]
    lw.upsert(recs, emb)
    lw.upsert(recs[:3] + [_rec("new", "n")], emb[:3] + [[9.0, 9.0, 9.0, 9.0]])  # 3 re-ingests + 1 new
    t = lw.table
    assert t.count_rows() == 11
    assert lw.existing_chunk_ids(["c0", "new", "zzz"]) == {"c0", "new"}
    assert any(list(c.columns) == ["chunk_id"] for c in t.list_indices())


def test_note_file_done_runs_maintain_every_n(monkeypatch):
    from docket.infra.index.manager import IndexManager

    monkeypatch.setenv("DOCKET_COMPACT_EVERY_FILES", "3")
    m = IndexManager(None, None, None)
    calls = []
    m.maintain = lambda: calls.append(1)
    for _ in range(7):
        m.note_file_done()
    assert len(calls) == 2
    monkeypatch.setenv("DOCKET_COMPACT_EVERY_FILES", "0")
    for _ in range(10):
        m.note_file_done()
    assert len(calls) == 2
