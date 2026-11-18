"""Shared helpers for the private eval kit (no document text is ever printed or saved from here)."""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from pathlib import Path

import yaml

GEN_MODEL = "qwen3:14b"
JUDGE_MODEL = "qwen3:14b"
EMBED_MODEL = "qwen3-embedding:0.6b"
REAL_DATA_DIR = Path.home() / ".local" / "share" / "docket"


class KitError(Exception):
    """User-facing setup problem."""


def _inside(child: Path, parent: Path) -> bool:
    return child == parent or parent in child.parents


def guard_scratch(scratch: Path, documents: Path | None = None) -> Path:
    """Refuse the live Docket data dir (or anything inside/around it), and
    scratch dirs that overlap the documents folder (ingest must stay read-only)."""
    scratch = Path(scratch).expanduser().resolve()
    real = REAL_DATA_DIR.resolve()
    if _inside(scratch, real) or _inside(real, scratch):
        raise KitError(f"refusing to use {scratch}: it is (or contains) the live Docket data directory {real}")
    if "/.local/share/docket" in str(scratch):
        raise KitError(f"refusing to use {scratch}: looks like a live Docket data directory")
    if documents is not None:
        documents = Path(documents).expanduser().resolve()
        if _inside(scratch, documents) or _inside(documents, scratch):
            raise KitError("the scratch directory and the documents folder must not overlap")
    return scratch


@dataclass(frozen=True)
class KitQuestion:
    question: str
    expected_answer: str
    expected_file: str
    answerable: bool


def load_questions(path: Path) -> list[KitQuestion]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    rows = data.get("questions") if isinstance(data, dict) else data
    if not isinstance(rows, list) or not rows:
        raise KitError("questions file has no 'questions:' list")
    out: list[KitQuestion] = []
    for i, row in enumerate(rows, 1):
        if not isinstance(row, dict) or not str(row.get("question", "")).strip():
            raise KitError(f"question #{i}: missing 'question'")
        ans = row.get("answerable", True)
        if isinstance(ans, str):
            ans = ans.strip().lower() in ("yes", "true", "y", "1")
        ans = bool(ans)
        exp = str(row.get("expected_answer") or "").strip()
        if ans and not exp:
            raise KitError(f"question #{i}: answerable questions need 'expected_answer'")
        out.append(KitQuestion(str(row["question"]).strip(), exp, str(row.get("expected_file") or "").strip(), ans))
    return out


def file_matches(expected: str, names: list[str]) -> bool:
    exp = Path(expected).name.lower().strip()
    if not exp:
        return False
    stem = Path(exp).stem
    for n in names:
        n = Path(n).name.lower()
        if n == exp or (stem and (stem in n or Path(n).stem in exp and len(Path(n).stem) >= 4)):
            return True
    return False


@dataclass
class Outcome:
    """Per-question result. Holds NO text: only booleans and a latency."""
    answerable: bool
    abstained: bool
    correct: bool
    file_recalled: bool | None  # None when no expected file was given
    latency_s: float
    errored: bool = False


def summarize(outcomes: list[Outcome], entity_matched: list[bool] | None = None) -> dict:
    n = len(outcomes)
    ans = [o for o in outcomes if o.answerable]
    with_file = [o for o in ans if o.file_recalled is not None]
    pct = lambda a, b: round(100.0 * a / b, 1) if b else None  # noqa: E731
    d = {
        "questions": n,
        "answerable": len(ans),
        "accuracy_pct": pct(sum(o.correct for o in outcomes), n),
        "wrongful_abstention_pct": pct(sum(o.abstained for o in ans), len(ans)),
        "expected_file_recall_pct": pct(sum(bool(o.file_recalled) for o in with_file), len(with_file)),
        "median_latency_s": round(statistics.median(o.latency_s for o in outcomes), 1) if n else None,
        "errors": sum(o.errored for o in outcomes),
    }
    if entity_matched is not None:
        d["entity_match_pct"] = pct(sum(entity_matched), len(entity_matched))
    return d


JUDGE_SYSTEM = (
    "You grade answers. Reply with JSON only: {\"supported\": true|false, \"reason\": \"short\"}. "
    "supported is true only if the candidate answer states the same facts as the expected answer "
    "(wording may differ; extra correct detail is fine; a contradiction or missing key fact is false)."
)
JUDGE_OPTS = {"think": False, "format": "json", "options": {"temperature": 0, "num_ctx": 8192, "num_predict": 256}}


def judge_answer(gateway, question: str, expected: str, answer: str) -> bool:
    """True/False from the judge; unparseable output counts as incorrect. Text never leaves this call."""
    from docket.eval.judge import parse_judge_output

    prompt = f"Question: {question}\n\nExpected answer: {expected}\n\nCandidate answer: {answer}\n\nIs the candidate answer correct?"
    for _ in range(2):
        try:
            parsed = parse_judge_output(gateway.generate(system=JUDGE_SYSTEM, prompt=prompt, **JUDGE_OPTS))
        except Exception:  # noqa: BLE001
            parsed = None
        if parsed is not None:
            return parsed[0]
    return False
