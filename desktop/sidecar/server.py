"""Real dispatch logic for the desktop sidecar's JSON-line wire protocol.

Mirrors the backend CLI's own thin-`main.py` / logic-lives-elsewhere split
(`attest.cli.main` is a thin Typer shell over `attest.cli.context
.build_context()`): `main.py` here is the thin stdin/stdout loop, this
module holds the actual op -> backend-call dispatch table and the
request/response envelope logic. Kept import-light and I/O-free by design
(no printing, no stdin/stdout handling) so it stays independently testable
and so `main.py` -- not this module -- owns the one place stdout hygiene
(see `main.py`'s docstring) needs to be enforced.

Wire contract (already assumed by the Rust bridge in
`desktop/src-tauri/src/lib.rs`, built in a prior checkpoint):

    request:  {"id": "req_1", "op": "sources.list", "params": {...}}
    response: {"id": "req_1", "ok": true, "result": {...}}
           or {"id": "req_1", "ok": false,
               "error": {"type": "...", "message": "...", "op": "..."}}

`dispatch()` is the only entrypoint `main.py` calls per request, and it
NEVER raises -- every exception path (known backend error, missing param,
unknown op, or a genuinely unexpected bug) is caught here and turned into a
valid error envelope. An unhandled exception in `dispatch()` would propagate
out of `main.py`'s stdin loop and kill the whole sidecar process, hanging
every pending and future frontend request -- there is no supervisor
restarting it mid-session, so this file has to be the backstop.
"""

from __future__ import annotations

import dataclasses
import sys
import traceback
from pathlib import Path
from typing import Any, Callable

from attest.inference.gateway import InferenceError
from attest.ingestion.pipeline import SourceNotActiveError
from attest.query.classifier import QueryMode
from attest.query.service import QueryService
from attest.sources.manager import SourceNotFoundError

Handler = Callable[[dict], dict]


class InvalidSourcePathError(Exception):
    """Raised in place of the raw `ValueError` that `SourceManager
    .register_source` raises for a bad/nonexistent path. `ValueError` is
    used too generically elsewhere in Python (and possibly elsewhere in the
    backend) to be a meaningful `error.type` on the wire -- a client seeing
    "ValueError" learns nothing actionable, where "InvalidSourcePath" tells
    it exactly what to fix."""


class NoContentIndexedError(Exception):
    """Raised by the `query.ask` handler when nothing has ever been
    ingested yet, i.e. `context.vector_writer.table` is still `None`.

    Why this needs an explicit check rather than just letting `QueryService`
    run and see what happens: `QueryService._ask_fast` unconditionally calls
    `attest.retrieval.hybrid.hybrid_search`, which calls `vector_search`,
    which calls `table.search(...)` with no `None` guard -- handing it a
    `None` table raises a raw `AttributeError` ("'NoneType' object has no
    attribute 'search'"), not a clean, named condition. The CLI
    (`attest.cli.main.query`) already checks `table is None` and refuses
    before ever constructing a `QueryService`; this mirrors that exact
    check so the sidecar reports the same expected condition cleanly
    instead of it surfacing as an `InternalError` with a confusing
    traceback. Checked once, before AGENT vs FAST routing even happens,
    since a `None` table breaks both paths identically (the investigation
    agent's own tools are built from the same table via
    `build_investigation_agent`)."""


def _source_to_dict(source: Any) -> dict:
    """`Source` is a SQLAlchemy ORM model, not JSON-serializable directly --
    pick out the wire-relevant fields, stringifying the enum status and the
    two datetimes."""
    return {
        "id": source.id,
        "workspace_id": source.workspace_id,
        "source_type": source.source_type,
        "path": source.path,
        "status": source.status.value,
        "created_at": source.created_at.isoformat(),
        "updated_at": source.updated_at.isoformat(),
    }


def _job_result_to_dict(result: Any) -> dict:
    """`IngestionJobResult` (and its nested `FileIngestResult`s) are plain
    `@dataclass`s, so `dataclasses.asdict` handles the structural
    conversion (including recursing into the `file_results` list) -- but
    `FileIngestResult.path` is a `pathlib.Path`, which `asdict` happily
    copies as-is rather than converting, and `json.dumps` can't serialize.
    Stringify it in the already-converted dict rather than mutating the
    original dataclass instances."""
    result_dict = dataclasses.asdict(result)
    for file_result in result_dict["file_results"]:
        file_result["path"] = str(file_result["path"])
    return result_dict


def build_dispatch_table(context: Any) -> dict[str, Handler]:
    """Returns {op_name: handler}. Each handler takes the request's
    "params" dict and returns a JSON-safe result dict -- just the "result"
    payload, not the full envelope (that's `dispatch()`'s job).

    A `QueryService` is built lazily, once, and cached in `_query_service`
    (a single-element list used as a mutable cell the nested closures can
    write to) on first `query.ask` call -- mirroring how `QueryService`
    itself lazily builds the investigation agent only on first AGENT-mode
    use: building one eagerly on every sidecar startup would mean
    constructing it (and checking for an indexed table) even for a session
    that only ever calls `sources.list`.
    """

    _query_service: list[QueryService] = []

    def _get_query_service() -> QueryService:
        if _query_service:
            return _query_service[0]
        table = context.vector_writer.table
        if table is None:
            raise NoContentIndexedError(
                "no content has been indexed yet -- ingest a source first"
            )
        service = QueryService(
            engine=context.engine,
            table=table,
            gateway=context.gateway,
            resolver=context.resolver,
            settings=context.settings,
        )
        _query_service.append(service)
        return service

    def sources_list(params: dict) -> dict:
        sources = context.source_manager.list_sources()
        return {"sources": [_source_to_dict(s) for s in sources]}

    def sources_add(params: dict) -> dict:
        path = params["path"]
        try:
            source = context.source_manager.register_source(Path(path))
        except ValueError as exc:
            raise InvalidSourcePathError(str(exc)) from exc
        return {"source": _source_to_dict(source)}

    def sources_ingest(params: dict) -> dict:
        source_id = params["source_id"]
        result = context.pipeline.run_ingestion_for_source(source_id)
        return _job_result_to_dict(result)

    def sources_revoke(params: dict) -> dict:
        source_id = params["source_id"]
        context.source_manager.deactivate_source(source_id)
        return {}

    def query_ask(params: dict) -> dict:
        question = params["question"]
        raw_mode = params.get("mode")
        mode = QueryMode(raw_mode) if raw_mode is not None else None
        service = _get_query_service()
        result = service.ask(question, mode=mode)
        return result.model_dump()

    return {
        "sources.list": sources_list,
        "sources.add": sources_add,
        "sources.ingest": sources_ingest,
        "sources.revoke": sources_revoke,
        "query.ask": query_ask,
    }


# One dispatch table per `context` instance, built lazily on first use and
# reused for the rest of the process's life -- `main.py` builds exactly one
# `AppContext` for the whole sidecar run and calls `dispatch()` once per
# request against it, so this just avoids rebuilding the (cheap, but not
# free) closures on every single request. Keyed by `id(context)` rather
# than holding the table in a global directly, so a test harness that
# builds multiple contexts in one process (e.g. across test cases) doesn't
# see stale handlers from a previous context.
_dispatch_tables: dict[int, dict[str, Handler]] = {}


def _get_dispatch_table(context: Any) -> dict[str, Handler]:
    key = id(context)
    table = _dispatch_tables.get(key)
    if table is None:
        table = build_dispatch_table(context)
        _dispatch_tables[key] = table
    return table


def _error_response(req_id: Any, error_type: str, message: str, op: Any) -> dict:
    return {
        "id": req_id,
        "ok": False,
        "error": {"type": error_type, "message": message, "op": op},
    }


def dispatch(context: Any, request: dict) -> dict:
    """Look up `request["op"]` in `context`'s dispatch table, call it with
    `request["params"]`, and return a full response envelope. Never raises:
    every exception path below produces a valid envelope instead, since an
    unhandled exception here would propagate into `main.py`'s stdin loop
    and kill the sidecar process outright (see module docstring)."""
    req_id = request.get("id")
    op = request.get("op")
    params = request.get("params") or {}

    dispatch_table = _get_dispatch_table(context)
    handler = dispatch_table.get(op)
    if handler is None:
        return _error_response(req_id, "UnknownOp", f"unknown op: {op!r}", op)

    try:
        result = handler(params)
    except KeyError as exc:
        return _error_response(
            req_id, "MissingParam", f"missing required param: {exc}", op
        )
    except SourceNotFoundError as exc:
        return _error_response(req_id, "SourceNotFoundError", str(exc), op)
    except SourceNotActiveError as exc:
        return _error_response(req_id, "SourceNotActiveError", str(exc), op)
    except InvalidSourcePathError as exc:
        return _error_response(req_id, "InvalidSourcePath", str(exc), op)
    except NoContentIndexedError as exc:
        return _error_response(req_id, "NoContentIndexed", str(exc), op)
    except InferenceError as exc:
        # Covers InferenceUnavailableError / ModelNotFoundError (and any
        # other InferenceError subclass) via the real exception's own class
        # name -- passthrough, not translated, since these are already
        # specific and meaningful to a client.
        return _error_response(req_id, type(exc).__name__, str(exc), op)
    except Exception:  # noqa: BLE001 -- last-resort backstop, see docstring
        traceback.print_exc(file=sys.stderr)
        return _error_response(
            req_id, "InternalError", "an internal error occurred", op
        )

    return {"id": req_id, "ok": True, "result": result}
