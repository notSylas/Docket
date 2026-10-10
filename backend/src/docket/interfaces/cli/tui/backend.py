"""The seam between the Screen A views and whatever supplies their data.

``TuiBackend`` is the only thing the application controller and the overlays
know about. Two implementations exist:

* ``FakeBackend`` (here): the invented demo content in ``fake_data`` (used by
  ``docket tui-demo`` and by most headless tests).
* ``RealBackend`` (``real_backend.py``): built from ``AppContext`` (used by
  ``docket ui``).

View-model records are the same dataclasses the prototype already rendered
(``World``/``FakeSource``/``FakeFile``/``FakeCitation``/``FakeAnswer`` from
``fake_data``); they are re-exported here under neutral names so a real
backend does not have to spell "Fake".

Threading contract: ``run_index`` and ``ask`` are blocking calls that the
controller runs on a worker thread. Everything else is called on the UI thread
and must be quick (a local database read or a small filesystem check) except
``readiness``, which may touch the network and is also run on a worker. A
backend never touches the UI; it reports through the callbacks it is given,
which receive immutable snapshots.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

from docket.interfaces.cli.tui import fake_data as fd

World = fd.World
SourceView = fd.FakeSource
FileView = fd.FakeFile
CitationView = fd.FakeCitation
AnswerView = fd.FakeAnswer


class BackendError(Exception):
    """A user-presentable failure of a backend action (message is shown as is)."""


@dataclass(frozen=True)
class ReadinessItem:
    label: str
    value: str
    bad: bool = False


@dataclass(frozen=True)
class ReadinessReport:
    items: tuple[ReadinessItem, ...]
    blocked: bool = False
    help: str = ""
    message: str = ""  # one line shown after an explicit "Check again"
    checking: bool = False


@dataclass(frozen=True)
class FolderCheck:
    ok: bool
    path: str = ""  # normalized path to register when ok
    message: str = ""  # why not, when not ok


@dataclass(frozen=True)
class JobRecord:
    name: str
    state: str  # Completed | Partial | Failed | Interrupted
    when: str
    summary: str


@dataclass(frozen=True)
class IndexProgress:
    """Immutable snapshot of one indexing run, published after every event."""

    index: int = 0  # files finished
    total: int = 0  # 0 until discovery has finished
    current: str = ""  # file being processed, relative to the source folder
    indexed: int = 0
    unchanged: int = 0
    failed: int = 0
    failures: tuple[tuple[str, str], ...] = ()  # (relative path, one-line reason)


@dataclass(frozen=True)
class IndexOutcome:
    state: str  # done | stopped | failed
    progress: IndexProgress
    reason: str = ""
    notices: tuple[str, ...] = ()  # dim follow-up lines (skipped folders, stale chunks)


@dataclass(frozen=True)
class Turn:
    question: str
    answer: str  # the original, tagged answer text (kept for follow-ups)


@dataclass(frozen=True)
class AskRequest:
    question: str
    mode: str  # a key from backend.modes
    history: tuple[Turn, ...] = ()


@dataclass(frozen=True)
class AskOutcome:
    answer: AnswerView | None = None
    history_answer: str = ""  # raw tagged answer for the conversation memory
    error: str = ""  # non-empty when the question could not be answered


IndexCallback = Callable[[IndexProgress], None]
StageCallback = Callable[[str], None]


class TuiBackend(Protocol):
    demo: bool  # True: invented data, timer-driven simulated operations
    scope_enforced: bool  # False: a chosen scope is display-only (see Upgrade/16)
    can_open_original: bool
    intro_notice: str
    supported_formats: str
    data_dir_label: str
    embed_model: str
    default_model: str
    answer_models: tuple[tuple[str, bool, str], ...]  # (name, installed, note)
    thinking_levels: tuple[str, ...]
    modes: tuple[tuple[str, str, str, bool, str], ...]  # (key, label, description, available, why not)
    command_extras: tuple[tuple[str, str, str], ...]  # (name, summary, why unavailable or "")
    settings_note: str  # shown after Settings > Save
    jobs_hint: str
    jobs_note: str
    stage_search: str
    stage_citations: str

    def load_world(self) -> World: ...
    def readiness(self) -> ReadinessReport: ...
    def job_history(self, *, running: bool) -> list[JobRecord]: ...
    def system_rows(self, world: World, readiness: ReadinessReport) -> list[tuple[str, str]]: ...
    def folder_suggestions(self, typed: str) -> list[str]: ...
    def check_folder(self, path: str) -> FolderCheck: ...
    def is_broad_location(self, path: str) -> bool: ...
    def add_source(self, path: str) -> SourceView: ...
    def disconnect_source(self, source_id: str) -> None: ...
    def reconnect_source(self, source_id: str) -> None: ...
    def job_finished(self, source_id: str, on_done: str) -> None: ...
    def open_original_note(self, citation: CitationView) -> str: ...
    def run_index(
        self,
        source_id: str,
        on_progress: IndexCallback,
        should_stop: Callable[[], bool],
    ) -> IndexOutcome: ...
    def ask(self, request: AskRequest, on_stage: StageCallback, scope_label: str, mode_label: str) -> AskOutcome: ...


class FakeBackend:
    """Invented data only. Nothing here reads the file system, a model or a database."""

    demo = True
    scope_enforced = True
    can_open_original = True
    intro_notice = fd.INTRO_NOTICE
    supported_formats = fd.SUPPORTED_FORMATS
    data_dir_label = fd.DATA_DIR_LABEL
    embed_model = fd.EMBED_MODEL
    default_model = fd.DEFAULT_MODEL
    answer_models = fd.ANSWER_MODELS
    thinking_levels = fd.THINKING_LEVELS
    modes = fd.MODES
    command_extras = fd.COMMAND_EXTRAS
    settings_note = "Settings saved for this demo session (nothing is written to disk)."
    jobs_hint = "history is demo data"
    jobs_note = ""
    stage_search = fd.QUERY_STAGES[0]
    stage_citations = fd.QUERY_STAGES[-1]

    def __init__(self, *, empty: bool = False, blocked: bool = False) -> None:
        self.blocked = blocked
        self.world = fd.World.fresh(empty=empty)

    # -- state -----------------------------------------------------------
    def load_world(self) -> World:
        return self.world  # the same live object: the demo mutates it in place

    def readiness(self) -> ReadinessReport:
        pairs = fd.READINESS_BLOCKED if self.blocked else fd.READINESS_OK
        items = tuple(ReadinessItem(k, v, self.blocked and "not" in v.lower()) for k, v in pairs)
        if self.blocked:
            return ReadinessReport(items, True, fd.BLOCKED_HELP, "Checked just now (demo): still blocked.")
        return ReadinessReport(items, False, "", "Checked just now (demo): everything is available.")

    def job_history(self, *, running: bool) -> list[JobRecord]:
        return [JobRecord(*row) for row in fd.JOB_HISTORY]

    def system_rows(self, world: World, readiness: ReadinessReport) -> list[tuple[str, str]]:
        return [
            ("Data folder", fd.DATA_DIR_LABEL),
            ("Ollama", "Available (demo)"),
            ("Answer model", self.default_model),
            ("Embedding", fd.EMBED_MODEL),
            ("Index", "Compatible with the embedding model (demo)"),
            ("Coverage", f"{world.ready_files()} files searchable"),
        ]

    # -- folders and sources ---------------------------------------------
    def folder_suggestions(self, typed: str) -> list[str]:
        t = typed.strip()
        if not t:
            return []
        return [p for p in fd.FOLDER_SUGGESTIONS if p.lower().startswith(t.lower()) and p != t]

    def check_folder(self, path: str) -> FolderCheck:
        for s in self.world.sources:
            if s.path == path:
                if s.status == fd.DISCONNECTED:
                    return FolderCheck(False, path, f"{s.name} is disconnected. Use Reconnect in Sources instead of adding it again.")
                return FolderCheck(False, path, f"{s.name} is already registered at that path.")
        return FolderCheck(True, path)

    def is_broad_location(self, path: str) -> bool:
        return path in (".", "~", "/")

    def add_source(self, path: str) -> SourceView:
        return self.world.new_source_from_path(path)

    def disconnect_source(self, source_id: str) -> None:
        s = self.world.find(source_id)
        if s:
            s.status = fd.DISCONNECTED
            s.note = "Disconnected by you. Stored originals are kept; it is not searched."

    def reconnect_source(self, source_id: str) -> None:
        return None  # the simulated job marks it ready when it completes

    def job_finished(self, source_id: str, on_done: str) -> None:
        src = self.world.find(source_id)
        if src and on_done == "ready":
            src.status, src.note = fd.READY, ""

    def open_original_note(self, citation: CitationView) -> str:
        return f"Demo: Docket would confirm {citation.rel_path} still matches the stored version, then open it with your system opener."

    # -- operations (the demo drives these from its own timer) ------------
    def run_index(self, source_id: str, on_progress: IndexCallback, should_stop: Callable[[], bool]) -> IndexOutcome:
        raise NotImplementedError("the demo simulates indexing with its timer")

    def ask(self, request: AskRequest, on_stage: StageCallback, scope_label: str, mode_label: str) -> AskOutcome:
        a = fd.answer_for(request.question, scope_label, mode_label)
        return AskOutcome(answer=a, history_answer=a.text)


def _first_line(exc: BaseException) -> str:
    lines = (str(exc) or type(exc).__name__).strip().splitlines()
    return (lines[0] if lines else type(exc).__name__)[:200]


def describe_error(exc: BaseException, gen_model: str = "") -> str:
    """One short, user-presentable line for a failure (mirrors interactive/errors.py)."""
    import re

    from docket.infra.inference.gateway import InferenceError, InferenceUnavailableError, ModelNotFoundError
    from docket.services.ingestion.pipeline import SourceNotActiveError
    from docket.services.sources.manager import SourceNotFoundError

    if isinstance(exc, InferenceUnavailableError):
        return "Can't reach Ollama. Is it running? (ollama serve)"
    if isinstance(exc, ModelNotFoundError):
        match = re.search(r"[Mm]odel '([^']+)'", str(exc))
        model = match.group(1) if match else (gen_model or "<model>")
        return f"Model not found: {model}. Run: ollama pull {model}"
    if isinstance(exc, InferenceError):
        return f"Inference error: {_first_line(exc)}"
    if isinstance(exc, (SourceNotFoundError, SourceNotActiveError, ValueError, BackendError)):
        return f"Error: {_first_line(exc)}"
    return f"Unexpected error ({type(exc).__name__}): {_first_line(exc)}"

