"""M3-1: orphan KAG entity lifecycle. Scratch SQLite + synthetic documents only (CPU, no models)."""
from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text

from docket.core.db.engine import get_session_factory
from docket.core.writer_lock import WriterLock
from docket.infra.evidence.store import ContentAddressedStore
from docket.services.documents import builder, kag
from docket.services.sources.manager import SourceManager
from docket.services.sources.prune import supersede_and_deindex
from docket.services.sources.purge import SourcePurgeService
from test_document_layer import BLM, BLM2, ZOG, ZOG2, _name, _rec
from test_hybrid_retrieval import _insert_metadata

ZOG_Q = "Which law governs the Distributor Agreement with Zogenix, Inc.?"
BLM_Q = "Which law governs the Bloom Energy agreement?"


def _n(e, sql):
    with e.connect() as c:
        return c.execute(text(sql)).scalar_one()


def _orphans(e):
    return _n(e, "SELECT COUNT(*) FROM kag_entities WHERE id NOT IN (SELECT entity_id FROM kag_document_entities)")


def _variants_orphaned(e):
    return _n(e, "SELECT COUNT(*) FROM kag_entity_variants WHERE entity_id NOT IN (SELECT id FROM kag_entities)")


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


def _purge(sf, tmp_path: Path, source_id: str) -> None:
    root = tmp_path / "evidence_store"
    for sub in ("objects", "manifests", "quarantine", "trash"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    mgr = SourceManager(sf)
    mgr.deactivate_source(source_id)
    mgr.request_hard_delete(source_id)
    SourcePurgeService(sf, ContentAddressedStore(root)).purge_source(source_id)


def _add_shared_second_zogenix(e, sf):
    _insert_metadata(e, [_rec("c_z2", "ev_z2", ZOG, 0, "src_z2"), _rec("c_z3", "ev_z2", ZOG2, 1, "src_z2")])
    _name(e, "ev_z2", "/lib/Zogenix_Distributor Agreement copy.pdf", 100.0)
    builder.build_for_version(sf, "ev_z2", True)


def test_baseline_has_no_orphans_and_gc_is_noop(lib):
    e, sf = lib
    ents = _n(e, "SELECT COUNT(*) FROM kag_entities")
    assert ents and _orphans(e) == 0
    with sf() as s:
        assert kag.gc_orphan_entities(s) == kag.GcReport(0, 0)
        s.commit()
    assert _n(e, "SELECT COUNT(*) FROM kag_entities") == ents


def test_single_document_purge_removes_entities_and_variants(lib, tmp_path):
    e, sf = lib
    ents, variants = _n(e, "SELECT COUNT(*) FROM kag_entities"), _n(e, "SELECT COUNT(*) FROM kag_entity_variants")
    assert kag.plan(e, ZOG_Q).entities
    _purge(sf, tmp_path, "src_z")
    assert _orphans(e) == 0 and _variants_orphaned(e) == 0
    assert _n(e, "SELECT COUNT(*) FROM kag_entities") < ents
    assert _n(e, "SELECT COUNT(*) FROM kag_entity_variants") < variants
    # the deleted party name no longer matches, even as a bare name in a question
    assert kag.match_entities(e, ZOG_Q) == []
    assert kag.match_entities(e, "Tell me about Zogenix") == []
    p = kag.plan(e, ZOG_Q)
    assert not p.active and not p.entities
    # the other document's graph is untouched
    assert kag.plan(e, BLM_Q).active


def test_shared_entity_survives_until_last_document_goes(lib, tmp_path):
    e, sf = lib
    _add_shared_second_zogenix(e, sf)
    assert _n(e, "SELECT COUNT(*) FROM documents WHERE source_id IN ('src_z','src_z2')") == 2
    shared = kag.match_entities(e, ZOG_Q)
    assert shared
    _purge(sf, tmp_path, "src_z")
    assert kag.match_entities(e, ZOG_Q) == shared and _orphans(e) == 0
    assert kag.plan(e, ZOG_Q).entities
    _purge(sf, tmp_path, "src_z2")
    assert kag.match_entities(e, ZOG_Q) == []
    assert _orphans(e) == 0


def test_supersede_removes_entities(lib):
    e, sf = lib

    class _Mgr:
        session_factory = sf

        def mark_version_status(self, vid, status):
            with sf() as s:
                s.execute(text("UPDATE evidence_versions SET status=:st WHERE id=:v"), {"st": status.name, "v": vid})
                s.commit()

    supersede_and_deindex(_Mgr(), None, "ev_z")
    assert kag.match_entities(e, ZOG_Q) == []
    assert _orphans(e) == 0 and _variants_orphaned(e) == 0
    assert kag.plan(e, BLM_Q).active


def test_supersede_keeps_entity_shared_with_live_document(lib):
    e, sf = lib
    _add_shared_second_zogenix(e, sf)
    builder.delete_for_version(sf, "ev_z")
    assert kag.match_entities(e, ZOG_Q) and _orphans(e) == 0


def test_rebuild_without_kag_drops_orphaned_entities(lib):
    """KAG flag off at rebuild time: the replaced row's edges cascade away, so GC must still run."""
    e, sf = lib
    assert _n(e, "SELECT COUNT(*) FROM kag_entities") > 0
    builder.build_all(sf, force=True, kag=False)
    assert _n(e, "SELECT COUNT(*) FROM kag_entities") == 0
    assert _n(e, "SELECT COUNT(*) FROM kag_entity_variants") == 0


def test_reingest_rebuild_with_changed_party_drops_old_entity(lib):
    e, sf = lib
    assert kag.match_entities(e, ZOG_Q)
    with e.begin() as c:  # new content for the same version: the old party name is gone
        c.execute(text("UPDATE chunks SET text = REPLACE(REPLACE(text, 'Zogenix, Inc.', 'Acme Widgets LLC'), 'Zogenix', 'Acme Widgets') WHERE evidence_version_id='ev_z'"))
    _name(e, "ev_z", "/lib/Renamed Acme Agreement.pdf", 100.0)  # the file name also yields an entity
    builder.build_all(sf, force=True, kag=True)
    assert kag.match_entities(e, ZOG_Q) == []
    assert _orphans(e) == 0
    assert kag.match_entities(e, "What does the Acme Widgets contract say?")


def test_purge_rollback_keeps_entities(lib, tmp_path, monkeypatch):
    e, sf = lib
    before = (_n(e, "SELECT COUNT(*) FROM kag_entities"), _n(e, "SELECT COUNT(*) FROM kag_entity_variants"),
              _n(e, "SELECT COUNT(*) FROM documents"))
    root = tmp_path / "evidence_store"
    for sub in ("objects", "manifests", "quarantine", "trash"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    mgr = SourceManager(sf)
    mgr.deactivate_source("src_z")
    mgr.request_hard_delete("src_z")
    store = ContentAddressedStore(root)

    def boom(*a, **k):
        raise RuntimeError("simulated failure after GC, before commit")

    monkeypatch.setattr(store, "move_to_trash", boom)
    monkeypatch.setattr("docket.services.sources.purge.count_blob_references", lambda *a, **k: 0)
    # make sure there is at least one blob to trash so the failure is reached
    with e.connect() as c:
        assert c.execute(text("SELECT COUNT(*) FROM evidence_versions WHERE source_id='src_z'")).scalar_one() == 1
    with pytest.raises(RuntimeError, match="simulated"):
        SourcePurgeService(sf, store).purge_source("src_z")
    assert (_n(e, "SELECT COUNT(*) FROM kag_entities"), _n(e, "SELECT COUNT(*) FROM kag_entity_variants"),
            _n(e, "SELECT COUNT(*) FROM documents")) == before
    assert kag.match_entities(e, ZOG_Q)


def test_prune_rollback_keeps_entities(lib, monkeypatch):
    e, sf = lib
    before = _n(e, "SELECT COUNT(*) FROM kag_entities")

    monkeypatch.setattr(kag, "gc_orphan_entities", lambda s, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    builder.delete_for_version(sf, "ev_z")  # never raises; the whole transaction rolls back
    assert _n(e, "SELECT COUNT(*) FROM documents WHERE evidence_version_id='ev_z'") == 1
    assert _n(e, "SELECT COUNT(*) FROM kag_entities") == before


def _historical_orphans(e, sf):
    """Simulate a pre-M3-1 library: documents deleted with raw SQL, entities left behind."""
    with e.begin() as c:
        c.execute(text("DELETE FROM documents WHERE source_id='src_z'"))
    assert _orphans(e) > 0


def test_historical_cleanup_dry_run_then_apply(lib, tmp_path):
    e, sf = lib
    _historical_orphans(e, sf)
    snap = (_n(e, "SELECT COUNT(*) FROM kag_entities"), _n(e, "SELECT COUNT(*) FROM kag_entity_variants"))
    lock = WriterLock(tmp_path)
    dry = kag.gc_graph(sf, lock)
    assert dry.entities > 0 and dry.variants > 0
    assert (_n(e, "SELECT COUNT(*) FROM kag_entities"), _n(e, "SELECT COUNT(*) FROM kag_entity_variants")) == snap
    assert kag.match_entities(e, ZOG_Q)  # still there after a dry run
    done = kag.gc_graph(sf, lock, apply=True)
    assert done == dry
    assert _orphans(e) == 0 and kag.match_entities(e, ZOG_Q) == []
    assert kag.gc_graph(sf, lock, apply=True) == kag.GcReport(0, 0)  # idempotent
    assert kag.plan(e, BLM_Q).active


def test_apply_waits_on_writer_lock_and_changes_nothing(lib, tmp_path):
    from docket.core.writer_lock import WriterLockBusyError

    e, sf = lib
    _historical_orphans(e, sf)
    snap = _n(e, "SELECT COUNT(*) FROM kag_entities")
    held = WriterLock(tmp_path)
    import subprocess
    import sys

    code = ("from pathlib import Path; import sys; from docket.core.writer_lock import WriterLock\n"
            "with WriterLock(Path(sys.argv[1])).hold('other'):\n print('ready',flush=True)\n sys.stdin.read()")
    proc = subprocess.Popen([sys.executable, "-c", code, str(tmp_path)], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "ready"
        with pytest.raises(WriterLockBusyError):
            kag.gc_graph(sf, WriterLock(tmp_path, timeout=.05), apply=True)
        assert _n(e, "SELECT COUNT(*) FROM kag_entities") == snap
        kag.gc_graph(sf, held, apply=False)  # dry-run needs no lock
    finally:
        proc.stdin.close()
        proc.wait(timeout=10)


def test_cli_gc_graph_dry_run_default_prints_counts_not_names(lib, tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from docket.interfaces.cli import main

    e, sf = lib
    _historical_orphans(e, sf)
    ctx = type("Ctx", (), {"session_factory": sf})()
    monkeypatch.setattr(main, "build_context", lambda: ctx)
    runner = CliRunner()
    snap = _n(e, "SELECT COUNT(*) FROM kag_entities")
    out = runner.invoke(main.app, ["library", "gc-graph"])
    assert out.exit_code == 0 and "Preview" in out.output and "orphaned entities" in out.output
    assert "zogenix" not in out.output.lower()
    assert _n(e, "SELECT COUNT(*) FROM kag_entities") == snap
    out = runner.invoke(main.app, ["library", "gc-graph", "--apply"])
    assert out.exit_code == 0 and "Removed" in out.output and "zogenix" not in out.output.lower()
    assert _orphans(e) == 0
    out = runner.invoke(main.app, ["library", "gc-graph", "--apply"])
    assert "Removed 0 orphaned entities and 0 name variants" in out.output
