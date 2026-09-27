"""Draft gold questions from an ingested corpus, then review them by hand.

`draft_questions` samples chunks (weighted toward tables, lists and numbers),
asks an injected, non-thinking LLM for a question + a verbatim quote + fact
strings + a type, and REJECTS any draft whose quote is not verbatim (after
`normalize_text`) in the parsed chunk text -- that is what kills hallucinated
gold. Near-duplicate questions are dropped. Output is a normal gold YAML with
`reviewed: false`, loadable by `load_gold_set`.

`review_gold` is a terminal loop over the unreviewed entries (accept / edit /
reject / skip / quit). It saves after every decision, so quitting and running
it again resumes where it stopped.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import re
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Protocol

import yaml
from pydantic import ValidationError

from docket.eval.schema import (
    GOLD_SPAN_MAX_CHARS,
    GOLD_SPAN_MIN_CHARS,
    GoldSet,
    GoldSetError,
    Question,
    QuestionType,
    load_gold_set,
)
from docket.eval.scoring import normalize_text
from docket.inference.gateway import InferenceGateway
from docket.prompts.draft import DRAFT_SYSTEM, draft_prompt

MIN_CHUNK_CHARS = 80
DUPLICATE_RATIO = 0.85
DUPLICATE_JACCARD = 0.8
DRAFT_TYPES = (
    QuestionType.SINGLE_FACT,
    QuestionType.TABLE_LOOKUP,
    QuestionType.ENUMERATION,
    QuestionType.NUMERIC,
)

DRAFT_OPTS: dict = {"think": False, "format": "json", "options": {"temperature": 0}}

_LIST_LINE_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+", re.MULTILINE)
_TABLE_LINE_RE = re.compile(r"^\s*\|.*\|\s*$", re.MULTILINE)
_CONTEXT_WORDS_RE = re.compile(
    r"\b(?:the|this|given|above|following)\s+(?:passage|excerpt|text|chunk|document|paragraph|section|table)\b",
    re.IGNORECASE,
)
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


class ChunkLike(Protocol):
    chunk_id: str
    source_name: str
    heading: str | None
    text: str


# -- sampling --------------------------------------------------------------


def chunk_weight(text: str) -> float:
    """Sampling weight: tables, lists and number-dense text are the hard cases
    the plan wants over-represented."""
    weight = 1.0
    if len(_TABLE_LINE_RE.findall(text)) >= 2:
        weight += 4.0
    if len(_LIST_LINE_RE.findall(text)) >= 2:
        weight += 2.0
    digits = sum(ch.isdigit() for ch in text)
    weight += min(digits / max(len(text), 1) * 20, 3.0)
    return weight


def weighted_order(chunks: Sequence[ChunkLike], rng: random.Random) -> list[ChunkLike]:
    """Weighted random permutation without replacement (Efraimidis-Spirakis)."""
    eligible = [c for c in chunks if len(c.text.strip()) >= MIN_CHUNK_CHARS]
    keyed = [(math.log(1.0 - rng.random()) / chunk_weight(c.text), c) for c in eligible]
    keyed.sort(key=lambda kc: kc[0], reverse=True)
    return [c for _, c in keyed]


# -- prompting / parsing -----------------------------------------------------


def parse_draft_output(text: str) -> dict | None:
    text = _THINK_RE.sub("", text or "").strip()
    text = _FENCE_RE.sub("", text).strip()
    decoder = json.JSONDecoder()
    for start in (i for i, ch in enumerate(text) if ch == "{"):
        try:
            obj, _ = decoder.raw_decode(text[start:])
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def _trim_quote(quote: str) -> str:
    """A quote over the limit is cut at a word boundary (a prefix of a verbatim
    quote is still verbatim)."""
    if len(quote) <= GOLD_SPAN_MAX_CHARS:
        return quote
    cut = quote[:GOLD_SPAN_MAX_CHARS]
    if quote[GOLD_SPAN_MAX_CHARS] != " " and " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.strip()


def question_id(question: str) -> str:
    return "draft-" + hashlib.sha1(normalize_text(question).encode("utf-8")).hexdigest()[:8]


def build_draft(chunk: ChunkLike, raw: dict) -> tuple[Question | None, str]:
    """Validate one model draft against the chunk. Returns `(question, "ok")`
    or `(None, reason)`."""
    if raw.get("skip"):
        return None, "skipped"
    question = str(raw.get("question") or "").strip()
    quote = _trim_quote(str(raw.get("quote") or "").strip().strip("`\"'").strip())
    facts_raw = raw.get("facts")
    if not question or not quote or not isinstance(facts_raw, list):
        return None, "malformed"
    if _CONTEXT_WORDS_RE.search(question):
        return None, "context_dependent"
    if normalize_text(quote) not in normalize_text(chunk.text):
        return None, "quote_not_verbatim"
    if len(quote) < GOLD_SPAN_MIN_CHARS:
        return None, "quote_too_short"
    facts = [f.strip() for f in (str(f) for f in facts_raw) if f.strip() and not f.strip().startswith("re:")]
    if not facts:
        return None, "no_facts"
    haystack = normalize_text(chunk.text)
    if not any(normalize_text(f) in haystack for f in facts):
        return None, "facts_not_in_passage"

    try:
        qtype = QuestionType(str(raw.get("type") or "").strip())
    except ValueError:
        qtype = QuestionType.SINGLE_FACT
    if qtype not in DRAFT_TYPES or (qtype is QuestionType.ENUMERATION and len(facts) < 2):
        qtype = QuestionType.SINGLE_FACT
    is_enum = qtype is QuestionType.ENUMERATION
    origin = chunk.source_name + (f" > {chunk.heading}" if chunk.heading else "")
    try:
        built = Question(
            id=question_id(question),
            type=qtype,
            question=question,
            answerable=True,
            must_contain=[] if is_enum else facts,
            enumeration=facts if is_enum else [],
            gold_spans=[quote],
            reviewed=False,
            origin=origin,
            source_documents=[chunk.source_document] if getattr(chunk, "source_document", None) else [],
        )
    except ValidationError:
        return None, "invalid"
    return built, "ok"


# -- dedupe ------------------------------------------------------------------


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"\w+", normalize_text(text)))


def is_near_duplicate(question: str, others: Sequence[str]) -> bool:
    a = normalize_text(question)
    ta = _tokens(question)
    for other in others:
        b = normalize_text(other)
        if a == b or SequenceMatcher(None, a, b).ratio() >= DUPLICATE_RATIO:
            return True
        tb = _tokens(other)
        if ta and tb and len(ta & tb) / len(ta | tb) >= DUPLICATE_JACCARD:
            return True
    return False


# -- drafting ----------------------------------------------------------------


@dataclass
class DraftResult:
    questions: list[Question] = field(default_factory=list)
    rejected: Counter = field(default_factory=Counter)  # reason -> count
    attempts: int = 0


def draft_questions(
    gateway: InferenceGateway,
    chunks: Sequence[ChunkLike],
    *,
    count: int,
    seed: int = 0,
    existing_questions: Sequence[str] = (),
    max_attempts: int | None = None,
    progress: Callable[[int, int, str], None] | None = None,
) -> DraftResult:
    """Draft up to `count` accepted questions (stops early when chunks run out
    or after `max_attempts` model calls, default 3x count)."""
    result = DraftResult()
    seen = list(existing_questions)
    limit = max_attempts if max_attempts is not None else count * 3
    for chunk in weighted_order(chunks, random.Random(seed)):
        if len(result.questions) >= count or result.attempts >= limit:
            break
        result.attempts += 1
        raw = parse_draft_output(
            gateway.generate(system=DRAFT_SYSTEM, prompt=draft_prompt(chunk), **DRAFT_OPTS)
        )
        if raw is None:
            reason, question = "unparseable", None
        else:
            question, reason = build_draft(chunk, raw)
        if question is not None and is_near_duplicate(question.question, seen):
            question, reason = None, "duplicate"
        if question is not None:
            result.questions.append(question)
            seen.append(question.question)
        else:
            result.rejected[reason] += 1
        if progress:
            progress(len(result.questions), result.attempts, reason)
    return result


# -- files -------------------------------------------------------------------


def question_to_dict(question: Question) -> dict:
    data = question.model_dump(mode="json", exclude_defaults=True, exclude_none=True)
    data["reviewed"] = question.reviewed
    ordered = {k: data[k] for k in ("id", "type", "question", "answerable") if k in data}
    ordered.update({k: v for k, v in data.items() if k not in ordered})
    return ordered


def save_gold_file(path: Path, questions: Sequence[Question]) -> None:
    """Atomically write a gold YAML (tmp file + rename)."""
    path = Path(path)
    population = load_gold_set(path).population if path.exists() else "unspecified"
    body = yaml.safe_dump(
        {"version": 1, "population": population, "questions": [question_to_dict(q) for q in questions]},
        sort_keys=False,
        allow_unicode=True,
        width=100,
    )
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(body, encoding="utf-8")
    os.replace(tmp, path)


def merge_into_file(path: Path, new: Sequence[Question]) -> list[Question]:
    """Append `new` to an existing draft file (if any); returns the full list."""
    existing = load_gold_set(path).questions if Path(path).exists() else []
    merged = list(existing) + list(new)
    save_gold_file(path, merged)
    return merged


# -- interactive review --------------------------------------------------------


class Reader(Protocol):
    def read(self, prompt: str) -> str: ...


@dataclass
class ReviewSummary:
    accepted: int = 0
    edited: int = 0
    rejected: int = 0
    skipped: int = 0
    remaining: int = 0
    quit_early: bool = False


MENU = "[a]ccept  [e]dit  [r]eject  [s]kip  [q]uit > "


def _show(question: Question, position: int, total: int, out: Callable[[str], None]) -> None:
    out("")
    out(f"--- [{position}/{total}] {question.id}  type={question.type.value}")
    if question.origin:
        out(f"source : {question.origin}")
    out(f"Q      : {question.question}")
    facts = question.enumeration or question.must_contain
    label = "items" if question.enumeration else "facts"
    out(f"{label:<7}: " + " | ".join(facts))
    for span in question.gold_spans:
        out(f'quote  : "{span}"')


def _edit(question: Question, reader: Reader, out: Callable[[str], None]) -> Question | None:
    """Prompt for each field (blank keeps it; facts are separated by ' | ').
    Returns the edited question, or None if the user gives up (EOF / 'cancel')."""
    while True:
        try:
            q = reader.read(f"question [{question.question}]: ").strip() or question.question
            current = " | ".join(question.enumeration or question.must_contain)
            f = reader.read(f"facts, ' | '-separated [{current}]: ").strip() or current
            quote = reader.read(f"quote, {GOLD_SPAN_MIN_CHARS}-{GOLD_SPAN_MAX_CHARS} chars [{question.gold_spans[0]}]: ").strip()
            typ = reader.read(f"type [{question.type.value}]: ").strip() or question.type.value
        except (EOFError, KeyboardInterrupt):
            return None
        facts = [x.strip() for x in f.split("|") if x.strip()]
        try:
            qtype = QuestionType(typ)
            enum = qtype is QuestionType.ENUMERATION
            edited = Question(
                id=question.id,
                type=qtype,
                question=q,
                answerable=True,
                must_contain=[] if enum else facts,
                enumeration=facts if enum else [],
                gold_spans=[quote or question.gold_spans[0]] + question.gold_spans[1:],
                reviewed=True,
                origin=question.origin,
                split=question.split,
                source_documents=question.source_documents,
                formula_dependent=question.formula_dependent,
                history=question.history,
                setup=question.setup,
                must_not_contain=question.must_not_contain,
            )
            return edited
        except (ValueError, ValidationError) as exc:
            out(f"invalid: {exc}")
            try:
                if reader.read("try again? [Y/n] ").strip().lower().startswith("n"):
                    return None
            except (EOFError, KeyboardInterrupt):
                return None


def review_gold(
    path: Path,
    reader: Reader,
    out: Callable[[str], None] = print,
) -> ReviewSummary:
    """Review every `reviewed: false` question in `path`, saving after each
    decision. Re-running resumes with whatever is still unreviewed."""
    path = Path(path)
    questions = list(load_gold_set(path).questions)  # raises GoldSetError if invalid
    summary = ReviewSummary()
    position = sum(q.reviewed for q in questions)  # already-reviewed count carries over
    for question in list(questions):
        if question.reviewed:
            continue
        position += 1
        _show(question, position, len(questions), out)
        while True:
            try:
                choice = reader.read(MENU).strip().lower()[:1]
            except (EOFError, KeyboardInterrupt):
                choice = "q"
            if choice in ("a", "e", "r", "s", "q"):
                break
            out("please answer a, e, r, s or q")
        index = questions.index(question)
        if choice == "q":
            summary.quit_early = True
            break
        if choice == "s":
            summary.skipped += 1
            continue
        if choice == "r":
            questions.pop(index)
            summary.rejected += 1
        elif choice == "e":
            edited = _edit(question, reader, out)
            if edited is None:
                summary.skipped += 1
                continue
            questions[index] = edited
            summary.edited += 1
        else:
            questions[index] = question.model_copy(update={"reviewed": True})
            summary.accepted += 1
        save_gold_file(path, questions)
    summary.remaining = sum(1 for q in questions if not q.reviewed)
    return summary


def format_summary(summary: ReviewSummary) -> str:
    return (
        f"accepted {summary.accepted}, edited {summary.edited}, rejected {summary.rejected}, "
        f"skipped {summary.skipped}; {summary.remaining} still unreviewed"
        + (" (quit early, run again to resume)" if summary.quit_early else "")
    )
