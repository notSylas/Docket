"""R2a status service: accounting units, readiness, read-only probes, privacy,
closed code sets and ask metadata. CPU only: no Ollama, no model, no network
beyond a loopback fake HTTP server."""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import pytest

from docket.core.db.engine import get_session_factory
from docket.core.writer_lock import LOCK_FILENAME
from docket.services import status as st
from docket.services.sources.manager import SourceManager

SECRET = "SECRET-TOKEN-9917"
PRIVATE_PATH = "/home/alice/private/payroll-2024.xlsx"


@pytest.fixture
def lib(migrated_sqlite_engine, tmp_path):
    """(db_path, data_dir, source_id, raw writer connection)."""
    sf = get_session_factory(migrated_sqlite_engine)
    folder = tmp_path / "docs"
    folder.mkdir()
    sid = SourceManager(sf).register_source(folder).id
    db = tmp_path / "docket.sqlite3"
    conn = sqlite3.connect(db)
    yield SimpleNamespace(db=db, data_dir=tmp_path, sid=sid, conn=conn, n=0)
    conn.close()


def version(lib, status, chunks=0, sid=None, provisional=False):
    lib.n += 1
    vid = f"ev_{lib.n}"
    sid = sid or lib.sid
    lib.conn.execute(
        "INSERT INTO evidence_versions (id, source_id, file_path, content_hash, byte_size, observed_at, parser_name,"
        " parser_version, status) VALUES (?,?,?,?,?,?,?,?,?)",
        (vid, sid, f"/f{lib.n}", f"h{lib.n}", 1, "2026-01-01 00:00:00", "p", "1", status))
    for i in range(chunks):
        lib.conn.execute(
            "INSERT INTO chunks (id, source_id, evidence_version_id, evidence_unit_id, chunk_recipe_id, ordinal, text,"
            " content_hash, provenance, provisional) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (f"{vid}_c{i}", sid, vid, "u", "r", i, "t", f"ch{vid}{i}", "extracted", int(provisional)))
    lib.conn.commit()
    return vid


def job(lib, status="SUCCEEDED", skipped_counts=None, rows=(), sid=None, started="2026-01-01 00:00:00"):
    lib.n += 1
    jid = f"job_{lib.n}"
    stats = {} if skipped_counts is None else {"skipped_counts": skipped_counts}
    lib.conn.execute("INSERT INTO ingestion_jobs (id, source_id, status, started_at, stats_json) VALUES (?,?,?,?,?)",
                     (jid, sid or lib.sid, status, started, json.dumps(stats)))
    for i, (outcome, reason) in enumerate(rows):
        lib.conn.execute(
            "INSERT INTO ingestion_file_results (id, job_id, path, outcome, chunks_written, failure_reason)"
            " VALUES (?,?,?,?,0,?)", (f"fr_{jid}_{i}", jid, f"/p/{i}", outcome, reason))
    lib.conn.commit()
    return jid


def queue(lib, state, tier=1, n=1):
    for _ in range(n):
        lib.n += 1
        lib.conn.execute(
            "INSERT INTO ingest_queue (id, source_id, file_path, tier, state, prio_class, mtime, recency_day,"
            " byte_size, attempts, not_before, enqueued_at) VALUES (?,?,?,?,?,2,0,0,0,0,0,0)",
            (f"q{lib.n}", lib.sid, f"/q{lib.n}", tier, state))
    lib.conn.commit()


def report(lib, sid=None, status="active"):
    return st.source_reports(lib.db, lib.data_dir, [(sid or lib.sid, status)])[sid or lib.sid]


# -- readiness ---------------------------------------------------------------

def test_zero_result_indexing_is_never_ready(lib):
    for _ in range(3):
        version(lib, "PENDING")
    r = report(lib)
    assert r["readiness"] == "indexing"
    assert r["counts"]["versions"]["ready"] == 0 and r["counts"]["versions"]["pending"] == 3
    queue(lib, "queued", n=2)
    assert report(lib)["readiness"] == "indexing"


def test_ready_source_and_reconciliation(lib):
    for _ in range(4):
        version(lib, "READY", chunks=2)
    r = report(lib)
    assert r["readiness"] == "ready" and r["anomalies"] == []
    v = r["counts"]["versions"]
    assert v["unit"] == "current_version_per_file" and v["total"] == 4 and v["ready"] == 4 and v["reconciles"] is True
    assert r["counts"]["snapshot_ms"] > 0


def test_unsupported_formats_reconcile_in_job_block(lib):
    version(lib, "READY", chunks=1)
    skip = st._skips.reason_text("unsupported_type")
    job(lib, rows=[("ingested", None), ("skipped", skip), ("skipped", skip), ("skipped", st._skips.reason_text("too_large"))],
        skipped_counts={"unsupported_other": 7, "ignored": 2})
    j = report(lib)["counts"]["job"]
    assert j["known"] and j["unit"] == "ingestion_file_result_of_one_job"
    assert j["skipped_by_code"] == {"unsupported_type": 2, "too_large": 1}
    assert j["counted_only"] == {"unsupported_other": 7, "ignored": 2}
    assert j["files"] == 4 and sum(j["outcomes"].values()) == 4 and j["reconciles"] is True
    assert j["status"] == "SUCCEEDED" and j["job_id"].startswith("job_")


def test_partial_ingestion_with_failed_files(lib):
    version(lib, "READY", chunks=1)
    version(lib, "FAILED")
    job(lib, status="PARTIAL", rows=[("ingested", None), ("failed", "Password-protected: remove it and retry")],
        skipped_counts={})
    r = report(lib)
    assert r["readiness"] == "partial"
    assert {"failed_versions", "job_partial"} <= set(r["anomalies"])
    assert r["counts"]["job"]["failed_by_code"] == {"password_protected": 1}
    assert r["counts"]["versions"]["reconciles"] is True


def test_unknown_outcome_breaks_reconciliation(lib):
    job(lib, rows=[("ingested", None), ("weird", None)], skipped_counts={})
    j = report(lib)["counts"]["job"]
    assert j["outcomes"]["other"] == 1 and j["reconciles"] is False


def test_empty_ready_is_excluded_and_flagged(lib):
    version(lib, "READY", chunks=3)
    version(lib, "READY", chunks=0)
    r = report(lib)
    v = r["counts"]["versions"]
    assert v["ready"] == 1 and v["empty_ready"] == 1 and v["reconciles"] is True
    assert r["readiness"] == "partial" and "empty_ready" in r["anomalies"]


def test_only_empty_ready_is_not_silently_usable(lib):
    version(lib, "READY", chunks=0)
    r = report(lib)
    assert r["readiness"] == "partial" and r["counts"]["versions"]["ready"] == 0


def test_superseded_separated_and_inactive_sources(lib):
    version(lib, "READY", chunks=1)
    version(lib, "SUPERSEDED", chunks=1)
    version(lib, "SUPERSEDED")
    r = report(lib)
    assert r["counts"]["versions"]["total"] == 1 and r["counts"]["history"]["superseded"] == 2
    assert r["readiness"] == "ready"
    lib.conn.execute("UPDATE sources SET status='TOMBSTONED' WHERE id=?", (lib.sid,))
    lib.conn.commit()
    gone = report(lib, status="tombstoned")
    assert gone["readiness"] == "unavailable"
    assert gone["counts"]["versions"] == {"known": False, "reason": "source_inactive"}
    assert gone["counts"]["history"]["superseded"] == 2


def test_tiered_only_source_has_unknown_not_zero_skips(lib):
    version(lib, "TEXT_READY", chunks=1, provisional=True)
    queue(lib, "queued", tier=2)
    job(lib, rows=[], skipped_counts=None)  # a path-level job: no skipped_counts
    r = report(lib)
    assert r["counts"]["job"] == {"known": False, "reason": "no_whole_source_job"}
    assert "skipped_by_code" not in r["counts"]["job"]
    assert r["counts"]["basis"]["skipped_by_code"] == "unknown"
    assert r["readiness"] == "indexing" and r["counts"]["versions"]["keyword_only"] == 1


def test_interrupted_job_is_detected_with_free_writer_lock(lib):
    version(lib, "READY", chunks=1)
    job(lib, status="RUNNING", rows=[("running", None)], skipped_counts={})
    r = report(lib)
    assert r["counts"]["job"]["interrupted"] is True and "interrupted_job" in r["anomalies"]
    assert r["readiness"] == "partial"
    fd = os.open(lib.data_dir / LOCK_FILENAME, os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        live = report(lib)
        assert live["counts"]["job"]["interrupted"] is False and live["readiness"] == "indexing"
        assert st.writer_lock_state(lib.data_dir) == "held"
    finally:
        os.close(fd)
    assert st.writer_lock_state(lib.data_dir) == "free"


def test_lock_probe_failure_leaves_interrupted_unknown(lib, monkeypatch):
    job(lib, status="RUNNING", skipped_counts={})
    monkeypatch.setattr(st, "writer_lock_state", lambda _d: "unknown")
    j = report(lib)["counts"]["job"]
    assert j["interrupted"] is None


def test_failed_when_nothing_usable_and_empty_when_nothing_exists(lib):
    assert report(lib)["readiness"] == "empty"
    version(lib, "FAILED")
    assert report(lib)["readiness"] == "failed"


def test_dead_letters_flag_partial(lib):
    version(lib, "READY", chunks=1)
    queue(lib, "dead", tier=2)
    r = report(lib)
    assert r["readiness"] == "partial" and "dead_letters" in r["anomalies"] and r["counts"]["queue"]["dead"] == 1


def test_unreadable_database_reports_unavailable_code(tmp_path):
    out = st.source_reports(tmp_path / "nope.sqlite3", tmp_path, [("s", "active")])["s"]
    assert out["readiness"] == "unavailable" and out["counts"]["versions"] == {"known": False, "reason": "db_missing"}


# -- read-only guarantees ----------------------------------------------------

def test_probes_do_not_write_or_migrate(lib):
    version(lib, "READY", chunks=1)
    lib.conn.close()
    before_rev = sqlite3.connect(lib.db).execute("SELECT version_num FROM alembic_version").fetchone()
    before_bytes = lib.db.read_bytes()
    before_ver = sqlite3.connect(lib.db).execute("PRAGMA data_version").fetchone()
    report(lib)
    st.indexing_section(lib.db, lib.data_dir)
    st.database_section(lib.db)
    assert sqlite3.connect(lib.db).execute("SELECT version_num FROM alembic_version").fetchone() == before_rev
    assert lib.db.read_bytes() == before_bytes
    assert sqlite3.connect(lib.db).execute("PRAGMA data_version").fetchone() == before_ver
    lib.conn = sqlite3.connect(lib.db)


def test_readonly_connection_rejects_writes(lib):
    with st.open_readonly(lib.db) as ro:
        with pytest.raises(sqlite3.Error):
            ro.execute("DELETE FROM sources")


def test_database_section_states(lib, tmp_path):
    s = st.database_section(lib.db)
    assert s["readable"] == "yes" and s["state"] in ("ok", "degraded") and s["code"] in st.DB_CODES
    assert st.database_section(tmp_path / "missing.db")["code"] == "db_missing"
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"not a database at all" * 50)
    s = st.database_section(bad)
    assert s["state"] == "unavailable" and s["code"] == "db_unreadable"
    lib.conn.execute("UPDATE alembic_version SET version_num='0001_old'")
    lib.conn.commit()
    s = st.database_section(lib.db)
    assert s["code"] == "schema_behind" and s["at_head"] == "no" and s["state"] == "degraded"


def test_indexing_section(lib):
    version(lib, "READY", chunks=1)
    version(lib, "TEXT_READY", chunks=1)
    queue(lib, "dead", tier=2)
    s = st.indexing_section(lib.db, lib.data_dir)
    assert s["state"] == "degraded" and s["code"] == "queue_dead_letters" and s["dead_letters"] == 1
    assert s["versions"]["ready"] == 1 and s["versions"]["keyword_only"] == 1 and s["versions"]["reconciles"]
    assert set(s["eta_s"]) == {"tier1", "tier2"} and s["checked_at_ms"] > 0
    assert "dead_letters_detail" not in s and "last_error" not in json.dumps(s)


# -- Ollama probe ------------------------------------------------------------

class _Ollama(BaseHTTPRequestHandler):
    seen: list = []
    tags = {"models": [{"name": "qwen3:14b"}, {"name": "emb:latest"}]}
    ps = {"models": [{"name": "qwen3:14b", "size_vram": 1234}]}

    def log_message(self, *a):
        pass

    def do_GET(self):
        type(self).seen.append(("GET", self.path))
        body = {"/api/tags": self.tags, "/api/ps": self.ps}.get(self.path)
        self.send_response(200 if body is not None else 404)
        self.end_headers()
        self.wfile.write(json.dumps(body or {}).encode())

    def do_POST(self):
        type(self).seen.append(("POST", self.path))
        self.send_response(500)
        self.end_headers()


@pytest.fixture
def ollama():
    _Ollama.seen = []
    srv = HTTPServer(("127.0.0.1", 0), _Ollama)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}", _Ollama
    srv.shutdown()


def test_models_probe_is_get_only_and_reports_three_states(ollama):
    host, h = ollama
    s = st.models_section("qwen3:14b", "emb", host)
    assert s["state"] == "ok" and s["ollama"] == "yes"
    assert s["answer"] == {"name": "qwen3:14b", "reachable": "yes", "installed": "yes", "resident": "yes", "size_vram": 1234}
    assert s["embed"]["installed"] == "yes" and s["embed"]["resident"] == "no" and s["embed"]["size_vram"] is None
    assert sorted(h.seen) == [("GET", "/api/ps"), ("GET", "/api/tags")]
    assert all(m == "GET" for m, _ in h.seen)


def test_model_missing_and_ps_unknown(ollama):
    host, h = ollama
    s = st.models_section("absent:1b", "emb", host)
    assert s["state"] == "degraded" and s["code"] == "model_missing" and s["answer"]["installed"] == "no"
    saved = h.ps
    h.ps = None  # /api/ps -> 404: residency unknown, not "no"
    try:
        s = st.models_section("qwen3:14b", "emb", host)
    finally:
        h.ps = saved
    assert s["answer"]["resident"] == "unknown" and s["answer"]["installed"] == "yes" and s["code"] == "ok"


def test_ollama_unreachable_and_non_local():
    s = st.models_section("m", "e", "http://127.0.0.1:1", timeout=0.3)
    assert s["state"] == "unavailable" and s["code"] == "ollama_unreachable" and s["ollama"] == "no"
    assert s["answer"]["installed"] == "unknown" and s["answer"]["resident"] == "unknown"
    s = st.models_section("m", "e", "http://example.com:11434")
    assert s["code"] == "ollama_not_local" and s["answer"]["reachable"] == "unknown"


def test_models_probe_never_posts_with_injected_fetch():
    urls = []

    def fetch(url, timeout):
        urls.append(url)
        return {"models": []}

    st.models_section("m", "e", "http://localhost:11434", fetch=fetch)
    assert urls == ["http://localhost:11434/api/tags", "http://localhost:11434/api/ps"]


# -- health document ---------------------------------------------------------

def test_build_health_sections_have_state_code_and_timestamp(lib):
    settings = SimpleNamespace(sqlite_path=lib.db, data_dir=lib.data_dir, gen_model="g", embed_model="e")
    h = st.build_health(settings, 0.0, 1, st_features(), fetch=lambda u, t: (_ for _ in ()).throw(OSError("down")))
    for name in ("process", "database", "models", "indexing"):
        sec = h[name]
        assert sec["state"] in st.SECTION_STATES and sec["checked_at_ms"] > 0 and re_code(sec["code"])
    assert h["models"]["state"] == "unavailable"
    assert h["process"]["features"] == st_features()


def st_features():
    from docket.interfaces.worker import FEATURES

    return list(FEATURES)


def re_code(code):
    import re

    return bool(re.fullmatch(r"[a-z0-9_]{1,40}", code))


def test_failing_section_reports_code_not_exception(lib, monkeypatch):
    monkeypatch.setattr(st, "indexing_section", lambda *a: (_ for _ in ()).throw(RuntimeError(SECRET)))
    settings = SimpleNamespace(sqlite_path=lib.db, data_dir=lib.data_dir, gen_model="g", embed_model="e")
    h = st.build_health(settings, 0.0, 1, [], fetch=lambda u, t: {"models": []})
    assert h["indexing"]["state"] == "unavailable" and h["indexing"]["code"] == "probe_failed"
    assert SECRET not in json.dumps(h)


# -- privacy -----------------------------------------------------------------

def test_planted_paths_secrets_and_text_never_reach_output(lib):
    version(lib, "READY", chunks=1)
    version(lib, "FAILED")
    lib.conn.execute("UPDATE evidence_versions SET failure_reason=? WHERE status='FAILED'", (f"{PRIVATE_PATH} {SECRET}",))
    lib.conn.execute("UPDATE sources SET status_reason=?", (PRIVATE_PATH,))
    jid = job(lib, status="FAILED", rows=[("failed", f"Could not read {PRIVATE_PATH}: {SECRET}"),
                                          ("skipped", f"{PRIVATE_PATH} {SECRET}"),
                                          ("failed", f"Timed out reading {PRIVATE_PATH}")],
              skipped_counts={"unsupported_other": 1})
    lib.conn.execute("UPDATE ingestion_jobs SET error=? WHERE id=?", (f"{SECRET} {PRIVATE_PATH}", jid))
    lib.conn.execute(
        "INSERT INTO ingest_queue (id, source_id, file_path, tier, state, prio_class, mtime, recency_day, byte_size,"
        " attempts, not_before, enqueued_at, last_error) VALUES ('qx',?,?,2,'dead',2,0,0,0,3,0,0,?)",
        (lib.sid, PRIVATE_PATH, SECRET))
    lib.conn.commit()
    settings = SimpleNamespace(sqlite_path=lib.db, data_dir=lib.data_dir, gen_model="g", embed_model="e")
    blob = json.dumps([report(lib), st.build_health(settings, 0.0, 1, [], fetch=lambda u, t: {"models": []})])
    assert SECRET not in blob and "alice" not in blob and "payroll" not in blob and "/home" not in blob
    codes = report(lib)["counts"]["job"]
    assert set(codes["failed_by_code"]) <= st.FAILURE_CODES and set(codes["skipped_by_code"]) <= st.SKIP_CODES
    assert codes["failed_by_code"].get("timeout") == 1 and codes["skipped_by_code"] == {"unknown_reason": 1}


@pytest.mark.parametrize("text,code", [
    ("Password-protected file", "password_protected"), ("Timed out after 30s", "timeout"),
    ("File too large", "too_large"), ("Docling ran out of memory", "out_of_memory"),
    ("Permission denied", "permission_denied"), ("zip file is corrupt", "corrupt_file"),
    ("something odd", "read_failed"), (None, "other")])
def test_failure_classifier_is_closed(text, code):
    got = st.classify_failure(text)
    assert got == code and got in st.FAILURE_CODES


# -- ask metadata ------------------------------------------------------------

def test_ask_metadata_maps_and_drops():
    result = SimpleNamespace(
        abstention_reason="NO_EVIDENCE", scope_file_count=4,
        index_coverage={"indexed": 3, "total": 5, "keyword_only": 2, "indexing": True, "used_provisional": True},
        validation_warnings=["answer contains no citations", f"free text with {SECRET}", "answer references an unknown citation: [x]",
                             "3 of 5 files fully indexed; the other 2 are keyword-only so far.",
                             "answer contains no citations"])
    meta = st.ask_metadata(result)
    assert meta == {"abstention_reason": "NO_EVIDENCE", "scope_file_count": 4,
                    "index_coverage": {"indexed": 3, "total": 5, "keyword_only": 2, "used_provisional": True},
                    "warning_codes": ["no_citations", "unknown_citation", "keyword_only_evidence"]}
    assert SECRET not in json.dumps(meta)


def test_ask_metadata_absent_when_engine_did_not_set_it():
    assert st.ask_metadata(SimpleNamespace(abstention_reason=None, validation_warnings=[], index_coverage=None,
                                           scope_file_count=None)) == {}
    assert st.ask_metadata(SimpleNamespace()) == {}
    assert st.ask_metadata(SimpleNamespace(abstention_reason="made_up")) == {}
