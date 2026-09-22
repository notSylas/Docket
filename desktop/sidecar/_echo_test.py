"""Throwaway test sidecar for the CP-IPC-bridge checkpoint.

NOT the real backend stub (that's main.py). This script speaks the trivial
JSON-line request/response protocol the Rust bridge expects, so the bridge's
id-correlation machinery can be verified independently of the real backend:

  request:  {"id": "...", "op": "ping"|"echo"|..., "params": {...}}
  response: {"id": "...", "ok": true, "result": {...}}
         or {"id": "...", "ok": false, "error": {"type": "...", "message": "..."}}

Reads JSON lines from stdin in a loop until EOF, writing one JSON response
line (flushed immediately -- buffered stdout would hang the Rust side) per
request received.
"""
import json
import sys


def handle(request):
    req_id = request.get("id")
    op = request.get("op")

    if op == "ping":
        return {"id": req_id, "ok": True, "result": {"pong": True}}

    if op == "echo":
        message = (request.get("params") or {}).get("message")
        return {"id": req_id, "ok": True, "result": {"echoed": message}}

    return {
        "id": req_id,
        "ok": False,
        "error": {"type": "UnknownOp", "message": f"unknown op: {op!r}"},
    }


def main():
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            response = {
                "id": None,
                "ok": False,
                "error": {"type": "InvalidJSON", "message": str(exc)},
            }
        else:
            response = handle(request)

        print(json.dumps(response), flush=True)


if __name__ == "__main__":
    main()
