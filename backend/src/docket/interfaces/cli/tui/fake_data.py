"""All demo content for the Screen A prototype.

Everything in this module is invented for visual review. Nothing here comes from
a database, a model or the file system, and no view may hard-code product
content: views ask this module (and only this module) for strings and records.

When the real backend is wired in (stage 4b), this module is the seam: each
function/dataclass here is replaced by a service call listed in
``Upgrade/15-terminal-ui-prototype-review.md``.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field

DEMO_TAG = "DEMO DATA"

# -- status vocabulary --------------------------------------------------------

READY, FAILED, DISCONNECTED = "ready", "failed", "disconnected"
STATUS_LABEL = {READY: "Ready", FAILED: "Failed", DISCONNECTED: "Disconnected"}

FILE_READY, FILE_FAILED, FILE_PENDING, FILE_EMPTY = "ready", "failed", "pending", "empty"
FILE_LABEL = {
    FILE_READY: "Ready",
    FILE_FAILED: "Failed",
    FILE_PENDING: "Pending",
    FILE_EMPTY: "Processed — no searchable text",
}


@dataclass
class FakeFile:
    path: str
    fmt: str
    status: str = FILE_READY
    chunks: int = 12
    error: str = ""


@dataclass
class FakeSource:
    id: str
    name: str
    path: str
    status: str
    files: list[FakeFile] = field(default_factory=list)
    last_indexed: str = "10 Oct 2026 10:42"
    note: str = ""

    def count(self, file_status: str) -> int:
        return sum(1 for f in self.files if f.status == file_status)

    @property
    def searchable(self) -> int:
        return self.count(FILE_READY) if self.status == READY else 0


@dataclass(frozen=True)
class FakeCitation:
    source_name: str  # file name only; always equals the last part of rel_path
    rel_path: str
    location: str | None
    passage: str
    available: bool = True
    reason: str = ""
    version: int = 3
    versions: int = 3  # versions stored for this file; the newest is current
    indexed: str = "10 Oct 2026 10:42"
    chunk_id: str = "chunk-demo-0001"
    source: str = ""  # registered source the file belongs to
    # Spreadsheet passages: header row first, then data rows. `table_origin` is the
    # address of the top-left cell (e.g. "B7"); `cited` lists the cells the answer used.
    table: tuple[tuple[str, ...], ...] = ()
    table_origin: str = ""
    cited: tuple[str, ...] = ()

    @property
    def version_label(self) -> str:
        state = "current" if self.version >= self.versions else "superseded"
        return f"version {self.version} of {self.versions} ({state})"


def cell_address(origin: str, row: int, col: int) -> str:
    """Address of the cell `row`, `col` (0-based) inside a table whose top-left is `origin`."""
    letters = "".join(c for c in origin if c.isalpha()).upper()
    number = int("".join(c for c in origin if c.isdigit()) or "1")
    return f"{chr(ord(letters[0]) + col)}{number + row}"


def table_range(origin: str, table: tuple[tuple[str, ...], ...]) -> str:
    rows, cols = len(table), max((len(r) for r in table), default=0)
    return f"{origin}:{cell_address(origin, rows - 1, cols - 1)}"


@dataclass(frozen=True)
class FakeAnswer:
    text: str
    citations: tuple[FakeCitation, ...]
    mode_label: str = "Quick search"
    elapsed: float = 8.2
    scope_label: str = "All ready sources"
    rewrite: str = ""
    ambiguity: str = ""
    warnings: tuple[str, ...] = ()
    compute: str = "Not used"
    abstained: bool = False
    abstain_reason: str = ""
    status: str = "Not verified (prototype)"
    model: str = "demo-model-14b"
    searched: int = 12  # passages searched; 0 means no search was made


# -- sources ------------------------------------------------------------------


def _files(names: list[tuple[str, str]], failed: dict[int, str] | None = None, empty: tuple[int, ...] = ()) -> list[FakeFile]:
    failed = failed or {}
    out: list[FakeFile] = []
    for i, (path, fmt) in enumerate(names):
        if i in failed:
            out.append(FakeFile(path, fmt, FILE_FAILED, 0, failed[i]))
        elif i in empty:
            out.append(FakeFile(path, fmt, FILE_EMPTY, 0))
        else:
            out.append(FakeFile(path, fmt, FILE_READY, 6 + (i * 5) % 17))
    return out


def initial_sources() -> list[FakeSource]:
    finance_names = [(f"reports/q{(i % 4) + 1}-summary-{2024 + i // 4}.xlsx", "xlsx") for i in range(12)]
    finance_names += [(f"board/minutes-{i + 1:02d}.pdf", "pdf") for i in range(6)]
    finance_names += [("budget/legacy-plan.xls", "xls"), ("budget/forecast.xlsx", "xlsx"), ("scans/cover-page.pdf", "pdf")]
    finance = FakeSource(
        "src-demo-finance",
        "Finance",
        "/demo/work/finance",
        READY,
        _files(finance_names, failed={18: "Unsupported legacy format .xls", 19: "Workbook is corrupt (demo)"}, empty=(20,)),
        last_indexed="10 Oct 2026 10:42",
    )
    handbook = FakeSource(
        "src-demo-handbook",
        "Handbook",
        "/demo/work/handbook",
        READY,
        _files([(f"policies/{n}.docx", "docx") for n in ("hiring", "leave", "travel", "security", "expenses", "onboarding")]),
        last_indexed="10 Oct 2026 09:15",
    )
    contracts = FakeSource(
        "src-demo-contracts",
        "Contracts",
        "/demo/work/contracts",
        FAILED,
        _files([(f"vendors/agreement-{i}.pdf", "pdf") for i in range(1, 5)]),
        last_indexed="9 Oct 2026 17:20",
        note="The folder cannot be reached at /demo/work/contracts. Restore it, then retry.",
    )
    old = FakeSource(
        "src-demo-old",
        "Old project",
        "/demo/archive/old-project",
        DISCONNECTED,
        _files([(f"notes/{i}.md", "md") for i in range(1, 6)]),
        last_indexed="3 Oct 2026 14:05",
        note="Disconnected by you. Stored originals are kept; it is not searched.",
    )
    return [finance, handbook, contracts, old]


# -- models / settings --------------------------------------------------------

ANSWER_MODELS = (
    ("demo-model-14b", True, ""),
    ("demo-model-7b", True, ""),
    ("demo-model-32b", False, "not installed — run: ollama pull demo-model-32b"),
)
EMBED_MODEL = "demo-embed-small"
THINKING_LEVELS = ("Fast", "Balanced", "Thorough")
DEFAULT_MODEL = ANSWER_MODELS[0][0]

MODES = (
    ("auto", "Auto", "Docket chooses the route. Recommended.", True, ""),
    ("fast", "Quick search", "Search and answer directly from retrieved passages.", True, ""),
    ("plan", "Plan", "Plan steps and ask you to approve them first.", False, "Not available yet: planning is not implemented in the backend."),
)

READINESS_OK = (
    ("Ollama", "Available"),
    ("Answer model", f"{DEFAULT_MODEL} — installed"),
    ("Embedding model", "installed"),
    ("Searchable documents", "None yet"),
)
READINESS_BLOCKED = (
    ("Ollama", "Not reachable at the configured address"),
    ("Answer model", f"{ANSWER_MODELS[2][0]} — not installed"),
    ("Embedding model", "installed"),
    ("Searchable documents", "None yet"),
)
BLOCKED_HELP = "Start Ollama, then run: ollama pull demo-model-32b"

# -- indexing -----------------------------------------------------------------

INDEX_STAGES = (
    "Discovering files",
    "Loading parser",
    "Reading and storing original",
    "Parsing",
    "Chunking",
    "Creating search embeddings",
    "Indexing",
    "Reconciling",
)
QUERY_STAGES = ("Searching documents", "Reading evidence", "Writing answer", "Checking citations")
INDEX_TOTAL_FILES = 24
INDEX_FAILED_AT = (11, 17)
INDEX_UNCHANGED_AT = (3, 9, 14)
SUPPORTED_FORMATS = "PDF, Word, Excel, PowerPoint, text and Markdown"

FOLDER_SUGGESTIONS = (
    "/demo/work/reports",
    "/demo/work/notes",
    "/demo/home/Documents",
    "/demo/home/Documents/Tax papers",
)
DATA_DIR_LABEL = "/demo/data (not a real directory)"

JOB_HISTORY = (
    ("Finance", "Partial", "10 Oct 2026 10:42", "18 indexed · 0 unchanged · 2 failed"),
    ("Handbook", "Completed", "10 Oct 2026 09:15", "6 indexed · 0 unchanged · 0 failed"),
    ("Old project", "Interrupted", "3 Oct 2026 14:05", "Stopped when the process exited; retry to finish."),
)

# -- conversation -------------------------------------------------------------

_CIT_SUMMARY = FakeCitation(
    "q3-summary-2025.xlsx",
    "reports/q3-summary-2025.xlsx",
    "Sheet Summary · B7:D10",
    "Region | Revenue | Target\nNorth | 4.2 | 4.0\nSouth | 3.1 | 3.4\nWest | 2.8 | 2.5",
    source="Finance",
    chunk_id="chunk-demo-0101",
    table=(("Region", "Revenue", "Target"), ("North", "4.2", "4.0"), ("South", "3.1", "3.4"), ("West", "2.8", "2.5")),
    table_origin="B7",
    cited=("C8", "C9", "C10"),
)
_CIT_REVIEW = FakeCitation(
    "regional-review.pdf",
    "board/regional-review.pdf",
    "Page 4 · Section 2.1",
    "The southern region fell short of its quarterly target because two large "
    "renewals slipped into the following quarter. Management expects the shortfall "
    "to reverse once those renewals close. This paragraph is invented sample text "
    "used to review how a long verbatim passage wraps, scrolls and keeps its "
    "original line breaks inside the evidence panel.\n\nA second paragraph shows "
    "that blank lines inside a passage are preserved exactly as stored.",
    source="Finance",
    indexed="10 Oct 2026 10:41",
    chunk_id="chunk-demo-0212",
)
_CIT_POLICY = FakeCitation(
    "hiring.docx",
    "policies/hiring.docx",
    None,
    "New hires are paused until the start of the next financial year unless a "
    "role is explicitly approved by the executive team.",
    source="Handbook",
    indexed="10 Oct 2026 09:15",
    chunk_id="chunk-demo-0307",
)
_CIT_GONE = FakeCitation(
    "forecast-draft.xlsx",
    "budget/forecast-draft.xlsx",
    None,
    "",
    available=False,
    reason="Its stored version was superseded after this answer was written, so the original passage can no longer be shown.",
    version=2,
    versions=3,
    source="Finance",
    chunk_id="chunk-demo-0420",
)
# Two different files that share a name: the Sources list must tell them apart.
_CIT_HANDBOOK_CURRENT = FakeCitation(
    "handbook.pdf",
    "policies/handbook.pdf",
    "Page 12 · Section 4",
    "Approved travel is booked through the central desk. Claims above the limit need a manager sign-off.",
    source="Handbook",
    indexed="10 Oct 2026 09:15",
    chunk_id="chunk-demo-0511",
)
_CIT_HANDBOOK_OLD = FakeCitation(
    "handbook.pdf",
    "archive/2023/handbook.pdf",
    "Page 9 · Section 4",
    "Travel is booked by each team. Claims above the limit need a manager sign-off.",
    source="Handbook",
    version=1,
    versions=1,
    indexed="3 Oct 2026 14:05",
    chunk_id="chunk-demo-0498",
)

INTRO_NOTICE = (
    "This is a design prototype. Every answer, source and number is invented demo data. "
    "Type a question, or press F1 for commands."
)


def initial_answer() -> FakeAnswer:
    text = (
        "The demo quarterly report shows revenue growing in most regions [1].\n\n"
        "Key points:\n"
        "- Revenue rose 12% on the prior quarter [1]\n"
        "- The southern region missed its target because renewals slipped [2]\n"
        "- Hiring is paused until the next financial year [3]\n"
        "- Travel above the limit needs a manager sign-off [5], unchanged from the older handbook [6]\n\n"
        "| Region | Revenue | Target | Result |\n"
        "|---|---|---|---|\n"
        "| North | 4.2 | 4.0 | Met |\n"
        "| South | 3.1 | 3.4 | Missed |\n"
        "| West | 2.8 | 2.5 | Met |\n\n"
        "One related figure [4] could not be checked."
    )
    return FakeAnswer(
        text,
        (_CIT_SUMMARY, _CIT_REVIEW, _CIT_POLICY, _CIT_GONE, _CIT_HANDBOOK_CURRENT, _CIT_HANDBOOK_OLD),
        rewrite='Rewritten as "quarterly revenue by region against target"',
        ambiguity="Not stated; the most recent quarter was used",
        warnings=("Citation [4] refers to a version that is no longer available.",),
        searched=14,
    )


def initial_question() -> str:
    return "How did each region do against target last quarter?"


_GREETING = re.compile(
    r"^(hi|hey|hello|hiya|yo|howdy|thanks|thank you|good (morning|afternoon|evening))( there| docket)?$"
)

GREETING_REPLY = (
    "Hello. I answer questions about the documents in your sources, with a citation for every claim. "
    "Ask about a topic or a file, or type /help to see what else I can do."
)


def is_greeting(question: str) -> bool:
    return bool(_GREETING.match(re.sub(r"[^a-z ]", "", question.lower()).strip()))


def answer_for(question: str, scope_label: str, mode_label: str) -> FakeAnswer:
    """Pick a canned answer from keywords. Deterministic; no model involved."""
    q = question.lower()
    if is_greeting(question):
        # Small talk: no retrieval, so no citations and no sourced claims.
        return FakeAnswer(
            GREETING_REPLY,
            (),
            mode_label,
            0.4,
            scope_label,
            status="Not applicable (no sourced claims)",
            searched=0,
        )
    if any(w in q for w in ("nothing", "unknown", "missing", "abstain", "weather")):
        return FakeAnswer(
            "The available evidence did not support an answer to that question.\n\n"
            "Try naming a file, narrowing the scope, or asking about a topic your sources cover.",
            (),
            mode_label,
            3.4,
            scope_label,
            abstained=True,
            abstain_reason="No retrieved evidence matched the question (demo).",
            status="No answer (evidence did not support one)",
            searched=9,
        )
    if any(w in q for w in ("table", "compare", "region", "target")):
        base = initial_answer()
        return FakeAnswer(base.text, base.citations, mode_label, 6.1, scope_label, base.rewrite, base.ambiguity, base.warnings)
    if any(w in q for w in ("list", "which", "what", "policy", "hiring")):
        return FakeAnswer(
            "Three demo policies mention approvals [1]:\n\n"
            "- Hiring needs executive approval [1]\n"
            "- Travel above the limit needs a manager sign-off [2]\n"
            "- Expense claims are due within 30 days [2]",
            (_CIT_POLICY, _CIT_HANDBOOK_CURRENT),
            mode_label,
            5.0,
            scope_label,
        )
    return FakeAnswer(
        "In the demo sources, revenue was higher this quarter than last [1]. "
        "The change is attributed to renewals closing earlier than planned [2].",
        (_CIT_SUMMARY, _CIT_REVIEW),
        mode_label,
        4.7,
        scope_label,
    )


COMMAND_EXTRAS = (
    ("scope", "choose what to search", ""),
    ("jobs", "show indexing jobs and results", ""),
    ("settings", "answering, appearance, history, system", ""),
    ("details", "details of the latest answer", ""),
    ("rechunk", "update stored chunks", "Maintenance arrives in a later stage; not wired in this prototype."),
    ("reindex", "rebuild the search index", "Maintenance arrives in a later stage; not wired in this prototype."),
)


@dataclass
class World:
    """Mutable demo state. Actions in the prototype change this and nothing else."""

    sources: list[FakeSource] = field(default_factory=initial_sources)

    @classmethod
    def fresh(cls, *, empty: bool = False) -> "World":
        w = cls()
        if empty:
            w.sources = []
        return w

    def clone(self) -> "World":
        return copy.deepcopy(self)

    def ready_files(self) -> int:
        return sum(s.searchable for s in self.sources)

    def attention(self) -> int:
        failed_files = sum(s.count(FILE_FAILED) for s in self.sources if s.status != DISCONNECTED)
        failed_sources = sum(1 for s in self.sources if s.status == FAILED)
        return failed_files + failed_sources

    def find(self, source_id: str) -> FakeSource | None:
        return next((s for s in self.sources if s.id == source_id), None)

    def new_source_from_path(self, path: str) -> FakeSource:
        name = path.rstrip("/").rsplit("/", 1)[-1] or path
        n = 1 + len(self.sources)
        src = FakeSource(
            f"src-demo-new-{n}",
            name,
            path,
            READY,
            _files([(f"docs/file-{i + 1:02d}.pdf", "pdf") for i in range(6)]),
            last_indexed="10 Oct 2026 11:05",
        )
        self.sources.append(src)
        return src
