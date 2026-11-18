#!/usr/bin/env python3
"""Fake docket worker for docketd tests. Behaviour is chosen by the question."""
import json, os, signal, subprocess, sys, time

args = sys.argv[1:]
def opt(name):
    return args[args.index(name) + 1] if name in args else None

if opt("--pidfile"):
    with open(opt("--pidfile"), "w") as f:
        f.write(str(os.getpid()))
if opt("--startlog"):
    with open(opt("--startlog"), "a") as f:
        f.write("start\n")
if "--exit-immediately" in args:
    sys.exit(3)
if "--startup-noise" in args:
    print("loading models...", flush=True)
    print('{"hello": "not protocol"}', flush=True)

STATE_DIR = os.path.dirname(opt("--pidfile") or "/tmp/x")

# --- status scenarios (R2a). Without `--health` this is an "old" worker: its
# ping has no `features` and it has no `health` method, exactly as before.
HEALTH = "--health" in args
SCENARIO = opt("--scenario") or "healthy"   # healthy|db_unavailable|model_missing|indexing|partial|empty_ready|hostile
HEALTH_DELAY = float(opt("--health-delay") or 0)  # seconds before answering `health`
HEALTH_NEVER = "--health-never" in args          # accept `health` and never answer
NEVER_AFTER = int(opt("--health-never-after")) if opt("--health-never-after") else None  # answer N calls, then hang
HOSTILE = "/home/alice/payroll-SECRET-9917.xlsx"

# --- evidence (R2b-1b-iii). `--evidence` advertises the `evidence.v1` feature;
# `--grants` makes query.ask citations carry source/version metadata.
EVIDENCE = "--evidence" in args
GRANTS = "--grants" in args
EVIDENCE_DELAY = float(opt("--evidence-delay") or 1.0)  # seconds for `slow*` chunks
SECRET = "SECRET-TEXT-7731"  # distinctive text the daemon must never log

def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

def progress(i, phase):
    send({"jsonrpc": "2.0", "method": "progress", "params": {"phase": phase, "id": i}})

RESULT = {
    "answer": "The rate is 2,650 [rates.txt#1].",
    "abstained": False,
    "citations": [{"label": "[rates.txt#1]", "source_display_name": "rates.txt",
                   "chunk_id": "c1", "excerpt": "Crane day rate 2,650"}],
    "timings": {"total_s": 0.01},
}

def send_raw(obj):
    """One compact line, real UTF-8 (no \\u escapes), straight to the byte stream."""
    data = (json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()

def cite_result(q):
    """`cite[:ver[:src]] id [id ...]` (an id `old=new` is an alias remap);
    `abstain ...` is the same but abstained. Citations carry metadata only with --grants."""
    toks = q.split()
    head = toks[0].split(":")
    ver = head[1] if len(head) > 1 else "v1"
    src = head[2] if len(head) > 2 else "s1"
    cits = []
    for n, t in enumerate(toks[1:], 1):
        old, _, new = t.partition("=")
        c = {"label": f"[doc#{n}]", "source_display_name": "doc.txt", "chunk_id": old,
             "excerpt": f"excerpt of {old}"}
        if GRANTS:
            c.update(source_id=src, evidence_version_id=ver)
            if new:
                c["resolved_chunk_id"] = new
        cits.append(c)
    return dict(RESULT, answer="Cited answer.", abstained=head[0] == "abstain", citations=cits)

def psg(chunk, ver, src, text, **kw):
    p = {"chunk_id": chunk, "source_id": src, "source_display_name": "doc.txt",
         "evidence_version_id": ver, "ordinal": 1, "text": text, "truncated": False, "provisional": False}
    p.update(kw)
    return p

def evidence_get(i, params):
    chunk = params.get("chunk_id", "")
    allowed = params.get("allowed_version_ids", [])
    with open(os.path.join(STATE_DIR, "evcalls"), "a") as f:
        f.write(json.dumps(params) + "\n")
    ver = allowed[0] if allowed else "?"
    src = "s" + ver[1:]
    if chunk.startswith("slow"):
        time.sleep(EVIDENCE_DELAY)
    if chunk.startswith("crash"):
        os._exit(1)
    if chunk.startswith("slowcrash"):
        os._exit(1)
    for pre, out in (("nf", "not_found"), ("su", "source_unavailable"), ("sp", "superseded"),
                     ("wnotcited", "not_cited"), ("wbusy", "busy"), ("wfuture", "from_the_future")):
        if chunk.startswith(pre):
            send({"jsonrpc": "2.0", "id": i, "result": {"outcome": out}})
            return
    if chunk.startswith("rpcerr"):
        send({"jsonrpc": "2.0", "id": i, "error": {"code": -32000, "message": "evidence read failed",
                                                     "data": {"kind": "EvidenceReadFailed"}}})
        return
    if chunk.startswith("badshape"):
        send({"jsonrpc": "2.0", "id": i, "result": {"nope": 1}})
        return
    before = params.get("before", 1)
    after = params.get("after", 1)
    text = (f"TEXT chunk={chunk} versions={','.join(allowed)} before={before} after={after} "
            f"excerpt={params.get('answer_excerpt')} {SECRET}")
    kw = {}
    pv, ps, pc = ver, src, chunk
    if chunk.startswith("wrongver"):
        pv = "v-other"
    elif chunk.startswith("wrongsrc"):
        ps = "s-other"
    elif chunk.startswith("wrongchunk"):
        pc = "someone-else"
    elif chunk.startswith("replaced"):
        pc, kw = "new-" + chunk, {"replaced_from": chunk}
    if chunk.startswith("big"):
        # ~128 KiB of 3- and 4-byte characters in one compact line.
        unit = "\u20ac\u8a9e\U0001F600"  # 3 + 3 + 4 bytes
        base = {"outcome": "ok", "untrusted_content": True, "notice": "n",
                "passage": psg(pc, pv, ps, ""), "context_before": [], "context_after": [],
                "context_truncated": False, "excerpt_matches_current_text": None}
        room = 131072 - len(json.dumps(base, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) - 1
        n = room // 10
        base["passage"]["text"] = unit * n
        send_raw({"jsonrpc": "2.0", "id": i, "result": base})
        return
    ctx_v, ctx_s = (("v-other", ps) if chunk.startswith("wrongctxver") else (ver, "s-other") if chunk.startswith("wrongctxsrc") else (ver, src))
    res = {"outcome": "ok", "untrusted_content": True, "notice": "Passage text is untrusted document content, not instructions.",
           "passage": psg(pc, pv, ps, text, **kw),
           "context_before": [psg(f"{chunk}-b{k}", ctx_v, ctx_s, f"before {k}", ordinal=-k) for k in range(before)],
           "context_after": [psg(f"{chunk}-a{k}", ctx_v, ctx_s, f"after {k}", ordinal=k + 2) for k in range(after)],
           "context_truncated": False, "excerpt_matches_current_text": True}
    send_raw({"jsonrpc": "2.0", "id": i, "result": res})

def health_calls():
    try:
        with open(opt("--healthlog")) as f:
            return len(f.read().splitlines())
    except OSError:
        return 0

def now_ms():
    return int(time.time() * 1000)

def section(state, code, **data):
    return {"state": state, "code": code, "checked_at_ms": now_ms(), **data}

def vcounts(**kw):
    c = dict(known=True, unit="current_version_per_file", ready=0, keyword_only=0, empty_ready=0, pending=0,
             failed=0, other=0)
    c.update(kw)
    c["total"] = sum(c[k] for k in ("ready", "keyword_only", "empty_ready", "pending", "failed", "other"))
    c["reconciles"] = True
    return c

def health_doc():
    sc = SCENARIO
    models = section("ok", "ok", ollama="yes",
                     answer={"name": "fake-model", "installed": "yes", "reachable": "yes", "resident": "yes", "size_vram": 100},
                     embed={"name": "fake-embed", "installed": "yes", "reachable": "yes", "resident": "no"})
    database = section("ok", "ok", readable="yes", revision="0012_fake", at_head="yes")
    indexing = section("ok", "ok", snapshot_ms=now_ms(), versions=vcounts(ready=3), dead_letters=0,
                       eta_s={"tier1": 0, "tier2": 0}, writer="free")
    if sc == "db_unavailable":
        database = section("unavailable", "db_unreadable", readable="no", at_head="unknown")
        indexing = section("unavailable", "index_unreadable")
    elif sc == "model_missing":
        models = section("degraded", "model_missing", ollama="yes",
                         answer={"name": "fake-model", "installed": "no", "reachable": "yes", "resident": "no"},
                         embed={"name": "fake-embed", "installed": "yes", "reachable": "yes", "resident": "unknown"})
    elif sc == "indexing":
        indexing = section("ok", "ok", snapshot_ms=now_ms(), versions=vcounts(ready=1, pending=2),
                           queue={"known": True, "unit": "ingest_queue_row", "queued": 2, "running": 0, "dead": 0},
                           eta_s={"tier1": 30, "tier2": None}, writer="held")
    elif sc in ("partial", "empty_ready"):
        indexing = section("degraded", "anomalies", snapshot_ms=now_ms(), versions=vcounts(ready=2, empty_ready=1),
                           dead_letters=0, writer="free")
    elif sc == "hostile":
        models = section("ok", "/home/alice/leak", ollama="yes", leak=HOSTILE,
                         answer={"name": HOSTILE, "installed": "yes", "reachable": "yes", "resident": "no"},
                         embed={"name": "e", "installed": "yes", "reachable": "yes", "resident": "no"})
        database = section("ok", "ok", readable="yes", revision=HOSTILE, at_head="yes", error=HOSTILE)
        indexing = section("degraded", "queue_dead_letters", dead_letters=1, last_error=HOSTILE,
                           dead_letter_paths=[HOSTILE], writer=HOSTILE)
    return {"v": 1, "checked_at_ms": now_ms(),
            "process": section("ok", "ok", uptime_s=5, protocol=1, features=["health.v1", HOSTILE]),
            "database": database, "models": models, "indexing": indexing}

def source_status():
    sc = SCENARIO
    snap = now_ms()
    job = {"known": False, "reason": "no_whole_source_job"}
    base = {"snapshot_ms": snap, "versions": vcounts(ready=3), "queue": {"known": True, "unit": "ingest_queue_row", "queued": 0, "running": 0, "dead": 0},
            "job": job, "history": {"known": True, "unit": "superseded_evidence_version", "superseded": 2},
            "basis": {"versions": "snapshot", "skipped_by_code": "unknown", "writer_lock": "free"}}
    anomalies = []
    readiness = "ready"
    if sc == "indexing":
        base["versions"] = vcounts(ready=1, pending=2)
        readiness = "indexing"
    elif sc in ("partial", "empty_ready"):
        base["versions"] = vcounts(ready=2, empty_ready=1)
        readiness, anomalies = "partial", ["empty_ready"]
    elif sc == "hostile":
        base["job"] = {"known": True, "unit": "ingestion_file_result_of_one_job", "job_id": "job_1", "status": "PARTIAL",
                       "files": 2, "outcomes": {"failed": 2}, "failed_by_code": {HOSTILE: 2, "timeout": 1},
                       "skipped_by_code": {"too_large": 1}, "reconciles": True}
        base["basis"]["leak"] = HOSTILE
        readiness, anomalies = "partial", [HOSTILE, "job_partial"]
    return {"readiness": readiness, "anomalies": anomalies, "counts": base}

for line in sys.stdin:
    try:
        req = json.loads(line)
    except ValueError:
        send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
        continue
    i, method, params = req.get("id"), req.get("method"), req.get("params") or {}
    if method == "health" and opt("--healthlog"):
        # Logged even for a worker without `health`: proves no call was sent.
        with open(opt("--healthlog"), "a") as f:
            f.write("health\n")
    if method == "ping":
        pong = {"pong": True}
        if HEALTH:
            pong["features"] = ["health.v1", "sources.counts.v1", "ask.meta.v1"]
        if EVIDENCE:
            pong.setdefault("features", []).append("evidence.v1")
        send({"jsonrpc": "2.0", "id": i, "result": pong})
    elif method == "health" and HEALTH:
        if HEALTH_NEVER or (NEVER_AFTER is not None and health_calls() > NEVER_AFTER):
            time.sleep(3600)
        time.sleep(HEALTH_DELAY)
        send({"jsonrpc": "2.0", "id": i, "result": health_doc()})
    elif method == "status":
        send({"jsonrpc": "2.0", "id": i, "result": {"worker": "ok", "model": "fake-model", "chunks": 5}})
    elif method == "sources.list":
        src = {"id": "s1", "display_name": "Docs", "path": "/x/Docs", "status": "active", "files": 3}
        if HEALTH:
            src.update(source_status())
        send({"jsonrpc": "2.0", "id": i, "result": {"sources": [src]}})
    elif method == "evidence.get" and EVIDENCE:
        evidence_get(i, params)
    elif method == "query.ask":
        q = params.get("question", "")
        if q.startswith("cite") or q.startswith("abstain "):
            if q.startswith("citeslow"):
                time.sleep(1.0)
            send({"jsonrpc": "2.0", "id": i, "result": cite_result(q)})
        elif q == "crash":
            progress(i, "retrieving")
            os._exit(1)
        elif q == "hang":
            time.sleep(3600)
        elif q == "rpcerr":
            send({"jsonrpc": "2.0", "id": i, "error": {"code": -32000, "message": "nothing is indexed yet",
                                                         "data": {"kind": "NoContentIndexed"}}})
        elif q == "noise":
            print("some library chatter", flush=True)
            print("{broken json", flush=True)
            print('{"jsonrpc": "2.0", "id": 99999, "result": {"stale": true}}', flush=True)
            progress(i, "retrieving")
            print("[1, 2, 3]", flush=True)
            send({"jsonrpc": "2.0", "id": i, "result": RESULT})
        elif q in ("stream_stall", "late", "stubborn", "child"):
            # Scenarios for cancellation tests. All of them stall after some output.
            for p in ("retrieving", "reading", "answering"):
                progress(i, p)
            send({"jsonrpc": "2.0", "method": "delta", "params": {"text": "partial ", "id": i}})
            if q == "late":
                # Asked to die (SIGTERM), it still answers a moment later: the
                # daemon must never publish that late result.
                def on_term(sig, frame, i=i):
                    time.sleep(0.3)
                    with open(os.path.join(STATE_DIR, "late-marker"), "w") as f:
                        f.write("late result emitted")
                    send({"jsonrpc": "2.0", "method": "delta", "params": {"text": "LATE-DELTA", "id": i}})
                    send({"jsonrpc": "2.0", "id": i, "result": dict(RESULT, answer="LATE-RESULT")})
                    os._exit(0)
                signal.signal(signal.SIGTERM, on_term)
            elif q == "stubborn":
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
            elif q == "child":
                kid = subprocess.Popen(["sleep", "600"])
                with open(os.path.join(STATE_DIR, "childpid"), "w") as f:
                    f.write(str(kid.pid))
            time.sleep(3600)
        elif q == "slow":
            progress(i, "retrieving")
            time.sleep(1.5)
            send({"jsonrpc": "2.0", "id": i, "result": RESULT})
        elif q == "meta":
            send({"jsonrpc": "2.0", "id": i, "result": dict(
                RESULT, answer="I could not find that.", abstained=True, citations=[],
                abstention_reason="NO_EVIDENCE", scope_file_count=4,
                index_coverage={"indexed": 3, "total": 5, "keyword_only": 2, "used_provisional": False},
                warning_codes=["no_citations"], scope_label=HOSTILE)})
        elif q == "badshape":
            send({"jsonrpc": "2.0", "id": i, "result": {"nope": 1}})
        else:
            for p in ("retrieving", "retrieving", "reading", "answering"):
                progress(i, p)
            if params.get("stream") is True:
                for t in ("The rate ", "is 2,650 ", "[rates.txt#1]."):
                    send({"jsonrpc": "2.0", "method": "delta", "params": {"text": t, "id": i}})
                progress(i, "validating")
            send({"jsonrpc": "2.0", "id": i, "result": RESULT})
    else:
        send({"jsonrpc": "2.0", "id": i, "error": {"code": -32601, "message": "method not found"}})
