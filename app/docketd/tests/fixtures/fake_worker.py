#!/usr/bin/env python3
"""Fake docket worker for docketd tests. Behaviour is chosen by the question."""
import json, os, sys, time

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

for line in sys.stdin:
    try:
        req = json.loads(line)
    except ValueError:
        send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
        continue
    i, method, params = req.get("id"), req.get("method"), req.get("params") or {}
    if method == "ping":
        send({"jsonrpc": "2.0", "id": i, "result": {"pong": True}})
    elif method == "status":
        send({"jsonrpc": "2.0", "id": i, "result": {"worker": "ok", "model": "fake-model", "chunks": 5}})
    elif method == "sources.list":
        send({"jsonrpc": "2.0", "id": i, "result": {"sources": [
            {"id": "s1", "display_name": "Docs", "path": "/x/Docs", "status": "active", "files": 3}]}})
    elif method == "query.ask":
        q = params.get("question", "")
        if q == "crash":
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
        elif q == "slow":
            progress(i, "retrieving")
            time.sleep(1.5)
            send({"jsonrpc": "2.0", "id": i, "result": RESULT})
        elif q == "badshape":
            send({"jsonrpc": "2.0", "id": i, "result": {"nope": 1}})
        else:
            for p in ("retrieving", "retrieving", "reading", "answering"):
                progress(i, p)
            send({"jsonrpc": "2.0", "id": i, "result": RESULT})
    else:
        send({"jsonrpc": "2.0", "id": i, "error": {"code": -32601, "message": "method not found"}})
