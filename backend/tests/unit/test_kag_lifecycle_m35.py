"""M3-5: graph lifecycle and reindex consistency. Scratch SQLite + synthetic documents only (CPU, no models)."""
from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import text

from docket.core.db.engine import get_session_factory
from docket.core.db.models import Chunk, EvidenceUnit, EvidenceVersion, VersionStatus
from docket.core.writer_lock import WriterLock
from docket.services.documents import builder, kag
from docket.services.ingestion.chunk_writer import BuiltRows, ChunkWriter
from test_document_layer import BLM, BLM2, ZOG, ZOG2, _name, _rec
from test_hybrid_retrieval import _insert_metadata

BLM_AUDIT_Q = "How often may a party audit the books under the agreement with Bloom Energy Corporation?"
SECRET_NAME = "Quuxwidget Distinctive Holdings"
SECRET_PATH = "/lib/zzsecret_dir/Quuxwidget_Agreement.pdf"


def _n(e, sql, **p):
    with e.connect() as c:
        return c.execute(text(sql), p).scalar_one()


def _edges(e, vid):
    return _n(e, "SELECT (SELECT COUNT(*) FROM kag_document_entities x JOIN documents d ON d.id=x.document_id "
                 "WHERE d.evidence_version_id=:v) + (SELECT COUNT(*) FROM kag_document_clauses x JOIN documents d "
                 "ON d.id=x.document_id WHERE d.evidence_version_id=:v)", v=vid)


def _dump(e):
    with e.connect() as c:
        rows = []
        for t in ("documents", "document_fields", "kag_entities", "kag_entity_variants",
                  "kag_document_entities", "kag_document_clauses", "chunks"):
            rows.append((t, sorted(map(repr, c.execute(text(f"SELECT * FROM {t}")).all()))))
    return hashlib.sha256(repr(rows).encode()).hexdigest()


class _Fts:
    def __init__(self, fail=False):
        self.fail = fail

    def delete_version_in(self, conn, vid):
        pass

    def upsert_in(self, conn, records):
        if self.fail:
            raise RuntimeError("boom")


def _built(vid, texts, src, prefix="n"):
    units, chunks = [], []
    for i, t in enumerate(texts):
        uid = f"eu_{prefix}_{vid}_{i}"
        units.append(EvidenceUnit(id=uid, evidence_version_id=vid, unit_index=i, heading=None, content_hash=f"uh_{prefix}{i}"))
        chunks.append(Chunk(id=f"c_{prefix}_{vid}_{i}", source_id=src, evidence_version_id=vid, evidence_unit_id=uid,
                            chunk_recipe_id="rcp_1", ordinal=i, heading=None, text=t, content_hash=f"h_{prefix}{i}"))
    return BuiltRows(units=units, chunks=chunks, records=[])


@pytest.fixture
def lib(migrated_sqlite_engine):
    e = migrated_sqlite_engine
    _insert_metadata(e, [_rec("c_z0", "ev_z", ZOG, 0, "src_z"), _rec("c_z1", "ev_z", ZOG2, 1, "src_z")])
    _insert_metadata(e, [_rec("c_b0", "ev_b", BLM, 0, "src_b"), _rec("c_b1", "ev_b", BLM2, 1, "src_b")])
    _name(e, "ev_z", "/lib/Zogenix_Distributor Agreement.pdf", 100.0)
    _name(e, "ev_b", "/lib/Bloom_Maintenance Agreement.pdf", 100.0)
    sf = get_session_factory(e)
    builder.build_all(sf, kag=True)
    return e, sf, None


@pytest.fixture
def cw(lib):
    e, sf, _ = lib
    w = ChunkWriter.__new__(ChunkWriter)
    w._session_factory = sf
    return w


def _replace(w, vid, texts, src, status=VersionStatus.READY, fts=None):
    return w.replace_version_content(fts=fts or _Fts(), evidence_version_id=vid,
                                     built=_built(vid, texts, src), new_status=status)


def _integrity(sf):
    with sf() as s:
        return kag.check_graph_integrity(s)


def test_normal_replace_invalidates_only_that_versions_graph(lib, cw):
    e, sf, _ = lib
    z_before = _edges(e, "ev_z")
    assert _edges(e, "ev_b") and z_before
    assert _replace(cw, "ev_b", [BLM, BLM2], "src_b") == 2
    assert _edges(e, "ev_b") == 0 and _edges(e, "ev_z") == z_before
    assert _n(e, "SELECT COUNT(*) FROM documents WHERE evidence_version_id='ev_b'") == 1  # documents row kept
    assert _n(e, "SELECT COUNT(*) FROM kag_entities WHERE norm LIKE '%bloom%'") == 0       # orphan GC ran
    assert kag.plan(e, "Which law governs the Distributor Agreement with Zogenix, Inc.?").active
    r = _integrity(sf)
    assert r.problems == 0 and r.documents_no_graph == 1 and r.documents_affected == 0


def test_hook_disabled_leaves_missing_not_stale_and_not_rebuilt(lib, cw):
    e, sf, _ = lib
    _replace(cw, "ev_b", [BLM, BLM2], "src_b")
    # no hook runs (documents_build_enabled off): still no graph, nothing dangling, nothing claimed rebuilt
    assert _edges(e, "ev_b") == 0 and _integrity(sf).problems == 0
    plan = kag.plan(e, BLM_AUDIT_Q)
    assert not plan.active and plan.reason == "no entity named" and not plan.clause_absent


def test_hook_failure_keeps_graph_missing(lib, cw, monkeypatch):
    e, sf, _ = lib
    _replace(cw, "ev_b", [BLM, BLM2], "src_b")
    monkeypatch.setattr(builder, "build_document", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    assert builder.build_for_version(sf, "ev_b", True) is False
    assert _edges(e, "ev_b") == 0 and _integrity(sf).problems == 0
    assert not kag.plan(e, BLM_AUDIT_Q).active


def test_hook_success_after_replace_rebuilds_clean(lib, cw):
    e, sf, _ = lib
    _replace(cw, "ev_b", [BLM, BLM2], "src_b")
    assert builder.build_for_version(sf, "ev_b", True)
    assert _edges(e, "ev_b") > 0
    r = _integrity(sf)
    assert r.problems == 0 and r.documents_no_graph == 0 and r.entity_legacy == 0


def test_retry_cleanup_delete_units_and_chunks(lib, cw):
    e, sf, _ = lib
    cw.delete_units_and_chunks("ev_b")
    assert _n(e, "SELECT COUNT(*) FROM chunks WHERE evidence_version_id='ev_b'") == 0
    assert _edges(e, "ev_b") == 0 and _edges(e, "ev_z") > 0
    assert _integrity(sf).problems == 0


def test_tiered_text_ready_to_ready_swap(lib, cw):
    e, sf, _ = lib
    with sf() as s:
        s.get(EvidenceVersion, "ev_b").status = VersionStatus.TEXT_READY
        s.commit()
    _replace(cw, "ev_b", [BLM, BLM2 + " Plus more."], "src_b", status=VersionStatus.READY)
    with sf() as s:
        assert s.get(EvidenceVersion, "ev_b").status == VersionStatus.READY
    assert _edges(e, "ev_b") == 0 and _integrity(sf).problems == 0


def test_superseded_version_replace_is_a_noop_for_graph(lib, cw):
    e, sf, _ = lib
    with sf() as s:
        s.get(EvidenceVersion, "ev_b").status = VersionStatus.SUPERSEDED
        s.commit()
    before = _edges(e, "ev_b")
    assert _replace(cw, "ev_b", [BLM], "src_b") is None
    assert _edges(e, "ev_b") == before  # nothing changed, so nothing invalidated


def test_supersession_cleanup_still_works(lib):
    e, sf, _ = lib
    builder.delete_for_version(sf, "ev_b")
    assert _edges(e, "ev_b") == 0 and _n(e, "SELECT COUNT(*) FROM kag_entities WHERE norm LIKE '%bloom%'") == 0


def test_rollback_restores_graph_rows(lib, cw):
    e, sf, _ = lib
    before = _dump(e)
    with pytest.raises(RuntimeError):
        _replace(cw, "ev_b", [BLM, BLM2], "src_b", fts=_Fts(fail=True))  # fails after invalidation, before commit
    assert _dump(e) == before
    assert _integrity(sf).problems == 0


def test_invalidate_rollback_in_caller_session(lib):
    e, sf, _ = lib
    before = _dump(e)
    with sf() as s:
        assert kag.invalidate_graph_for_version(s, "ev_b") > 0
        s.rollback()
    assert _dump(e) == before


def test_shared_entity_survives_when_other_version_references_it(lib, cw):
    e, sf, _ = lib
    _insert_metadata(e, [_rec("c_z2", "ev_z2", ZOG, 0, "src_z2"), _rec("c_z3", "ev_z2", ZOG2, 1, "src_z2")])
    _name(e, "ev_z2", "/lib/Zogenix_Distributor Agreement copy.pdf", 100.0)
    builder.build_for_version(sf, "ev_z2", True)
    z2 = _edges(e, "ev_z2")
    ents = _n(e, "SELECT COUNT(*) FROM kag_entities WHERE norm LIKE '%zogenix%'")
    assert ents
    cw.delete_units_and_chunks("ev_z")
    assert _edges(e, "ev_z") == 0 and _edges(e, "ev_z2") == z2
    assert _n(e, "SELECT COUNT(*) FROM kag_entities WHERE norm LIKE '%zogenix%'") == ents
    assert _n(e, "SELECT COUNT(*) FROM kag_entities WHERE id NOT IN (SELECT entity_id FROM kag_document_entities)") == 0


def test_invalidate_is_noop_without_graph(migrated_sqlite_engine):
    e = migrated_sqlite_engine
    _insert_metadata(e, [_rec("c_b0", "ev_b", BLM, 0, "src_b")])
    sf = get_session_factory(e)
    with sf() as s:
        assert kag.invalidate_graph_for_version(s, "ev_b") == 0 and kag.invalidate_graph_for_version(s, "nope") == 0


def test_clause_absent_never_claimed_for_invalidated_or_partial_graph(lib, cw):
    e, sf, _ = lib
    # baseline: a fully built document legitimately yields 'absent'
    p = kag.plan(e, BLM_AUDIT_Q)
    assert p.active and p.clause_absent
    # fully invalidated: plan is inactive (no entity edges), never an absent claim
    _replace(cw, "ev_b", [BLM, BLM2], "src_b")
    p = kag.plan(e, BLM_AUDIT_Q)
    assert not p.active and not p.clause_absent
    # partial graph: entity edges present but clause rows missing AND chunk refs dangling -> unknown, not absent
    builder.build_for_version(sf, "ev_b", True)
    assert kag.plan(e, BLM_AUDIT_Q).clause_absent
    with e.begin() as c:
        c.execute(text("DELETE FROM kag_document_clauses"))
        c.execute(text("UPDATE kag_document_entities SET chunk_id='gone', evidence_kind='span', span_start=0, span_end=3"))
    p = kag.plan(e, BLM_AUDIT_Q)
    assert p.active and not p.clause_absent
    # document whose version lost all chunks: unknown
    builder.build_for_version(sf, "ev_b", True)
    with e.begin() as c:
        c.execute(text("DELETE FROM kag_document_clauses"))
        c.execute(text("DELETE FROM chunks WHERE evidence_version_id='ev_b'"))
    assert not kag.plan(e, BLM_AUDIT_Q).clause_absent


def test_regressions_orphan_gc_and_span_integrity_after_rebuild(lib, cw):
    e, sf, _ = lib
    cw.delete_units_and_chunks("ev_b")
    _replace(cw, "ev_b", [BLM, BLM2], "src_b")
    builder.build_for_version(sf, "ev_b", True)
    with sf() as s:
        assert kag.gc_orphan_entities(s) == kag.GcReport(0, 0)
    r = _integrity(sf)
    assert r.problems == 0 and r.entity_span >= 1


# ---- repair ----------------------------------------------------------------

def _break(e):
    """Dangling + legacy damage on ev_b only (simulates a pre-M3-5 stale graph)."""
    with e.begin() as c:
        c.execute(text("UPDATE kag_document_clauses SET chunk_id='gone' WHERE document_id IN "
                       "(SELECT id FROM documents WHERE evidence_version_id='ev_b')"))
        c.execute(text("UPDATE kag_document_entities SET evidence_kind=NULL, span_start=NULL, span_end=NULL WHERE document_id IN "
                       "(SELECT id FROM documents WHERE evidence_version_id='ev_b')"))


def test_repair_dry_run_changes_nothing(lib, tmp_path):
    e, sf, _ = lib
    _break(e)
    before = _dump(e)
    rep = kag.repair_graph(sf, WriterLock(tmp_path), apply=False)
    assert rep.repairable == 1 and rep.rebuilt == 0
    assert _dump(e) == before


def test_repair_apply_bounded_clean_and_idempotent(lib, tmp_path):
    e, sf, _ = lib
    z_rows = _n(e, "SELECT COUNT(*) FROM kag_document_clauses c JOIN documents d ON d.id=c.document_id WHERE d.evidence_version_id='ev_z'")
    z_doc = _n(e, "SELECT id FROM documents WHERE evidence_version_id='ev_z'")
    _break(e)
    lock = WriterLock(tmp_path)
    rep = kag.repair_graph(sf, lock, apply=True)
    assert rep.rebuilt == 1 and rep.failed == 0 and rep.unrepairable_no_chunks == 0
    assert _n(e, "SELECT id FROM documents WHERE evidence_version_id='ev_z'") == z_doc  # untouched document keeps its row id
    assert _n(e, "SELECT COUNT(*) FROM kag_document_clauses c JOIN documents d ON d.id=c.document_id WHERE d.evidence_version_id='ev_z'") == z_rows
    r = _integrity(sf)
    assert r.problems == 0 and r.legacy_edges == 0 and r.documents_affected == 0
    again = kag.repair_graph(sf, lock, apply=True)
    assert again.repairable == 0 and again.rebuilt == 0
    assert kag.plan(e, BLM_AUDIT_Q).clause_absent  # full graph again behaves as before


def test_repair_reports_unrepairable_and_respects_bound(lib, tmp_path):
    e, sf, _ = lib
    _break(e)
    with e.begin() as c:  # make ev_z affected too, then strip its chunks and mark it non-current
        c.execute(text("UPDATE kag_document_entities SET evidence_kind=NULL, span_start=NULL, span_end=NULL"))
    _insert_metadata(e, [_rec("c_old", "ev_old", BLM, 0, "src_old")], version_status=VersionStatus.SUPERSEDED)
    with e.begin() as c:
        c.execute(text("INSERT INTO documents (evidence_version_id, source_id, file_name, doc_type, title, doc_text, extractor_version) "
                       "VALUES ('ev_old','src_old','o.pdf','other','t','x','1')"))
        c.execute(text("INSERT INTO kag_document_clauses (document_id, clause_type, chunk_id, confidence) "
                       "SELECT id,'insurance','c_old',0.6 FROM documents WHERE evidence_version_id='ev_old'"))
    rep = kag.repair_graph(sf, WriterLock(tmp_path), apply=True, max_documents=1)
    assert rep.repairable == 2 and rep.rebuilt == 1 and rep.deferred == 1 and rep.unrepairable_not_current == 1
    rep2 = kag.repair_graph(sf, WriterLock(tmp_path), apply=True, max_documents=1)
    assert rep2.rebuilt == 1 and rep2.deferred == 0
    assert kag.repair_graph(sf, WriterLock(tmp_path)).repairable == 0


def test_repair_apply_waits_on_lock(lib, tmp_path):
    import subprocess
    import sys

    from docket.core.writer_lock import WriterLockBusyError

    e, sf, _ = lib
    _break(e)
    before = _dump(e)
    code = ("from pathlib import Path; import sys; from docket.core.writer_lock import WriterLock\n"
            "with WriterLock(Path(sys.argv[1])).hold('other'):\n print('ready',flush=True)\n sys.stdin.read()")
    proc = subprocess.Popen([sys.executable, "-c", code, str(tmp_path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "ready"
        with pytest.raises(WriterLockBusyError):
            kag.repair_graph(sf, WriterLock(tmp_path, timeout=.05), apply=True)
        assert _dump(e) == before
    finally:
        proc.stdin.close()
        proc.wait(timeout=10)


def test_cli_integrity_and_repair_counts_only(migrated_sqlite_engine, tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from docket.interfaces.cli import main

    e = migrated_sqlite_engine
    body = (f"This Agreement is entered into as of March 21, 2018 by and between {SECRET_NAME} Corporation and "
            "Pacific Utilities LLC, whereas the parties wish to cooperate.")
    _insert_metadata(e, [_rec("c_q0", "ev_q", "MASTER AGREEMENT", 0, "src_q"), _rec("c_q1", "ev_q", body, 1, "src_q")])
    _name(e, "ev_q", SECRET_PATH, 100.0)
    sf = get_session_factory(e)
    builder.build_all(sf, kag=True)
    with e.begin() as c:
        c.execute(text("UPDATE kag_document_clauses SET chunk_id='gone'"))
    monkeypatch.setattr(main, "build_context", lambda: type("Ctx", (), {"session_factory": sf})())
    runner = CliRunner()
    before = _dump(e)

    def bad(out):
        low = out.lower()
        return "quuxwidget" in low or "zzsecret" in low or "pacific" in low

    out = runner.invoke(main.app, ["library", "graph-integrity"])
    assert out.exit_code == 0 and "dangling chunk references" in out.output and "without graph rows" in out.output
    assert not bad(out.output)
    out = runner.invoke(main.app, ["library", "graph-integrity", "--repair"])
    assert out.exit_code == 0 and "Preview: would rebuild 1 documents" in out.output and not bad(out.output)
    assert _dump(e) == before
    out = runner.invoke(main.app, ["library", "graph-integrity", "--apply"])
    assert out.exit_code == 2
    out = runner.invoke(main.app, ["library", "graph-integrity", "--repair", "--apply"])
    assert out.exit_code == 0 and "Rebuilt 1 documents" in out.output and not bad(out.output)
    out = runner.invoke(main.app, ["library", "graph-integrity", "--repair", "--apply"])
    assert "Rebuilt 0 documents" in out.output and "0 dangling" in out.output


def test_repair_preserves_document_identity_and_inbound_supersession_links(lib, tmp_path):
    """supersedes_document_id is a plain integer (no FK): rebuilding a document in place must keep its id,
    or a newer document's link to it would silently dangle and the older document would look current."""
    e, sf, _ = lib
    b_doc = _n(e, "SELECT id FROM documents WHERE evidence_version_id='ev_b'")
    with e.begin() as c:
        c.execute(text("UPDATE documents SET supersedes_document_id=:b, supersedes_kind='replaces' "
                       "WHERE evidence_version_id='ev_z'"), {"b": b_doc})
    _break(e)
    rep = kag.repair_graph(sf, WriterLock(tmp_path), apply=True)
    assert rep.rebuilt == 1
    assert _n(e, "SELECT id FROM documents WHERE evidence_version_id='ev_b'") == b_doc
    assert _n(e, "SELECT COUNT(*) FROM documents z JOIN documents t ON t.id = z.supersedes_document_id "
                 "WHERE z.evidence_version_id='ev_z'") == 1
    # the document-level FTS row follows the same id (trigger-maintained)
    assert _n(e, "SELECT COUNT(*) FROM fts_documents WHERE rowid=:b", b=b_doc) == 1
    assert _integrity(sf).problems == 0


def test_identity_reuse_only_for_same_version_in_place_rebuild(lib):
    """A same-version rebuild keeps its id (one row, one FTS row, one edge set); a different version always
    gets a fresh id and never collides with an existing one."""
    e, sf, _ = lib
    ids = {v: _n(e, "SELECT id FROM documents WHERE evidence_version_id=:v", v=v) for v in ("ev_z", "ev_b")}
    builder.build_all(sf, force=True, kag=True)          # in-place rebuild of both
    for v, i in ids.items():
        assert _n(e, "SELECT id FROM documents WHERE evidence_version_id=:v", v=v) == i
        assert _n(e, "SELECT COUNT(*) FROM documents WHERE id=:i", i=i) == 1
        assert _n(e, "SELECT COUNT(*) FROM fts_documents WHERE rowid=:i", i=i) == 1
    assert _n(e, "SELECT COUNT(*) FROM documents") == _n(e, "SELECT COUNT(*) FROM fts_documents") == 2
    # a brand-new version gets a new, distinct id
    _insert_metadata(e, [_rec("c_n0", "ev_n", BLM, 0, "src_n")])
    _name(e, "ev_n", "/lib/Newco_Agreement.pdf", 100.0)
    assert builder.build_for_version(sf, "ev_n", True)
    new_id = _n(e, "SELECT id FROM documents WHERE evidence_version_id='ev_n'")
    assert new_id not in ids.values() and new_id > max(ids.values())
    assert _n(e, "SELECT COUNT(*) FROM fts_documents") == 3
    assert _integrity(sf).problems == 0
