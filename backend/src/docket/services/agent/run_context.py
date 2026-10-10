"""Per-run state for one `QueryService.ask()` call.

`QueryService` (and the compiled agent graph it caches) is shared across
questions and may serve several concurrent asks. Anything that belongs to ONE
run therefore lives in a `RunContext`, created per ask and never stored on the
service. The agent's `search_knowledge` tool is built once, so it reads the
active run through a `ContextVar`: each thread (and each LangGraph executor
task, which runs under a copy of the caller's context) sees only its own run.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Iterator


@dataclass
class RunContext:
    # Follow-up rewrite added as an extra query to every search_knowledge call.
    extra_queries: list[str] = field(default_factory=list)
    # Evidence versions an explicitly named file scopes every search to.
    scope_ids: list[str] = field(default_factory=list)
    # The scope searches actually run inside: the outer query scope intersected
    # with `scope_ids`. `None` = unrestricted; an EMPTY list means "matches
    # nothing" and must never be treated as unrestricted.
    search_scope_ids: list[str] | None = None
    # The outer query-scope boundary alone (reads must stay inside it). `None`
    # = no boundary.
    boundary_ids: list[str] | None = None


_current_run: ContextVar[RunContext | None] = ContextVar("docket_current_run", default=None)


@contextmanager
def bind_run_context(run: RunContext) -> Iterator[RunContext]:
    """Make `run` the active context for the current thread/task."""
    token = _current_run.set(run)
    try:
        yield run
    finally:
        _current_run.reset(token)


def current_extra_queries() -> list[str]:
    run = _current_run.get()
    return run.extra_queries if run is not None else []


def current_scope_ids() -> list[str]:
    run = _current_run.get()
    return run.scope_ids if run is not None else []


def current_search_scope() -> list[str] | None:
    """Versions searches must stay inside; None = unrestricted, [] = nothing."""
    run = _current_run.get()
    return run.search_scope_ids if run is not None else None


def current_boundary_ids() -> list[str] | None:
    """The outer scope boundary evidence reads must stay inside; None = none."""
    run = _current_run.get()
    return run.boundary_ids if run is not None else None
