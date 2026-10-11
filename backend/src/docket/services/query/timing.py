"""Per-question wall-clock phases, isolated across threads and agent tasks.

Nested spans are exclusive in the result: a citation repair is generation,
not validation time. Bookkeeping and agent orchestration are ``overhead``.
Skipped phases are zero. No model, prompt or retrieval options are changed.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
from threading import Lock
from typing import Callable, Iterator

PHASES = (
    "scope_resolve", "rewrite", "search", "resolve_evidence",
    "prompt_assembly", "generation", "citation_validation",
)


@dataclass
class QuestionTiming:
    on_progress: Callable[[str], None] | None = None
    started: float = field(default_factory=lambda: time.perf_counter())
    spans: list[tuple[float, float, str, int]] = field(default_factory=list)
    mutex: Lock = field(default_factory=Lock)

    def finish(self) -> tuple[dict[str, float], float]:
        ended = time.perf_counter()
        total = ended - self.started
        values = dict.fromkeys((*PHASES, "overhead"), 0.0)
        # Agent task contexts share this recorder. Partition overlapping or
        # nested intervals rather than summing inclusive spans twice.
        with self.mutex:
            spans = list(self.spans)
        edges = sorted({self.started, ended, *(v for s in spans for v in s[:2])})
        for left, right in zip(edges, edges[1:]):
            active = [s for s in spans if s[0] <= left and s[1] >= right]
            name = max(active, key=lambda s: s[3])[2] if active else "overhead"
            values[name] = values.get(name, 0.0) + right - left
        return values, total


_timing: ContextVar[QuestionTiming | None] = ContextVar("docket_question_timing", default=None)
_depth: ContextVar[int] = ContextVar("docket_timing_depth", default=0)


@contextmanager
def phase(name: str) -> Iterator[None]:
    recorder = _timing.get()
    if recorder is None:
        yield
        return
    started = time.perf_counter()
    depth = _depth.get() + 1
    token = _depth.set(depth)
    try:
        if recorder.on_progress is not None:
            try:
                recorder.on_progress(name)
            except Exception:
                pass  # Display callbacks must never alter question behaviour.
        yield
    finally:
        ended = time.perf_counter()
        _depth.reset(token)
        with recorder.mutex:
            recorder.spans.append((started, ended, name, depth))


def timed_phase(name: str):
    def decorate(method):
        @wraps(method)
        def timed(*args, **kwargs):
            with phase(name):
                return method(*args, **kwargs)
        return timed
    return decorate


def record_question(method):
    @wraps(method)
    def timed(*args, **kwargs):
        recorder = QuestionTiming(on_progress=kwargs.get("on_progress"))
        token = _timing.set(recorder)
        depth_token = _depth.set(0)
        try:
            result = method(*args, **kwargs)
            phases, total = recorder.finish()
            return result.model_copy(update={"latency_by_phase": phases, "latency_s": total})
        finally:
            _depth.reset(depth_token)
            _timing.reset(token)
    return timed
