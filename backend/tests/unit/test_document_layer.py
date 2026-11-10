"""Document layer: migration, builder, FTS triggers, routing, lookup, family, leaks."""
from __future__ import annotations

import time

import pytest
from alembic import command
from sqlalchemy import text

from docket.core.config import Settings
from docket.services.query.classifier import QueryMode
from docket.core.db.engine import get_engine, get_session_factory
from docket.core.db.models import EvidenceVersion, SourceStatus, VersionStatus
from docket.infra.index.base import ChunkRecord
from docket.infra.inference.gateway import FakeInferenceGateway
from docket.infra.retrieval.resolver import EvidenceResolver
from docket.services.documents import builder, family, lookup, routing
from docket.services.documents.layer import DocumentLayer
from conftest import _alembic_config
from test_hybrid_retrieval import _insert_metadata
from test_scale_step1a import _bulk_clone_versions

ZOG = ("DISTRIBUTOR AGREEMENT\nThis Distributor Agreement is made and entered into as of May 9, 2019 (the \"Effective Date\") by and between "
       "Zogenix, Inc., a Delaware corporation (\"Zogenix\"), and Sumitomo Dainippon Pharma, a Japanese company (\"Distributor\").")
ZOG2 = ("This Agreement shall be governed by and construed in accordance with the laws of the State of New York, without regard to conflict of laws. "
        "Either party may terminate this Agreement for convenience upon sixty (60) days' prior written notice. "
        "In no event shall Zogenix's aggregate liability exceed $2,500,000.")
BLM = ("MASTER MAINTENANCE AGREEMENT\nThis Agreement is entered into as of March 21, 2018 by and between Bloom Energy Corporation and Pacific Utilities LLC, "
       "whereas the parties wish to cooperate.")
BLM2 = "This Agreement shall be governed by the laws of the State of California."


def _rec(cid, vid, body, ordinal=0, source="src_a"):
    return ChunkRecord(chunk_id=cid, source_id=source, evidence_version_id=vid, evidence_unit_id=f"eu_{vid}_{ordinal}",
                       chunk_recipe_id="rcp_1", ordinal=ordinal, heading=None, text=body, content_hash="h_" + cid)


def _name(engine, vid, path, mtime=None):
    with engine.begin() as c:
        c.execute(text("UPDATE evidence_versions SET file_path=:p, source_mtime=:m WHERE id=:i"), {"p": path, "m": mtime, "i": vid})


@pytest.fixture
def lib(migrated_sqlite_engine):
    e = migrated_sqlite_engine
    _insert_metadata(e, [_rec("c_z0", "ev_z", ZOG, 0), _rec("c_z1", "ev_z", ZOG2, 1)])
    _insert_metadata(e, [_rec("c_b0", "ev_b", BLM, 0), _rec("c_b1", "ev_b", BLM2, 1)], )
    _name(e, "ev_z", "/lib/Zogenix_Distributor Agreement.pdf", 100.0)
    _name(e, "ev_b", "/lib/Bloom_Maintenance Agreement.pdf", 100.0)
    return e, get_session_factory(e)


def test_migration_up_down_up(tmp_path):
    p = tmp_path / "d.sqlite3"
    cfg = _alembic_config(p)
    command.upgrade(cfg, "head")
    eng = get_engine(p)
    with eng.connect() as c:
        names = {r[0] for r in c.execute(text("SELECT name FROM sqlite_master"))}
    assert {"documents", "document_fields", "fts_documents", "documents_fts_ai"} <= names
    eng.dispose()
    command.downgrade(cfg, "e5b8d2f07a31")
    eng = get_engine(p)
    with eng.connect() as c:
        names = {r[0] for r in c.execute(text("SELECT name FROM sqlite_master"))}
        assert not ({"documents", "document_fields", "fts_documents"} & names)
        assert c.execute(text("SELECT COUNT(*) FROM chunks")).scalar_one() == 0
    eng.dispose()
    command.upgrade(cfg, "head")


def test_build_all_idempotent_and_fields(lib):
    e, sf = lib
    s1 = builder.build_all(sf)
    assert s1.built == 2 and s1.fields > 0
    s2 = builder.build_all(sf)
    assert s2.built == 0 and s2.skipped == 2
    builder.build_all(sf, force=True)
    with e.connect() as c:
        assert c.execute(text("SELECT COUNT(*) FROM documents")).scalar_one() == 2
        assert c.execute(text("SELECT COUNT(*) FROM fts_documents")).scalar_one() == 2
        row = c.execute(text("SELECT doc_type,title,parties_json,dates_json,family_key,page_count FROM documents d JOIN evidence_versions v ON v.id=d.evidence_version_id WHERE v.id='ev_z'")).one()
        gl = c.execute(text("SELECT normalized,evidence_chunk_id,char_span,confidence FROM document_fields f JOIN documents d ON d.id=f.document_id WHERE d.evidence_version_id='ev_z' AND field='governing_law'")).one()
    assert row[0] == "agreement" and row[1] == "DISTRIBUTOR AGREEMENT" and "Zogenix" in row[2] and "2019-05-09" in row[3]
    assert gl[0] == "New York" and gl[1] == "c_z1" and gl[3] >= 0.8
    st, en = map(int, gl[2].split(":"))
    assert "New York" in ZOG2[st:en]
    assert builder.counts(sf) == (2, 2, s1.fields)


def test_supersede_revoke_purge_remove_rows(lib):
    e, sf = lib
    builder.build_all(sf)
    builder.delete_for_version(sf, "ev_z")
    with e.connect() as c:
        assert c.execute(text("SELECT COUNT(*) FROM documents")).scalar_one() == 1
        assert c.execute(text("SELECT COUNT(*) FROM fts_documents")).scalar_one() == 1
    with e.begin() as c:
        c.execute(text("UPDATE sources SET status=:s WHERE id='src_a'"), {"s": SourceStatus.REVOKED.name})
    assert builder.prune_documents(sf) == 1
    with e.connect() as c:
        assert c.execute(text("SELECT COUNT(*) FROM fts_documents")).scalar_one() == 0
        assert c.execute(text("SELECT COUNT(*) FROM document_fields")).scalar_one() == 0
    # ORM cascade path used by purge_source
    with e.begin() as c:
        c.execute(text("UPDATE sources SET status='ACTIVE' WHERE id='src_a'"))
    builder.build_all(sf)
    with sf() as s:
        for v in s.query(EvidenceVersion).all():
            s.delete(v)
        s.commit()
    with e.connect() as c:
        assert c.execute(text("SELECT COUNT(*) FROM documents")).scalar_one() == 0
        assert c.execute(text("SELECT COUNT(*) FROM fts_documents")).scalar_one() == 0
        assert c.execute(text("SELECT COUNT(*) FROM document_fields")).scalar_one() == 0


def test_routing_confident_and_fallbacks(lib):
    e, sf = lib
    builder.build_all(sf)
    d = routing.route(e, "Under the Distributor Agreement with Zogenix, Inc., which jurisdiction's law governs?")
    assert d.routed and d.top_unique and d.version_ids == ["ev_z"]
    assert not routing.route(e, "Which of the contracts in this library are governed by the law of California?").routed
    assert not routing.route(e, "what is the weather like").routed
    assert not routing.route(e, "Under the Maintenance Agreement what law governs?").routed  # generic title only
    assert not routing.route(e, "What does Initech Holdings say about payment?").routed  # no such document
    assert routing.extract_entities("Does the \"Zogenix Distributor Agreement\" restrict assignment?")[0].kind == "quoted"


def test_routing_never_returns_revoked_or_superseded(lib):
    e, sf = lib
    builder.build_all(sf)
    with e.begin() as c:
        c.execute(text("UPDATE evidence_versions SET status=:s WHERE id='ev_z'"), {"s": VersionStatus.SUPERSEDED.name})
    assert not routing.route(e, "Under the Distributor Agreement with Zogenix, Inc., which law governs?").routed
    with e.begin() as c:
        c.execute(text("UPDATE evidence_versions SET status='READY' WHERE id='ev_z'"))
        c.execute(text("UPDATE sources SET status=:s WHERE id='src_a'"), {"s": SourceStatus.REVOKED.name})
    assert not routing.route(e, "Under the Distributor Agreement with Zogenix, Inc., which law governs?").routed


def test_routing_respects_scope(lib):
    e, sf = lib
    builder.build_all(sf)
    q = "Under the Distributor Agreement with Zogenix, Inc., which law governs?"
    assert not routing.route(e, q, scope_version_ids=["ev_b"]).routed
    assert routing.route(e, q, scope_version_ids=["ev_b", "ev_z"]).routed


def test_intent_classifier():
    c = lookup.classify_intent
    assert c("Under the X Agreement with Y, which jurisdiction's law governs the agreement?") == ("governing_law", "value")
    assert c("What is the effective date of the Affiliate Agreement with Link Plus Corporation?")[0] == "effective_date"
    assert c("For how long does the Website Content License Agreement renew each time it renews?")[0] == "renewal_term"
    assert c("How much notice is required to terminate the Distributor Agreement for convenience?")[0] == "termination_notice_period"
    assert c("What dollar amount caps liability under the Hosting Agreement?")[0] == "liability_cap"
    assert c("What minimum amount of insurance coverage must a party maintain under the X?") == ("insurance", "amount")
    assert c("Does the Distributor Agreement require a party to maintain insurance? Quote the clause.") == ("insurance", "presence")
    assert c("How often may a party audit the other party's books under the Endorsement Agreement?") is None
    assert c("Summarize the Zogenix agreement") is None


class _FakeResolver:
    def __init__(self, engine):
        self._r = EvidenceResolver(get_session_factory(engine))

    def resolve_many(self, ids):
        return self._r.resolve_many(ids)


def test_lookup_answer_cited_and_falls_through(lib):
    e, sf = lib
    builder.build_all(sf)
    layer = DocumentLayer(e, sf, Settings(doc_routing="on", field_lookup="on"))
    q = "Under the Distributor Agreement with Zogenix, Inc., which jurisdiction's law governs the agreement?"
    dec = layer.route(q, None)
    got = layer.lookup_answer(q, dec, _FakeResolver(e))
    assert got is not None
    ans, ev = got
    assert "laws of New York" in ans.answer and f"[{ev.citation_label}]" in ans.answer and ev.chunk_id == "c_z1"
    assert "[" not in ans.answer.replace(f"[{ev.citation_label}]", "")
    # no value for the field (renewal not present) -> None, never an absence claim
    q2 = "For how long does the Distributor Agreement with Zogenix, Inc. renew each time it renews?"
    assert layer.lookup_answer(q2, layer.route(q2, None), _FakeResolver(e)) is None
    # flag off -> nothing
    off = DocumentLayer(e, sf, Settings())
    assert off.route(q, None) is None and off.lookup_answer(q, dec, _FakeResolver(e)) is None
    # low-confidence stored field -> None
    with e.begin() as c:
        c.execute(text("UPDATE document_fields SET confidence=0.6"))
    assert layer.lookup_answer(q, dec, _FakeResolver(e)) is None


def test_lookup_requires_unique_document(migrated_sqlite_engine):
    e = migrated_sqlite_engine
    sf = get_session_factory(e)
    _insert_metadata(e, [_rec("a0", "ev_a", ZOG, 0), _rec("a1", "ev_a", ZOG2, 1)])
    _insert_metadata(e, [_rec("b0", "ev_b2", ZOG, 0), _rec("b1", "ev_b2", ZOG2.replace("New York", "Texas"), 1)])
    _name(e, "ev_a", "/x/Zogenix Distributor Agreement.pdf", 1.0)
    _name(e, "ev_b2", "/x/Zogenix Distributor Agreement copy.pdf", 2.0)
    builder.build_all(sf)
    layer = DocumentLayer(e, sf, Settings(doc_routing="on", field_lookup="on"))
    q = "Under the Distributor Agreement with Zogenix, Inc., which jurisdiction's law governs the agreement?"
    dec = layer.route(q, None)
    assert dec.routed and not dec.top_unique and len(dec.version_ids) == 2
    assert layer.lookup_answer(q, dec, _FakeResolver(e)) is None  # two near-identical docs: never pick one


def test_restrict_scope_intersects(lib):
    e, sf = lib
    builder.build_all(sf)
    layer = DocumentLayer(e, sf, Settings(doc_routing="on"))
    dec = layer.route("Under the Distributor Agreement with Zogenix, Inc., what law?", None)
    assert layer.restrict_scope(dec, None) == ["ev_z"]
    assert layer.restrict_scope(dec, ["ev_z", "ev_b"]) == ["ev_z"]
    assert layer.restrict_scope(dec, ["ev_b"]) == ["ev_b"]  # disjoint: keep the outer scope
    assert DocumentLayer(e, sf, Settings()).restrict_scope(dec, None) is None


def test_amendment_links_and_family_latest(migrated_sqlite_engine):
    e = migrated_sqlite_engine
    sf = get_session_factory(e)
    base = "MASTER SERVICES AGREEMENT\nThis Master Services Agreement is made as of January 5, 2020 by and between Acme Widgets, Inc. and Globex Corporation, whereas"
    amend = ("AMENDMENT NO. 1 TO MASTER SERVICES AGREEMENT\nThis Amendment No. 1 to the Master Services Agreement dated as of January 5, 2020 "
             "is made by and between Acme Widgets, Inc. and Globex Corporation, whereas")
    restated = ("AMENDED AND RESTATED MASTER SERVICES AGREEMENT\nThis Amended and Restated Master Services Agreement amends and restates the "
                "Master Services Agreement dated as of January 5, 2020, by and between Acme Widgets, Inc. and Globex Corporation, whereas")
    _insert_metadata(e, [_rec("m0", "ev_m", base)])
    _insert_metadata(e, [_rec("m1", "ev_am", amend)])
    _insert_metadata(e, [_rec("m2", "ev_re", restated)])
    _name(e, "ev_m", "/c/Acme MSA.pdf", 10.0)
    _name(e, "ev_am", "/c/Acme MSA Amendment 1.pdf", 20.0)
    _name(e, "ev_re", "/c/Acme MSA restated.pdf", 30.0)
    builder.build_all(sf)
    with e.connect() as c:
        rows = {r[0]: (r[1], r[2]) for r in c.execute(text(
            "SELECT d.evidence_version_id, d.supersedes_kind, (SELECT x.evidence_version_id FROM documents x WHERE x.id=d.supersedes_document_id) FROM documents d"))}
    assert rows["ev_am"] == ("amends", "ev_m")
    assert rows["ev_re"][0] == "replaces" and rows["ev_re"][1] == "ev_m"
    assert rows["ev_m"] == (None, None)

    class Ev:
        def __init__(self, cid, vid, t):
            self.chunk_id, self.evidence_version_id, self.text = cid, vid, t
    chunks = [Ev("m0", "ev_m", "Payment terms: net 30."), Ev("m2", "ev_re", "Payment terms: net 45.")]
    dec = family.apply_latest(sf, chunks, lambda c: c.text)
    assert dec.drop_version_ids == frozenset({"ev_m"}) and "restated.pdf" in dec.note
    same = [Ev("m0", "ev_m", "Same."), Ev("m2", "ev_re", "Same.")]
    assert family.apply_latest(sf, same, lambda c: c.text).note is None
    partial = family.apply_latest(sf, [Ev("m0", "ev_m", "a"), Ev("m1", "ev_am", "b")], lambda c: c.text)
    assert not partial.drop_version_ids and "amends" in partial.note
    layer = DocumentLayer(e, sf, Settings(family_latest="on"))
    kept, note = layer.family_filter(chunks)
    assert [c.chunk_id for c in kept] == ["m2"] and note
    assert DocumentLayer(e, sf, Settings()).family_filter(chunks) == (chunks, None)


def test_family_same_mtime_not_guessed(migrated_sqlite_engine):
    e = migrated_sqlite_engine
    sf = get_session_factory(e)
    _insert_metadata(e, [_rec("p0", "ev_p", "x")])
    _insert_metadata(e, [_rec("q0", "ev_q", "y")])
    _name(e, "ev_p", "/c/Plan v1.docx", 5.0)
    _name(e, "ev_q", "/c/Plan v2.docx", 5.0)
    builder.build_all(sf)

    class Ev:
        def __init__(self, c, v):
            self.chunk_id, self.evidence_version_id, self.text = c, v, "t"
    assert not family.apply_latest(sf, [Ev("p0", "ev_p"), Ev("q0", "ev_q")], lambda c: c.text).drop_version_ids


def test_100k_versions_routing_no_leak_and_fast(migrated_sqlite_engine):
    e = migrated_sqlite_engine
    sf = get_session_factory(e)
    _insert_metadata(e, [_rec("g0", "ev_good", ZOG, 0), _rec("g1", "ev_good", ZOG2, 1)], )
    bad = {"sup": (VersionStatus.SUPERSEDED, SourceStatus.ACTIVE), "rev": (VersionStatus.READY, SourceStatus.REVOKED),
           "tomb": (VersionStatus.READY, SourceStatus.TOMBSTONED), "pend": (VersionStatus.PENDING, SourceStatus.ACTIVE)}
    for name, (vs, ss) in bad.items():
        _insert_metadata(e, [_rec(f"x_{name}", f"ev_{name}", ZOG.replace("Zogenix", "Zogenix"), 0, source=f"src_{name}")], status=ss, version_status=vs)
    _bulk_clone_versions(e, "ev_good", 100_000, "ev_fake")
    # documents for the ineligible ones exist (as if never pruned) and must stay invisible
    with e.begin() as c:
        for name in bad:
            c.execute(text("INSERT INTO documents (evidence_version_id, source_id, doc_type, title, doc_text) VALUES (:v,:s,'agreement','DISTRIBUTOR AGREEMENT',:t)"),
                      {"v": f"ev_{name}", "s": f"src_{name}", "t": ZOG})
    builder.build_for_version(sf, "ev_good")
    t0 = time.perf_counter()
    d = routing.route(e, "Under the Distributor Agreement with Zogenix, Inc., which law governs?")
    assert time.perf_counter() - t0 < 5
    assert d.routed and d.version_ids == ["ev_good"]
    assert all(c.version_id == "ev_good" for c in d.candidates)


def _service(e, settings, monkeypatch, calls, results):
    import docket.services.query.service as sm
    from docket.infra.retrieval.hybrid import RankedChunk

    def fake(**kw):
        calls.append(kw)
        out = results.pop(0)
        return [RankedChunk(chunk_id=c, score=1.0) for c in out]

    monkeypatch.setattr(sm, "hybrid_search", fake)
    return sm.QueryService(engine=e, table=object(), gateway=FakeInferenceGateway(),
                           resolver=EvidenceResolver(get_session_factory(e)), settings=settings)


def test_service_flags_off_unchanged_and_on_scopes(lib, monkeypatch):
    e, sf = lib
    builder.build_all(sf)
    q = "Under the Distributor Agreement with Zogenix, Inc., which jurisdiction's law governs the agreement?"
    calls: list = []
    off = _service(e, Settings(), monkeypatch, calls, [[]])
    off.ask(q, mode=QueryMode.FAST)
    assert "scope_version_ids" not in calls[0]
    calls.clear()
    on = _service(e, Settings(doc_routing="on"), monkeypatch, calls, [[], []])
    r = on.ask(q, mode=QueryMode.FAST)
    assert calls[0]["scope_version_ids"] == ["ev_z"]
    # an empty routed search falls back to the global search
    calls.clear()
    on2 = _service(e, Settings(doc_routing="on"), monkeypatch, calls, [[], []])
    on2.ask(q, mode=QueryMode.FAST)
    assert len(calls) == 2 and "scope_version_ids" not in calls[1]


def test_service_field_lookup_bypasses_model_and_search(lib, monkeypatch):
    e, sf = lib
    builder.build_all(sf)
    q = "Under the Distributor Agreement with Zogenix, Inc., which jurisdiction's law governs the agreement?"
    calls: list = []
    svc = _service(e, Settings(doc_routing="on", field_lookup="on"), monkeypatch, calls, [[]])
    r = svc.ask(q, mode=QueryMode.FAST)
    assert not calls and not r.abstained and "laws of New York" in r.answer
    assert r.citations and r.citations[0].chunk_id == "c_z1" and r.doc_layer["lookup"] == "governing_law"
    # a question without a matching intent goes through normal retrieval
    r2 = _service(e, Settings(doc_routing="on", field_lookup="on"), monkeypatch, calls, [[], []]).ask(
        "Summarize the Distributor Agreement with Zogenix, Inc.", mode=QueryMode.FAST)
    assert calls
