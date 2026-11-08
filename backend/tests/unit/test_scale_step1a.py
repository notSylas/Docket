"""Step 1a: scalable eligibility, ANN index, compaction (see
`docket.infra.retrieval.hybrid` block comment and `docket.infra.index.tuning`)."""

from __future__ import annotations

import json
import time
from pathlib import Path

import lancedb
import numpy as np
import pytest
from sqlalchemy import Engine, text

from docket.core.db.models import SourceStatus, VersionStatus
from docket.infra.index.base import ChunkRecord
from docket.infra.index.manifest import (
    IndexManifestGuard,
    check_compatible,
    new_manifest,
    read_manifest,
    record_ann_index,
    write_manifest,
)
from docket.infra.index.vector_index import (
    LanceIndexWriter,
    ann_num_partitions,
    maintain_table,
    pq_sub_vectors,
    vector_index_stats,
)
from docket.infra.inference.gateway import FakeInferenceGateway
from docket.infra.retrieval import hybrid
from docket.infra.retrieval.hybrid import fts_search, vector_search
from test_hybrid_retrieval import _insert_metadata

DIM = 16


def _rec(chunk_id: str, version_id: str, body: str, source_id: str = "src_1") -> ChunkRecord:
    return ChunkRecord(
        chunk_id=chunk_id, source_id=source_id, evidence_version_id=version_id,
        evidence_unit_id=f"eu_{version_id}", chunk_recipe_id="rcp_1", ordinal=0, heading=None,
        text=body, content_hash="hash_" + chunk_id,
    )


class _SpyTable:
    """Wraps a LanceDB table and records every `.where()` predicate."""

    def __init__(self, table, wheres):
        self._t, self._wheres = table, wheres

    def search(self, *a, **k):
        spy = self

        class Q:
            def __init__(self, q):
                self.q = q

            def where(self, w, **kw):
                spy._wheres.append(w)
                return Q(self.q.where(w, **kw))

            def __getattr__(self, name):
                attr = getattr(self.q, name)
                if callable(attr):
                    def call(*aa, **kk):
                        r = attr(*aa, **kk)
                        return Q(r) if hasattr(r, "limit") else r
                    return call
                return attr

        return Q(self._t.search(*a, **k))


def _bulk_clone_versions(engine: Engine, template_id: str, n: int, prefix: str) -> None:
    with engine.begin() as conn:
        cols = [r[1] for r in conn.execute(text("PRAGMA table_info(evidence_versions)"))]
        row = dict(conn.execute(text("SELECT * FROM evidence_versions WHERE id=:i"), {"i": template_id}).mappings().one())
        stmt = text(
            f"INSERT INTO evidence_versions ({','.join(cols)}) VALUES ({','.join(':' + c for c in cols)})"
        )
        batch = []
        for i in range(n):
            r = dict(row)
            r["id"] = f"{prefix}{i}"
            if "content_hash" in r:
                r["content_hash"] = f"h_{prefix}{i}"
            if "file_path" in r and r["file_path"] is not None:
                r["file_path"] = f"/tmp/{prefix}{i}"
            batch.append(r)
            if len(batch) == 5000:
                conn.execute(stmt, batch)
                batch = []
        if batch:
            conn.execute(stmt, batch)


@pytest.fixture
def gateway():
    return FakeInferenceGateway()


# -- eligibility ---------------------------------------------------------------


def test_100k_versions_no_giant_in_list_and_no_leaks(migrated_sqlite_engine, tmp_path, gateway, monkeypatch):
    monkeypatch.setenv("DOCKET_ELIGIBILITY_MODE", "scalable")
    query = "needle query"
    good = [_rec(f"chk_ok{i}", f"ev_ok{i}", f"other {i}") for i in range(5)]
    _insert_metadata(migrated_sqlite_engine, good)
    # Ineligible rows sit exactly on the query vector (they would win unfiltered).
    bad = {
        "sup": (VersionStatus.SUPERSEDED, SourceStatus.ACTIVE),
        "fail": (VersionStatus.FAILED, SourceStatus.ACTIVE),
        "pend": (VersionStatus.PENDING, SourceStatus.ACTIVE),
        "rev": (VersionStatus.READY, SourceStatus.REVOKED),
        "tomb": (VersionStatus.READY, SourceStatus.TOMBSTONED),
    }
    bad_records = []
    for name, (vs, ss) in bad.items():
        recs = [_rec(f"chk_{name}{i}", f"ev_{name}", query, source_id=f"src_{name}") for i in range(3)]
        _insert_metadata(migrated_sqlite_engine, recs, status=ss, version_status=vs)
        bad_records += recs
    _bulk_clone_versions(migrated_sqlite_engine, "ev_ok0", 100_000, "ev_fake")
    with migrated_sqlite_engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM evidence_versions")).scalar_one() > 100_000

    writer = LanceIndexWriter(tmp_path / "lancedb")
    writer.upsert(bad_records, [gateway.embed(r.text) for r in bad_records])
    writer.upsert(good, [gateway.embed(r.text) for r in good])

    wheres: list[str] = []
    spy = _SpyTable(writer.table, wheres)
    t0 = time.perf_counter()
    result = vector_search(spy, migrated_sqlite_engine, gateway, query, top_k=4)
    elapsed = time.perf_counter() - t0
    assert wheres and max(len(w) for w in wheres) < 2000
    assert all(r.startswith("chk_ok") for r in result) and len(result) == 4
    assert elapsed < 5

    monkeypatch.setenv("DOCKET_ELIGIBILITY_MODE", "legacy")
    legacy = vector_search(writer.table, migrated_sqlite_engine, gateway, query, top_k=4)
    assert legacy == result


def test_widening_when_ineligible_rows_dominate(migrated_sqlite_engine, tmp_path, gateway, monkeypatch):
    monkeypatch.setenv("DOCKET_ELIGIBILITY_MODE", "scalable")
    query = "q"
    stale = [_rec(f"chk_s{i}", "ev_old", query) for i in range(60)]
    live = [_rec(f"chk_l{i}", "ev_new", f"far {i}") for i in range(5)]
    _insert_metadata(migrated_sqlite_engine, stale, version_status=VersionStatus.SUPERSEDED)
    _insert_metadata(migrated_sqlite_engine, live)
    writer = LanceIndexWriter(tmp_path / "lancedb")
    writer.upsert(stale, [gateway.embed(r.text) for r in stale])
    writer.upsert(live, [gateway.embed(r.text) for r in live])
    res = vector_search(writer.table, migrated_sqlite_engine, gateway, query, top_k=3)
    assert len(res) == 3 and all(r.startswith("chk_l") for r in res)


def test_equivalence_with_legacy_incl_scope(migrated_sqlite_engine, tmp_path, gateway, monkeypatch):
    rng = np.random.default_rng(1)
    recs = []
    for i in range(300):
        recs.append(_rec(f"chk_{i}", f"ev_{i % 40}", f"word{rng.integers(1000)} text {i}", source_id=f"src_{i % 3}"))
    # versions of one source are revoked / one version superseded
    for sid, status in (("src_0", SourceStatus.ACTIVE), ("src_1", SourceStatus.ACTIVE), ("src_2", SourceStatus.REVOKED)):
        _insert_metadata(migrated_sqlite_engine, [r for r in recs if r.source_id == sid], status=status)
    with migrated_sqlite_engine.begin() as conn:
        conn.execute(text("UPDATE evidence_versions SET status='SUPERSEDED' WHERE id IN ('ev_3','ev_7')"))
    writer = LanceIndexWriter(tmp_path / "lancedb")
    writer.upsert(recs, [gateway.embed(r.text) for r in recs])
    scopes = [None, ["ev_1", "ev_4", "ev_3", "ev_2"], [], [f"ev_{i}" for i in range(40)]]
    for q in ("word5 text", "alpha", "text 77"):
        for scope in scopes:
            monkeypatch.setenv("DOCKET_ELIGIBILITY_MODE", "legacy")
            a = vector_search(writer.table, migrated_sqlite_engine, gateway, q, 10, scope_version_ids=scope)
            monkeypatch.setenv("DOCKET_ELIGIBILITY_MODE", "scalable")
            b = vector_search(writer.table, migrated_sqlite_engine, gateway, q, 10, scope_version_ids=scope)
            assert a == b, (q, scope)
            if scope:
                allowed = set(scope)
                with migrated_sqlite_engine.connect() as conn:
                    vs = {r[0]: r[1] for r in conn.execute(text("SELECT id, evidence_version_id FROM chunks"))}
                assert all(vs[c] in allowed for c in b)


def test_large_scope_uses_postfilter_and_matches_legacy(migrated_sqlite_engine, tmp_path, gateway, monkeypatch):
    monkeypatch.setattr(hybrid, "_SCOPE_PREFILTER_MAX", 3)
    recs = [_rec(f"chk_{i}", f"ev_{i}", f"t{i}") for i in range(30)]
    _insert_metadata(migrated_sqlite_engine, recs)
    writer = LanceIndexWriter(tmp_path / "lancedb")
    writer.upsert(recs, [gateway.embed(r.text) for r in recs])
    scope = [f"ev_{i}" for i in range(0, 30, 2)]
    monkeypatch.setenv("DOCKET_ELIGIBILITY_MODE", "legacy")
    a = vector_search(writer.table, migrated_sqlite_engine, gateway, "t5", 6, scope_version_ids=scope)
    monkeypatch.setenv("DOCKET_ELIGIBILITY_MODE", "scalable")
    b = vector_search(writer.table, migrated_sqlite_engine, gateway, "t5", 6, scope_version_ids=scope)
    assert a == b and len(b) == 6


def test_fts_large_scope_path_matches_scoped_sql(migrated_sqlite_engine, monkeypatch):
    from docket.infra.index.fts_index import FtsIndexWriter

    recs = [_rec(f"chk_{i}", f"ev_{i}", f"zebra item{i}") for i in range(20)]
    _insert_metadata(migrated_sqlite_engine, recs)
    FtsIndexWriter(migrated_sqlite_engine).upsert(recs, None)
    scope = [f"ev_{i}" for i in range(0, 20, 2)] + ["ev_missing"]
    expected = fts_search(migrated_sqlite_engine, "zebra", 5, scope_version_ids=scope)
    monkeypatch.setattr(hybrid, "_FTS_SCOPE_IN_MAX", 2)
    big = fts_search(migrated_sqlite_engine, "zebra", 5, scope_version_ids=scope)
    assert big == expected and len(big) == 5


def test_all_sources_inactive_returns_empty(migrated_sqlite_engine, tmp_path, gateway):
    recs = [_rec("chk_a", "ev_1", "alpha")]
    _insert_metadata(migrated_sqlite_engine, recs, status=SourceStatus.TOMBSTONED)
    writer = LanceIndexWriter(tmp_path / "lancedb")
    writer.upsert(recs, [gateway.embed("alpha")])
    assert vector_search(writer.table, migrated_sqlite_engine, gateway, "alpha", 3) == []


def test_mode_env_default_and_invalid(monkeypatch):
    from docket.infra.index import tuning

    monkeypatch.delenv("DOCKET_ELIGIBILITY_MODE", raising=False)
    assert tuning.eligibility_mode() == "scalable"
    monkeypatch.setenv("DOCKET_ELIGIBILITY_MODE", "legacy")
    assert tuning.eligibility_mode() == "legacy"
    monkeypatch.setenv("DOCKET_ELIGIBILITY_MODE", "bogus")
    assert tuning.eligibility_mode() == "scalable"


# -- ANN index -----------------------------------------------------------------


def _synthetic(n: int, dim: int, seed: int = 0, clusters: int = 200) -> np.ndarray:
    rng = np.random.default_rng(seed)
    centers = rng.standard_normal((clusters, dim)).astype("float32")
    assign = rng.integers(clusters, size=n)
    return (centers[assign] + 0.5 * rng.standard_normal((n, dim))).astype("float32")


def _make_table(path: Path, vecs: np.ndarray, start: int = 0, table=None):
    rows = [
        {"chunk_id": f"c{start + i}", "source_id": "s", "text": "", "evidence_version_id": f"v{(start + i) % 97}", "vector": vecs[i].tolist()}
        for i in range(len(vecs))
    ]
    if table is None:
        return lancedb.connect(str(path)).create_table("chunks", data=rows)
    table.add(rows)
    return table


def test_pq_params():
    assert pq_sub_vectors(1024) == 128
    assert pq_sub_vectors(384) == 48
    d = 100
    assert d % pq_sub_vectors(d) == 0
    assert ann_num_partitions(1_000_000) == 1000
    assert ann_num_partitions(4) == 2


def test_below_threshold_stays_flat_and_above_builds_and_records_manifest(tmp_path):
    mpath = tmp_path / "index_manifest.json"
    write_manifest(mpath, new_manifest("m", DIM))
    table = _make_table(tmp_path / "lancedb", _synthetic(600, DIM))
    w = LanceIndexWriter(tmp_path / "lancedb")
    r = w.maintain(manifest_path=mpath, min_rows=1000, compact=False, keep_hours=1)
    assert r["action"] == "none" and vector_index_stats(w.table) is None
    assert read_manifest(mpath).ann_index is None
    _make_table(tmp_path / "lancedb", _synthetic(600, DIM, seed=2), start=600, table=w.table)
    r = w.maintain(manifest_path=mpath, min_rows=1000, compact=False, keep_hours=1)
    assert r["action"] == "built"
    stats = vector_index_stats(w.table)
    assert stats["index_type"] == "IVF_PQ" and stats["unindexed_rows"] == 0
    ann = read_manifest(mpath).ann_index
    assert ann["num_sub_vectors"] == DIM // 8 and ann["num_partitions"] == ann_num_partitions(1200)
    # an index is not a model change
    check_compatible(read_manifest(mpath), "m", DIM)


def test_legacy_manifest_without_ann_field_reads(tmp_path):
    mpath = tmp_path / "m.json"
    write_manifest(mpath, new_manifest("m", 8))
    data = json.loads(mpath.read_text())
    del data["ann_index"]
    mpath.write_text(json.dumps(data))
    assert read_manifest(mpath).ann_index is None
    record_ann_index(mpath, {"index_type": "IVF_PQ"})
    assert read_manifest(mpath).ann_index == {"index_type": "IVF_PQ"}
    record_ann_index(tmp_path / "absent.json", {"x": 1})  # no manifest: no-op


def test_unindexed_tail_is_searchable_and_optimize_folds_it(tmp_path):
    vecs = _synthetic(3000, DIM)
    table = _make_table(tmp_path / "lancedb", vecs)
    maintain_table(table, min_rows=1000, compact=False, keep_hours=1)
    tail = np.full((1, DIM), 50.0, dtype="float32")
    _make_table(tmp_path / "lancedb", tail, start=9000, table=table)
    assert vector_index_stats(table)["unindexed_rows"] == 1
    hit = table.search(tail[0].tolist()).nprobes(10).refine_factor(5).limit(1).to_list()
    assert hit[0]["chunk_id"] == "c9000"
    r = maintain_table(table, min_rows=1000, compact=True, keep_hours=1,
                       previous_params={"rows_at_build": 3000})
    assert r["action"] == "optimized" and vector_index_stats(table)["unindexed_rows"] == 0
    hit = table.search(tail[0].tolist()).limit(1).to_list()
    assert hit[0]["chunk_id"] == "c9000"


def test_rebuild_when_table_doubles(tmp_path):
    table = _make_table(tmp_path / "lancedb", _synthetic(1500, DIM))
    r = maintain_table(table, min_rows=1000, compact=False, keep_hours=1)
    _make_table(tmp_path / "lancedb", _synthetic(2000, DIM, seed=3), start=1500, table=table)
    r2 = maintain_table(table, min_rows=1000, compact=False, keep_hours=1, previous_params=r["ann"])
    assert r2["action"] == "rebuilt" and r2["ann"]["rows_at_build"] == 3500


def test_recall_at_30_vs_flat_1024d(tmp_path):
    """IVF_PQ with the shipped defaults vs exact flat search, 1024-d synthetic
    (clustered, so ANN is meaningful), CPU only."""
    from docket.infra.index import tuning

    n, dim, k = 60_000, 1024, 30
    vecs = _synthetic(n, dim, clusters=300)
    table = _make_table(tmp_path / "lancedb", vecs)
    queries = vecs[np.random.default_rng(9).choice(n, 12, replace=False)] + 0.3 * np.random.default_rng(10).standard_normal((12, dim)).astype("float32")
    flat = [[r["chunk_id"] for r in table.search(q.tolist()).limit(k).to_list()] for q in queries]
    maintain_table(table, min_rows=1000, compact=False, keep_hours=1)
    assert vector_index_stats(table)["index_type"] == "IVF_PQ"
    hits = 0
    for q, f in zip(queries, flat):
        got = [r["chunk_id"] for r in table.search(q.tolist()).nprobes(tuning.ann_nprobes()).refine_factor(tuning.ann_refine_factor()).limit(k).to_list()]
        hits += len(set(got) & set(f))
    assert hits / (k * len(queries)) >= 0.95


def test_ann_search_through_eligibility_filters_ineligible(migrated_sqlite_engine, tmp_path, gateway):
    stale = [_rec(f"chk_s{i}", "ev_old", "q") for i in range(5)]
    live = [_rec(f"chk_l{i}", "ev_new", f"x{i}") for i in range(2000)]
    _insert_metadata(migrated_sqlite_engine, stale, version_status=VersionStatus.SUPERSEDED)
    _insert_metadata(migrated_sqlite_engine, live)
    w = LanceIndexWriter(tmp_path / "lancedb")
    w.upsert(stale + live, [gateway.embed(r.text) for r in stale + live])
    w.maintain(min_rows=1000, compact=False, keep_hours=1)
    assert vector_index_stats(w.table) is not None
    res = vector_search(w.table, migrated_sqlite_engine, gateway, "q", 5)
    assert len(res) == 5 and not any(r.startswith("chk_s") for r in res)


# -- compaction ----------------------------------------------------------------


def test_compaction_merges_fragments_and_cleans_old_versions(tmp_path):
    table = _make_table(tmp_path / "lancedb", _synthetic(100, DIM))
    for i in range(8):
        _make_table(tmp_path / "lancedb", _synthetic(50, DIM, seed=i + 5), start=1000 * (i + 1), table=table)
    frags_before = _frags(table)
    versions_before = len(table.list_versions())
    maintain_table(table, min_rows=10**9, compact=True, keep_hours=0)
    assert _frags(table) < frags_before
    assert len(table.list_versions()) < versions_before
    assert table.count_rows() == 100 + 8 * 50


def test_manager_maintain_never_raises_and_disabled_flag(tmp_path, monkeypatch):
    from docket.infra.index.manager import IndexManager

    class Boom:
        def maintain(self, **kw):
            raise RuntimeError("x")

    mgr = IndexManager(fts_writer=None, vector_writer=Boom(), gateway=None)
    assert mgr.maintain() is None
    monkeypatch.setenv("DOCKET_COMPACT_AFTER_INGEST", "false")
    from docket.infra.index import tuning

    assert tuning.compact_after_ingest() is False


def test_doctor_vector_index_line(tmp_path):
    from types import SimpleNamespace

    from docket.services.doctor import ann_index_check

    s = SimpleNamespace(lancedb_path=tmp_path / "lancedb")
    assert ann_index_check(s).status == "OK"
    _make_table(s.lancedb_path, _synthetic(500, DIM))
    assert "exact scan" in ann_index_check(s).detail
    maintain_table(lancedb.connect(str(s.lancedb_path)).open_table("chunks"), min_rows=100, compact=False, keep_hours=1)
    assert "IVF_PQ" in ann_index_check(s).detail


def _frags(table) -> int:
    s = table.stats()
    fs = s["fragment_stats"] if isinstance(s, dict) else s.fragment_stats
    return fs["num_fragments"] if isinstance(fs, dict) else fs.num_fragments
