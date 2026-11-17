"""M0 lifecycle eligibility: stale KAG graph rows must never keep superseded,
revoked, tombstoned or deleted evidence eligible.

CPU-only: scratch SQLite (migrated engine fixture) plus the query service with
a faked `hybrid_search`. These tests DOCUMENT current behaviour; they do not
change it. A strict xfail marks a real finding (see the reason string).
"""
from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text

from docket.core.config import Settings
from docket.core.db.engine import get_session_factory
from docket.core.db.models import SourceStatus, VersionStatus
from docket.infra.evidence.store import ContentAddressedStore
from docket.services.documents import builder, kag
from docket.services.query.classifier import QueryMode
from docket.services.sources.manager import SourceManager
from docket.services.sources.prune import supersede_and_deindex
from docket.services.sources.purge import SourcePurgeService
from test_document_layer import BLM, BLM2, ZOG, ZOG2, _name, _rec, _service
from test_hybrid_retrieval import _insert_metadata

ZOG_Q = "What is the effective date of the agreement with Zogenix, Inc.?"
ZOG_CLAUSE_Q = "Which law governs the Distributor Agreement with Zogenix, Inc.?"


def _scalar(engine, sql, **params):
    with engine.connect() as c:
        return c.execute(text(sql), params).scalar_one()


@pytest.fixture
def lib(migrated_sqlite_engine):
    e = migrated_sqlite_engine
    _insert_metadata(e, [_rec("c_z0", "ev_z", ZOG, 0, "src_z"), _rec("c_z1", "ev_z", ZOG2, 1, "src_z")])
    _insert_metadata(e, [_rec("c_b0", "ev_b", BLM, 0, "src_b"), _rec("c_b1", "ev_b", BLM2, 1, "src_b")])
    _name(e, "ev_z", "/lib/Zogenix_Distributor Agreement.pdf", 100.0)
    _name(e, "ev_b", "/lib/Bloom_Maintenance Agreement.pdf", 100.0)
    sf = get_session_factory(e)
    builder.build_all(sf, kag=True)
    return e, sf


def _assert_ineligible(e, question=ZOG_Q):
    p = kag.plan(e, question)
    assert not p.active
    assert p.version_ids == [] and p.document_ids == [] and p.clause_chunk_ids == {}


def test_baseline_graph_makes_zogenix_eligible(lib):
    e, _ = lib
    p = kag.plan(e, ZOG_CLAUSE_Q)
    assert p.active and p.version_ids == ["ev_z"] and "c_z1" in p.clause_chunk_ids


@pytest.mark.parametrize("status", [
    SourceStatus.REVOKED, SourceStatus.MISSING, SourceStatus.TOMBSTONED,
    SourceStatus.HARD_DELETE_PENDING, SourceStatus.DELETED,
])
def test_non_active_source_is_ineligible_despite_graph_rows(lib, status):
    e, _ = lib
    with e.begin() as c:
        c.execute(text("UPDATE sources SET status=:s WHERE id='src_z'"), {"s": status.name})
    assert _scalar(e, "SELECT COUNT(*) FROM kag_document_entities") > 0  # stale edges are still there
    _assert_ineligible(e)
    _assert_ineligible(e, ZOG_CLAUSE_Q)


@pytest.mark.parametrize("status", [VersionStatus.SUPERSEDED, VersionStatus.FAILED, VersionStatus.PENDING, VersionStatus.TEXT_READY])
def test_non_ready_version_is_ineligible_despite_graph_rows(lib, status):
    e, _ = lib
    with e.begin() as c:
        c.execute(text("UPDATE evidence_versions SET status=:s WHERE id='ev_z'"), {"s": status.name})
    _assert_ineligible(e)


def test_real_source_manager_transitions_hide_the_document(lib):
    e, sf = lib
    mgr = SourceManager(sf)
    mgr.deactivate_source("src_z", reason="user_disconnected")
    _assert_ineligible(e)
    with e.begin() as c:  # reconnect: ACTIVE again -> eligible again (the graph was never lost)
        c.execute(text("UPDATE sources SET status='ACTIVE' WHERE id='src_z'"))
    assert kag.plan(e, ZOG_CLAUSE_Q).active
    mgr.deactivate_source("src_z")
    mgr.request_hard_delete("src_z")
    _assert_ineligible(e)


def test_tombstone_flow_hides_the_document(lib):
    e, sf = lib
    mgr = SourceManager(sf)
    mgr.mark_unreachable("src_z")
    mgr.mark_tombstoned("src_z", reason="confirmed deletion", retention_days=1)
    _assert_ineligible(e)


def test_revoked_duplicate_does_not_join_the_active_document(lib):
    """Same entity in two documents; one revoked: only the active one is planned,
    and mode stays 'unique' (the revoked copy must not turn it into 'multi')."""
    e, sf = lib
    _insert_metadata(e, [_rec("c_y0", "ev_y", ZOG.replace("Sumitomo Dainippon Pharma", "Other Pharma Ltd") + " Different.", 0, "src_y"),
                         _rec("c_y1", "ev_y", ZOG2 + " Extra.", 1, "src_y")])
    _name(e, "ev_y", "/lib/Zogenix_Second.pdf", 100.0)
    builder.build_all(sf, kag=True)
    assert kag.plan(e, ZOG_Q).mode == "multi"
    with e.begin() as c:
        c.execute(text("UPDATE sources SET status='REVOKED' WHERE id='src_y'"))
    p = kag.plan(e, ZOG_CLAUSE_Q)
    assert p.active and p.mode == "unique" and p.version_ids == ["ev_z"]
    assert not any(cid.startswith("c_y") for cid in p.clause_chunk_ids)


def test_supersede_status_only_leaves_graph_but_not_eligibility(lib):
    e, _ = lib
    with e.begin() as c:
        c.execute(text("UPDATE evidence_versions SET status='SUPERSEDED' WHERE id='ev_z'"))
    assert _scalar(e, "SELECT COUNT(*) FROM documents WHERE evidence_version_id='ev_z'") == 1
    _assert_ineligible(e)


def test_supersede_and_deindex_removes_graph_rows(lib):
    e, sf = lib

    class _Mgr:  # the one transition the services share, with a stand-in evidence manager
        session_factory = sf

        def mark_version_status(self, vid, status):
            with sf() as s:
                s.execute(text("UPDATE evidence_versions SET status=:st WHERE id=:v"), {"st": status.name, "v": vid})
                s.commit()

    supersede_and_deindex(_Mgr(), None, "ev_z")
    assert _scalar(e, "SELECT COUNT(*) FROM documents WHERE evidence_version_id='ev_z'") == 0
    assert _scalar(e, "SELECT COUNT(*) FROM kag_document_clauses WHERE chunk_id LIKE 'c_z%'") == 0
    assert _scalar(e, "SELECT COUNT(*) FROM kag_document_entities WHERE document_id NOT IN (SELECT id FROM documents)") == 0
    _assert_ineligible(e)


def test_new_version_replaces_old_in_plan(lib):
    """A new version of the same contract (old SUPERSEDED, new READY) is the only one planned
    and clause chunks come only from the new version."""
    e, sf = lib
    new_body = ZOG.replace("May 9, 2019", "June 1, 2021")
    _insert_metadata(e, [_rec("c_n0", "ev_zn", new_body, 0, "src_z"), _rec("c_n1", "ev_zn", ZOG2, 1, "src_z")])
    _name(e, "ev_zn", "/lib/Zogenix_Distributor Agreement.pdf", 200.0)
    with e.begin() as c:
        c.execute(text("UPDATE evidence_versions SET status='SUPERSEDED' WHERE id='ev_z'"))
    builder.build_all(sf, kag=True)
    p = kag.plan(e, ZOG_CLAUSE_Q)
    assert p.active and p.version_ids == ["ev_zn"]
    assert set(p.clause_chunk_ids) <= {"c_n0", "c_n1"}


def _purge(e, sf, tmp_path: Path, source_id: str) -> None:
    store_root = tmp_path / "evidence_store"
    for sub in ("objects", "manifests", "quarantine", "trash"):
        (store_root / sub).mkdir(parents=True, exist_ok=True)
    mgr = SourceManager(sf)
    mgr.deactivate_source(source_id)
    mgr.request_hard_delete(source_id)
    SourcePurgeService(sf, ContentAddressedStore(store_root)).purge_source(source_id)


def test_purge_cascades_graph_edges_and_clause_links(lib, tmp_path):
    e, sf = lib
    _purge(e, sf, tmp_path, "src_z")
    assert _scalar(e, "SELECT status FROM sources WHERE id='src_z'") == "DELETED"
    assert _scalar(e, "SELECT COUNT(*) FROM documents WHERE source_id='src_z'") == 0
    assert _scalar(e, "SELECT COUNT(*) FROM kag_document_entities WHERE document_id NOT IN (SELECT id FROM documents)") == 0
    assert _scalar(e, "SELECT COUNT(*) FROM kag_document_clauses WHERE document_id NOT IN (SELECT id FROM documents)") == 0
    assert _scalar(e, "SELECT COUNT(*) FROM kag_document_clauses WHERE chunk_id LIKE 'c_z%'") == 0
    assert _scalar(e, "SELECT COUNT(*) FROM kag_document_clauses WHERE chunk_id NOT IN (SELECT id FROM chunks)") == 0
    _assert_ineligible(e)
    # the other document is untouched
    assert kag.plan(e, "Which law governs the Bloom Energy agreement?").active


@pytest.mark.xfail(strict=True, reason=(
    "FINDING (M0, not fixed by design): hard-deleting a source leaves its kag_entities/kag_entity_variants rows "
    "(only the document edges cascade). The deleted contract's party name stays queryable in the graph and "
    "KagPlan.entities/doc_info['kag'] echo it for an inactive plan (reason 'named entity has no document in scope')."))
def test_purge_leaves_no_orphan_entities(lib, tmp_path):
    e, sf = lib
    _purge(e, sf, tmp_path, "src_z")
    orphans = _scalar(e, "SELECT COUNT(*) FROM kag_entities WHERE id NOT IN (SELECT entity_id FROM kag_document_entities)")
    assert orphans == 0


def test_inactive_plan_for_deleted_source_returns_no_evidence_ids(lib, tmp_path):
    e, sf = lib
    _purge(e, sf, tmp_path, "src_z")
    p = kag.plan(e, ZOG_CLAUSE_Q)
    assert not p.active and not p.version_ids and not p.clause_chunk_ids and p.top_version_id is None


@pytest.mark.parametrize("status", ["REVOKED", "TOMBSTONED", "SUPERSEDED"])
def test_query_service_never_scopes_into_ineligible_evidence(lib, monkeypatch, status):
    """End to end through `_kag_scope`: the scoped search must not even run."""
    e, _ = lib
    with e.begin() as c:
        if status == "SUPERSEDED":
            c.execute(text("UPDATE evidence_versions SET status='SUPERSEDED' WHERE id='ev_z'"))
        else:
            c.execute(text("UPDATE sources SET status=:s WHERE id='src_z'"), {"s": status})
    calls: list = []
    svc = _service(e, Settings(kag_scoping="on"), monkeypatch, calls, [["c_b1"], ["c_z1"]])
    svc.ask(ZOG_CLAUSE_Q, mode=QueryMode.FAST)
    assert all("scope_version_ids" not in c or "ev_z" not in c["scope_version_ids"] for c in calls)
    assert len(calls) == 1


def test_query_service_scopes_only_into_eligible_version(lib, monkeypatch):
    e, _ = lib
    calls: list = []
    svc = _service(e, Settings(kag_scoping="on"), monkeypatch, calls, [["c_b1"], ["c_z1"]])
    svc.ask(ZOG_CLAUSE_Q, mode=QueryMode.FAST)
    scoped = [c for c in calls if "scope_version_ids" in c]
    assert scoped and scoped[0]["scope_version_ids"] == ["ev_z"]


def test_finding_evidence_deleted_entity_name_still_echoed(lib, tmp_path):
    """Pins the observable half of the orphan-entity finding above (documents what happens today)."""
    e, sf = lib
    _purge(e, sf, tmp_path, "src_z")
    p = kag.plan(e, ZOG_CLAUSE_Q)
    assert p.reason == "named entity has no document in scope"
    assert p.entities  # the deleted contract's party name is still echoed
