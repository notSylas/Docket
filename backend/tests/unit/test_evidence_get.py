"""`evidence.get` worker method and `EvidenceReader` (synthetic scratch libraries)."""

from __future__ import annotations

import hashlib
import io
import json
import logging
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import event

from docket.core.db.engine import get_engine, get_session_factory
from docket.core.db.identity import compute_chunk_id, compute_recipe_id
from docket.core.db.models import (
    AuthorizedSource,
    Base,
    Chunk,
    ChunkAlias,
    ChunkRecipe,
    EvidenceUnit,
    EvidenceVersion,
    Source,
    SourceStatus,
    VersionStatus,
    Workspace,
)
from docket.infra.retrieval import evidence_read
from docket.interfaces import worker
from docket.interfaces.worker import FEATURES, build_methods, handle_line, serve

SECRET = "ZX-SECRET-9f3a71"


class Lib:
    def __init__(self, tmp_path: Path):
        self.db = tmp_path / "lib.sqlite3"
        self.engine = get_engine(self.db)
        Base.metadata.create_all(self.engine)
        self.factory = get_session_factory(self.engine)
        with self.factory() as s:
            ws = Workspace(name="w")
            s.add(ws)
            s.flush()
            auth = AuthorizedSource(workspace_id=ws.id, scope_path="/a")
            s.add(auth)
            s.flush()
            self.ws, self.auth = ws.id, auth.id
            s.add(ChunkRecipe(id=compute_recipe_id(chunk_size=1, overlap=0, splitter="w",
                                                   parser_name="p", parser_version="1"),
                              chunk_size=1, overlap=0, splitter="w", parser_name="p", parser_version="1"))
            s.commit()
        self.recipe = compute_recipe_id(chunk_size=1, overlap=0, splitter="w", parser_name="p", parser_version="1")
        self.n = 0

    def source(self, path="/home/u/docs", status=SourceStatus.ACTIVE) -> str:
        with self.factory() as s:
            src = Source(workspace_id=self.ws, authorized_source_id=self.auth, source_type="local_folder",
                         path=path, status=status)
            s.add(src)
            s.commit()
            return src.id

    def version(self, source_id, status=VersionStatus.READY, file_path=None) -> str:
        self.n += 1
        with self.factory() as s:
            ev = EvidenceVersion(source_id=source_id, file_path=file_path, content_hash=f"{self.n:064d}",
                                 byte_size=1, observed_at=datetime.now(timezone.utc), parser_name="p",
                                 parser_version="1", status=status)
            s.add(ev)
            s.commit()
            return ev.id

    def chunk(self, source_id, version_id, ordinal, text, **kw) -> str:
        with self.factory() as s:
            unit = EvidenceUnit(evidence_version_id=version_id, unit_index=ordinal, heading=kw.get("heading"),
                                content_hash="u" * 64, locator_json=kw.pop("locator", None))
            s.add(unit)
            s.flush()
            cid = compute_chunk_id(version_id, self.recipe, ordinal, hashlib.sha256(text.encode()).hexdigest())
            s.add(Chunk(id=cid, source_id=source_id, evidence_version_id=version_id, evidence_unit_id=unit.id,
                        chunk_recipe_id=self.recipe, ordinal=ordinal, text=text,
                        content_hash=hashlib.sha256(text.encode()).hexdigest(), **kw))
            s.commit()
            return cid

    def set_status(self, model, ident, **vals):
        with self.factory() as s:
            row = s.get(model, ident)
            for k, v in vals.items():
                setattr(row, k, v)
            s.commit()

    def dump_hash(self) -> str:
        con = sqlite3.connect(self.db)
        try:
            return hashlib.sha256("\n".join(con.iterdump()).encode()).hexdigest()
        finally:
            con.close()

    def methods(self):
        return build_methods(SimpleNamespace(session_factory=self.factory), lambda c, t: None)

    def call(self, params, raw=False):
        out: list[dict] = []
        line = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "evidence.get", "params": params})
        handle_line(line, self.methods(), out.append)
        (msg,) = out
        return json.dumps(msg, sort_keys=True) if raw else msg


@pytest.fixture()
def lib(tmp_path):
    lb = Lib(tmp_path)
    yield lb
    lb.engine.dispose()


def ok(lib, chunk_id, versions, **kw):
    msg = lib.call({"chunk_id": chunk_id, "allowed_version_ids": versions, **kw})
    assert "error" not in msg, msg
    return msg["result"]


def invalid(lib, params):
    msg = lib.call(params)
    assert msg["error"]["code"] == worker.INVALID_PARAMS
    assert msg["error"]["message"] == "invalid evidence.get params"
    return msg


def seed(lib, texts=("zero", "one", "two", "three", "four"), **vkw):
    src = lib.source()
    ver = lib.version(src, **vkw)
    ids = [lib.chunk(src, ver, i, t) for i, t in enumerate(texts)]
    return src, ver, ids


def test_exact_passage_and_defaults(lib):
    src, ver, ids = seed(lib)
    r = ok(lib, ids[2], [ver])
    p = r["passage"]
    assert (p["chunk_id"], p["text"], p["ordinal"], p["source_id"], p["evidence_version_id"]) == (ids[2], "two", 2, src, ver)
    assert p["truncated"] is False and p["provisional"] is False
    assert r["outcome"] == "ok" and r["untrusted_content"] is True and "untrusted" in r["notice"]
    assert [c["ordinal"] for c in r["context_before"]] == [1]
    assert [c["ordinal"] for c in r["context_after"]] == [3]
    assert r["context_truncated"] is False
    assert "verified" not in json.dumps(r).lower()


def test_missing_metadata_is_omitted_not_null(lib):
    _, ver, ids = seed(lib, texts=("only",))
    p = ok(lib, ids[0], [ver])["passage"]
    for key in ("heading", "location", "page_start", "page_end", "replaced_from"):
        assert key not in p
    assert None not in p.values()


def test_present_metadata(lib):
    src = lib.source()
    ver = lib.version(src)
    cid = lib.chunk(src, ver, 0, "t", heading="Intro", page_start=2, page_end=3, provisional=True)
    p = ok(lib, cid, [ver])["passage"]
    assert p["heading"] == "Intro" and p["page_start"] == 2 and p["page_end"] == 3 and p["provisional"] is True
    assert p["location"] == "Section: Intro (pages 2-3)"


def test_not_in_allowed_equals_nonexistent_byte_identical(lib):
    src = lib.source()
    v1, v2 = lib.version(src), lib.version(src)
    c1 = lib.chunk(src, v1, 0, "alpha")
    c2 = lib.chunk(src, v2, 0, "beta")
    outside = lib.call({"chunk_id": c2, "allowed_version_ids": [v1]}, raw=True)
    missing = lib.call({"chunk_id": "chk_nope", "allowed_version_ids": [v1]}, raw=True)
    assert outside == missing
    assert json.loads(outside)["result"] == {"outcome": "not_found"}
    assert ok(lib, c1, [v1])["outcome"] == "ok"
    # Not-found for a lifecycle-broken chunk outside the set also leaks nothing.
    lib.set_status(Source, src, status=SourceStatus.DELETED)
    assert lib.call({"chunk_id": c2, "allowed_version_ids": [v1 + "x"]}, raw=True) == missing


def test_chunk_outside_set_is_never_loaded(lib):
    src = lib.source()
    v1, v2 = lib.version(src), lib.version(src)
    c2 = lib.chunk(src, v2, 0, "beta")
    seen: list[str] = []

    @event.listens_for(lib.engine, "before_cursor_execute")
    def grab(conn, cur, stmt, params, ctx, many):
        seen.append(stmt)

    ok(lib, c2, [v1])
    assert seen and all("evidence_version_id IN" in s for s in seen if "FROM chunks" in s and "chunk_aliases" not in s)


def test_lifecycle_outcomes_inside_allowed(lib):
    src, ver, ids = seed(lib)
    lib.set_status(Source, src, status=SourceStatus.TOMBSTONED)
    assert ok(lib, ids[1], [ver]) == {"outcome": "source_unavailable"}
    lib.set_status(Source, src, status=SourceStatus.DELETED)
    assert ok(lib, ids[1], [ver]) == {"outcome": "source_unavailable"}
    lib.set_status(Source, src, status=SourceStatus.ACTIVE)
    lib.set_status(EvidenceVersion, ver, status=VersionStatus.SUPERSEDED)
    assert ok(lib, ids[1], [ver]) == {"outcome": "superseded"}
    for st in (VersionStatus.PENDING, VersionStatus.FAILED):
        lib.set_status(EvidenceVersion, ver, status=st)
        assert ok(lib, ids[1], [ver]) == {"outcome": "not_found"}
    lib.set_status(EvidenceVersion, ver, status=VersionStatus.TEXT_READY)
    assert ok(lib, ids[1], [ver])["outcome"] == "ok"
    with lib.factory() as s:
        s.delete(s.get(Chunk, ids[1]))
        s.commit()
    assert ok(lib, ids[1], [ver]) == {"outcome": "not_found"}


def test_unknown_params_such_as_scope_rejected(lib):
    _, ver, ids = seed(lib)
    invalid(lib, {"chunk_id": ids[0], "allowed_version_ids": [ver], "scope": "src_x"})
    invalid(lib, {"chunk_id": ids[0], "allowed_version_ids": [ver], "folder": "/x"})


def test_alias_inside_and_outside_allowed(lib):
    src = lib.source()
    v1, v2 = lib.version(src), lib.version(src)
    new_in = lib.chunk(src, v1, 0, "new in")
    new_out = lib.chunk(src, v2, 0, "new out")
    with lib.factory() as s:
        s.add_all([ChunkAlias(old_chunk_id="old_in", new_chunk_id=new_in, evidence_version_id=v1, overlap=0.9),
                   ChunkAlias(old_chunk_id="old_out", new_chunk_id=new_out, evidence_version_id=v1, overlap=0.9),
                   ChunkAlias(old_chunk_id="old_null", new_chunk_id=None, evidence_version_id=v1, overlap=0.0)])
        s.commit()
    r = ok(lib, "old_in", [v1])
    assert r["passage"]["chunk_id"] == new_in and r["passage"]["replaced_from"] == "old_in"
    assert lib.call({"chunk_id": "old_out", "allowed_version_ids": [v1]}, raw=True) == \
        lib.call({"chunk_id": "unknown", "allowed_version_ids": [v1]}, raw=True)
    assert ok(lib, "old_null", [v1]) == {"outcome": "not_found"}
    # alias target no longer servable -> not_found
    lib.set_status(EvidenceVersion, v1, status=VersionStatus.SUPERSEDED)
    assert ok(lib, "old_in", [v1]) == {"outcome": "not_found"}


def test_neighbours_same_version_and_windows(lib):
    src = lib.source()
    v1, v2 = lib.version(src), lib.version(src)
    ids = [lib.chunk(src, v1, i, f"a{i}") for i in range(9)]
    for i in range(9):
        lib.chunk(src, v2, i, f"b{i}")
    r = ok(lib, ids[4], [v1, v2])
    texts = [c["text"] for c in r["context_before"] + r["context_after"]]
    assert texts == ["a3", "a5"]
    r = ok(lib, ids[4], [v1], before=3, after=3)
    assert [c["text"] for c in r["context_before"]] == ["a1", "a2", "a3"]
    assert [c["text"] for c in r["context_after"]] == ["a5", "a6", "a7"]
    assert all(c["evidence_version_id"] == v1 for c in r["context_before"] + r["context_after"])
    r = ok(lib, ids[4], [v1], before=0, after=0)
    assert r["context_before"] == [] and r["context_after"] == []
    r = ok(lib, ids[0], [v1], before=3, after=0)
    assert r["context_before"] == []
    for bad in (4, -1, True, 1.0, "1"):
        invalid(lib, {"chunk_id": ids[4], "allowed_version_ids": [v1], "before": bad})
        invalid(lib, {"chunk_id": ids[4], "allowed_version_ids": [v1], "after": bad})


def test_neighbour_gaps_omitted(lib):
    src = lib.source()
    ver = lib.version(src)
    cid = lib.chunk(src, ver, 5, "mid")
    lib.chunk(src, ver, 3, "far")
    lib.chunk(src, ver, 6, "next")
    r = ok(lib, cid, [ver], before=3, after=3)
    assert [c["ordinal"] for c in r["context_before"]] == [3]
    assert [c["ordinal"] for c in r["context_after"]] == [6]


def test_neighbours_inactive_source_not_cross_source(lib):
    s1, s2 = lib.source(), lib.source()
    v1, v2 = lib.version(s1), lib.version(s2)
    c = lib.chunk(s1, v1, 1, "x")
    lib.chunk(s2, v2, 2, "other")
    assert ok(lib, c, [v1, v2])["context_after"] == []


def test_text_limits(lib):
    src = lib.source()
    ver = lib.version(src)
    cid = lib.chunk(src, ver, 1, ("word " * 2000))
    lib.chunk(src, ver, 0, "n" * 5000)
    lib.chunk(src, ver, 2, "m" * 5000)
    r = ok(lib, cid, [ver])
    assert len(r["passage"]["text"]) <= evidence_read.MAX_PASSAGE_CHARS and r["passage"]["truncated"] is True
    assert r["passage"]["text"].endswith("word")
    for c in r["context_before"] + r["context_after"]:
        assert len(c["text"]) <= evidence_read.MAX_NEIGHBOUR_CHARS and c["truncated"] is True
    assert r["context_truncated"] is True
    short = lib.chunk(src, ver, 9, "short")
    assert ok(lib, short, [ver])["passage"]["truncated"] is False


def test_total_budget_and_size_bound(lib, monkeypatch):
    src = lib.source()
    ver = lib.version(src)
    ids = [lib.chunk(src, ver, i, chr(97 + i) * 1500) for i in range(7)]
    monkeypatch.setattr(evidence_read, "MAX_TOTAL_TEXT_CHARS", 4000)
    r = ok(lib, ids[3], [ver], before=3, after=3)
    ctx = r["context_before"] + r["context_after"]
    assert len(r["passage"]["text"]) + sum(len(c["text"]) for c in ctx) <= 4000
    assert r["passage"]["text"] == "d" * 1500  # cited passage has priority
    assert r["context_truncated"] is True
    # nearest neighbours survive
    assert {c["ordinal"] for c in ctx} >= {2, 4}
    monkeypatch.setattr(evidence_read, "MAX_TOTAL_TEXT_CHARS", 16000)
    monkeypatch.setattr(evidence_read, "MAX_RESULT_BYTES", 3000)
    r = ok(lib, ids[3], [ver], before=3, after=3)
    assert len(json.dumps(r, ensure_ascii=False, separators=(",", ":")).encode()) <= 3000
    assert r["context_truncated"] is True and r["passage"]["text"]


def test_default_limits_fit_budget():
    assert evidence_read.MAX_PASSAGE_CHARS == 4000 and evidence_read.MAX_NEIGHBOUR_CHARS == 2000
    assert evidence_read.MAX_TOTAL_TEXT_CHARS == 16000 and evidence_read.MAX_CONTEXT == 3
    assert evidence_read.EXCERPT_CHARS == worker.EXCERPT_CHARS


def test_excerpt_consistency(lib):
    text = "Crane   day rate\n2,650 " + "filler " * 200
    _, ver, ids = seed(lib, texts=(text,))
    norm = " ".join(text.split())[:400]
    assert ok(lib, ids[0], [ver], answer_excerpt=norm)["excerpt_matches_current_text"] is True
    assert ok(lib, ids[0], [ver], answer_excerpt="Crane day rate 2,650 changed")["excerpt_matches_current_text"] is False
    assert ok(lib, ids[0], [ver])["excerpt_matches_current_text"] is None
    assert ok(lib, ids[0], [ver], answer_excerpt=None)["excerpt_matches_current_text"] is None


def test_malformed_requests(lib):
    _, ver, ids = seed(lib)
    good = {"chunk_id": ids[0], "allowed_version_ids": [ver]}
    bad = [
        {}, {"allowed_version_ids": [ver]}, {"chunk_id": ids[0]},
        {**good, "chunk_id": ""}, {**good, "chunk_id": 5}, {**good, "chunk_id": None},
        {**good, "chunk_id": "a\x00b"}, {**good, "chunk_id": "x" * 201},
        {**good, "allowed_version_ids": []}, {**good, "allowed_version_ids": "v"},
        {**good, "allowed_version_ids": [1]}, {**good, "allowed_version_ids": [""]},
        {**good, "allowed_version_ids": ["v" * 201]}, {**good, "allowed_version_ids": ["a\x00"]},
        {**good, "allowed_version_ids": [f"v{i}" for i in range(10001)]},
        {**good, "answer_excerpt": 5}, {**good, "answer_excerpt": "x" * 1001},
        {**good, "extra": 1},
    ]
    for params in bad:
        invalid(lib, params)
    assert ok(lib, "x" * 200, ["v" * 200])["outcome"] == "not_found"
    assert ok(lib, ids[0], [f"v{i}" for i in range(9999)] + [ver])["outcome"] == "ok"
    msg = invalid(lib, {**good, "chunk_id": "LEAK-" + SECRET + "\x00"})
    assert SECRET not in json.dumps(msg)
    out: list[dict] = []
    handle_line(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "evidence.get", "params": [1]}), lib.methods(), out.append)
    assert out[0]["error"]["code"] == worker.INVALID_PARAMS


def test_display_name_is_basename_only(lib):
    src = lib.source(path="/home/secretuser/private/docs")
    v_file = lib.version(src, file_path="/home/secretuser/private/docs/sub/report.pdf")
    v_win = lib.version(src, file_path="C:\\Users\\x\\a b.docx")
    v_src = lib.version(src)
    c1 = lib.chunk(src, v_file, 0, "t", heading="H",
                   locator=json.dumps({"line_start": 1, "line_end": 2}))
    c2 = lib.chunk(src, v_win, 0, "t")
    c3 = lib.chunk(src, v_src, 0, "t")
    r1 = ok(lib, c1, [v_file])
    assert r1["passage"]["source_display_name"] == "report.pdf"
    assert r1["passage"]["location"].startswith("Location: report.pdf")
    assert ok(lib, c2, [v_win])["passage"]["source_display_name"] == "a b.docx"
    assert ok(lib, c3, [v_src])["passage"]["source_display_name"] == "docs"
    blob = json.dumps([r1, ok(lib, c2, [v_win]), ok(lib, c3, [v_src])])
    assert "secretuser" not in blob and "/home" not in blob and "Users" not in blob
    assert "/" not in r1["passage"]["source_display_name"]


def _all_output(capsys, caplog):
    cap = capsys.readouterr()
    return cap.out + cap.err + "\n".join(r.getMessage() for r in caplog.records)


def test_no_content_in_logs(lib, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    src = lib.source(path=f"/x/{SECRET}-dir")
    ver = lib.version(src, file_path=f"/x/{SECRET}-file.txt")
    cid = lib.chunk(src, ver, 0, f"text {SECRET}", heading=f"head {SECRET}")
    out = io.StringIO()
    lines = [json.dumps({"jsonrpc": "2.0", "id": i, "method": "evidence.get", "params": p}) for i, p in enumerate([
        {"chunk_id": cid, "allowed_version_ids": [ver], "answer_excerpt": SECRET},
        {"chunk_id": SECRET, "allowed_version_ids": [ver], "answer_excerpt": SECRET},
        {"chunk_id": cid, "allowed_version_ids": [SECRET], "before": SECRET},
    ])]
    serve(lib.methods(), io.StringIO("\n".join(lines) + "\n"), out)
    emitted = _all_output(capsys, caplog)
    assert SECRET not in emitted
    # error path: reader blows up; message must be generic and nothing logged
    boom = SimpleNamespace(session_factory=lambda: (_ for _ in ()).throw(RuntimeError(f"db {SECRET} {cid}")))
    methods = build_methods(boom, lambda c, t: None)
    res: list[dict] = []
    handle_line(lines[0], methods, res.append)
    assert res[0]["error"] == {"code": worker.SERVER_ERROR, "message": "evidence read failed",
                               "data": {"kind": "EvidenceReadFailed"}}
    assert SECRET not in json.dumps(res) and SECRET not in _all_output(capsys, caplog)
    noattr: list[dict] = []
    handle_line(lines[0], build_methods(SimpleNamespace(), lambda c, t: None), noattr.append)
    assert noattr[0]["error"]["message"] == "evidence read failed"
    assert SECRET not in _all_output(capsys, caplog)


def test_read_only_no_writes(lib):
    src, ver, ids = seed(lib)
    with lib.factory() as s:
        s.add(ChunkAlias(old_chunk_id="old", new_chunk_id=ids[1], evidence_version_id=ver, overlap=1.0))
        s.commit()
    events: list[str] = []
    for name in ("before_commit", "before_flush", "after_flush"):
        event.listen(lib.factory, name, lambda *a, _n=name: events.append(_n))

    def only_selects(orm_execute_state):
        events.append("select" if orm_execute_state.is_select else "NON-SELECT")

    event.listen(lib.factory, "do_orm_execute", only_selects)
    stmts: list[str] = []
    event.listen(lib.engine, "before_cursor_execute", lambda c, cur, st, p, x, m: stmts.append(st.lstrip().upper()))
    before = lib.dump_hash()
    ok(lib, ids[2], [ver], before=3, after=3)
    ok(lib, "old", [ver])
    ok(lib, "missing", [ver])
    assert lib.dump_hash() == before
    assert "NON-SELECT" not in events and not {"before_commit", "before_flush", "after_flush"} & set(events)
    assert stmts and all(st.startswith(("SELECT", "BEGIN")) or "PRAGMA" in st for st in stmts)


def test_reader_import_is_light():
    code = ("import sys; import docket.infra.retrieval.evidence_read; "
            "bad=[m for m in sys.modules if m.startswith('docket.infra.index') or 'gateway' in m "
            "or m.startswith(('lancedb','ollama','docling'))]; print(bad)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]", out.stdout


def test_old_worker_compat_and_features():
    ctx = SimpleNamespace()
    ping = build_methods(ctx, lambda c, t: None)["ping"]({}, lambda *a: None)
    assert ping["pong"] is True and ping["protocol"] == 1
    assert set(ping) == {"pong", "protocol", "uptime_s", "features"}
    assert ping["features"][:3] == ["health.v1", "sources.counts.v1", "ask.meta.v1"]
    assert ping["features"] == FEATURES and "evidence.v1" in ping["features"]
    assert set(build_methods(ctx, lambda c, t: None)) == {
        "ping", "status", "health", "sources.list", "query.ask", "evidence.get"}
    # An old worker lacks the method: handle_line answers METHOD_NOT_FOUND.
    # The daemon decides `unsupported` from ping features, not from this error.
    old = {k: v for k, v in build_methods(ctx, lambda c, t: None).items() if k != "evidence.get"}
    out: list[dict] = []
    handle_line(json.dumps({"jsonrpc": "2.0", "id": 3, "method": "evidence.get", "params": {}}), old, out.append)
    assert out[0]["error"]["code"] == worker.METHOD_NOT_FOUND


@pytest.mark.parametrize("glyph", ["\U0001F600", "\x01", "é", "‮"])
def test_worst_case_unicode_and_control_characters_stay_within_wire_bound(lib, glyph):
    """Limits are in code points and the serialized result (worker writes ensure_ascii=False) stays bounded
    even for 4-byte glyphs and control characters that JSON escapes to 6 bytes each."""
    src = lib.source()
    ver = lib.version(src)
    ids = [lib.chunk(src, ver, i, glyph * 6000, heading=glyph * 900) for i in range(7)]
    r = ok(lib, ids[3], [ver], before=3, after=3)
    ctx = r["context_before"] + r["context_after"]
    assert len(r["passage"]["text"]) <= evidence_read.MAX_PASSAGE_CHARS
    assert all(len(c["text"]) <= evidence_read.MAX_NEIGHBOUR_CHARS for c in ctx)
    assert len(r["passage"]["text"]) + sum(len(c["text"]) for c in ctx) <= evidence_read.MAX_TOTAL_TEXT_CHARS
    wire = json.dumps({"jsonrpc": "2.0", "id": 1, "result": r}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    assert len(wire) <= evidence_read.MAX_RESULT_BYTES + 256
    json.loads(wire)  # still valid JSON
