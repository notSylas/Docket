"""Unit tests for `docket.query.classifier.HeuristicQueryClassifier`.

Pure function, no I/O, no Ollama needed -- just phrasing -> `QueryMode`.
"""

from __future__ import annotations

import pytest

from docket.services.query.classifier import HeuristicQueryClassifier, QueryMode

classifier = HeuristicQueryClassifier()


# ---------------------------------------------------------------------------
# Should stay on the fast path: single-fact lookups, the common case.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "question",
    [
        "What does Reciprocal Rank Fusion do?",
        "Who is the author of the Q3 report?",
        "When was the contract signed?",
        "What is the total budget for the marketing campaign?",
        "Does the policy cover remote employees?",
        "What is the deadline for the grant application?",
        "How many chunks were ingested from the source?",
        "Summarize the onboarding checklist.",
    ],
)
def test_routes_simple_lookups_to_fast(question: str) -> None:
    assert classifier.classify(question) == QueryMode.FAST


# ---------------------------------------------------------------------------
# Should route to the agent: phrasing that needs multi-step investigation.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "question",
    [
        "Compare the Q1 and Q2 budgets.",
        "What is the difference between the draft and final contract?",
        "How has the vendor policy evolved over the years?",
        "Why did the project timeline slip?",
        "What led to the change in pricing strategy?",
        "What is the impact of the new compliance rule on operations?",
        "What is the relationship between the two subsidiaries?",
        "List all instances of the term 'force majeure' across the contracts.",
        "Trace the approval chain for this expense report.",
        "Which sources mention the data retention policy?",
    ],
)
def test_routes_investigative_questions_to_agent(question: str) -> None:
    assert classifier.classify(question) == QueryMode.AGENT


# ---------------------------------------------------------------------------
# Case-insensitivity.
# ---------------------------------------------------------------------------


def test_matching_is_case_insensitive() -> None:
    assert classifier.classify("COMPARE the two invoices.") == QueryMode.AGENT
    assert classifier.classify("Why Did the outage happen?") == QueryMode.AGENT


# ---------------------------------------------------------------------------
# Boundary / deliberately ambiguous case.
# ---------------------------------------------------------------------------


def test_plain_why_question_without_investigative_framing_routes_to_agent() -> None:
    # "Why is X true" is phrased as a causal/explanatory question, which is
    # exactly the kind of question that tends to need chaining across more
    # than one piece of evidence to answer well (an explanation, not a
    # single fact) -- so this is deliberately routed to AGENT even though a
    # narrow-context version of it could in principle be answered from one
    # chunk. The cost of a false positive here (routing an occasional
    # single-chunk "why" question to the agent) is extra latency, not a
    # wrong answer -- the agent can still find and cite a single chunk if
    # that's all the evidence needed -- so leaning toward AGENT on "why" is
    # the safer default given that asymmetry.
    assert classifier.classify("Why is the sky blue according to this document?") == QueryMode.AGENT


def test_question_with_superlative_but_no_investigative_phrasing_routes_to_fast() -> None:
    # "What is the best X" / "largest Y" style questions look like they might
    # require comparison, but they're phrased as a direct request for a
    # single fact already present in the source material (a labeled
    # "best"/"largest" value), not a request for the assistant to itself
    # compare multiple retrieved things -- so this deliberately stays FAST.
    assert (
        classifier.classify("What is the largest expense category in the Q3 report?")
        == QueryMode.FAST
    )


# ---------------------------------------------------------------------------
# Real Physics-eval false positives: "difference between"/"relationship
# between" firing on single-quantity technical phrasing, not an actual
# comparison. See the `_FALSE_POSITIVE_PATTERNS` comment in classifier.py
# for the full rationale -- these are the exact two sentences from the real
# 93-run eval that were misrouted to AGENT and then produced uncited/
# inconsistent answers there.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "question",
    [
        "What is the relationship between the potential difference across R "
        "and the current I according to Ohm's law?",
        "What is the potential difference between conductors 1 and 2 when "
        "they have charges Q' and -Q' respectively?",
        # The third question from the same real eval run: this one was
        # misrouted not by "difference between"/"relationship between" but
        # by `\bacross\b` firing on the ordinary spatial sense of "across"
        # ("emf across the ends of a rod"), not the cross-document/breadth
        # sense the pattern is meant for.
        "What is the formula for the induced emf across the ends of a metal "
        "rod moving in a magnetic field?",
    ],
)
def test_single_quantity_technical_phrasing_routes_to_fast(question: str) -> None:
    assert classifier.classify(question) == QueryMode.FAST


@pytest.mark.parametrize(
    "question",
    [
        # Other "<quantity> difference between" shapes should also be caught
        # by the same carve-out, not just the one real example above.
        "What is the temperature difference between the two rooms?",
        "What is the phase difference between the current and the voltage "
        "in a purely inductive circuit?",
        # A "relationship between ... according to ... law" question about a
        # different named law should also stay FAST, confirming the
        # carve-out isn't hardcoded to "Ohm's law".
        "What is the relationship between pressure and volume according to "
        "Boyle's law?",
        # Other "<quantity> across" shapes beyond the one real example.
        "What is the voltage across the resistor in this circuit?",
        "What is the potential difference across the capacitor plates?",
    ],
)
def test_other_single_quantity_technical_phrasings_route_to_fast(question: str) -> None:
    assert classifier.classify(question) == QueryMode.FAST


def test_genuine_cross_document_breadth_with_across_still_routes_to_agent() -> None:
    # The `\bacross\b` pattern's actual intended use case (see its comment
    # in classifier.py) must still work once it's narrowed to exclude the
    # physics-quantity-spatial sense -- this has no quantity word or
    # "difference" directly before "across", so it's untouched by the new
    # carve-out.
    assert (
        classifier.classify(
            "Which practices are consistent across all documents in the corpus?"
        )
        == QueryMode.AGENT
    )


@pytest.mark.parametrize(
    "question",
    [
        # Genuine comparisons phrased with "difference between"/"relationship
        # between" but WITHOUT the single-quantity carve-out shape must still
        # route to AGENT -- the fix must not blunt real detection.
        "What is the difference between socialism and capitalism?",
        "What is the relationship between inflation and unemployment?",
        "What is the relationship between the two subsidiaries?",
        "What is the difference between the draft and final contract?",
    ],
)
def test_genuine_comparisons_still_route_to_agent(question: str) -> None:
    assert classifier.classify(question) == QueryMode.AGENT
