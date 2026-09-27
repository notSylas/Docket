"""Deterministic scoring of recorded runs against gold questions.

Everything here is pure (no I/O, no model calls). Where a regex cannot decide
whether an answer is right (paraphrased facts, whether a cited chunk really
supports a claim) the result is `Verdict.NEEDS_JUDGE`; the LLM judge that
resolves those lands in a later task and plugs into `undecidable_by_regex`.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from enum import Enum

from docket.eval.schema import REGEX_PREFIX, Question, RunRecord
from docket.services.query.citations import CITATION_TAG_RE as _CITATION_TAG_RE
from docket.services.query.prompts import ABSTENTION_PHRASE

# Ollama drops the *start* of an over-long prompt, so a prompt_eval_count far
# below the size we sent means truncation. ~4 chars/token is the rough English
# ratio; the 0.7 slack keeps table-heavy text (fewer chars/token) from tripping it.
CHARS_PER_TOKEN = 4.0
TRUNCATION_RATIO = 0.7

_DASHES = "‐‑‒–—―−"
_DASH_RE = re.compile(f"[{_DASHES}]")
_QUOTE_TABLE = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"'})
_THOUSANDS_RE = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")
_WS_RE = re.compile(r"\s+")
_DIGIT_RE = re.compile(r"\d")


def normalize_text(text: str) -> str:
    """Case/whitespace/thousands-separator/unicode-dash normalization.

    NFKC folds non-breaking spaces and full-width forms; en/em/minus dashes
    become '-'; curly quotes become straight; "1,200" becomes "1200"; runs of
    whitespace collapse to one space.
    """
    text = unicodedata.normalize("NFKC", text)
    text = _DASH_RE.sub("-", text).translate(_QUOTE_TABLE)
    text = _THOUSANDS_RE.sub("", text)
    return _WS_RE.sub(" ", text).strip().casefold()


def strip_citations(answer: str) -> str:
    """Remove `[file #chunk]` tags so a tag can never satisfy a content match."""
    return _CITATION_TAG_RE.sub(" ", answer)


def matches(pattern: str, normalized_text: str) -> bool:
    """`pattern` is plain text (normalized substring match) or `re:<regex>`
    (searched, case-insensitively, in already-normalized text)."""
    if pattern.startswith(REGEX_PREFIX):
        return re.search(pattern[len(REGEX_PREFIX) :], normalized_text, re.IGNORECASE) is not None
    normalized_pattern = normalize_text(pattern)
    if normalized_pattern in normalized_text:
        return True
    # Whitespace-insensitive fallback: real-world equations/units/symbols get
    # spaced differently by different renderers with no change in meaning
    # ("V = I R" vs "V = IR", superscript "10⁻⁷" NFKC-folds to "10-7" with no
    # space where the gold text has "10 -7", "∠ BCA" vs "∠BCA"). Comparing
    # with all whitespace removed catches these without weakening the match
    # otherwise -- it's still an exact character-for-character match, just
    # blind to *where* spaces fall, not a fuzzy/edit-distance comparison.
    stripped_pattern = _WS_RE.sub("", normalized_pattern)
    if not stripped_pattern:
        return False
    return stripped_pattern in _WS_RE.sub("", normalized_text)


def _prepare_answer(answer: str) -> str:
    return normalize_text(strip_citations(answer))


@dataclass(frozen=True)
class ContainsCheck:
    found: list[str]
    missing: list[str]

    @property
    def ok(self) -> bool:
        return not self.missing


def check_must_contain(answer: str, patterns: list[str]) -> ContainsCheck:
    text = _prepare_answer(answer)
    found = [p for p in patterns if matches(p, text)]
    return ContainsCheck(found=found, missing=[p for p in patterns if p not in found])


def check_must_not_contain(answer: str, patterns: list[str]) -> list[str]:
    """The forbidden patterns that DO appear in the answer (empty = clean)."""
    text = _prepare_answer(answer)
    return [p for p in patterns if matches(p, text)]


# -- abstention ------------------------------------------------------------


def is_abstention(answer: str) -> bool:
    """True iff the answer is exactly the fixed refusal phrase (ignoring case,
    whitespace, and any citation tags). A refusal with extra content is NOT an
    abstention -- the plan requires "exactly abstains"."""
    return normalize_text(strip_citations(answer)) == normalize_text(ABSTENTION_PHRASE)


def contains_refusal(answer: str) -> bool:
    """True if the refusal phrase appears anywhere (a hedged, partial refusal)."""
    return normalize_text(ABSTENTION_PHRASE) in normalize_text(answer)


# -- citations -------------------------------------------------------------


@dataclass(frozen=True)
class CitationCheck:
    has_citation: bool
    invalid_ids: list[str]  # cited chunk ids that are not in the retrieved set
    unknown_tags: list[str]  # validation warnings about fabricated/mangled tags

    @property
    def ok(self) -> bool:
        return self.has_citation and not self.invalid_ids and not self.unknown_tags


def check_citations(
    cited_ids: list[str], retrieved_ids: list[str], warnings: list[str]
) -> CitationCheck:
    retrieved = set(retrieved_ids)
    return CitationCheck(
        has_citation=bool(cited_ids),
        invalid_ids=[c for c in cited_ids if c not in retrieved],
        unknown_tags=[w for w in warnings if "unknown citation" in w],
    )


# -- enumeration -----------------------------------------------------------


@dataclass(frozen=True)
class EnumerationCheck:
    found: list[str]
    missing: list[str]

    @property
    def total(self) -> int:
        return len(self.found) + len(self.missing)

    @property
    def completeness(self) -> float:
        return len(self.found) / self.total if self.total else 1.0


def enumeration_completeness(answer: str, items: list[str]) -> EnumerationCheck:
    check = check_must_contain(answer, items)
    return EnumerationCheck(found=check.found, missing=check.missing)


# -- retrieval / context ---------------------------------------------------


def span_hits(spans: list[str], texts: list[str]) -> list[bool]:
    """Per gold span: does its normalized text occur in any normalized text?
    Chunk ids change when chunking changes; a verbatim quote does not."""
    haystack = [normalize_text(t) for t in texts]
    return [any(normalize_text(s) in h for h in haystack) for s in spans]


def retrieval_hit(spans: list[str], chunk_texts: list[str]) -> bool:
    """True if at least one gold span is inside some retrieved chunk."""
    return any(span_hits(spans, chunk_texts))


def extract_context(prompt: str) -> str:
    """The Context block of a QueryService prompt, excluding conversation
    history (which may quote the answer) and the question."""
    start = prompt.find("Context:\n")
    if start == -1:
        return prompt
    body = prompt[start + len("Context:\n") :]
    end = body.rfind("\n\nQuestion: ")
    return body if end == -1 else body[:end]


def fact_in_context(spans: list[str], prompt: str | None) -> list[bool]:
    """Per gold span: is it in the context of the exact prompt sent?"""
    if prompt is None:
        return [False] * len(spans)
    return span_hits(spans, [extract_context(prompt)])


def looks_truncated(system: str | None, prompt: str | None, prompt_eval_count: int | None) -> bool:
    """Heuristic: Ollama evaluated far fewer tokens than the prompt we sent."""
    if not prompt_eval_count or prompt is None:
        return False
    expected = (len(system or "") + len(prompt)) / CHARS_PER_TOKEN
    return prompt_eval_count < expected * TRUNCATION_RATIO


# -- pass rule -------------------------------------------------------------


class Verdict(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    NEEDS_JUDGE = "needs_judge"


def undecidable_by_regex(pattern: str) -> bool:
    """JUDGE HOOK. Whether a *missing* required fact might still be present in
    paraphrase, so the deterministic check cannot call it wrong.

    Numbers, ids and explicit regexes are decisive: they either appear or they
    do not. Plain word facts can be paraphrased, so a miss is left to the judge.
    """
    if pattern.startswith(REGEX_PREFIX):
        return False
    return not _DIGIT_RE.search(pattern)


@dataclass
class RunScore:
    question_id: str
    repeat: int
    verdict: Verdict
    reasons: list[str] = field(default_factory=list)
    abstained: bool = False
    missing_facts: list[str] = field(default_factory=list)
    forbidden_found: list[str] = field(default_factory=list)
    enumeration_found: int = 0
    enumeration_total: int = 0
    citation_ok: bool = False
    span_retrieved: list[bool] = field(default_factory=list)
    span_in_context: list[bool] = field(default_factory=list)
    truncated: bool = False
    leaked: bool = False  # revoked-source evidence was still retrieved
    judged: bool = False  # verdict was set by the LLM judge, not the regex checks
    judge_disagreement: bool = False  # cross-check judge disagreed with the primary

    @property
    def passed(self) -> bool:
        return self.verdict is Verdict.PASS


def score_run(question: Question, record: RunRecord) -> RunScore:
    """Apply the per-question pass rule to one run.

    Answerable: all required facts present, no forbidden fact, every
    enumeration item, >=1 citation and every cited chunk in the retrieved set,
    and a cited chunk that carries a gold span. Unanswerable: exactly abstains.
    Cases regex cannot settle come back as NEEDS_JUDGE.
    """
    chunk_texts = [c.text for c in record.retrieved]
    retrieved_ids = [c.chunk_id for c in record.retrieved]
    cited_ids = [c.chunk_id for c in record.citations]

    score = RunScore(
        question_id=question.id,
        repeat=record.repeat,
        verdict=Verdict.FAIL,
        abstained=is_abstention(record.answer),
        span_retrieved=span_hits(question.gold_spans, chunk_texts),
        span_in_context=(span_hits(question.gold_spans, chunk_texts) if record.mode == "agent"
                         else fact_in_context(question.gold_spans, record.prompt)),
        # Agent inputs use a chat template and tool schemas; a character/token
        # heuristic on their JSON serialization would falsely signal truncation.
        truncated=record.mode != "agent" and looks_truncated(record.system, record.prompt, record.prompt_eval_count),
    )
    score.leaked = question.type.value == "revoked" and (
        any(c.source_id in record.revoked_source_ids for c in record.retrieved)
        or any(score.span_retrieved)
    )

    if record.error:
        score.reasons.append(f"run error: {record.error}")
        return score

    if not question.answerable:
        if score.abstained:
            score.verdict = Verdict.PASS
        else:
            score.reasons.append("answered an unanswerable question instead of abstaining")
        return score

    # -- answerable ---
    if score.abstained or contains_refusal(record.answer):
        score.reasons.append("abstained on an answerable question")
        return score

    contains = check_must_contain(record.answer, question.must_contain)
    forbidden = check_must_not_contain(record.answer, question.must_not_contain)
    enum = enumeration_completeness(record.answer, question.enumeration)
    citations = check_citations(cited_ids, retrieved_ids, record.validation_warnings)
    score.missing_facts = contains.missing + enum.missing
    score.forbidden_found = forbidden
    score.enumeration_found = len(enum.found)
    score.enumeration_total = enum.total
    score.citation_ok = citations.ok

    if forbidden:
        score.reasons.append(f"forbidden content present: {forbidden}")
    if not citations.has_citation:
        score.reasons.append("no citation")
    if citations.invalid_ids:
        score.reasons.append(f"cited chunks not in retrieved set: {citations.invalid_ids}")
    if citations.unknown_tags:
        score.reasons.append("fabricated/mangled citation tag")
    decisive_missing = [m for m in score.missing_facts if not undecidable_by_regex(m)]
    if decisive_missing:
        score.reasons.append(f"missing required facts: {decisive_missing}")
    if score.reasons:
        return score

    if score.missing_facts:  # only paraphrase-able facts are missing
        score.verdict = Verdict.NEEDS_JUDGE
        score.reasons.append(f"facts not found verbatim, judge to decide: {score.missing_facts}")
        return score

    # Facts are right. Citation *support* has no regex check beyond this proxy:
    # some cited chunk must actually contain a gold span.
    cited_set = set(cited_ids)
    cited_texts = [c.text for c in record.retrieved if c.chunk_id in cited_set]
    if question.gold_spans and not retrieval_hit(question.gold_spans, cited_texts):
        score.verdict = Verdict.NEEDS_JUDGE
        score.reasons.append("no cited chunk contains a gold span, judge to check support")
        return score

    score.verdict = Verdict.PASS
    return score
