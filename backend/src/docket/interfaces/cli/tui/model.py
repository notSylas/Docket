"""Small view-model types for the prototype (no I/O, no backend)."""

from __future__ import annotations

import time
from typing import Any
from dataclasses import dataclass, field

from docket.interfaces.cli.tui import fake_data as fd


@dataclass(frozen=True)
class Scope:
    kind: str = "all"  # all | source | folder | file
    source_id: str = ""
    file_path: str = ""

    def label(self, world: fd.World) -> str:
        if self.kind == "all":
            return "All ready sources"
        src = world.find(self.source_id)
        name = src.name if src else "Unknown source"
        if self.kind == "source":
            return name
        if self.kind == "folder":
            return f"{name} / {self.file_path}/"
        return f"{name} / {self.file_path}"


@dataclass
class Message:
    role: str  # user | assistant | system
    text: str
    level: str = "info"  # system only: info | attention | error
    answer: fd.FakeAnswer | None = None
    mode_label: str = ""
    elapsed: float | None = None
    scope_label: str = ""
    uid: int = 0
    history_answer: str = ""  # tagged answer text kept for follow-up context


@dataclass
class IndexJob:
    source_id: str
    source_name: str
    verb: str = "Indexing"
    total: int = fd.INDEX_TOTAL_FILES
    file_index: int = 0  # files completed
    stage_index: int = 0
    ticks: int = 0
    indexed: int = 0
    unchanged: int = 0
    failed: int = 0
    state: str = "running"  # running | stopping | done | stopped | failed | busy
    reason: str = ""
    failures: list[tuple[str, str]] = field(default_factory=list)
    on_done: str = ""  # "ready" -> mark source ready on completion
    # Real runs (a worker thread feeds snapshots in; nothing is simulated).
    real: bool = False
    current_name: str = ""
    started: float | None = None
    ended: float | None = None
    notices: tuple[str, ...] = ()
    paused: bool = False  # waiting between files while a question is answered
    # "index" (read a folder), "update" (library maintenance) or "retry" (one file).
    kind: str = "index"
    stage_text: str = ""  # real update runs: the plain stage label

    @property
    def active(self) -> bool:
        return self.state in ("running", "stopping")

    @property
    def current_file(self) -> str:
        if self.real:
            return self.current_name
        n = min(self.file_index + 1, self.total)
        return f"file-{n:02d}.pdf"

    @property
    def elapsed_label(self) -> str:
        if self.real:
            secs = int(((self.ended if self.ended is not None else time.monotonic()) - (self.started or time.monotonic())))
        else:
            secs = self.ticks * 2
        return f"{secs // 60:02d}:{secs % 60:02d}"

    @property
    def stage(self) -> str:
        """Simulated jobs report a stage; real ones only know file events."""
        if self.real:
            return self.stage_text
        return fd.INDEX_STAGES[self.stage_index % len(fd.INDEX_STAGES)]

    def advance(self, *, blocked: bool = False) -> None:
        """One progress update. Pure state change; the caller redraws."""
        if not self.active:
            return
        self.ticks += 1
        if blocked:
            self.state, self.reason = "failed", "The answer model is not installed (demo)."
            return
        if self.state == "stopping":
            self.state = "stopped"
            return
        self.stage_index += 3
        if self.stage_index >= len(fd.INDEX_STAGES):
            self.stage_index = 0
            done_idx = self.file_index
            self.file_index += 1
            if done_idx in fd.INDEX_FAILED_AT:
                self.failed += 1
                self.failures.append((f"file-{done_idx + 1:02d}.pdf", "Document is corrupt (demo)"))
            elif done_idx in fd.INDEX_UNCHANGED_AT:
                self.unchanged += 1
            else:
                self.indexed += 1
            if self.file_index >= self.total:
                self.state = "done"

    def request_stop(self) -> None:
        if self.state == "running":
            self.state = "stopping"


@dataclass
class QueryOp:
    question: str
    stage_index: int = 0
    state: str = "running"  # running | stopping | cancelled | done
    scope_label: str = ""
    scope: Any = None  # the `Scope` chosen when the question was asked
    mode_label: str = ""
    real: bool = False
    stage_name: str = ""
    started: float | None = None
    retry: bool = False
    coverage: dict | None = None  # {"indexed", "total", "indexing"} when asked during indexing

    @property
    def stage(self) -> str:
        if self.real:
            return self.stage_name
        return fd.QUERY_STAGES[min(self.stage_index, len(fd.QUERY_STAGES) - 1)]

    @property
    def elapsed_secs(self) -> int:
        return int(time.monotonic() - self.started) if self.started is not None else 0

    @property
    def active(self) -> bool:
        return self.state in ("running", "stopping")

    def advance(self) -> None:
        if self.state == "stopping":
            self.state = "cancelled"
        elif self.state == "running":
            self.stage_index += 1
            if self.stage_index >= len(fd.QUERY_STAGES):
                self.state = "done"


INDEXING_NOTE = (
    "Some documents are still being indexed, so this may be missing content - "
    "try again when indexing finishes"
)


def coverage_text(coverage: dict | None, *, when_answered: bool = True) -> str:
    """One plain line for a coverage record, or "" when the answer was not asked mid-index."""
    if not coverage or not coverage.get("indexing"):
        return ""
    total = int(coverage.get("total") or 0)
    done = int(coverage.get("indexed") or 0)
    tail = " when answered" if when_answered else ""
    if total <= 0:
        return f"documents were still being indexed{tail}"
    noun = "document" if total == 1 else "documents"
    return f"{done} of {total} {noun} indexed{tail}"
