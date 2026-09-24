"""Gold-set and run-record schemas, plus the YAML loader/validator.

A gold file is a YAML mapping::

    version: 1
    questions:
      - id: journeys-count
        type: single_fact
        question: How many user journeys does the PRD define?
        answerable: true
        must_contain: ["six"]          # plain text, or "re:<regex>"
        gold_spans: ["J-06 | Export a report to PDF"]   # verbatim, 20-60 chars
        reviewed: true

Matching strings (`must_contain`, `must_not_contain`, `enumeration`) are
normalized (see `docket.eval.scoring.normalize_text`) before comparison; an
entry starting with ``re:`` is a regular expression matched against the
normalized answer instead.

`split` defaults to a deterministic ~70/30 dev/test assignment by hash of the
question id, so adding questions never reshuffles existing ones.
"""

from __future__ import annotations

import hashlib
import re
from enum import Enum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

GOLD_SPAN_MIN_CHARS = 20
GOLD_SPAN_MAX_CHARS = 60
DEV_FRACTION_PERCENT = 70
REGEX_PREFIX = "re:"


class GoldSetError(ValueError):
    """A gold file could not be read or failed validation (message is user-facing)."""


class QuestionType(str, Enum):
    SINGLE_FACT = "single_fact"
    TABLE_LOOKUP = "table_lookup"
    ENUMERATION = "enumeration"
    NUMERIC = "numeric"
    MULTI_DOC = "multi_doc"
    FOLLOW_UP = "follow_up"
    OUT_OF_CORPUS = "out_of_corpus"
    REVOKED = "revoked"


class Split(str, Enum):
    DEV = "dev"
    TEST = "test"


def default_split(question_id: str) -> Split:
    """Deterministic ~70/30 dev/test assignment from a hash of the id."""
    bucket = int(hashlib.sha256(question_id.encode("utf-8")).hexdigest(), 16) % 100
    return Split.DEV if bucket < DEV_FRACTION_PERCENT else Split.TEST


class HistoryTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1)
    answer: str = Field(min_length=1)


class Setup(BaseModel):
    """Per-question preconditions applied by the runner before asking."""

    model_config = ConfigDict(extra="forbid")

    revoke: list[str] = Field(default_factory=list)  # source names (corpus sub-folders)


def _check_patterns(field: str, patterns: list[str]) -> None:
    for pattern in patterns:
        if not pattern.strip():
            raise ValueError(f"{field} contains an empty entry")
        if pattern.startswith(REGEX_PREFIX):
            try:
                re.compile(pattern[len(REGEX_PREFIX) :])
            except re.error as exc:
                raise ValueError(f"{field}: invalid regex {pattern!r}: {exc}") from exc


class Question(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_.-]+$")
    type: QuestionType
    question: str = Field(min_length=1)
    answerable: bool
    must_contain: list[str] = Field(default_factory=list)
    must_not_contain: list[str] = Field(default_factory=list)
    gold_spans: list[str] = Field(default_factory=list)
    enumeration: list[str] = Field(default_factory=list)
    history: list[HistoryTurn] = Field(default_factory=list)
    setup: Setup = Field(default_factory=Setup)
    reviewed: bool = False
    split: Split | None = None
    origin: str | None = None  # where a drafted question came from (informational)

    @model_validator(mode="after")
    def _validate(self) -> Question:
        for field in ("must_contain", "must_not_contain", "enumeration"):
            _check_patterns(field, getattr(self, field))
        for span in self.gold_spans:
            length = len(span.strip())
            if not GOLD_SPAN_MIN_CHARS <= length <= GOLD_SPAN_MAX_CHARS:
                raise ValueError(
                    f"gold_spans entries must be {GOLD_SPAN_MIN_CHARS}-{GOLD_SPAN_MAX_CHARS} "
                    f"chars, got {length}: {span!r}"
                )

        if self.answerable:
            if not (self.must_contain or self.enumeration):
                raise ValueError("answerable questions need must_contain or enumeration")
            if not self.gold_spans:
                raise ValueError("answerable questions need at least one gold_spans quote")
        else:
            if self.must_contain or self.enumeration:
                raise ValueError("unanswerable questions must not set must_contain/enumeration")

        if self.type is QuestionType.OUT_OF_CORPUS and self.answerable:
            raise ValueError("out_of_corpus questions must have answerable: false")
        if self.type is QuestionType.REVOKED:
            if self.answerable:
                raise ValueError("revoked questions must have answerable: false")
            if not self.setup.revoke:
                raise ValueError("revoked questions need setup.revoke")
        if self.type is QuestionType.ENUMERATION and not self.enumeration:
            raise ValueError("enumeration questions need an enumeration list")
        if self.type is QuestionType.FOLLOW_UP and not self.history:
            raise ValueError("follow_up questions need history")
        if self.type is QuestionType.MULTI_DOC and len(self.gold_spans) < 2:
            raise ValueError("multi_doc questions need at least two gold_spans")

        if self.split is None:
            self.split = default_split(self.id)
        return self


class GoldSet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = 1
    questions: list[Question]

    @model_validator(mode="after")
    def _unique_ids(self) -> GoldSet:
        seen: set[str] = set()
        for question in self.questions:
            if question.id in seen:
                raise ValueError(f"duplicate question id: {question.id!r}")
            seen.add(question.id)
        return self

    def by_id(self) -> dict[str, Question]:
        return {q.id: q for q in self.questions}


def _format_validation_error(path: Path, raw: Any, exc: ValidationError) -> str:
    lines = [f"invalid gold set {path}:"]
    questions = raw.get("questions") if isinstance(raw, dict) else None
    for err in exc.errors():
        loc = list(err["loc"])
        where = ".".join(str(part) for part in loc)
        if len(loc) >= 2 and loc[0] == "questions" and isinstance(loc[1], int):
            index = loc[1]
            qid = None
            if isinstance(questions, list) and index < len(questions):
                entry = questions[index]
                qid = entry.get("id") if isinstance(entry, dict) else None
            label = f"question #{index + 1}" + (f" ({qid})" if qid else "")
            where = f"{label}: {'.'.join(str(p) for p in loc[2:])}".rstrip(": ")
        lines.append(f"  - {where}: {err['msg']}")
    return "\n".join(lines)


def load_gold_set(path: str | Path) -> GoldSet:
    """Read and validate a gold YAML file. Raises `GoldSetError` with a
    readable message on any problem (missing file, bad YAML, bad schema)."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise GoldSetError(f"cannot read gold set {path}: {exc}") from exc
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise GoldSetError(f"gold set {path} is not valid YAML: {exc}") from exc
    if isinstance(raw, list):  # bare list of questions is accepted too
        raw = {"questions": raw}
    if not isinstance(raw, dict):
        raise GoldSetError(f"gold set {path} must be a mapping with a 'questions' list")
    try:
        return GoldSet.model_validate(raw)
    except ValidationError as exc:
        raise GoldSetError(_format_validation_error(path, raw, exc)) from exc


# -- run records -----------------------------------------------------------


class RecordedCitation(BaseModel):
    citation_label: str
    chunk_id: str
    source_display_name: str


class RecordedChunk(BaseModel):
    chunk_id: str
    text: str
    citation_label: str
    source_display_name: str


class RunRecord(BaseModel):
    """Everything captured for one (question, repeat) run; one JSONL line."""

    question_id: str
    repeat: int
    answer: str = ""
    abstained: bool = False
    citations: list[RecordedCitation] = Field(default_factory=list)
    validation_warnings: list[str] = Field(default_factory=list)
    mode: str | None = None
    retrieved: list[RecordedChunk] = Field(default_factory=list)
    system: str | None = None  # exact system prompt sent (None if generation never ran)
    prompt: str | None = None  # exact user prompt sent
    prompt_eval_count: int | None = None  # tokens Ollama actually evaluated, if known
    latency_s: float = 0.0
    # Per gold span: does it appear in any indexed chunk of the corpus? Lets the
    # report tell a parse/chunk failure from a retrieval failure.
    spans_indexed: list[bool] = Field(default_factory=list)
    error: str | None = None


def load_records(path: str | Path) -> list[RunRecord]:
    records: list[RunRecord] = []
    with Path(path).open(encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            try:
                records.append(RunRecord.model_validate_json(line))
            except ValidationError as exc:
                raise GoldSetError(f"{path}:{line_no}: bad run record: {exc}") from exc
    return records
