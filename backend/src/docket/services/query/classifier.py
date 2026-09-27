"""Routes a question to the fast path or the bounded investigation agent.

`QueryService.ask()` used to always do direct retrieve->generate (the "fast
path", validated at 12/12 correct in the spike's eval -- see
`docket.query.service`'s module docstring). Now that the bounded LangGraph
agent (`docket.agent.graph.build_investigation_agent`) exists as a second,
already-tested query mechanism, something has to decide -- per question --
which one actually answers it. That's this module's job, and only this
module's job: it makes a routing decision and nothing else, so `QueryService`
never has to know *how* the decision is made.

`QueryClassifier` is a `Protocol` (mirroring `docket.inference.gateway
.InferenceGateway`'s pattern: depend on the interface, swap the
implementation) specifically so a smarter classifier -- e.g. one that asks a
small/cheap LLM call "does this need multi-step investigation?" -- can
replace `HeuristicQueryClassifier` later without `QueryService` changing at
all.
"""

from __future__ import annotations

import enum
import re
from typing import Protocol


class QueryMode(str, enum.Enum):
    FAST = "fast"
    AGENT = "agent"


class QueryClassifier(Protocol):
    def classify(self, question: str) -> QueryMode: ...


# Phrasing that signals the question can't be answered from a single
# retrieval pass over one (or a few similar) chunks -- it asks the answerer
# to actively gather and relate MULTIPLE pieces of evidence itself:
#
# - comparison ("compare", "difference between", "X vs Y", "which is
#   better"): answering requires retrieving evidence for more than one thing
#   and weighing them against each other, not reading one passage.
# - change/history over time ("history of", "evolved", "changed over time",
#   "over the years", "timeline of"): requires finding and ordering evidence
#   from multiple points in time, not one snapshot.
# - causal/explanatory "why"/provenance ("why did", "why does", "what led
#   to", "what caused", "root cause"): usually means chaining from an effect
#   back through contributing evidence -- exactly the kind of multi-hop
#   lookup search_knowledge -> read_evidence -> search_knowledge again is
#   for, vs. a single fact lookup.
# - impact/relationship tracing ("impact of", "effect of", "how does X
#   affect Y", "relationship between", "trace", "downstream of"): needs
#   evidence about X AND evidence about Y AND how they connect.
# - exhaustive enumeration ("all instances of", "every time", "each place
#   where", "list all"): a single top-k retrieval pass is tuned for "the
#   best matching chunk(s)", not "every matching chunk" -- an investigation
#   that can re-search with different terms is a better fit.
# - explicit cross-document/breadth signal ("across", "throughout", "in
#   every document", "which sources"): asks the answerer to reason over more
#   than whatever one retrieval pass happens to surface.
#
# Deliberately NOT included: generic wh-words ("what", "when", "who",
# "where", "how many", "does X"), superlatives ("what is the best/largest"),
# or plain definition/fact-lookup phrasing -- those are the common case
# (single-passage lookups) that the fast path already handles correctly, and
# routing them to the agent would just add LangGraph/tool-loop latency for
# no accuracy benefit. Kept intentionally short and reviewable rather than
# exhaustive: a false negative (an investigative question that slips through
# to FAST) still gets an answer, just a possibly-incomplete one -- a false
# positive (routing an ordinary lookup to AGENT) costs latency but not
# correctness. That asymmetry is why the list below leans conservative.
_AGENT_PATTERNS = [
    r"\bcompare\b",
    r"\bcomparison\b",
    r"\bdifference between\b",
    r"\bdifferences between\b",
    r"\bversus\b",
    r"\bvs\.?\b",
    r"\bwhich (?:is|are) (?:better|worse|more|less|preferred|recommended)\b",
    r"\bhistory of\b",
    r"\bevolv(?:ed|ing|e)\b",
    r"\bchanged? over time\b",
    r"\bover the years\b",
    r"\btimeline of\b",
    r"\bwhy did\b",
    r"\bwhy does\b",
    r"\bwhy is\b",
    r"\bwhat led to\b",
    r"\bwhat caused\b",
    r"\broot cause\b",
    r"\bimpact of\b",
    r"\beffect(?:s)? of\b",
    r"\bhow does .+ affect\b",
    r"\brelationship between\b",
    r"\btrace\b",
    r"\bdownstream of\b",
    r"\ball instances of\b",
    r"\bevery time\b",
    r"\beach place where\b",
    r"\blist all\b",
    r"\bacross\b",
    r"\bthroughout\b",
    r"\bin every document\b",
    r"\bwhich sources\b",
]

_AGENT_RE = re.compile("|".join(_AGENT_PATTERNS), re.IGNORECASE)


# Real Physics-eval false positives (see the eval that found this: 3/93 runs
# misrouted to AGENT, all three from `\bdifference between\b` /
# `\brelationship between\b` firing on ordinary single-quantity technical
# phrasing rather than an actual comparison) exposed that those two patterns
# are ambiguous: "difference between"/"relationship between" is both (a) how
# you phrase a genuine comparison of two distinct things ("difference
# between socialism and capitalism", "relationship between inflation and
# unemployment") AND (b) standard scientific/technical terminology for
# specifying or relating a SINGLE quantity ("potential difference between
# conductors 1 and 2" names one value, at two points; "relationship between
# the potential difference across R and the current I according to Ohm's
# law" asks to recall one named law/formula, not to investigate and weigh
# two separate things against each other). A pure regex can't reliably tell
# those apart in general -- there's no syntactic marker that distinguishes
# "between X and Y" where X/Y are two points defining one measurement from
# "between X and Y" where X/Y are two different things being compared -- so
# rather than deleting the patterns (losing real comparison detection) or
# leaving them as-is (the false-positive-costly failure mode this file's
# module docstring says to avoid), this narrows them with two conservative,
# documented carve-outs for the two shapes that actually showed up in real
# data:
#
# 1. "<quantity> difference between ..." where <quantity> is one of a short
#    list of physical/technical quantities that conventionally form a fixed
#    compound noun with "difference" (potential difference, temperature
#    difference, pressure difference, phase difference, voltage difference,
#    energy difference, concentration/density/pH difference) -- in that
#    shape "between ..." specifies which two points/objects the ONE named
#    quantity is measured between, not two different things being
#    contrasted. Plain "difference between X and Y" (no such quantity word
#    immediately before it) is untouched and still routes to AGENT.
# 2. "relationship between ... according to ... law" -- the "according to
#    <named law>" framing is a strong, general signal that the question is
#    asking to recall one named scientific law/formula (a single-fact
#    lookup), not to investigate how two independent things relate. Plain
#    "relationship between X and Y" with no law/formula reference is
#    untouched and still routes to AGENT (e.g. "relationship between the two
#    subsidiaries", "relationship between inflation and unemployment").
# 3. "<quantity>/difference across ..." -- found the same way as (1)/(2)
#    (re-checking the same real eval's third misrouted question after fixing
#    the first two): `\bacross\b` is meant as a cross-document/breadth
#    signal ("across the contracts", "across all documents" -- see that
#    pattern's comment above), but "across" is also the ordinary spatial
#    preposition technical writing uses for a quantity measured between two
#    points of ONE object ("induced emf across the ends of a rod", "voltage
#    across a resistor", "potential difference across a membrane") -- not a
#    request to reason over multiple documents/sources at all. Excluded the
#    same way as (1): only when "across" is immediately preceded by one of a
#    short list of quantity words (reusing most of the same list) or by
#    "difference". Plain "across" elsewhere (documents, sources, contracts,
#    departments, ...) is untouched and still routes to AGENT.
#
# This is a deliberately narrow fix, not a general "same entity type"
# detector -- e.g. "temperature difference between room A and the freezer"
# without the word "temperature" directly before "difference between", or a
# law-reference phrase that happens to appear in a genuinely comparative
# question, will not be caught/excluded by this. That's an accepted gap
# given the asymmetry this file already leans on (false negative here still
# gets an answer via FAST; forcing a fragile, over-fitted regex to close
# every gap would risk the opposite, costlier failure mode instead).
_QUANTITY_WORDS = (
    r"(?:potential|temperature|pressure|phase|voltage|energy|concentration|density|ph"
    r"|emf|current|field|force|charge|resistance|capacitance)"
)

_FALSE_POSITIVE_PATTERNS = [
    rf"\b{_QUANTITY_WORDS}\s+difference between\b",
    r"\brelationship between\b.{0,100}\baccording to\b.{0,60}\blaw\b",
    rf"\b(?:{_QUANTITY_WORDS}|difference)\s+across\b",
]

_FALSE_POSITIVE_RE = re.compile("|".join(_FALSE_POSITIVE_PATTERNS), re.IGNORECASE)


class HeuristicQueryClassifier:
    """Rule-based classifier, no LLM call -- deterministic, zero added
    latency/cost on every query. Routes to AGENT when the question's phrasing
    matches `_AGENT_PATTERNS` (comparisons, history/change-over-time,
    causal "why", impact/relationship tracing, exhaustive enumeration, or
    explicit cross-document breadth -- see the module-level comment above
    each pattern group for the reasoning behind each one) AND does not also
    match `_FALSE_POSITIVE_PATTERNS` (the narrow, documented carve-outs for
    single-quantity technical phrasing that looks like but isn't a genuine
    comparison -- see the comment above that list). Routes to FAST
    otherwise, which is the default and the common case: most real questions
    are single-fact lookups, and the fast path was validated at 12/12 correct
    in the spike's eval, so an unmatched question should stay on it rather
    than pay for an agent loop it doesn't need.
    """

    def classify(self, question: str) -> QueryMode:
        if _AGENT_RE.search(question) and not _FALSE_POSITIVE_RE.search(question):
            return QueryMode.AGENT
        return QueryMode.FAST
