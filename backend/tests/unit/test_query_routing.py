"""classify_route: deterministic route decision (additive; QueryService does not use it)."""

from __future__ import annotations

import pytest

from docket.services.query.classifier import HeuristicQueryClassifier, QueryMode
from docket.services.query.routing import (
    ExecutionPath,
    RouteDecision,
    UserMode,
    classify_route,
)
from docket.services.query.signals import QuerySignals

F, I, C = ExecutionPath.FAST, ExecutionPath.INVESTIGATION, ExecutionPath.CLARIFY

HIST = [{"question": "What was net revenue in July 2025?", "answer": "INR 4,100 thousand in July 2025."}]
HIST_NO_YEAR = [{"question": "How many tickets were closed in August?", "answer": "212 tickets."}]


def test_enums_and_decision_shape():
    assert {m.value for m in UserMode} == {"auto", "fast", "plan"}
    assert {p.value for p in ExecutionPath} == {"FAST", "INVESTIGATION", "CLARIFY"}
    d = classify_route("Who is the CFO?")
    assert isinstance(d, RouteDecision)
    assert d.path is F and d.reason_codes == ["SIMPLE_LOOKUP"] and d.clarification_question is None


# Phrasings NOT taken from the G5 gold set (held out on purpose).
@pytest.mark.parametrize(
    "q",
    [
        "What was net profit in FY2024-25?",
        "What is the lease expiry date in the Harbour Street lease agreement?",
        "How many headcount were reported in the HR workbook for 2025?",
        "Who approved the vendor onboarding policy?",
        "What was the Q3 FY2025-26 gross margin?",
        "What does the Assumptions sheet of Plan-2026.xlsx say about FX rates?",
        "What was the closing cash balance on 31 March 2025?",
        "What was freight cost in December 2024?",
        "What is the notice period in the employment contract?",
        "Which sheet in Costs-CY2025.xlsx contains the depreciation schedule?",
        "What is the TOTAL shown across the region columns in Sales-2025.xlsx?",
        "What is the potential difference across the resistor in the example?",
    ],
)
def test_fast_stays_fast(q):
    assert classify_route(q).path is F, q


@pytest.mark.parametrize(
    "q,code",
    [
        ("List every vendor whose invoice exceeded 50,000 in 2025.", "EXHAUSTIVE"),
        ("Which quarters of FY2024-25 had negative margin?", "EXHAUSTIVE"),
        ("Which regions missed target in 2025?", "EXHAUSTIVE"),
        ("Do the HR workbook and the policy memo agree on leave days?", "CONFLICT_CHECK"),
        ("Does the finance deck match the ledger workbook on 2025 revenue?", "CONFLICT_CHECK"),
        ("By what percentage did cost fall from 2024 to 2025?", "DERIVED_CALCULATION"),
        ("How much higher was spend in 2025 than in 2024?", "DERIVED_CALCULATION"),
        ("What proportion of 2025 sales came from exports?", "DERIVED_CALCULATION"),
        ("What was the average quarterly profit in FY2024-25?", "DERIVED_CALCULATION"),
        ("Compare headcount in 2024 and 2025.", "COMPARISON"),
        ("How did sales trend month by month through 2025?", "TIMELINE"),
        ("Why did margin drop in Q2 FY2025-26?", "CAUSAL"),
        ("What changed between version 2 and version 3 of the pricing workbook?", "VERSION_DIFF"),
        ("How does the latest 2025 headcount differ from the original file?", "VERSION_DIFF"),
        ("What is the combined salary of all employees in Pune?", "DERIVED_CALCULATION"),
    ],
)
def test_investigation_phrasings(q, code):
    d = classify_route(q)
    assert d.path is I and code in d.reason_codes, (q, d)


@pytest.mark.parametrize(
    "q,code",
    [
        ("What was net revenue in October?", "AMBIGUOUS_PERIOD"),
        ("What was Q2 spend?", "AMBIGUOUS_PERIOD"),
        ("What was the budget for the second half?", "AMBIGUOUS_PERIOD"),
        ("What were total costs last year?", "RELATIVE_PERIOD_UNANCHORED"),
        ("What was the variance?", "MISSING_SCOPE"),
        ("And what about the other one?", "MISSING_REFERENT"),
        ("How much did it change?", "MISSING_REFERENT"),
    ],
)
def test_clarify_without_context(q, code):
    d = classify_route(q)
    assert d.path is C and d.reason_codes[0] == code, (q, d)
    assert d.clarification_question


def test_quarter_label_alone_is_not_a_stated_year():
    # signals.has_period counts a bare Q label as a period; the router must not.
    sig = QuerySignals(has_period=True)
    d = classify_route("What was Q1 revenue?", signals=sig)
    assert d.path is C and d.reason_codes == ["AMBIGUOUS_PERIOD"]
    assert classify_route("What was Q1 revenue in FY2025-26?", signals=sig).path is F


def test_non_financial_month_is_not_ambiguous():
    assert classify_route("How many parcels were delivered in March?").path is F
    assert classify_route("What was the return rate in October?").path is F


def test_followups_with_history_are_not_clarify():
    for q in ("What about September?", "And Saffron Rusk?", "And the same month last year?"):
        d = classify_route(q, HIST)
        assert d.path is F and "FOLLOWUP_RESOLVED" in d.reason_codes, (q, d)
        assert classify_route(q).path is C, q


def test_period_resolved_from_history_year():
    q = "What was net revenue in October?"
    assert classify_route(q).path is C
    d = classify_route(q, HIST)
    assert d.path is F and "PERIOD_RESOLVED_FROM_HISTORY" in d.reason_codes
    assert classify_route(q, HIST_NO_YEAR).path is C


def test_scope_signal_or_document_reference_suppresses_period_clarify():
    sig = QuerySignals(scope_version_ids=["v1"], scope_files=["Revenue.xlsx"])
    assert classify_route("What was revenue in June?", signals=sig).path is F
    assert classify_route("What was the June revenue in the sales workbook?").path is F
    assert classify_route("What was June revenue in Revenue-FY2025-26.xlsx?").path is F


def test_investigation_of_all_periods_is_not_clarify():
    d = classify_route("What share of Q2 revenue came from exports in each fiscal year?")
    assert d.path is I
    assert classify_route("List every month in which revenue exceeded budget.").path is I


def test_clarify_precedes_investigation_only_for_unresolved_referent():
    assert classify_route("Why did it fall?").path is C
    assert classify_route("Why did it fall?", HIST).path is I


def test_heuristic_classifier_unchanged():
    h = HeuristicQueryClassifier()
    assert h.classify("Compare Q2 and Q3 revenue.") is QueryMode.AGENT
    assert h.classify("What was revenue in June?") is QueryMode.FAST
    # the old classifier still misses the phrasings classify_route adds
    assert h.classify("List every month in which revenue exceeded budget.") is QueryMode.FAST
    assert h.classify("Do the deck and the memo agree on revenue?") is QueryMode.FAST
