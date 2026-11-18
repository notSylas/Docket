"""R2b-1b-i: citation grant metadata (source_id / evidence_version_id /
resolved_chunk_id) derived from the authoritative resolver and proven against
the question's own scope. Synthetic scratch libraries, no model calls."""

from __future__ import annotations

import io
import json
from types import SimpleNamespace

import pytest
from test_evidence_get import Lib  # synthetic scratch library helper

from docket.core.db.models import ChunkAlias
from docket.infra.retrieval.resolver import EvidenceResolver
from docket.interfaces.worker import build_methods, serve
from docket.services.query.classifier import QueryMode
from docket.services.query.scope import ResolvedScope, ScopeKind
from docket.services.query.service import Citation, CitationGrant, QueryResult, QueryService


@pytest.fixture()
def lib(tmp_path):
    lb = Lib(tmp_path)
    yield lb
    lb.engine.dispose()


def make_service(lib: Lib) -> QueryService:
    svc = QueryService.__new__(QueryService)
    svc._resolver = EvidenceResolver(lib.factory)
    svc._engine = lib.engine
    return svc


def cite(chunk_id: str, name: str = "same.txt") -> Citation:
    return Citation(citation_label=f"[{name} #{chunk_id[:12]}]", chunk_id=chunk_id, source_display_name=name)


def scope(*ids, unrestricted=False) -> ResolvedScope:
    return ResolvedScope(
        kind=ScopeKind.EVERYTHING if unrestricted else ScopeKind.SOURCE, label="l", file_count=len(ids),
        version_ids=None if unrestricted else tuple(ids), empty=False,
    )


def two_sources(lib: Lib):
    s1, s2 = lib.source("/a"), lib.source("/b")
    v1 = lib.version(s1, file_path="/a/same.txt")
    v2 = lib.version(s2, file_path="/b/same.txt")
    return s1, s2, v1, v2, lib.chunk(s1, v1, 0, "alpha text"), lib.chunk(s2, v2, 0, "beta text")


def test_identical_file_names_get_their_own_ids(lib):
    s1, s2, v1, v2, c1, c2 = two_sources(lib)
    grants = make_service(lib)._citation_grants([cite(c1), cite(c2)], scope(v1, v2), [v1, v2])
    assert [(g.chunk_id, g.source_id, g.evidence_version_id, g.resolved_chunk_id) for g in grants] == [
        (c1, s1, v1, None), (c2, s2, v2, None)]


def test_citation_outside_boundary_gets_nothing(lib):
    s1, s2, v1, v2, c1, c2 = two_sources(lib)
    grants = make_service(lib)._citation_grants([cite(c1), cite(c2)], scope(v1), [v1])
    assert [g.chunk_id for g in grants] == [c1]


def test_outside_retrieval_scope_ids_even_if_in_boundary(lib):
    _, _, v1, v2, c1, c2 = two_sources(lib)
    grants = make_service(lib)._citation_grants([cite(c1), cite(c2)], scope(v1, v2), [v2])
    assert [g.chunk_id for g in grants] == [c2]


def test_unrestricted_requires_only_resolvable(lib):
    s1, _, v1, _, c1, _ = two_sources(lib)
    grants = make_service(lib)._citation_grants([cite(c1), cite("nope")], None, None)
    assert [(g.chunk_id, g.source_id, g.evidence_version_id) for g in grants] == [(c1, s1, v1)]
    grants = make_service(lib)._citation_grants([cite(c1)], scope(unrestricted=True), None)
    assert len(grants) == 1
    # named-file narrowing still applies without a selection
    assert make_service(lib)._citation_grants([cite(c1)], None, ["other"]) == []


def test_alias_resolved_citation_uses_new_chunk_and_version(lib):
    src = lib.source()
    v1 = lib.version(src)
    new = lib.chunk(src, v1, 0, "replacement")
    with lib.factory() as s:
        s.add(ChunkAlias(old_chunk_id="old_chunk", new_chunk_id=new, evidence_version_id=v1, overlap=0.9))
        s.commit()
    (g,) = make_service(lib)._citation_grants([cite("old_chunk")], scope(v1), [v1])
    assert (g.chunk_id, g.resolved_chunk_id, g.evidence_version_id, g.source_id) == ("old_chunk", new, v1, src)
    # the new chunk's version decides: not in scope -> omitted
    assert make_service(lib)._citation_grants([cite("old_chunk")], scope("zzz"), ["zzz"]) == []


def test_duplicates_malformed_and_oversized_omit(lib):
    _, _, v1, _, c1, c2 = two_sources(lib)
    svc = make_service(lib)
    assert svc._citation_grants([cite(c1), cite(c1)], None, None) == []
    got = svc._citation_grants([cite(c1), cite(c2), cite(c2)], None, None)
    assert [g.chunk_id for g in got] == [c1]
    assert svc._citation_grants([cite("x" * 201), cite("a\x00b")], None, None) == []


def test_resolver_failure_omits(lib):
    _, _, _, _, c1, _ = two_sources(lib)
    svc = make_service(lib)

    def boom(ids):
        raise RuntimeError("db down")

    svc._resolver = SimpleNamespace(resolve_many=boom)
    assert svc._citation_grants([cite(c1)], None, None) == []


def test_resolver_returning_empty_ids_omits(lib):
    svc = make_service(lib)
    bad = SimpleNamespace(chunk_id="c", source_id="", evidence_version_id="v", text="t")
    svc._resolver = SimpleNamespace(resolve_many=lambda ids: [bad for _ in ids])
    assert svc._citation_grants([cite("c")], None, None) == []


# -- through QueryService.ask (retrieval stubbed, no model) --------------------

def ask_with(svc, lib, result, scope_ids, boundary, monkeypatch):
    svc._classifier = SimpleNamespace(classify=lambda q: QueryMode.FAST)
    svc._settings = SimpleNamespace()

    def fake_fast(question, history=None, boundary=None, on_delta=None, scope_sink=None):
        if scope_sink is not None:
            scope_sink["ids"] = scope_ids
        return result

    svc._ask_fast = fake_fast
    svc._tier_coverage = lambda r: {}
    from docket.services.query import service as mod

    monkeypatch.setattr(mod, "resolve_scope", lambda engine, sc: boundary)
    return svc.ask("q", scope=object() if boundary is not None else None)


def result_for(*cites, abstained=False):
    return QueryResult(question="q", answer="a", citations=list(cites), abstained=abstained,
                       validation_warnings=[], mode="fast")


def test_ask_attaches_grants_for_in_scope_only(lib, monkeypatch):
    _, _, v1, v2, c1, c2 = two_sources(lib)
    out = ask_with(make_service(lib), lib, result_for(cite(c1), cite(c2)), [v1], scope(v1), monkeypatch)
    assert [g.chunk_id for g in out.evidence_grants] == [c1]
    assert [c.chunk_id for c in out.citations] == [c1, c2]  # citations untouched


def test_ask_no_scope_no_boundary(lib, monkeypatch):
    _, _, v1, _, c1, _ = two_sources(lib)
    out = ask_with(make_service(lib), lib, result_for(cite(c1)), None, None, monkeypatch)
    assert [g.evidence_version_id for g in out.evidence_grants] == [v1]


def test_ask_abstained_or_uncited_has_none(lib, monkeypatch):
    _, _, v1, _, c1, _ = two_sources(lib)
    assert ask_with(make_service(lib), lib, result_for(cite(c1), abstained=True), None, None,
                    monkeypatch).evidence_grants == []
    assert ask_with(make_service(lib), lib, result_for(), None, None, monkeypatch).evidence_grants == []


def test_ask_without_scope_sink_value_has_none(lib, monkeypatch):
    """Paths that never established the retrieval scope (e.g. abstentions) grant nothing."""
    _, _, v1, _, c1, _ = two_sources(lib)
    svc = make_service(lib)
    svc._classifier = SimpleNamespace(classify=lambda q: QueryMode.FAST)
    svc._ask_fast = lambda *a, **k: result_for(cite(c1))
    svc._tier_coverage = lambda r: {}
    assert svc.ask("q").evidence_grants == []


# -- worker query.ask + evidence.get end to end --------------------------------

def run_ask(lib, result):
    class Svc:
        def ask(self, question, **kw):
            return result

    ctx = SimpleNamespace(
        session_factory=lib.factory, resolver=EvidenceResolver(lib.factory),
        vector_writer=SimpleNamespace(table="T"),
    )
    methods = build_methods(ctx, lambda c, t: Svc())
    out = io.StringIO()
    line = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "query.ask", "params": {"question": "q"}})
    serve(methods, io.StringIO(line + "\n"), out)
    msgs = [json.loads(x) for x in out.getvalue().splitlines() if '"method": "progress"' not in x]
    return msgs[-1]["result"], methods


def test_worker_merges_additively_and_evidence_get_accepts(lib):
    s1, _, v1, v2, c1, c2 = two_sources(lib)
    src = lib.source()
    v3 = lib.version(src)
    new = lib.chunk(src, v3, 0, "replacement text")
    with lib.factory() as s:
        s.add(ChunkAlias(old_chunk_id="old_chunk", new_chunk_id=new, evidence_version_id=v3, overlap=0.9))
        s.commit()
    svc = make_service(lib)
    qr = QueryResult(
        question="q", answer="a", abstained=False, validation_warnings=[], mode="fast",
        citations=[cite(c1), cite(c2), cite("old_chunk")],
        evidence_grants=svc._citation_grants([cite(c1), cite("old_chunk")], None, None),
    )
    r, methods = run_ask(lib, qr)
    c = r["citations"]
    assert [x["chunk_id"] for x in c] == [c1, c2, "old_chunk"]
    assert set(c[0]) == {"label", "source_display_name", "chunk_id", "excerpt", "source_id", "evidence_version_id"}
    assert c[0]["source_id"] == s1 and c[0]["evidence_version_id"] == v1
    assert set(c[1]) == {"label", "source_display_name", "chunk_id", "excerpt"}  # no grant -> legacy shape
    assert c[2]["resolved_chunk_id"] == new and c[2]["evidence_version_id"] == v3

    for cit in (c[0], c[2]):
        res = methods["evidence.get"](
            {"chunk_id": cit["chunk_id"], "allowed_version_ids": [cit["evidence_version_id"]]}, lambda *a: None)
        assert res["outcome"] == "ok"
    assert methods["evidence.get"](
        {"chunk_id": "old_chunk", "allowed_version_ids": [v3]}, lambda *a: None)["passage"]["chunk_id"] == new


def test_worker_legacy_result_without_field_serialises(lib):
    _, _, _, _, c1, _ = two_sources(lib)
    legacy = SimpleNamespace(answer="a", abstained=False, citations=[cite(c1)], latency_by_phase=None)
    r, _ = run_ask(lib, legacy)
    assert set(r["citations"][0]) == {"label", "source_display_name", "chunk_id", "excerpt"}
    assert QueryResult(question="q", answer="a", citations=[], abstained=True,
                       validation_warnings=[], mode="fast").evidence_grants == []
    assert CitationGrant(chunk_id="a", source_id="b", evidence_version_id="c").resolved_chunk_id is None


def test_document_routing_never_widens_beyond_the_effective_scope():
    """Review finding: routed extra candidates are always inside the retrieval scope the grant proof uses, so a
    legitimately routed citation is grantable and routing cannot introduce an out-of-scope version."""
    from docket.services.documents.layer import DocumentLayer
    from docket.services.documents.routing import RouteDecision

    layer = DocumentLayer(None, None, SimpleNamespace(doc_routing="on", field_lookup="off", family_latest="off"))
    d = RouteDecision(routed=True, version_ids=["v2"], top_version_id="v2", top_unique=True)
    assert layer.routed_scope(d, ["v1"]) is None          # outside the scope: never added
    assert layer.routed_scope(d, ["v1", "v2"]) == ["v2"]  # inside: added, and the grant proof accepts it
    assert layer.routed_scope(d, None) == ["v2"]          # unrestricted scope
