"""Line-delimited JSON-RPC 2.0 worker for the Rust service (`docketd`).

One JSON object per line on stdin (requests) and stdout (responses and
notifications). Nothing else may ever reach stdout: at start-up the real
stdout is duplicated for the protocol and file descriptor 1 is pointed at
stderr, so stray prints and native-library noise cannot corrupt the stream.
Logs go to stderr, which the daemon captures; log lines never carry question
or answer text.

Methods: ``ping``, ``status``, ``sources.list``, ``query.ask``.
``query.ask`` emits ``progress`` notifications (no ``id``) with a ``phase`` of
``retrieving``, ``reading`` or ``answering`` before the final response.

The worker is read-only: it reuses ``SourceManager``/``QueryService`` exactly as
the CLI and TUI do, performs no ingestion and takes no writer lock. Requests
are handled one at a time; the daemon allows a single in-flight query.
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any, Callable, TextIO

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
SERVER_ERROR = -32000

PROTOCOL_VERSION = 1
EXCERPT_CHARS = 400

Notify = Callable[[str, dict], None]
Method = Callable[[dict, Notify], Any]

_PHASES = {
    "scope_resolve": "retrieving",
    "rewrite": "retrieving",
    "search": "retrieving",
    "resolve_evidence": "reading",
    "prompt_assembly": "answering",
    "generation": "answering",
    "citation_validation": "answering",
    "compute": "answering",
}


class RpcError(Exception):
    def __init__(self, code: int, message: str, kind: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.kind = kind


def _error(req_id: Any, code: int, message: str, kind: str | None = None) -> dict:
    err: dict[str, Any] = {"code": code, "message": message}
    if kind:
        err["data"] = {"kind": kind}
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


def _safe_message(exc: BaseException) -> str:
    from docket.core.privacy import redact_text

    first = (str(exc).strip().splitlines() or [""])[0][:200]
    return redact_text(first) or type(exc).__name__


def handle_line(line: str, methods: dict[str, Method], write: Callable[[dict], None]) -> None:
    """Handle one request line. Never raises."""
    if not line.strip():
        return
    try:
        req = json.loads(line)
    except ValueError:
        write(_error(None, PARSE_ERROR, "parse error"))
        return
    if not isinstance(req, dict) or req.get("jsonrpc") != "2.0" or not isinstance(req.get("method"), str):
        write(_error(req.get("id") if isinstance(req, dict) else None, INVALID_REQUEST, "invalid request"))
        return
    req_id = req.get("id")
    is_notification = "id" not in req
    params = req.get("params", {})
    if params is None:
        params = {}
    if not isinstance(params, dict):
        if not is_notification:
            write(_error(req_id, INVALID_PARAMS, "params must be an object"))
        return
    method = methods.get(req["method"])
    if method is None:
        if not is_notification:
            write(_error(req_id, METHOD_NOT_FOUND, "method not found"))
        return

    def notify(name: str, body: dict) -> None:
        write({"jsonrpc": "2.0", "method": name, "params": {**body, "id": req_id}})

    try:
        result = method(params, notify)
    except RpcError as exc:
        if not is_notification:
            write(_error(req_id, exc.code, exc.message, exc.kind))
        return
    except Exception as exc:  # noqa: BLE001 -- the loop must survive any handler bug
        print(f"worker: {req['method']} failed: {type(exc).__name__}", file=sys.stderr, flush=True)
        if not is_notification:
            write(_error(req_id, SERVER_ERROR, _safe_message(exc), type(exc).__name__))
        return
    if not is_notification:
        write({"jsonrpc": "2.0", "id": req_id, "result": result})


def serve(methods: dict[str, Method], rfile: TextIO, wfile: TextIO) -> None:
    def write(obj: dict) -> None:
        wfile.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")
        wfile.flush()

    for line in rfile:
        handle_line(line, methods, write)


def build_methods(context: Any, service_factory: Callable[[Any, Any], Any]) -> dict[str, Method]:
    """Wire the protocol methods to the existing services.

    ``context`` is an ``AppContext``-shaped object; ``service_factory(context,
    table)`` returns a ``QueryService`` (the interactive session's factory).
    """
    started = time.monotonic()

    def ping(_params: dict, _notify: Notify) -> dict:
        return {"pong": True, "protocol": PROTOCOL_VERSION, "uptime_s": round(time.monotonic() - started, 1)}

    def status(_params: dict, _notify: Notify) -> dict:
        settings = context.settings
        out: dict[str, Any] = {
            "worker": "ok",
            "protocol": PROTOCOL_VERSION,
            "model": str(settings.gen_model),
            "embed_model": str(settings.embed_model),
            "chunks": None,
        }
        try:
            out["chunks"] = int(context.readiness.snapshot().chunks)
        except Exception:  # noqa: BLE001
            pass
        return out

    def sources_list(_params: dict, _notify: Notify) -> dict:
        from docket.services.sources.resolve import display_names

        sources = [s for s in context.source_manager.list_sources() if s.status.value != "deleted"]
        names = display_names(sources)
        try:
            files = context.readiness.files_by_source()
        except Exception:  # noqa: BLE001
            files = {}
        return {
            "sources": [
                {
                    "id": s.id,
                    "display_name": names[s.id],
                    "path": s.path,
                    "status": s.status.value,
                    "files": int(files.get(s.id, 0)),
                }
                for s in sources
            ]
        }

    def _excerpt(chunk_id: str) -> str:
        try:
            text = context.resolver.resolve(chunk_id).text
        except Exception:  # noqa: BLE001 -- an unresolvable chunk still has a label
            return ""
        text = " ".join(str(text).split())
        return text[:EXCERPT_CHARS]

    def query_ask(params: dict, notify: Notify) -> dict:
        question = params.get("question")
        if not isinstance(question, str) or not question.strip():
            raise RpcError(INVALID_PARAMS, "question must be a non-empty string")
        scope_param = params.get("scope")
        scope = None
        if scope_param is not None:
            from docket.services.query.scope import QueryScope

            if isinstance(scope_param, str):
                scope = QueryScope.for_source(scope_param)
            elif isinstance(scope_param, dict) and scope_param.get("source_id"):
                scope = QueryScope.for_source(str(scope_param["source_id"]))
            elif isinstance(scope_param, dict) and scope_param.get("folder"):
                scope = QueryScope.for_folder(str(scope_param["folder"]))
            else:
                raise RpcError(INVALID_PARAMS, "scope must be a source id or {source_id|folder}")
        t0 = time.perf_counter()
        notify("progress", {"phase": "retrieving"})
        # Fresh table handle per question so a concurrent index write is seen.
        table = context.vector_writer.table
        if table is None:
            raise RpcError(SERVER_ERROR, "nothing is indexed yet", "NoContentIndexed")
        seen: list[str] = ["retrieving"]

        def on_progress(phase: str) -> None:
            name = _PHASES.get(phase, "retrieving")
            if name != seen[-1]:
                seen.append(name)
                notify("progress", {"phase": name})

        service = service_factory(context, table)
        kwargs: dict[str, Any] = {"on_progress": on_progress}
        if scope is not None:
            kwargs["scope"] = scope
        result = service.ask(question, **kwargs)
        total = time.perf_counter() - t0
        timings: dict[str, Any] = {"total_s": round(total, 3)}
        phases = getattr(result, "latency_by_phase", None)
        if phases:
            timings["by_phase"] = {k: round(float(v), 3) for k, v in phases.items()}
        return {
            "answer": result.answer,
            "abstained": bool(result.abstained),
            "citations": [
                {
                    "label": c.citation_label,
                    "source_display_name": c.source_display_name,
                    "chunk_id": c.chunk_id,
                    "excerpt": _excerpt(c.chunk_id),
                }
                for c in result.citations
            ],
            "timings": timings,
        }

    return {"ping": ping, "status": status, "sources.list": sources_list, "query.ask": query_ask}


def run() -> None:
    """Entry point for `docket worker`."""
    # Reserve the real stdout for the protocol, then send fd 1 to stderr.
    proto = os.fdopen(os.dup(1), "w", encoding="utf-8", buffering=1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    from docket.core.privacy import RedactedLogStream

    sys.stderr = RedactedLogStream(sys.stderr)
    from docket.interfaces.cli.context import build_context
    from docket.interfaces.cli.interactive.session import _default_factory

    context = build_context()
    try:
        serve(build_methods(context, _default_factory), sys.stdin, proto)
    finally:
        try:
            context.engine.dispose()
        except Exception:  # noqa: BLE001
            pass
