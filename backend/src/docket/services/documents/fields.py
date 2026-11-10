"""Deterministic contract-field extractors (regex + heading awareness).

Input is the stored chunk text of ONE document (no re-parse). Each extractor
returns `FieldValue`s that keep the supporting chunk id and the character span
inside that chunk's text, so any answer built from them cites real text.

Confidence is deliberately coarse: >= 0.8 means "a labelled, unambiguous
clause"; 0.5-0.7 means "plausible but ambiguous" (conflicting jurisdictions,
unlabelled date); 0.3 marks an absence. Query-time lookup only answers from
>= LOOKUP_MIN_CONFIDENCE; everything else falls through to normal retrieval.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from docket.services.documents.classify import find_dates

EXTRACTOR_VERSION = "1"
LOOKUP_MIN_CONFIDENCE = 0.8

FIELDS = (
    "governing_law", "effective_date", "expiration_or_term", "renewal_term",
    "termination_notice_period", "parties", "liability_cap", "exclusivity",
    "non_compete", "anti_assignment", "audit_rights", "insurance",
)


@dataclass(frozen=True)
class ChunkText:
    chunk_id: str
    text: str
    ordinal: int = 0
    heading: str | None = None


@dataclass(frozen=True)
class FieldValue:
    field: str
    value: str
    normalized: str | None
    chunk_id: str | None
    span: tuple[int, int] | None
    confidence: float


# --- helpers -------------------------------------------------------------

_NUM_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40, "forty-five": 45, "fortyfive": 45,
    "sixty": 60, "ninety": 90, "one hundred twenty": 120, "one hundred eighty": 180,
    "ninety-day": 90, "hundred": 100, "a": 1, "an": 1,
}
_UNIT = r"(?:day|week|month|year)s?"
_NUMBER = (
    r"(?:(?P<word>(?:one hundred (?:twenty|eighty)|forty[- ]five|"
    + "|".join(w for w in _NUM_WORDS if " " not in w and "-" not in w and w not in ("a", "an"))
    + r"))\s*(?:\(\s*(?P<paren>\d+)\s*\))?|(?P<digits>\d+))"
)
_DURATION = re.compile(rf"{_NUMBER}[\s-]*(?:\(\s*(?P<paren2>\d+)\s*\)\s*)?(?P<unit>{_UNIT})", re.I)


def parse_duration(text: str) -> tuple[str, int, int] | None:
    """First 'sixty (60) days' / '30 days' / 'one year' in `text` ->
    (normalised '60 days', start, end). A parenthesised digit wins over the word."""
    for m in _DURATION.finditer(text):
        n: int | None = None
        if m.group("paren") or m.group("paren2"):
            n = int(m.group("paren") or m.group("paren2"))
        elif m.group("digits"):
            n = int(m.group("digits"))
        elif m.group("word"):
            n = _NUM_WORDS.get(m.group("word").lower().replace(" ", " "))
        if n is None:
            continue
        unit = m.group("unit").lower().rstrip("s")
        return (f"{n} {unit}{'s' if n != 1 else ''}", m.start(), m.end())
    return None


def _sentence_bounds(text: str, pos: int, end: int | None = None) -> tuple[int, int]:
    end = pos if end is None else end
    start = 0
    for m in re.finditer(r"(?<=[.;:])\s+(?=[A-Z(“\"\d])|\n\s*\n", text[: pos]):
        start = m.end()
    start = max(start, pos - 700)
    m = re.search(r"(?<=[a-z0-9)\"”])[.;](?=\s|$)|\n\s*\n", text[end:])
    stop = end + m.end() if m else min(len(text), end + 700)
    return start, min(stop, end + 900)


def _fv(field: str, ch: ChunkText, s: int, e: int, normalized: str | None, conf: float, value: str | None = None) -> FieldValue:
    return FieldValue(field, (value if value is not None else ch.text[s:e]).strip(), normalized, ch.chunk_id, (s, e), conf)


def _heading_boost(ch: ChunkText, pattern: str) -> float:
    return 0.05 if ch.heading and re.search(pattern, ch.heading, re.I) else 0.0


# --- governing law -------------------------------------------------------

_US_STATES = [
    "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado", "Connecticut", "Delaware",
    "Florida", "Georgia", "Hawaii", "Idaho", "Illinois", "Indiana", "Iowa", "Kansas", "Kentucky",
    "Louisiana", "Maine", "Maryland", "Massachusetts", "Michigan", "Minnesota", "Mississippi",
    "Missouri", "Montana", "Nebraska", "Nevada", "New Hampshire", "New Jersey", "New Mexico",
    "New York", "North Carolina", "North Dakota", "Ohio", "Oklahoma", "Oregon", "Pennsylvania",
    "Rhode Island", "South Carolina", "South Dakota", "Tennessee", "Texas", "Utah", "Vermont",
    "Virginia", "Washington", "West Virginia", "Wisconsin", "Wyoming", "District of Columbia",
    "Puerto Rico",
]
_OTHER_JURISDICTIONS = [
    "England and Wales", "England", "Wales", "Scotland", "Northern Ireland", "Ireland",
    "Singapore", "Hong Kong", "People's Republic of China", "China", "Japan", "Korea",
    "Republic of Korea", "India", "Australia", "New South Wales", "Victoria", "Queensland",
    "New Zealand", "Canada", "Ontario", "British Columbia", "Alberta", "Quebec", "Germany",
    "France", "Switzerland", "Netherlands", "The Netherlands", "Belgium", "Luxembourg", "Sweden",
    "Denmark", "Norway", "Finland", "Spain", "Italy", "Austria", "Israel", "Cayman Islands",
    "British Virgin Islands", "Bermuda", "Jersey", "Guernsey", "Brazil", "Mexico", "Taiwan",
    "United Arab Emirates", "Dubai", "Russia", "South Africa", "Thailand", "Malaysia", "Indonesia",
    "Philippines", "Vietnam", "Turkey", "Poland", "Portugal", "Greece", "Czech Republic",
    "Federal Republic of Germany", "United Kingdom", "Hungary", "Chile", "Argentina", "Peru",
    "Colombia", "Panama", "Cyprus", "Malta", "Mauritius", "Nigeria", "Kenya", "Egypt", "Saudi Arabia",
    "Qatar", "Pakistan", "Bangladesh", "Sri Lanka",
]
_ALL_JURIS = sorted(_US_STATES + _OTHER_JURISDICTIONS, key=len, reverse=True)
_JURIS_RE = re.compile(r"^(?:the\s+)?(?:(?:state|commonwealth|province|republic|kingdom|territory)\s+of\s+)?(?:the\s+)?(" + "|".join(re.escape(j) for j in _ALL_JURIS) + r")\b", re.I)
_CANON = {j.lower(): j for j in _ALL_JURIS}

_GOV_ANCHOR = re.compile(
    r"(?:governed|construed|interpreted|enforced|controlled)\b[^.;]{0,120}?|governing\s+law\b[^.;]{0,40}?|"
    r"subject\s+to\b[^.;]{0,40}?|in\s+accordance\s+with\b[^.;]{0,40}?|under\b[^.;]{0,30}?", re.I)
_LAWS_OF = re.compile(r"\b(?:the\s+)?(?:internal\s+|substantive\s+|domestic\s+)?laws?\s+(?:of|in)\s+(?:the\s+)?", re.I)
_ORG_CTX = re.compile(r"organi[sz]ed|incorporated|existing\s+under|formed\s+under|duly\s+(?:organi|incorp)|registered\s+(?:in|under)|licensed\s+under|qualified\s+to", re.I)
_OTHER_LAW = re.compile(r"intellectual\s+property|patent|trademark|copyright|except\s+(?:that|as)|provided\s+that|notwithstanding", re.I)


def _governing_law_candidates(ch: ChunkText) -> list[tuple[str, int, int, float, int]]:
    """(canonical, span_start, span_end, confidence, sentence_priority)"""
    out = []
    text = ch.text
    for m in re.finditer(r"\b(?:governed|construed|interpreted|enforced)\b|governing\s+law|choice\s+of\s+law|applicable\s+law", text, re.I):
        s, e = _sentence_bounds(text, m.start(), m.end())
        sent = text[s:e]
        for lm in _LAWS_OF.finditer(sent):
            tail = sent[lm.end():lm.end() + 80]
            jm = _JURIS_RE.match(tail)
            if not jm:
                continue
            before = sent[max(0, lm.start() - 120):lm.start()]
            if _ORG_CTX.search(before) and not re.search(r"govern|construed|interpret", before, re.I):
                continue
            canon = _CANON[jm.group(1).lower()]
            if re.search(r"federal\s+(?:and\s+\w+\s+)?$", before, re.I):
                continue  # "...and the federal laws of the United States" is not the chosen law
            conf = 0.9
            if _heading_boost(ch, r"govern|law|jurisdiction|miscellaneous|general"):
                conf += 0.05
            # a carve-out (IP / except-that) before the clause lowers confidence
            if _OTHER_LAW.search(sent[: lm.start()]) and not re.search(r"govern", before, re.I):
                conf -= 0.25
            start = s + lm.end() + jm.start(1)
            out.append((canon, start, s + lm.end() + jm.end(1), conf, 0))
    # "governed by Michigan law" / "construed under New York State law"
    for m in re.finditer(r"\b(?:governed|construed|interpreted)\s+(?:by|under|in\s+accordance\s+with)\s+(?:the\s+)?(?:internal\s+|substantive\s+)?(?:(?:state\s+of|commonwealth\s+of)\s+)?(" + "|".join(re.escape(j) for j in _ALL_JURIS) + r")(?:\s+state)?\s+law\b", text, re.I):
        s, e = _sentence_bounds(text, m.start(), m.end())
        canon = _CANON[m.group(1).lower()]
        conf = 0.88 + (0.05 if _heading_boost(ch, r"govern|law|jurisdiction|miscellaneous|general") else 0)
        out.append((canon, m.start(1), m.end(1), conf, 0))
    return out


def extract_governing_law(chunks: list[ChunkText]) -> list[FieldValue]:
    cands: list[tuple[str, ChunkText, int, int, float]] = []
    for ch in chunks:
        for canon, s, e, conf, _ in _governing_law_candidates(ch):
            cands.append((canon, ch, s, e, conf))
    if not cands:
        return []
    best_by: dict[str, tuple[ChunkText, int, int, float]] = {}
    for canon, ch, s, e, conf in cands:
        if canon not in best_by or conf > best_by[canon][3]:
            best_by[canon] = (ch, s, e, conf)
    ranked = sorted(best_by.items(), key=lambda kv: -kv[1][3])
    canon, (ch, s, e, conf) = ranked[0]
    if len(ranked) > 1 and ranked[1][1][3] >= conf - 0.1:
        conf = min(conf, 0.6)  # conflicting jurisdictions: never claim one
    ss, se = _sentence_bounds(ch.text, s, e)
    return [FieldValue("governing_law", ch.text[ss:se].strip(), canon, ch.chunk_id, (ss, se), round(conf, 2))]


# --- dates ---------------------------------------------------------------

_EFF_LABEL = re.compile(
    r"(effective\s+(?:as\s+of|on|date\s*(?:of|is|:|shall\s+be)?)|dated\s+(?:as\s+of|effective)|"
    r"(?:made|entered\s+into|executed)\s+(?:and\s+entered\s+into\s+)?(?:as\s+of|on|effective)|"
    r"(?:is|are)\s+(?:made\s+)?(?:as\s+of|dated)|commenc\w+\s+on|as\s+of)\s*[:,]?\s*(?:the\s+)?$",
    re.I,
)
_EFF_AFTER = re.compile(r"^\W{0,4}\s*\(\s*(?:the\s+|this\s+)?[\"“”']?\s*(?:effective|agreement|execution|closing|commencement)\s+date", re.I)


def extract_effective_date(chunks: list[ChunkText]) -> list[FieldValue]:
    # Effective dates live in the preamble: only the first chunks count.
    for ch in sorted(chunks, key=lambda c: c.ordinal)[:3]:
        text = ch.text[:4000]
        for iso, s, e in find_dates(text):
            before = text[max(0, s - 70):s]
            after = text[e:e + 70]
            sent_s, sent_e = _sentence_bounds(ch.text, s, e)
            if _EFF_AFTER.search(after) or _EFF_LABEL.search(before):
                # only an explicit "effective" label is trusted for a deterministic answer;
                # "dated/made as of" is usually the execution date, which can differ
                labelled = bool(re.search(r"effective", before[-45:] + after[:45], re.I))
                return [_fv("effective_date", ch, max(sent_s, s - 70), min(len(ch.text), e + 40), iso, 0.9 if labelled else 0.7, ch.text[s:e])]
        # Explicit label elsewhere: "Effective Date: <date>" / "Effective Date means <date>"
        m = re.search(r"effective\s+date[^.\n]{0,60}?(?:means|is|shall\s+be|:)\s*", text, re.I)
        if m:
            for iso, s, e in find_dates(text[m.end():m.end() + 60]):
                return [_fv("effective_date", ch, m.start(), m.end() + e, iso, 0.85, text[m.end() + s:m.end() + e])]
    return []


_TERM_UNTIL = re.compile(r"(?:expires?|expir\w+|terminat\w+|continue\w*|remain\w*\s+in\s+(?:full\s+)?force)\b[^.]{0,80}?\b(?:on|until|through|ending|to)\s+", re.I)


_TERM_ANCHOR = re.compile(
    r"\b(?:the\s+)?(?:initial|primary|original|first)\s+term\b|\b(?:the\s+)?term\s+of\s+this\s+(?:agreement|contract|license)\s+(?:shall|will|is|begins?|commences?)\b|"
    r"\bthe\s+term\s+(?:shall|will)\s+(?:be|commence|continue|begin)\b|\bthis\s+(?:agreement|contract)\s+(?:shall|will)\s+(?:continue|remain|be\s+effective)\b[^.]{0,40}\b(?:for|until)\b",
    re.I,
)
_TERM_BAD_BETWEEN = re.compile(r"\b(?:cure|notice|breach|within|after\s+(?:receipt|written)|default|renew\w*|extend\w*|additional|successive)\b", re.I)


def extract_term(chunks: list[ChunkText]) -> list[FieldValue]:
    best: FieldValue | None = None
    for ch in chunks:
        text = ch.text
        for m in _TERM_ANCHOR.finditer(text):
            if re.search(r"\b(?:material|any|such|each|that|this)\s+$", text[max(0, m.start() - 12):m.start()], re.I):
                continue
            s, e = _sentence_bounds(text, m.start(), m.end())
            sent = text[s:e]
            tail = text[m.end():min(len(text), m.end() + 140)]
            norm, conf = None, 0.0
            dur = parse_duration(tail)
            if dur and not _TERM_BAD_BETWEEN.search(tail[:dur[1]]):
                norm = dur[0]
                conf = 0.85 if re.search(r"initial|primary|original|first|term\s+of\s+this", text[m.start():m.end()], re.I) else 0.7
            else:
                um = _TERM_UNTIL.search(sent)
                if um and not _TERM_BAD_BETWEEN.search(sent[:um.start()]):
                    ds = find_dates(sent[um.end():um.end() + 60])
                    if ds:
                        norm, conf = ds[0][0], 0.8
            if norm:
                fv = FieldValue("expiration_or_term", sent.strip(), norm, ch.chunk_id, (s, e), round(conf + _heading_boost(ch, r"term"), 2))
                if best is None or fv.confidence > best.confidence:
                    best = fv
    return [best] if best else []


_RENEW_LEAD = re.compile(r"(?:for|of|each|every)\s+(?:(?:successive|additional|further|consecutive|subsequent|renewal|extended|automatic|like|equal|new)\s+)*(?:(?:periods?|terms?)\s+(?:of|equal\s+to)\s+)?(?:an\s+)?(?:additional\s+)?(?:period\s+of\s+)?$", re.I)
_NOTICE_AFTER = re.compile(r"^\W*(?:\w+\W+){0,3}?(?:prior|notice|before|written|advance|in\s+advance|preceding)", re.I)


def extract_renewal(chunks: list[ChunkText]) -> list[FieldValue]:
    best: FieldValue | None = None
    for ch in chunks:
        text = ch.text
        for m in re.finditer(r"\b(?:automatically\s+renew\w*|renewal\s+terms?|successive\s+(?:periods|terms)|auto-?renew\w*|renew\w*\s+for|extended\s+for)\b", text, re.I):
            s, e = _sentence_bounds(text, m.start(), m.end())
            sent = text[s:e]
            window_end = min(len(text), m.end() + 160)
            chosen = None
            for dm in _DURATION.finditer(text, m.start(), window_end):
                lead = text[max(m.start(), dm.start() - 70):dm.start()]
                if not _RENEW_LEAD.search(lead):
                    continue
                if _NOTICE_AFTER.match(text[dm.end():dm.end() + 40]):
                    continue
                chosen = parse_duration(text[dm.start():dm.end()])
                break
            if not chosen:
                continue
            conf = 0.85
            if re.search(r"successive|additional|each\s+(?:renewal|such)|automatic|renewal\s+term", sent, re.I):
                conf = 0.9
            fv = FieldValue("renewal_term", sent.strip(), chosen[0], ch.chunk_id, (s, e), round(conf, 2))
            if best is None or fv.confidence > best.confidence:
                best = fv
    return [best] if best else []


# --- notice period ---------------------------------------------------------

_CONVENIENCE = re.compile(r"convenience|without\s+cause|for\s+any\s+reason|for\s+no\s+reason|at\s+any\s+time|without\s+reason|in\s+its\s+sole\s+discretion", re.I)
_CURE = re.compile(r"breach|default|cure|remed(?:y|ied)|insolven|bankrupt|material", re.I)


def extract_notice_period(chunks: list[ChunkText]) -> list[FieldValue]:
    best: FieldValue | None = None
    for ch in chunks:
        text = ch.text
        for m in re.finditer(r"\bterminat\w+", text, re.I):
            s, e = _sentence_bounds(text, m.start(), m.end())
            sent = text[s:e]
            if not re.search(r"\bnotice\b", sent, re.I):
                continue
            nm = re.search(rf"(?:upon|on|with|by\s+giving|by\s+providing|giving|providing|after|at\s+least|not\s+less\s+than|prior\s+to)[^.]{{0,40}}?{_NUMBER}[\s-]*(?:\(\s*\d+\s*\)\s*)?(?:calendar\s+|business\s+|working\s+)?{_UNIT}[^.]{{0,25}}?(?:prior|advance|written|notice)", sent, re.I)
            dur = parse_duration(sent[nm.start():]) if nm else None
            if not dur:
                continue
            conv = bool(_CONVENIENCE.search(sent))
            cure = bool(_CURE.search(sent))
            if conv and not cure:
                conf = 0.9
            elif conv and cure:
                conf = 0.65
            elif cure:
                continue
            else:
                conf = 0.6
            fv = FieldValue("termination_notice_period", sent.strip(), dur[0], ch.chunk_id, (s, e), round(conf, 2))
            if best is None or fv.confidence > best.confidence:
                best = fv
    return [best] if best else []


# --- liability cap -----------------------------------------------------------

_AMOUNT = re.compile(r"(?:US\s*)?\$\s?(\d[\d,]*(?:\.\d+)?)(?:\s*(million|billion|thousand))?", re.I)


def amount_to_str(m: re.Match[str]) -> str:
    base = m.group(1).rstrip(",")
    unit = (m.group(2) or "").lower()
    return f"${base}{' ' + unit if unit else ''}"


_CAP_AMOUNT = re.compile(
    r"(?:exceed|exceeding|limited\s+to|not\s+(?:be\s+)?(?:greater|more|higher)\s+than|cap(?:ped)?\s+(?:at|of)|maximum[^.$]{0,40}?(?:of|equal\s+to)|in\s+an\s+amount\s+(?:not\s+(?:to\s+)?exceed(?:ing)?|equal\s+to)|equal\s+to)[^.$]{0,25}?"
    r"(?:US\s*)?\$\s?(?P<n>\d[\d,]*(?:\.\d+)?)(?:\s*(?P<u>million|billion|thousand))?", re.I)


def extract_liability_cap(chunks: list[ChunkText]) -> list[FieldValue]:
    best: FieldValue | None = None
    for ch in chunks:
        text = ch.text
        for m in re.finditer(r"\b(?:aggregate\s+|total\s+|maximum\s+)?liabilit(?:y|ies)\b", text, re.I):
            s, e = _sentence_bounds(text, m.start(), m.end())
            sent = text[s:e]
            if re.search(r"\binsurance\b|\bindemnif|\bdamages\s+(?:resulting|arising)\s+from\s+(?:third|any\s+third)", sent, re.I) and not re.search(r"in\s+no\s+event|aggregate\s+liabilit|total\s+liabilit", sent, re.I):
                continue
            am = _CAP_AMOUNT.search(sent)
            if am and am.start() >= max(0, m.start() - s - 250):
                norm = f"${am.group('n').rstrip(',')}{' ' + am.group('u').lower() if am.group('u') else ''}"
                conf = 0.85
                if not re.search(r"in\s+no\s+event|aggregate|total|maximum|shall\s+not|limited", sent, re.I):
                    conf = 0.7
            elif re.search(r"(?:in\s+no\s+event|shall\s+not\s+exceed|limited\s+to|not\s+be\s+liable\s+for\s+more)[^.]{0,120}(?:fees|amounts?|charges|payments?)\s+(?:paid|payable)|purchase\s+price|contract\s+(?:price|value)", sent, re.I):
                conf, norm = 0.7, "amount paid/payable (derived from fees)"
            else:
                continue
            fv = FieldValue("liability_cap", sent.strip(), norm, ch.chunk_id, (s, e), round(conf + _heading_boost(ch, r"liabilit|limitation"), 2))
            if best is None or fv.confidence > best.confidence:
                best = fv
    return [best] if best else []


# --- presence clauses --------------------------------------------------------

def _presence(field: str, chunks: list[ChunkText], triggers: list[str], *, veto: str | None = None, heading: str = "", base: float = 0.85) -> list[FieldValue]:
    best: FieldValue | None = None
    for ch in chunks:
        for pat in triggers:
            for m in re.finditer(pat, ch.text, re.I):
                s, e = _sentence_bounds(ch.text, m.start(), m.end())
                sent = ch.text[s:e]
                if veto and re.search(veto, sent, re.I):
                    continue
                conf = base + (_heading_boost(ch, heading) if heading else 0.0)
                fv = FieldValue(field, sent.strip(), "present", ch.chunk_id, (s, e), round(conf, 2))
                if best is None or fv.confidence > best.confidence:
                    best = fv
    if best:
        return [best]
    return [FieldValue(field, "", "absent", None, None, 0.3)]


def extract_exclusivity(chunks):
    return _presence(
        "exclusivity", chunks,
        [r"\bexclusive(?:ly)?\s+(?:distribut\w+|supplier|dealer|reseller|agent|licensee|licensor|provider|manufactur\w+|endorse\w+|partner|source|vendor|basis|territory)\b",
         r"\bexclusive\s+(?:rights?|licen[sc]es?)\s+(?:to|for|in)\s+(?:\w+\s+){0,3}?(?:sell|distribut\w+|market|promot\w+|suppl\w+|purchas\w+|manufactur\w+|resell\w*|commerciali[sz]\w+|import|endors\w+)",
         r"\bexclusive\s+dealing\b",
         r"\b(?:shall|will|agrees?\s+to)\s+(?:deal|purchase|buy|source|distribute|sell)[^.]{0,40}\bexclusively\b",
         r"\bgrants?\b[^.]{0,80}\bexclusive\b[^.]{0,30}\b(?:right|license|distribut)"],
        veto=r"non-?\s?exclusiv|not\s+(?:be\s+)?exclusive|exclusive\s+(?:jurisdiction|venue|remedy|property|remedies|forum|liability|control|authority|ownership|benefit)|sole\s+and\s+exclusive\s+(?:remedy|liability|property)|exclusive\s+right[^.]{0,30}(?:prosecute|enforce|defend|file|prepare|change|decide|elect|settle|but\s+not)",
        heading=r"exclusiv",
    )


def extract_non_compete(chunks):
    return _presence(
        "non_compete", chunks,
        [r"\bcovenant\s+not\s+to\s+compete\b",
         r"\bnon-?competition\s+(?:covenant|agreement|obligation|provision|restriction)s?\b",
         r"\b(?:shall|will|agrees?\s+to|may)\s+not[^.]{0,100}\b(?:compete\s+with|competing\s+(?:business|products?|with)|engage\s+in[^.]{0,40}(?:competing|similar)\s+business|(?:sell|market|distribute|promote)[^.]{0,60}\bcompet\w+\s+(?:product|service))"],
        veto=r"represent|warrant|not\s+under\s+any|does\s+not\s+(?:restrict|prohibit)|waive|exclusion|forward-looking|notwithstanding\s+Subsection",
        heading=r"compet|restrictive",
    )


def extract_anti_assignment(chunks):
    return _presence(
        "anti_assignment", chunks,
        [r"\b(?:may|shall|will|can)\s+not\s+(?:\w+\s+){0,3}?(?:assign|transfer)\w*",
         r"\bno\s+(?:party|assignment)[^.]{0,60}\b(?:assign|transfer)",
         r"\bassign(?:ment|ed|s)?\b[^.]{0,120}\bwithout\b[^.]{0,50}\b(?:prior\s+)?(?:written\s+)?consent",
         r"\bnot\s+be\s+assign\w+"],
        heading=r"assign",
    )


def extract_audit_rights(chunks):
    return _presence(
        "audit_rights", chunks,
        [r"\b(?:right\s+to\s+)?audit\w*\b[^.]{0,120}\b(?:books|records|accounts|facilities|premises)",
         r"\b(?:books|records|accounts)\b[^.]{0,120}\b(?:audit\w*|inspect\w*|examin\w+)",
         r"\bright\s+to\s+audit\b"],
        heading=r"audit|records",
    )


def extract_insurance(chunks):
    out: FieldValue | None = None
    for ch in chunks:
        for m in re.finditer(r"\b(?:maintain|carry|procure|obtain|keep|provide|have|purchase)\b[^.]{0,100}?\binsurance\b|\binsurance\s+(?:coverage|policy|policies)\b[^.]{0,80}\b(?:shall|must|will)\b", ch.text, re.I):
            s, e = _sentence_bounds(ch.text, m.start(), m.end())
            sent = ch.text[s:e]
            if re.search(r"\bno\s+insurance|not\s+(?:be\s+)?required\s+to\s+(?:maintain|carry)|\bwaive|subrogat|insurance\s+(?:company|carrier)\s+of", sent, re.I):
                continue
            if not re.search(r"\b(?:shall|must|will|agrees?|required|at\s+its\s+own\s+(?:cost|expense)|at\s+all\s+times)\b", sent, re.I):
                continue
            am = _AMOUNT.search(sent)
            conf = 0.85 + (_heading_boost(ch, r"insurance") or 0)
            fv = FieldValue("insurance", sent.strip(), amount_to_str(am) if am else "present", ch.chunk_id, (s, e), round(conf, 2))
            if out is None or (fv.normalized != "present" and out.normalized == "present") or (fv.confidence > out.confidence and (fv.normalized != "present") == (out.normalized != "present")):
                out = fv
    return [out] if out else [FieldValue("insurance", "", "absent", None, None, 0.3)]


def extract_parties_field(chunks, parties: list[str]) -> list[FieldValue]:
    if not parties:
        return []
    for ch in sorted(chunks, key=lambda c: c.ordinal)[:3]:
        low = ch.text.lower()
        pos = [low.find(p.lower()) for p in parties]
        if all(p >= 0 for p in pos):
            s = min(pos)
            e = max(p + len(parties[i]) for i, p in enumerate(pos))
            return [FieldValue("parties", " and ".join(parties), " | ".join(parties), ch.chunk_id, (s, e), 0.75)]
    return []


def extract_all(chunks: list[ChunkText], parties: list[str] | None = None) -> list[FieldValue]:
    chunks = sorted(chunks, key=lambda c: c.ordinal)
    out: list[FieldValue] = []
    for fn in (extract_governing_law, extract_effective_date, extract_term, extract_renewal,
               extract_notice_period, extract_liability_cap, extract_exclusivity, extract_non_compete,
               extract_anti_assignment, extract_audit_rights, extract_insurance):
        try:
            out.extend(fn(chunks))
        except Exception:  # noqa: BLE001 -- one extractor must never break a build
            continue
    out.extend(extract_parties_field(chunks, parties or []))
    return out
