"""Deterministic route decision for the Auto router (Upgrade doc 06 sections 3.1
and 4.2): layer 1 only, no model call.

This is ADDITIVE. `HeuristicQueryClassifier` (classifier.py) is the path
`QueryService` executes today and is unchanged; nothing imports this module on
the query path yet. `classify_route` is the candidate for layer 1 of the Auto
router and is measured by `eval-public/experiments/g5_route_decision.py`.

Output is a `RouteDecision`: a path, reason codes (not a confidence), and, for
CLARIFY, one focused question. Reason codes are labels:

  INVESTIGATION: EXHAUSTIVE, COMPARISON, DERIVED_CALCULATION, CROSS_DOCUMENT,
                 CONFLICT_CHECK, TIMELINE, VERSION_DIFF, CAUSAL, IMPACT
  CLARIFY:       AMBIGUOUS_PERIOD, RELATIVE_PERIOD_UNANCHORED, MISSING_SCOPE,
                 MISSING_REFERENT
  FAST:          SIMPLE_LOOKUP, FOLLOWUP_RESOLVED, PERIOD_RESOLVED_FROM_HISTORY

Design rules (all phrase-level, none copied from a gold question):

* Precedence: unresolved referent / missing scope / unanchored relative period
  (CLARIFY) -> investigation phrasing (INVESTIGATION) -> period ambiguity
  (CLARIFY) -> FAST. A question that investigates every period is not
  ambiguous about which period.
* CLARIFY comes only from pre-retrieval signals: `QuerySignals.scoped` /
  `has_period` (or the equivalents computed here when no signals are passed).
  Known gap in `signals.has_period`: a bare Q1-Q4/H1/H2 label counts as a
  stated period, but a quarter label does not say WHICH fiscal year, so here a
  quarter or month needs an explicit year / fiscal-year label (or a named
  document, or a year in the conversation history) before it counts as stated.
  Only period-dependent financial metrics trigger it (revenue, sales, budget,
  cost...), so "orders shipped in August" does not.
* Where a follow-up ("And X?", pronoun in a short question) has history, the
  referent is considered resolved and the question is not CLARIFY.
* Real ambiguity detection (`detect_period_ambiguity`) is post-retrieval and
  out of scope; this layer cannot know how many fiscal years a corpus holds,
  so it asks whenever a financial period has no year (a deliberately
  conservative proxy).
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass, field
from typing import Mapping, Sequence

from docket.services.query.signals import QuerySignals, has_period


class UserMode(str, enum.Enum):
    AUTO = "auto"
    FAST = "fast"
    PLAN = "plan"


class ExecutionPath(str, enum.Enum):
    FAST = "FAST"
    INVESTIGATION = "INVESTIGATION"
    CLARIFY = "CLARIFY"


@dataclass(frozen=True)
class RouteDecision:
    path: ExecutionPath
    reason_codes: list[str] = field(default_factory=list)
    clarification_question: str | None = None


# --------------------------------------------------------------------------
# Investigation phrasing, by reason code. Each entry is a general phrasing
# family (a question shape), not a gold-question string.
# --------------------------------------------------------------------------

_UNIT_NOUNS = r"(?:months?|quarters?|years?|weeks?|days?|periods?|half(?:s|ves)?)"
_ENTITY_PLURALS = (
    r"(?:months|quarters|years|weeks|days|periods|products|product lines|lines|regions|"
    r"departments|employees|people|staff|vendors|suppliers|customers|clients|branches|"
    r"sites|stores|warehouses|vessels|projects|teams|items|sheets|files|documents|"
    r"workbooks|sources|metrics|figures|values|cells|rows|columns|entries|orders|contracts)"
)
_DOC_NOUN = (
    r"(?:deck|slides?|presentation|memo|workbooks?|spreadsheets?|sheets?|files?|report|"
    r"reports|documents?|policy|policies|statement|statements|pdf|pptx|xlsx|docx|"
    r"minutes|contract|contracts|version|versions)"
)
_NUM_MEASURE = r"(?:below|above|under|over|exceed(?:ed|s)?|less than|more than|greater than|at least|at most|beyond)"

_CODE_PATTERNS: dict[str, list[str]] = {
    "COMPARISON": [
        r"\bcompar(?:e|es|ed|ing|ison)\b",
        r"\bdifferences? between\b",
        r"\bversus\b",
        r"\bvs\.?\b",
        r"\bwhich (?:is|are) (?:better|worse|more|less|preferred|recommended)\b",
        r"\b(?:higher|lower|greater|larger|bigger|smaller|better|worse) than\b",
        r"\b(?:fastest|slowest|highest|lowest|largest|smallest|biggest|most|least)\b.{0,40}\b(?:growth|grew|grow|increase|increased|decrease|decreased|decline|declined|fell|rose|change|changed)\b",
        r"\b(?:grew|grow|increased?|decreased?|declined?|fell|dropped|rose|changed?)\b.{0,40}\b(?:fastest|slowest|most|least|the most|the least)\b",
        r"\bwhich\b.{0,40}\b(?:fastest|slowest|highest|lowest|largest|smallest|biggest)\b",
    ],
    "CONFLICT_CHECK": [
        r"\b(?:agree|agrees|agreed|disagree|disagrees|disagreed|consistent|inconsistent|reconcil\w*|discrepanc\w*|contradict\w*|conflicts?)\b",
        r"\bmatch(?:es|ed)?\b",
    ],
    "DERIVED_CALCULATION": [
        r"\bby (?:what|how much|how many)\b",
        r"\bhow much (?:more|less|higher|lower|greater|larger|smaller|fewer)\b",
        r"\bhow many (?:more|fewer|times)\b",
        r"\b(?:percent(?:age)?|%)\s*(?:change|growth|increase|decrease|decline|difference|rise|drop)\b",
        r"\b(?:growth|change|increase|decrease|decline) (?:rate|in percent(?:age)?)\b",
        r"\bwhat (?:share|proportion|portion|fraction|percent(?:age)?|ratio|part)\b",
        r"\b(?:share|proportion|ratio|percentage) of\b",
        r"\b(?:combined|aggregate|cumulative|sum of|net of|difference of)\b",
        r"\b(?:differ|differed|differs)\b.{0,30}\bfrom\b",
        r"\b(?:exceed|exceeded|exceeds|outperform\w*|underperform\w*)\b",
        r"\b(?:average|mean|median)\b.{0,50}\b(?:monthly|quarterly|weekly|daily|annual|across|over|between|per (?:month|quarter|week|year)|from)\b",
        r"\b(?:monthly|quarterly|weekly|daily|annual)\b.{0,15}\b(?:average|mean|median)\b",
        r"\b(?:total|combined|sum)\b.{0,40}\b(?:of|for) (?:all |the |each )?" + _ENTITY_PLURALS + r"\b",
    ],
    "EXHAUSTIVE": [
        r"\b(?:list|enumerate|show|name|give me|summari[sz]e) (?:all|every|each)\b",
        r"\b(?:list|enumerate)\b.{0,30}\b(?:all|every|each)\b",
        r"\b(?:every|each) (?:one of )?(?:the )?(?:" + _UNIT_NOUNS + r"|metric|product|item|figure|instance|time|place|file|document|sheet|source|employee|vendor|customer)\b",
        r"\ball (?:the )?" + _ENTITY_PLURALS + r"\b",
        r"\ball instances of\b",
        r"\bwhich (?:\w+\s+){0,2}" + _ENTITY_PLURALS + r"\b",
        r"\bwhat (?:\w+\s+)?(?:months|quarters|years|weeks|days|periods)\b",
        r"\bin which " + _ENTITY_PLURALS + r"\b",
        r"\bhow many " + _ENTITY_PLURALS + r"\b.{0,50}\b" + _NUM_MEASURE + r"\b",
        r"\bwith no\b.{0,30}\b(?:recorded|reported|entries|output|sales|revenue)\b",
        r"\bwhat (?:has |have )?changed\b",
        r"\bwhat (?:are|were) the (?:differences|changes)\b",
    ],
    "CROSS_DOCUMENT": [
        r"\bthroughout\b",
        r"\bin every document\b",
        r"\bwhich sources\b",
        r"\bacross (?:all |the |every |both |multiple |several |different |these |those )?(?:\w+\s+){0,2}(?:documents?|files?|sources?|workbooks?|sheets?|decks?|memos?|reports?|versions?|years?|fiscal years?|periods?|quarters?|months?|departments?|regions?|entities|subsidiar\w+|contracts?|policies|vendors?|products?|lines?)\b",
        r"\bacross all\b",
    ],
    "TIMELINE": [
        r"\bhistory of\b",
        r"\btimeline\b",
        r"\bevolv(?:ed|ing|e|es|ution)\b",
        r"\bchanged? over time\b",
        r"\bover the (?:years|months|quarters|period)\b",
        r"\b(?:month|quarter|week|year)[- ]by[- ](?:month|quarter|week|year)\b",
        r"\b(?:year|quarter|month)[- ]over[- ](?:year|quarter|month)\b",
        r"\btrends?\b",
        r"\btrace\b",
        r"\bhow (?:did|has|have|does)\b.{0,60}\b(?:change|changed|vary|varied|trend|move|moved|progress|progressed|develop|developed)\b",
    ],
    "CAUSAL": [
        r"\bwhy\b",
        r"\bwhat (?:led to|caused|drove|explains?|explained|accounts? for)\b",
        r"\broot cause\b",
        r"\breasons? (?:for|behind)\b",
        r"\bdrivers? of\b",
    ],
    "IMPACT": [
        r"\b(?:impact|effects?|implications?|consequences?) (?:of|on)\b",
        r"\bhow does\b.{0,60}\baffect\b",
        r"\brelationship between\b",
        r"\bdownstream of\b",
    ],
    "VERSION_DIFF": [],  # built from two cues below
}

_COMPILED = {c: re.compile("|".join(p), re.IGNORECASE) for c, p in _CODE_PATTERNS.items() if p}

# VERSION_DIFF = a difference/change verb AND a version cue.
_DIFF_VERB_RE = re.compile(
    r"\b(?:differ\w*|different|difference|compar\w+|vs\.?|versus|changed?|changes|diff|delta|updated|"
    r"revis\w+|restat\w+|supersed\w+|agree\w*|match\w*)\b", re.IGNORECASE)
_VERSION_CUE_RE = re.compile(
    r"\bversions?\s*(?:\d+|[a-z]\b)|\bv\d+\s*(?:and|vs\.?|to|->|with)\s*v\d+\b"
    r"|\b(?:original|earlier|previous|prior|older|first|initial|restated|revised|amended|superseded)\s+"
    r"(?:version|workbook|file|deck|memo|figures?|values?|numbers?|draft)\b"
    r"|\bbetween (?:the )?versions\b|\b(?:latest|newest|current) (?:version|copy)\b"
    r"|\b(?:restated|revised|amended)\b",
    re.IGNORECASE,
)
_WHAT_CHANGED_RE = re.compile(r"\bwhat (?:has |have )?changed between\b|\bchanges? between\b", re.IGNORECASE)

# Spatial "across" ("across the columns", "across the ends of a rod") is a
# layout word, not a breadth signal. Likewise a quantity "difference between".
_SPATIAL_ACROSS_RE = re.compile(
    r"\bacross (?:the |all the |these |those )?(?:\w+\s+){0,2}(?:columns?|rows?|cells?|tabs?|ends?|header|table|bar|terminals?)\b",
    re.IGNORECASE,
)
_QUANTITY_DIFF_RE = re.compile(
    r"\b(?:potential|temperature|pressure|phase|voltage|energy|concentration|density|ph|emf)\s+difference\b"
    r"|\brelationship between\b.{0,100}\baccording to\b.{0,60}\blaw\b",
    re.IGNORECASE,
)

_DOC_MENTION_RE = re.compile(r"\b" + _DOC_NOUN + r"\b", re.IGNORECASE)
_NAMED_FILE_RE = re.compile(r"\b[\w-]+\.(?:xlsx|xls|csv|docx|pptx|pdf|md|txt)\b", re.IGNORECASE)

# --------------------------------------------------------------------------
# CLARIFY signals
# --------------------------------------------------------------------------

_MONTH = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|"
    r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
)
_MONTH_RE = re.compile(rf"\b{_MONTH}\b(?!\s*\d)", re.IGNORECASE)
_QUARTER_RE = re.compile(r"(?<![A-Za-z0-9])(?:Q[1-4]|H[12])(?![A-Za-z0-9])", re.IGNORECASE)
_PERIOD_PHRASE_RE = re.compile(
    r"\b(?:first|second|third|fourth|1st|2nd|3rd|4th|last|final)\s+(?:half|quarter)\b|\bfiscal year\b(?!\s*\d)|\bthe year\b",
    re.IGNORECASE,
)
_YEAR_STATED_RE = re.compile(
    r"(?<!\d)(?:19|20)\d{2}(?!\d)|\b(?:FY|CY|FYE)\s?'?\d{2,4}\b|\bfiscal year \d{2,4}\b|\b(?:FY|CY)\d{2}",
    re.IGNORECASE,
)
_ALL_PERIODS_RE = re.compile(
    r"\b(?:each|every|all|any) (?:fiscal |calendar )?(?:year|period)s?\b|\bin each\b|\bper (?:fiscal )?year\b",
    re.IGNORECASE,
)
_FINANCIAL_METRIC_RE = re.compile(
    r"\b(?:revenue|revenues|sales|turnover|income|profit|margin|ebitda|budget|budgeted|variance|"
    r"cost|costs|spend|spending|expense|expenses|expenditure|opex|capex|capital|target|forecast|"
    r"headcount|payroll|salary|salaries|total|totals)\b",
    re.IGNORECASE,
)
_RELATIVE_PERIOD_RE = re.compile(
    r"\b(?:last|previous|prior|preceding|next|this|current|same)\s+(?:fiscal\s+|calendar\s+)?"
    r"(?:year|quarter|month|half|period|fy)\b|\b(?:year|quarter|month)[- ]to[- ]date\b|\blast (?:fy|cy)\b"
    r"|\b(?:ytd|qtd|mtd)\b|\bsame month\b|\bsame (?:period|quarter)\b",
    re.IGNORECASE,
)
_DOC_REF_RE = re.compile(
    r"\b(?:in|from|according to|per|on|of|within)\s+(?:the|this|that|our|its)\s+(?:[\w'’-]+\s+){0,4}"
    + _DOC_NOUN + r"\b",
    re.IGNORECASE,
)

_FOLLOWUP_START_RE = re.compile(r"^\s*(?:and|also|then|what about|how about|same (?:for|with)|ok(?:ay)?,? (?:and|what))\b", re.IGNORECASE)
_PRONOUN_RE = re.compile(r"\b(?:it|its|that|those|they|them|their|this|these|the same)\b", re.IGNORECASE)
_SHORT_FOLLOWUP_WORDS = 8

_STOPWORDS = frozenset(
    "a an the what was were is are did does do how much many which who whom whose when where of in for on at to by "
    "and or with from as be been being it its this that these those there their please tell me show give "
    "value amount number figure".split()
)
# Generic, entity-free metric words: a question made only of these names no
# subject and no period at all ("What was the total?").
_GENERIC_METRIC_WORDS = frozenset(
    "total totals sum subtotal variance budget budgeted actual actuals revenue sales cost costs spend expense "
    "expenses profit margin growth change difference target average count rate".split()
)


def _year_stated(text: str) -> bool:
    return bool(_YEAR_STATED_RE.search(text))


def _history_text(history: Sequence[Mapping[str, str]] | None) -> str:
    if not history:
        return ""
    parts: list[str] = []
    for turn in history:
        for key in ("question", "answer"):
            v = turn.get(key) if hasattr(turn, "get") else None
            if v:
                parts.append(str(v))
    return " ".join(parts)


def _is_followup(question: str) -> bool:
    q = question.strip()
    if _FOLLOWUP_START_RE.search(q):
        return True
    return len(q.split()) <= _SHORT_FOLLOWUP_WORDS and bool(_PRONOUN_RE.search(q))


def _content_tokens(question: str) -> list[str]:
    return [t for t in re.findall(r"[A-Za-z][A-Za-z'-]*", question.lower()) if t not in _STOPWORDS]


def _investigation_codes(question: str) -> list[str]:
    q = question
    codes: list[str] = []
    if _SPATIAL_ACROSS_RE.search(q):
        q = _SPATIAL_ACROSS_RE.sub(" ", q)
    quantity_diff = bool(_QUANTITY_DIFF_RE.search(q))
    if quantity_diff:
        q = _QUANTITY_DIFF_RE.sub(" ", q)

    n_docs = len({m.group(0).lower().rstrip("s") for m in _DOC_MENTION_RE.finditer(q)})
    n_docs += len(_NAMED_FILE_RE.findall(q))
    for code in ("EXHAUSTIVE", "COMPARISON", "DERIVED_CALCULATION", "CROSS_DOCUMENT", "TIMELINE", "CAUSAL", "IMPACT"):
        if _COMPILED[code].search(q):
            codes.append(code)
    # CONFLICT_CHECK needs two sources being checked against each other: a bare
    # "match"/"agree" is too common in single lookups.
    if _COMPILED["CONFLICT_CHECK"].search(q) and n_docs >= 2:
        codes.append("CONFLICT_CHECK")
        if "CROSS_DOCUMENT" not in codes:
            codes.append("CROSS_DOCUMENT")
    # "how does the memo's X compare with the workbooks" is also a source cross-check.
    if "COMPARISON" in codes and n_docs >= 2 and "CROSS_DOCUMENT" not in codes:
        codes.append("CROSS_DOCUMENT")
    if _WHAT_CHANGED_RE.search(q) or (_DIFF_VERB_RE.search(q) and _VERSION_CUE_RE.search(q)):
        codes.append("VERSION_DIFF")
        if "EXHAUSTIVE" not in codes and _WHAT_CHANGED_RE.search(q):
            codes.append("EXHAUSTIVE")
    return codes


@dataclass(frozen=True)
class _Ctx:
    scoped: bool
    period_stated: bool


def _clarify_period_question(question: str) -> str:
    month = _MONTH_RE.search(question)
    quarter = _QUARTER_RE.search(question)
    what = (month or quarter)
    label = what.group(0) if what else "that period"
    return (
        f"Which fiscal or calendar year do you mean for {label}? "
        "The question does not state a year and more than one may be on file."
    )


def classify_route(
    question: str,
    history: Sequence[Mapping[str, str]] | None = None,
    signals: QuerySignals | None = None,
) -> RouteDecision:
    """Deterministic route for `question` (no model call).

    `history` is the prior turns as mappings with `question`/`answer` keys.
    `signals` are the pre-retrieval `QuerySignals`; when omitted the file
    scope is unknown (treated as unscoped) and the period check is computed
    from the question text."""
    q = question.strip()
    scoped = bool(signals and signals.scoped)
    period_any = signals.has_period if signals is not None else has_period(q)
    hist_text = _history_text(history)
    has_history = bool(history)

    codes = _investigation_codes(q)
    followup = _is_followup(q)

    # 1) Unresolved referent: a follow-up with nothing to resolve it against.
    if followup and not has_history and not scoped and not codes:
        return RouteDecision(
            ExecutionPath.CLARIFY,
            ["MISSING_REFERENT"],
            "What should this refer to? There is no earlier question in this conversation to resolve it against.",
        )
    if followup and not has_history and not scoped and codes and _PRONOUN_RE.search(q) and len(q.split()) <= _SHORT_FOLLOWUP_WORDS:
        return RouteDecision(
            ExecutionPath.CLARIFY,
            ["MISSING_REFERENT"],
            "What should this refer to? There is no earlier question in this conversation to resolve it against.",
        )

    # 2) Missing scope: only generic metric words, no subject, no period, no file.
    tokens = _content_tokens(q)
    if (
        tokens
        and not codes
        and not followup
        and not scoped
        and not period_any
        and not _NAMED_FILE_RE.search(q)
        and not _DOC_REF_RE.search(q)
        and not _DOC_MENTION_RE.search(q)
        and all(t in _GENERIC_METRIC_WORDS for t in tokens)
    ):
        return RouteDecision(
            ExecutionPath.CLARIFY,
            ["MISSING_SCOPE"],
            "Which figure, file or period do you mean? The question does not say what the value belongs to.",
        )

    # 3) Relative period with no anchor and nothing in history to anchor it.
    rel = _RELATIVE_PERIOD_RE.search(q)
    if rel and not has_history and not scoped and not _year_stated(q) and not codes:
        return RouteDecision(
            ExecutionPath.CLARIFY,
            ["RELATIVE_PERIOD_UNANCHORED"],
            f"What period does \"{rel.group(0)}\" refer to? Please give the year or fiscal year.",
        )

    # 4) Investigation phrasing.
    if codes:
        return RouteDecision(ExecutionPath.INVESTIGATION, codes)

    # 5) Period ambiguity: a financial metric tied to a month/quarter/half with
    # no year, no named document, and no year available from the conversation.
    mentions_period = bool(_MONTH_RE.search(q) or _QUARTER_RE.search(q) or _PERIOD_PHRASE_RE.search(q))
    if (
        mentions_period
        and _FINANCIAL_METRIC_RE.search(q)
        and not _year_stated(q)
        and not _ALL_PERIODS_RE.search(q)
        and not scoped
        and not _NAMED_FILE_RE.search(q)
        and not _DOC_REF_RE.search(q)
        and not followup
    ):
        if _year_stated(hist_text):
            return RouteDecision(ExecutionPath.FAST, ["PERIOD_RESOLVED_FROM_HISTORY", "SIMPLE_LOOKUP"])
        return RouteDecision(
            ExecutionPath.CLARIFY, ["AMBIGUOUS_PERIOD"], _clarify_period_question(q)
        )

    # 6) FAST.
    if followup and has_history:
        return RouteDecision(ExecutionPath.FAST, ["FOLLOWUP_RESOLVED", "SIMPLE_LOOKUP"])
    return RouteDecision(ExecutionPath.FAST, ["SIMPLE_LOOKUP"])
