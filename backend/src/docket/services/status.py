"""Read-only status for the daemon's health view and the source list (R2a).

Everything here is pure functions over a plain ``sqlite3`` connection opened
``file:...?mode=ro`` with ``PRAGMA query_only=ON`` (the doctor pattern) plus
GET-only requests to a local Ollama. Nothing here builds an ``AppContext``,
runs a migration, writes a row, loads a model or runs inference.

Privacy: the output carries closed-set codes and integer counts only. Paths,
file names, document text, error messages and scope labels are never copied
into it, even when a source row holds them (they are classified into the code
sets below and dropped).

ACCOUNTING UNITS (each count says what it counts; blocks never mix units and
are never claimed to reconcile with each other):

``versions``  unit ``current_version_per_file``: non-superseded
              ``evidence_versions`` rows of an ACTIVE source, read in ONE
              read transaction. Categories: ``ready`` (READY with chunks),
              ``keyword_only`` (TEXT_READY with chunks), ``empty_ready`` (READY
              or TEXT_READY with ZERO chunks: anomalous, NOT usable),
              ``pending``, ``failed``, ``other`` (unknown status names).
              ``reconciles`` is true only when ``total`` (an independent count
              in the same snapshot) equals the sum of the categories and
              ``other`` is zero.
``queue``     unit ``ingest_queue_row``: queued/running/dead rows (tiered
              indexing). Dead-letter COUNT only, never paths or errors.
``job``       unit ``ingestion_file_result_of_one_job``: the rows of ONE named
              whole-source job (its id and status are reported). Tiered runs
              and path-level jobs leave no such rows: their skipped/failed
              history is UNKNOWN (``known: false``), never zero.
``history``   unit ``superseded_evidence_version``: kept apart from the active
              equation. Sources that are revoked/tombstoned/missing get no
              ``versions`` block at all.

Read state is a snapshot of the database at ``snapshot_ms``; it does not claim
the vector index agrees with SQLite.
"""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import time
import urllib.request
from contextlib import closing
from pathlib import Path
from typing import Any, Callable, Iterable

from docket.services.ingestion import skips as _skips

# ---- closed code sets -----------------------------------------------------

SECTION_STATES = ("ok", "degraded", "unavailable", "unknown")
READINESS = ("ready", "indexing", "partial", "empty", "failed", "unavailable", "unknown")
TRI = ("yes", "no", "unknown")

DB_CODES = ("ok", "db_missing", "db_busy", "db_unreadable", "schema_behind", "schema_unknown")
MODEL_CODES = ("ok", "probe_failed", "model_missing", "ollama_unreachable", "ollama_not_local", "ollama_bad_response")
INDEX_CODES = ("ok", "queue_dead_letters", "anomalies", "index_unreadable", "db_missing")
SOURCE_REASONS = ("source_inactive", "no_whole_source_job", "read_failed", "db_missing", "job_stats_unreadable")

SKIP_CODES = frozenset(_skips.REASON_TEXT) | {"unknown_reason"}
FAILURE_CODES = frozenset({
    "password_protected", "conversion_failed", "conversion_timeout", "converter_missing", "too_large",
    "unsupported_type", "invalid_file", "corrupt_file", "timeout", "out_of_memory", "permission_denied",
    "file_missing", "empty_file", "read_failed", "other",
})
JOB_STATUSES = frozenset({"PENDING", "RUNNING", "SUCCEEDED", "FAILED", "PARTIAL"})
OUTCOMES = ("ingested", "unchanged", "skipped", "failed", "pending", "running", "interrupted", "cancelled", "deleted")
ANOMALIES = ("empty_ready", "interrupted_job", "dead_letters", "failed_versions", "keyword_only_idle", "job_partial")
WARNING_CODES = {
    "cited evidence is no longer available": "evidence_unavailable",
    "retrieved evidence is no longer available": "evidence_unavailable",
    "answer contains no citations": "no_citations",
    "answer is empty": "empty_answer",
    "the named file is outside the selected scope": "named_file_outside_scope",
}
ABSTENTION_REASONS = frozenset({
    "NO_EVIDENCE", "INSUFFICIENT_SUPPORT", "SOURCE_UNAVAILABLE", "PERMISSION_CHANGED", "UNRESOLVED_CONFLICT",
    "INVALID_DERIVATION", "VERIFIER_UNAVAILABLE", "CONTEXT_BUDGET_EXCEEDED",
})

_KEYWORD_SERVABLE = ("READY", "TEXT_READY")


def _now_ms() -> int:
    return int(time.time() * 1000)


# ---- read-only connection -------------------------------------------------

def open_readonly(db_path: Path, timeout: float = 0.2) -> sqlite3.Connection:
    """A read-only connection that cannot write or migrate. Raises sqlite3.Error/OSError."""
    conn = sqlite3.connect(Path(db_path).absolute().as_uri() + "?mode=ro", uri=True, timeout=timeout,
                           isolation_level=None)
    try:
        conn.execute("PRAGMA query_only=ON")
    except sqlite3.Error:
        conn.close()
        raise
    return conn


def writer_lock_state(data_dir: Path) -> str:
    """``free`` | ``held`` | ``unknown``. A non-blocking shared flock probe: no write, never waits."""
    from docket.core.writer_lock import LOCK_FILENAME

    path = Path(data_dir) / LOCK_FILENAME
    if not path.exists():
        return "free"
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return "held"
        fcntl.flock(fd, fcntl.LOCK_UN)
        return "free"
    except OSError:
        return "unknown"
    finally:
        if fd is not None:
            os.close(fd)


# ---- classification of stored text into closed codes ----------------------

def classify_failure(text: str | None) -> str:
    """Closed failure code for a stored failure sentence. The sentence is read
    here and never copied to the output."""
    low = (text or "").lower()
    base = _skips.classify_text(text)
    if base in FAILURE_CODES and base != "read_failed":
        return base
    if "timed out" in low or "timeout" in low:
        return "timeout"
    if "memory" in low:
        return "out_of_memory"
    if "permission" in low or "access is denied" in low:
        return "permission_denied"
    if "no such file" in low or "not found" in low or "missing" in low:
        return "file_missing"
    if "empty" in low or "zero-byte" in low or "0 bytes" in low:
        return "empty_file"
    if any(w in low for w in ("corrupt", "damaged", "malformed", "truncated", "unreadable", "not a valid", "invalid")):
        return "corrupt_file"
    return "read_failed" if base == "read_failed" and low else "other"


def _skip_code(text: str | None) -> str:
    return _skips.code_for_text(text) or "unknown_reason"


def _bump(d: dict[str, int], key: str, n: int = 1) -> None:
    d[key] = d.get(key, 0) + n


# ---- version / queue / job / history blocks -------------------------------

def _unknown(reason: str, **extra: Any) -> dict:
    return {"known": False, "reason": reason, **extra}


def version_block(conn: sqlite3.Connection, source_id: str | None = None) -> dict:
    """``versions`` block (see module docstring). One source, or every ACTIVE
    source when ``source_id`` is None. Must be called inside the caller's read
    transaction."""
    where = " AND v.source_id = ?" if source_id else ""
    args: tuple = (source_id,) if source_id else ()
    rows = conn.execute(
        "SELECT v.status, EXISTS(SELECT 1 FROM chunks c WHERE c.evidence_version_id = v.id), COUNT(*) "
        "FROM evidence_versions v JOIN sources s ON s.id = v.source_id "
        "WHERE s.status = 'ACTIVE' AND v.status != 'SUPERSEDED'" + where + " GROUP BY 1, 2", args).fetchall()
    total = int(conn.execute(
        "SELECT COUNT(*) FROM evidence_versions v JOIN sources s ON s.id = v.source_id "
        "WHERE s.status = 'ACTIVE' AND v.status != 'SUPERSEDED'" + where, args).fetchone()[0])
    out = {"ready": 0, "keyword_only": 0, "empty_ready": 0, "pending": 0, "failed": 0, "other": 0}
    for status, has_chunks, n in rows:
        n = int(n)
        if status in _KEYWORD_SERVABLE:
            if not has_chunks:
                out["empty_ready"] += n
            else:
                out["ready" if status == "READY" else "keyword_only"] += n
        elif status == "PENDING":
            out["pending"] += n
        elif status == "FAILED":
            out["failed"] += n
        else:
            out["other"] += n
    return {"known": True, "unit": "current_version_per_file", "total": total, **out,
            "reconciles": total == sum(out.values()) and out["other"] == 0}


def queue_block(conn: sqlite3.Connection, source_id: str | None = None) -> dict:
    where = " WHERE source_id = ?" if source_id else ""
    args: tuple = (source_id,) if source_id else ()
    c = {"queued": 0, "running": 0, "dead": 0}
    for state, n in conn.execute("SELECT state, COUNT(*) FROM ingest_queue" + where + " GROUP BY state", args):
        if state in c:
            c[state] = int(n)
    return {"known": True, "unit": "ingest_queue_row", **c}


def history_block(conn: sqlite3.Connection, source_id: str | None = None) -> dict:
    where = " AND source_id = ?" if source_id else ""
    args: tuple = (source_id,) if source_id else ()
    n = conn.execute("SELECT COUNT(*) FROM evidence_versions WHERE status = 'SUPERSEDED'" + where, args).fetchone()[0]
    return {"known": True, "unit": "superseded_evidence_version", "superseded": int(n)}


def job_block(conn: sqlite3.Connection, source_id: str, lock: str) -> dict:
    """The latest whole-source ingest job of the source (the newest job whose
    stats carry ``skipped_counts``), with its file-result rows classified into
    closed codes. UNKNOWN when there is none: tiered runs and path-level jobs
    leave no job or file-result rows."""
    jobs = conn.execute(
        "SELECT id, status, stats_json FROM ingestion_jobs WHERE source_id = ? ORDER BY started_at DESC LIMIT 25",
        (source_id,)).fetchall()
    chosen = None
    stats: dict = {}
    for job_id, status, raw in jobs:
        try:
            parsed = json.loads(raw or "{}")
        except ValueError:
            continue
        if isinstance(parsed, dict) and "skipped_counts" in parsed:
            chosen, stats = (job_id, status), parsed
            break
    if chosen is None:
        return _unknown("no_whole_source_job")
    job_id, status = chosen
    outcomes: dict[str, int] = {}
    skipped: dict[str, int] = {}
    failed: dict[str, int] = {}
    files = 0
    for outcome, reason in conn.execute(
            "SELECT outcome, failure_reason FROM ingestion_file_results WHERE job_id = ?", (job_id,)):
        files += 1
        key = outcome if outcome in OUTCOMES else "other"
        _bump(outcomes, key)
        if outcome == "skipped":
            _bump(skipped, _skip_code(reason))
        elif outcome == "failed":
            _bump(failed, classify_failure(reason))
    total = int(conn.execute("SELECT COUNT(*) FROM ingestion_file_results WHERE job_id = ?", (job_id,)).fetchone()[0])
    counted_only: dict[str, int] = {}
    raw_counts = stats.get("skipped_counts")
    for code in (_skips.UNSUPPORTED_OTHER, _skips.IGNORED):
        try:
            n = int((raw_counts or {}).get(code, 0))
        except (TypeError, ValueError, AttributeError):
            n = 0
        if n > 0:
            counted_only[code] = n
    status = status if status in JOB_STATUSES else "UNKNOWN"
    interrupted: bool | None = None
    if status == "RUNNING":
        interrupted = {"free": True, "held": False}.get(lock)  # None = probe failed: unknown
    return {
        "known": True, "unit": "ingestion_file_result_of_one_job", "job_id": str(job_id), "status": status,
        "interrupted": interrupted, "files": total, "outcomes": outcomes,
        "skipped_by_code": skipped, "failed_by_code": failed, "counted_only": counted_only,
        "reconciles": total == files == sum(outcomes.values()) and "other" not in outcomes,
    }


# ---- readiness ------------------------------------------------------------

def decide_readiness(versions: dict | None, queue: dict | None, job: dict | None) -> tuple[str, list[str]]:
    """Readiness of ONE active source and its anomaly flags (closed set).

    ``ready``     usable files exist and nothing is outstanding or anomalous.
    ``indexing``  work is outstanding (pending versions, queued/running rows or a
                  live job). Zero usable files while indexing is NEVER ``ready``.
    ``partial``   some files usable but some failed, dead-lettered, keyword-only
                  with no work left, empty-ready (anomalous) or interrupted.
    ``failed``    nothing usable and failures (or an interrupted job) explain it.
    ``empty``     nothing to index (no versions, no work).
    ``unknown``   counts could not be read."""
    if not versions or not versions.get("known"):
        return "unknown", []
    flags: list[str] = []
    q = queue if queue and queue.get("known") else {"queued": 0, "running": 0, "dead": 0}
    usable = versions["ready"] + versions["keyword_only"]
    interrupted = bool(job and job.get("known") and job.get("interrupted") is True)
    live_job = bool(job and job.get("known") and job.get("status") == "RUNNING" and not interrupted)
    if versions["empty_ready"]:
        flags.append("empty_ready")
    if interrupted:
        flags.append("interrupted_job")
    if q["dead"]:
        flags.append("dead_letters")
    if versions["failed"]:
        flags.append("failed_versions")
    if job and job.get("known") and job.get("status") == "PARTIAL":
        flags.append("job_partial")
    outstanding = versions["pending"] or q["queued"] or q["running"] or live_job
    if outstanding:
        return "indexing", flags
    if versions["keyword_only"]:
        flags.append("keyword_only_idle")
    if usable == 0 and versions["empty_ready"] == 0 and not flags and versions["total"] == 0:
        return "empty", flags
    if usable and not flags:
        return "ready", flags
    if usable or versions["empty_ready"]:
        return "partial", flags
    if flags:
        return "failed", flags
    return "empty", flags


def source_reports(db_path: Path, data_dir: Path, source_rows: Iterable[tuple[str, str]]) -> dict[str, dict]:
    """``{source_id: {readiness, anomalies, counts}}`` for ``(id, status_value)``
    pairs, all from ONE read transaction. On any read failure every source is
    ``unavailable`` with a reason code (never an error text)."""
    pairs = [(sid, str(st).lower()) for sid, st in source_rows]
    lock = writer_lock_state(data_dir)
    out: dict[str, dict] = {}
    try:
        with closing(open_readonly(db_path)) as conn:
            conn.execute("BEGIN")
            try:
                snapshot = _now_ms()
                for sid, st in pairs:
                    if st != "active":
                        out[sid] = {"readiness": "unavailable", "anomalies": [], "counts": {
                            "snapshot_ms": snapshot, "versions": _unknown("source_inactive"),
                            "history": history_block(conn, sid)}}
                        continue
                    versions = version_block(conn, sid)
                    queue = queue_block(conn, sid)
                    job = job_block(conn, sid, lock)
                    readiness, flags = decide_readiness(versions, queue, job)
                    out[sid] = {"readiness": readiness, "anomalies": flags, "counts": {
                        "snapshot_ms": snapshot, "versions": versions, "queue": queue, "job": job,
                        "history": history_block(conn, sid),
                        "basis": {"versions": "snapshot", "job_id": job.get("job_id"),
                                  "skipped_by_code": "latest_whole_source_job" if job.get("known") else "unknown",
                                  "writer_lock": lock}}}
            finally:
                conn.execute("ROLLBACK")
    except (sqlite3.Error, OSError, ValueError):
        reason = "db_missing" if not Path(db_path).exists() else "read_failed"
        return {sid: {"readiness": "unavailable", "anomalies": [], "counts": {
            "snapshot_ms": None, "versions": _unknown(reason)}} for sid, _ in pairs}
    return out


# ---- health sections ------------------------------------------------------

def _section(state: str, code: str, **data: Any) -> dict:
    return {"state": state, "code": code, "checked_at_ms": _now_ms(), **data}


_HEAD: list[str] = []


def _migration_head() -> str | None:
    if not _HEAD:
        try:
            from docket.services.doctor import migration_head

            _HEAD.append(migration_head())
        except Exception:  # noqa: BLE001 -- unknown head is reported as unknown, never raised
            return None
    return _HEAD[0]


def database_section(db_path: Path) -> dict:
    if not Path(db_path).exists():
        return _section("unavailable", "db_missing", readable="no", revision=None, at_head="unknown")
    try:
        with closing(open_readonly(db_path)) as conn:
            revision = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    except sqlite3.OperationalError as exc:
        busy = "locked" in str(exc).lower() or "busy" in str(exc).lower()
        return _section("degraded" if busy else "unavailable", "db_busy" if busy else "db_unreadable",
                        readable="unknown" if busy else "no", revision=None, at_head="unknown")
    except (sqlite3.Error, OSError):
        return _section("unavailable", "db_unreadable", readable="no", revision=None, at_head="unknown")
    if not revision:
        return _section("degraded", "schema_unknown", readable="yes", revision=None, at_head="unknown")
    rev = str(revision[0])
    head = _migration_head()
    if head is None:
        return _section("ok", "schema_unknown", readable="yes", revision=rev, at_head="unknown")
    if rev != head:
        return _section("degraded", "schema_behind", readable="yes", revision=rev, at_head="no")
    return _section("ok", "ok", readable="yes", revision=rev, at_head="yes")


def indexing_section(db_path: Path, data_dir: Path) -> dict:
    """Library-level indexing view from one read transaction (queue counts, ETA,
    dead-letter COUNT only, version states with ``empty_ready`` split out)."""
    if not Path(db_path).exists():
        return _section("unavailable", "db_missing")
    lock = writer_lock_state(data_dir)
    try:
        with closing(open_readonly(db_path)) as conn:
            conn.execute("BEGIN")
            try:
                snapshot = _now_ms()
                versions = version_block(conn)
                queue = queue_block(conn)
                history = history_block(conn)
                from docket.services.ingestion.tiered import payload_from_dbapi

                tier = payload_from_dbapi(conn)
                running = conn.execute(
                    "SELECT COUNT(*) FROM ingestion_jobs j JOIN sources s ON s.id = j.source_id "
                    "WHERE j.status = 'RUNNING' AND s.status = 'ACTIVE'").fetchone()[0]
            finally:
                conn.execute("ROLLBACK")
    except (sqlite3.Error, OSError, ImportError, KeyError, TypeError, ValueError):
        return _section("unavailable", "index_unreadable")

    def eta(t: str) -> int | None:
        v = tier["eta_s"].get(t)
        return None if v is None else int(round(v))

    flags = []
    if queue["dead"]:
        flags.append("queue_dead_letters")
    if versions["empty_ready"] or versions["failed"] or not versions["reconciles"]:
        flags.append("anomalies")
    interrupted_jobs = int(running) if lock == "free" else (None if lock == "unknown" else 0)
    state, code = ("degraded", flags[0]) if flags else ("ok", "ok")
    return _section(
        state, code, snapshot_ms=snapshot, versions=versions, queue=queue, history=history,
        dead_letters=queue["dead"], eta_s={"tier1": eta("tier1"), "tier2": eta("tier2")},
        writer=lock, interrupted_jobs=interrupted_jobs)


def process_section(started_monotonic: float, protocol: int, features: list[str]) -> dict:
    return _section("ok", "ok", uptime_s=int(time.monotonic() - started_monotonic), protocol=protocol,
                    features=list(features))


# -- models (GET /api/tags and GET /api/ps only) --

def _http_get_json(url: str, timeout: float) -> dict:
    from docket.services.doctor import _NoRedirect

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    with opener.open(urllib.request.Request(url, method="GET"), timeout=timeout) as response:
        raw = response.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError("response too large")
    data = json.loads(raw)
    if not isinstance(data, dict) or not isinstance(data.get("models", []), list):
        raise ValueError("bad response")
    return data


def _entries(data: dict) -> dict[str, int | None]:
    from docket.infra.inference.health import _normalize

    names: dict[str, int | None] = {}
    for entry in data.get("models") or []:
        if not isinstance(entry, dict):
            continue
        name = entry.get("model") or entry.get("name")
        if name:
            vram = entry.get("size_vram")
            names[_normalize(str(name))] = int(vram) if isinstance(vram, (int, float)) and not isinstance(vram, bool) else None
    return names


def models_section(gen_model: str, embed_model: str, host: str | None = None, *,
                   fetch: Callable[[str, float], dict] | None = None, timeout: float = 1.0) -> dict:
    """installed (/api/tags), reachable (Ollama answered) and resident
    (/api/ps, with size_vram when given), each yes/no/unknown, for the answer
    and the embedding model separately. Only GET /api/tags and GET /api/ps; a
    local host only; no model is loaded or run."""
    from docket.infra.inference.health import _normalize
    from docket.services.doctor import local_tags_url

    def pair(name: str, reachable: str, installed: dict | None, resident: dict | None) -> dict:
        key = _normalize(name)
        return {
            "name": name, "reachable": reachable,
            "installed": "unknown" if installed is None else ("yes" if key in installed else "no"),
            "resident": "unknown" if resident is None else ("yes" if key in resident else "no"),
            "size_vram": None if not resident else resident.get(key),
        }

    def unknown(code: str, state: str, reachable: str) -> dict:
        return _section(state, code, ollama=reachable, answer=pair(gen_model, reachable, None, None),
                        embed=pair(embed_model, reachable, None, None))

    host = host or os.environ.get("OLLAMA_HOST") or "http://127.0.0.1:11434"
    try:
        tags_url = local_tags_url(host)
    except ValueError:
        return unknown("ollama_not_local", "unavailable", "unknown")
    base = tags_url[: -len("/api/tags")]
    get = fetch or _http_get_json
    try:
        installed = _entries(get(base + "/api/tags", timeout))
    except ValueError:
        return unknown("ollama_bad_response", "degraded", "yes")
    except Exception:  # noqa: BLE001 -- connection refused, timeout, redirect: all "unreachable"
        return unknown("ollama_unreachable", "unavailable", "no")
    try:
        resident: dict | None = _entries(get(base + "/api/ps", timeout))
    except Exception:  # noqa: BLE001 -- residency stays unknown
        resident = None
    answer = pair(gen_model, "yes", installed, resident)
    embed = pair(embed_model, "yes", installed, resident)
    missing = "no" in (answer["installed"], embed["installed"])
    return _section("degraded" if missing else "ok", "model_missing" if missing else "ok",
                    ollama="yes", answer=answer, embed=embed)


def build_health(settings: Any, started_monotonic: float, protocol: int, features: list[str], *,
                 fetch: Callable[[str, float], dict] | None = None) -> dict:
    """The worker's ``health`` result. A section that fails reports ``unavailable``
    with a closed code; nothing is swallowed silently."""
    def guarded(make: Callable[[], dict]) -> dict:
        try:
            return make()
        except Exception:  # noqa: BLE001 -- reported as a code, never raised or described
            return _section("unavailable", "probe_failed")

    return {
        "v": 1, "checked_at_ms": _now_ms(),
        "process": guarded(lambda: process_section(started_monotonic, protocol, features)),
        "database": guarded(lambda: database_section(Path(settings.sqlite_path))),
        "models": guarded(lambda: models_section(str(settings.gen_model), str(settings.embed_model), fetch=fetch)),
        "indexing": guarded(lambda: indexing_section(Path(settings.sqlite_path), Path(settings.data_dir))),
    }


# ---- query metadata -------------------------------------------------------

def ask_metadata(result: Any) -> dict:
    """Optional ``query.ask`` fields from an engine result: only what the engine
    set, mapped into closed codes / plain numbers. Free-text warnings and scope
    labels are never forwarded."""
    meta: dict[str, Any] = {}
    reason = getattr(result, "abstention_reason", None)
    reason = getattr(reason, "value", reason)
    if isinstance(reason, str) and reason in ABSTENTION_REASONS:
        meta["abstention_reason"] = reason
    cov = getattr(result, "index_coverage", None)
    if isinstance(cov, dict):
        try:
            meta["index_coverage"] = {
                "indexed": int(cov["indexed"]), "total": int(cov["total"]),
                "keyword_only": int(cov.get("keyword_only", 0)), "used_provisional": bool(cov.get("used_provisional", False)),
            }
        except (KeyError, TypeError, ValueError):
            pass
    count = getattr(result, "scope_file_count", None)
    if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
        meta["scope_file_count"] = count
    codes: list[str] = []
    for w in getattr(result, "validation_warnings", None) or []:
        text = str(w)
        code = WARNING_CODES.get(text)
        if code is None:
            if text.startswith("answer references an unknown citation"):
                code = "unknown_citation"
            elif text.startswith("no retrievable chunks in the named file"):
                code = "named_file_empty"
            elif " files fully indexed; the other " in text and text.endswith("are keyword-only so far."):
                code = "keyword_only_evidence"
        if code and code not in codes:
            codes.append(code)
    if codes:
        meta["warning_codes"] = codes
    return meta
