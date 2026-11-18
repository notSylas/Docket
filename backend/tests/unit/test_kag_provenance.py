"""M3-2: exact provenance for the KAG-lite graph. Scratch SQLite + synthetic documents only (CPU)."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic import command
from sqlalchemy import text

from docket.core.db.engine import get_engine, get_session_factory
from docket.core.db.models import EvidenceVersion
from docket.services.documents import builder, kag
from test_document_layer import BLM2, ZOG, ZOG2, _alembic_config, _name, _rec
from test_hybrid_retrieval import _insert_metadata

TITLE = "MASTER MAINTENANCE AGREEMENT"
BODY = ("This Agreement is entered into as of March 21, 2018 by and between Bloom Energy Corporation and "
        "Pacific Utilities LLC, whereas the parties wish to cooperate.")


def _n(e, sql, **p):
    with e.connect() as c:
        return c.execute(text(sql), p).scalar_one()


def _rows(e, sql):
    with e.connect() as c:
        return c.execute(text(sql)).all()


def _chunk_text(e, cid):
    return _n(e, "SELECT text FROM chunks WHERE id = :i", i=cid)


def _lib(e, vid="ev_b", src="src_b", texts=(TITLE, BODY, BLM2), path="/lib/Bloom_Maintenance Agreement.pdf"):
    _insert_metadata(e, [_rec(f"c_{vid}_{i}", vid, t, i, src) for i, t in enumerate(texts)])
    _name(e, vid, path, 100.0)
    sf = get_session_factory(e)
    builder.build_all(sf, kag=True)
    return sf


def _edges(e):
    return _rows(e, "SELECT x.role, x.chunk_id, x.span_start, x.span_end, x.evidence_kind, k.display "
                    "FROM kag_document_entities x JOIN kag_entities k ON k.id = x.entity_id ORDER BY k.display, x.role")


def test_span_slice_equals_matched_name_and_party_not_in_first_chunk(migrated_sqlite_engine):
    e = migrated_sqlite_engine
    _lib(e)
    edges = [r for r in _edges(e) if r[4] == "span"]
    assert edges
    for role, cid, s, en, kind, display in edges:
        assert kag.normalize_name(_chunk_text(e, cid)[s:en]) in kag.name_variants(display)
    # the parties live in chunk 1, not the first chunk
    assert {r[1] for r in edges} == {"c_ev_b_1"}
    assert kag.check_graph_integrity(_session(e)).problems == 0


def _session(e):
    return get_session_factory(e)()


def test_aliases_and_repeated_mentions_are_deterministic():
    chunks = [
        SimpleNamespace(id="a", ordinal=0, text="Intro with ACME, Inc. first. Later Acme Inc again.", heading=None),
        SimpleNamespace(id="b", ordinal=1, text="Acme Inc. appears here too.", heading=None),
    ]
    first = kag.find_mention(chunks, "Acme, Inc.")
    assert first == kag.find_mention(list(reversed(chunks)), "Acme, Inc.") == ("a", 11, 20)
    assert chunks[0].text[11:20] == "ACME, Inc"
    # alias-only mention: display has a suffix, text only has the core name
    only_core = [SimpleNamespace(id="x", ordinal=0, text="We sell to Acme today.", heading=None)]
    cid, s, en = kag.find_mention(only_core, "Acme Inc.")
    assert only_core[0].text[s:en] == "Acme"


def test_same_result_on_repeat_build(migrated_sqlite_engine):
    e = migrated_sqlite_engine
    sf = _lib(e)
    before = _edges(e)
    builder.build_all(sf, force=True, kag=True)
    assert _edges(e) == before


def test_whitespace_punctuation_and_unicode_names():
    ch = SimpleNamespace(id="u", ordinal=0, heading=None,
                         text="Café — \U0001F600 “Reed’s,\n   Inc.” and Müller Holdings Ltd")
    cid, s, en = kag.find_mention([ch], "Reed's, Inc.")
    assert ch.text[s:en] == "Reed’s,\n   Inc"  # real slice across a newline run and a curly apostrophe
    # multibyte characters earlier in the text shift offsets by code points, not bytes
    assert s == ch.text.index("Reed")
    assert kag._span_valid(ch.text, s, en, kag.name_variants("Reed's, Inc."))
    # an ASCII-folding name still resolves on its ASCII tokens only when the same tokens occur
    ch2 = SimpleNamespace(id="v", ordinal=0, heading=None, text="Acme & Sons Co. signed")
    cid, s, en = kag.find_mention([ch2], "Acme & Sons Co.")
    assert ch2.text[s:en] == "Acme & Sons Co"


def test_validation_rejects_bad_spans():
    t = "Zogenix, Inc. here"
    allowed = kag.name_variants("Zogenix, Inc.")
    assert kag._span_valid(t, 0, 12, allowed)
    assert not kag._span_valid(t, 0, 5, allowed)
    assert not kag._span_valid(t, 5, 5, allowed) and not kag._span_valid(t, -1, 4, allowed)
    assert not kag._span_valid(t, 0, 999, allowed) and not kag._span_valid(t, None, 3, allowed)


def test_filename_only_entity_is_none_with_null_columns(migrated_sqlite_engine):
    e = migrated_sqlite_engine
    _lib(e, texts=(TITLE, "This agreement is between the parties named below.", BLM2),
         path="/lib/Initech Holdings_Maintenance Agreement.pdf")
    rows = _edges(e)
    fn = [r for r in rows if r[0] == "filename"]
    assert fn and all(r[4] == "none" and r[1] is None and r[2] is None and r[3] is None for r in fn)


def test_legacy_rows_read_as_unverified_not_spans(migrated_sqlite_engine):
    e = migrated_sqlite_engine
    _lib(e)
    with e.begin() as c:
        c.execute(text("UPDATE kag_document_entities SET evidence_kind = NULL, span_start = NULL, span_end = NULL"))
        c.execute(text("UPDATE kag_document_clauses SET evidence_kind = NULL"))
    with get_session_factory(e)() as s:
        did, eid, role = s.execute(text("SELECT document_id, entity_id, role FROM kag_document_entities")).first()
        p = kag.entity_edge_provenance(s, did, eid, role)
        assert p["kind"] == kag.KIND_LEGACY and p["span_start"] is None and p["span_end"] is None
        assert kag.clause_edge_provenance(s, s.execute(text("SELECT MIN(id) FROM kag_document_clauses")).scalar_one())["kind"] == "legacy"
        rep = kag.check_graph_integrity(s)
        assert rep.entity_legacy == rep.entity_edges > 0 and rep.entity_span == 0 and rep.clause_legacy == rep.clause_edges
    # existing query path still works with NULL columns
    assert kag.plan(e, "Which law governs the Bloom Energy agreement?").entities


def test_read_helper_contract(migrated_sqlite_engine):
    e = migrated_sqlite_engine
    _lib(e, path="/lib/Initech Holdings_Maintenance Agreement.pdf")
    with get_session_factory(e)() as s:
        seen = set()
        for did, eid, role in s.execute(text("SELECT document_id, entity_id, role FROM kag_document_entities")).all():
            p = kag.entity_edge_provenance(s, did, eid, role)
            seen.add(p["kind"])
            assert set(p) == {"chunk_id", "span_start", "span_end", "kind"}
            if p["kind"] != "span":
                assert p["span_start"] is None and p["span_end"] is None
            if p["kind"] == "none":
                assert p["chunk_id"] is None
        assert {"span", "none"} <= seen
        assert kag.entity_edge_provenance(s, 99999, 1, "party") is None
        # a changed chunk text is reported stale, never as a span
        s.execute(text("UPDATE chunks SET text = 'nothing relevant here'"))
        did, eid, role = s.execute(text("SELECT document_id, entity_id, role FROM kag_document_entities WHERE evidence_kind='span'")).first()
        p = kag.entity_edge_provenance(s, did, eid, role)
        assert p["kind"] == "stale" and p["span_start"] is None
        assert kag.check_graph_integrity(s).span_mismatch > 0
        s.rollback()


def test_clause_provenance_is_chunk_level_only(migrated_sqlite_engine):
    e = migrated_sqlite_engine
    _lib(e)
    rows = _rows(e, "SELECT evidence_kind, span_start, span_end, chunk_id FROM kag_document_clauses")
    assert rows and all(r[0] == "chunk" and r[1] is None and r[2] is None and r[3] for r in rows)


def test_reingest_changed_content_updates_provenance(migrated_sqlite_engine):
    e = migrated_sqlite_engine
    sf = _lib(e)
    old = {r[1:4] for r in _edges(e) if r[4] == "span"}
    with e.begin() as c:  # new content: party moves to a different chunk, with different offsets
        c.execute(text("DELETE FROM chunks WHERE evidence_version_id = 'ev_b'"))
    _insert_metadata(e, [_rec("c_new0", "ev_b", TITLE, 0, "src_b"),
                         _rec("c_new1", "ev_b", "Preamble words. " + BODY, 1, "src_b")])
    with get_session_factory(e)() as s:  # before rebuild the old graph is detected as dangling
        assert kag.check_graph_integrity(s).dangling > 0
    assert builder.build_for_version(sf, "ev_b", True)
    new = {r[1:4] for r in _edges(e) if r[4] == "span"}
    assert new and new != old and {r[0] for r in new} == {"c_new1"}
    with sf() as s:
        assert kag.check_graph_integrity(s).problems == 0


def test_supersede_and_deletion_keep_graph_consistent(migrated_sqlite_engine):
    e = migrated_sqlite_engine
    sf = _lib(e)
    _insert_metadata(e, [_rec("c_z0", "ev_z", ZOG, 0, "src_z"), _rec("c_z1", "ev_z", ZOG2, 1, "src_z")])
    _name(e, "ev_z", "/lib/Zogenix_Distributor Agreement.pdf", 100.0)
    builder.build_for_version(sf, "ev_z", True)
    with sf() as s:
        assert kag.check_graph_integrity(s).problems == 0
    builder.delete_for_version(sf, "ev_b")
    with sf() as s:
        rep = kag.check_graph_integrity(s)
        assert rep.problems == 0
        assert kag.gc_orphan_entities(s, dry_run=True) == kag.GcReport(0, 0)
    assert _n(e, "SELECT COUNT(*) FROM kag_document_entities x JOIN documents d ON d.id = x.document_id "
                 "WHERE d.evidence_version_id = 'ev_b'") == 0


def test_integrity_detects_version_mismatch_and_dangling(migrated_sqlite_engine):
    e = migrated_sqlite_engine
    sf = _lib(e)
    _insert_metadata(e, [_rec("c_other", "ev_other", "Unrelated text.", 0, "src_o")])
    with e.begin() as c:
        c.execute(text("UPDATE kag_document_clauses SET chunk_id = 'c_other' WHERE id = (SELECT MIN(id) FROM kag_document_clauses)"))
        c.execute(text("UPDATE kag_document_entities SET chunk_id = 'gone' WHERE evidence_kind = 'span'"))
    with sf() as s:
        rep = kag.check_graph_integrity(s)
    assert rep.version_mismatch == 1 and rep.dangling >= 1


def test_rollback_leaves_no_partial_provenance(migrated_sqlite_engine):
    e = migrated_sqlite_engine
    sf = _lib(e)
    before = (_edges(e), _rows(e, "SELECT * FROM kag_document_clauses ORDER BY id"))
    with sf() as s:
        version = s.get(EvidenceVersion, "ev_b")
        builder.build_document(s, version, True)  # rewrites the graph inside the txn...
        s.rollback()                              # ...and the caller aborts
    assert (_edges(e), _rows(e, "SELECT * FROM kag_document_clauses ORDER BY id")) == before
    with sf() as s:
        assert kag.check_graph_integrity(s).problems == 0


def test_kag_disabled_writes_no_graph_and_gc_still_runs(migrated_sqlite_engine):
    e = migrated_sqlite_engine
    _insert_metadata(e, [_rec("c_b0", "ev_b", TITLE, 0, "src_b"), _rec("c_b1", "ev_b", BODY, 1, "src_b")])
    _name(e, "ev_b", "/lib/Bloom_Maintenance Agreement.pdf", 100.0)
    sf = get_session_factory(e)
    builder.build_all(sf, kag=False)
    assert _n(e, "SELECT COUNT(*) FROM kag_document_entities") == 0
    with e.begin() as c:  # a historical orphan entity
        c.execute(text("INSERT INTO kag_entities (norm, display, kind) VALUES ('ghost', 'Ghost', 'party')"))
    builder.build_all(sf, force=True, kag=False)  # replaced row -> GC still runs with the flag off
    assert _n(e, "SELECT COUNT(*) FROM kag_entities") == 0
    with sf() as s:
        assert kag.check_graph_integrity(s) == kag.IntegrityReport()


def test_migration_0019_upgrade_keeps_legacy_rows_and_downgrades(tmp_path: Path):
    path = tmp_path / "docket.sqlite3"
    cfg = _alembic_config(path)
    command.upgrade(cfg, "f6c9e3a18e36")
    con = sqlite3.connect(path)
    con.execute("INSERT INTO workspaces (id, name, created_at) VALUES ('w', 'w', '2024-01-01')")
    con.execute("INSERT INTO authorized_sources (id, workspace_id, scope_path, created_at) VALUES ('a','w','/t','2024-01-01')")
    con.execute("INSERT INTO sources (id, workspace_id, authorized_source_id, source_type, path, status, created_at, updated_at) "
                "VALUES ('s','w','a','local_folder','/t','ACTIVE','2024-01-01','2024-01-01')")
    con.commit(); con.close()
    # legacy graph rows as 0018 wrote them; FK checks off because no evidence version/chunk rows are needed here
    con = sqlite3.connect(path)
    con.execute("PRAGMA foreign_keys=OFF")
    con.execute("INSERT INTO documents (id, evidence_version_id, source_id, doc_type, doc_text, extractor_version) "
                "VALUES (1, 'ev', 's', 'other', '', '1')")
    con.execute("INSERT INTO kag_entities (id, norm, display, kind) VALUES (1, 'acme', 'Acme', 'party')")
    con.execute("INSERT INTO kag_document_entities (document_id, entity_id, role, chunk_id) VALUES (1, 1, 'party', 'c1')")
    con.execute("INSERT INTO kag_document_clauses (document_id, clause_type, chunk_id, confidence) VALUES (1, 'insurance', 'c1', 0.6)")
    con.commit(); con.close()

    command.upgrade(cfg, "head")
    con = sqlite3.connect(path)
    assert con.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "f6c9e3a18f47"
    for t in ("kag_document_entities", "kag_document_clauses"):
        names = {r[1]: r for r in con.execute(f"PRAGMA table_info({t})")}
        for c in ("span_start", "span_end", "evidence_kind"):
            assert c in names and names[c][3] == 0 and names[c][4] is None  # nullable, no default
    assert con.execute("SELECT chunk_id, span_start, span_end, evidence_kind FROM kag_document_entities").fetchall() == [("c1", None, None, None)]
    assert con.execute("SELECT chunk_id, span_start, evidence_kind FROM kag_document_clauses").fetchall() == [("c1", None, None)]
    con.close()

    command.downgrade(cfg, "f6c9e3a18e36")
    con = sqlite3.connect(path)
    assert con.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "f6c9e3a18e36"
    for t in ("kag_document_entities", "kag_document_clauses"):
        names = {r[1] for r in con.execute(f"PRAGMA table_info({t})")}
        assert not names & {"span_start", "span_end", "evidence_kind"}
    assert con.execute("SELECT chunk_id FROM kag_document_entities").fetchall() == [("c1",)]
    assert con.execute("SELECT COUNT(*) FROM kag_document_clauses").fetchone()[0] == 1
    con.close()
    command.upgrade(cfg, "head")  # and back up again


def test_gc_graph_cli_helpers_unchanged(migrated_sqlite_engine):
    e = migrated_sqlite_engine
    _lib(e)
    with get_session_factory(e)() as s:
        assert kag.gc_orphan_entities(s, dry_run=True) == kag.GcReport(0, 0)


def test_no_state_is_promoted_to_exact_or_verified_evidence(migrated_sqlite_engine):
    """legacy / none / stale / malformed rows never expose a span, and legacy never exposes its (old,
    always-first-chunk) chunk id; a dangling or foreign-version clause chunk is 'stale'."""
    e = migrated_sqlite_engine
    _lib(e)
    assert kag._provenance("chk", 1, 5, None) == {"chunk_id": None, "span_start": None, "span_end": None, "kind": "legacy"}
    for bad in (("chk", None, None, "span"), (None, 1, 5, "span"), ("chk", 5, 5, "span"), ("chk", 1, 5, "weird")):
        p = kag._provenance(*bad)
        assert p["kind"] == "stale" and p["span_start"] is None and p["chunk_id"] is None
    with get_session_factory(e)() as s:
        cid = s.execute(text("SELECT MIN(id) FROM kag_document_clauses")).scalar_one()
        assert kag.clause_edge_provenance(s, cid)["kind"] == "chunk"
        s.execute(text("UPDATE kag_document_clauses SET chunk_id = 'chk_gone' WHERE id = :i"), {"i": cid})
        p = kag.clause_edge_provenance(s, cid)
        assert p["kind"] == "stale" and p["chunk_id"] is None and p["span_start"] is None
        s.rollback()
