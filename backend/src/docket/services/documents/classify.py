"""Deterministic document metadata: type, title, parties, dates, family key.

Pure functions over text already stored as chunks (no re-parse, no model).
Everything here is conservative: when unsure the answer is "other"/None, never
a guess presented as fact.
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import PurePath

DOC_TYPES = (
    "contract", "agreement", "amendment", "memo", "invoice", "report",
    "spreadsheet", "email", "other",
)

_SPREADSHEET_EXT = {".xlsx", ".xls", ".xlsm", ".csv", ".tsv", ".ods"}
_EMAIL_EXT = {".eml", ".msg", ".mbox", ".emlx"}

_MONTHS = {
    m: i + 1
    for i, m in enumerate(
        ["january", "february", "march", "april", "may", "june", "july",
         "august", "september", "october", "november", "december"]
    )
}
_MONTHS.update({k[:3]: v for k, v in list(_MONTHS.items())})
_MONTHS["sept"] = 9
_MONTH_RE = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"

# --- dates ---------------------------------------------------------------

_DATE_PATTERNS = [
    # March 21, 2018 / Mar. 21 2018 / March 21st, 2018
    re.compile(rf"\b({_MONTH_RE})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\s*,?\s+(\d{{4}})\b", re.I),
    # 21 March 2018 / 21st day of March, 2018 / the 21st of March 2018
    re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:day\s+)?(?:of\s+)?({_MONTH_RE})\.?\s*,?\s+(\d{{4}})\b", re.I),
    # 2018-03-21
    re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b"),
    # 03/21/2018 (US month-first when unambiguous, else month-first)
    re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b"),
]


def _mk(y: int, m: int, d: int) -> str | None:
    try:
        return date(y, m, d).isoformat()
    except ValueError:
        return None


def find_dates(text: str) -> list[tuple[str, int, int]]:
    """All parseable calendar dates as (iso, start, end), in text order."""
    found: dict[int, tuple[str, int, int]] = {}
    for idx, pat in enumerate(_DATE_PATTERNS):
        for m in pat.finditer(text):
            g = m.groups()
            iso = None
            if idx == 0:
                iso = _mk(int(g[2]), _MONTHS[g[0].lower()[:3]], int(g[1]))
            elif idx == 1:
                iso = _mk(int(g[2]), _MONTHS[g[1].lower()[:3]], int(g[0]))
            elif idx == 2:
                iso = _mk(int(g[0]), int(g[1]), int(g[2]))
            else:
                iso = _mk(int(g[2]), int(g[0]), int(g[1]))
            if iso and m.start() not in found:
                found[m.start()] = (iso, m.start(), m.end())
    # drop dates nested inside an earlier, longer match
    out: list[tuple[str, int, int]] = []
    for item in sorted(found.values(), key=lambda t: (t[1], -(t[2] - t[1]))):
        if out and item[1] < out[-1][2]:
            continue
        out.append(item)
    return out


_EFFECTIVE_CUE = re.compile(
    r"(?:effective\s+(?:as\s+of|on|date)|dated\s+(?:as\s+of|effective)?|entered\s+into\s+(?:as\s+of|on|effective)?|"
    r"made\s+(?:and\s+entered\s+into\s+)?(?:as\s+of|on|effective)?|commenc\w+\s+on|as\s+of|"
    r"\(\s*(?:the\s+)?[\"“”']?\s*effective\s+date)",
    re.I,
)


def preamble_dates(preamble: str) -> dict[str, str]:
    """Effective/execution dates from the first lines of a contract. A date
    counts as `effective` only when an effective-style cue sits just before it
    (or the date is followed by '(the "Effective Date")')."""
    out: dict[str, str] = {}
    for iso, s, e in find_dates(preamble):
        before = preamble[max(0, s - 60):s]
        after = preamble[e:e + 60]
        if re.search(r"\(\s*(?:the\s+)?[\"“”']?\s*effective\s+date", after, re.I) or re.search(
            r"effective\s+(?:as\s+of|on|date)[^.]{0,20}$|(?:dated|made|entered\s+into|agreement\s+is)[^.]{0,40}(?:as\s+of|on|dated)?\s*$|as\s+of\s*$",
            before, re.I,
        ):
            out.setdefault("effective", iso)
        else:
            out.setdefault("other", iso)
    return out


# --- parties -------------------------------------------------------------

_SUFFIX = r"(?:inc|incorporated|llc|l\.l\.c|ltd|limited|corp|corporation|co|company|plc|gmbh|s\.a|n\.v|b\.v|lp|llp|ag)"
_CUT_AFTER = re.compile(
    r"\s*(?:\(|\[|,?\s+(?:a|an|the)\s+(?:[a-z]+\s+){0,3}(?:corporation|company|limited|partnership|llc|association|bank|trust|entity|organization|corp|inc)\b|"
    r",?\s+(?:having|with\s+its|whose|located|organized|incorporated|existing|duly|a\s+resident|of\s+\d)|,\s+\d|;|\bwhereas\b)",
    re.I,
)
_LEAD_STRIP = re.compile(r"^(?:the\s+(?:undersigned\s+)?|and\s+|by\s+and\s+between\s+|between\s+|among\s+)", re.I)


def _clean_party(raw: str) -> str | None:
    raw = raw.strip().strip("\"“”'‘’,;:.- ").replace("\n", " ")
    raw = re.sub(r"\s+", " ", raw)
    m = _CUT_AFTER.search(raw)
    if m:
        raw = raw[: m.start()]
    raw = _LEAD_STRIP.sub("", raw).strip().strip("\"“”'‘’,;:.- ")
    # keep a trailing corporate suffix's period
    if re.search(rf"\b{_SUFFIX}$", raw, re.I) and not raw.endswith("."):
        pass
    if not (2 <= len(raw) <= 90) or len(raw.split()) > 12:
        return None
    if not re.search(r"[A-Za-z]", raw) or raw.lower() in {"company", "party", "parties", "the parties"}:
        return None
    if re.fullmatch(r"(?:this|such|each|either|any)\b.*", raw, re.I):
        return None
    return raw


_BETWEEN = re.compile(
    r"\b(?:by\s+and\s+(?:between|among)|between|among)\s+(?P<a>.{3,260}?)\s+(?:and|&)\s+(?P<b>.{3,260}?)(?=\s*(?:\(|,?\s*whereas\b|,?\s+(?:a|an)\s+[A-Za-z]+\s+(?:corporation|company|limited|partnership|llc)|;|\.\s|$))",
    re.I | re.S,
)
_BY_AND = re.compile(
    r"\b(?:is|are)\s+(?:made\s+and\s+)?entered\s+into\s+(?:as\s+of\s+[^,]{3,30},?\s+|on\s+[^,]{3,30},?\s+)?by\s+(?P<a>.{3,200}?)\s+(?:and|&)\s+(?P<b>.{3,200}?)(?=\s*(?:\(|,|;|\.\s|$))",
    re.I | re.S,
)
_PARTY_ALIAS = re.compile(r"\bParty\s+([A-D])\b\s*(?:[:\-–]|\(|is|shall)\s*(?P<n>[A-Z][^\n(,;]{2,70})")


def extract_parties(preamble: str) -> list[str]:
    text = re.sub(r"\s+", " ", preamble)[:3000]
    names: list[str] = []
    for pat in (_BETWEEN, _BY_AND):
        m = pat.search(text)
        if m:
            for g in ("a", "b"):
                n = _clean_party(m.group(g))
                if n and n.lower() not in {x.lower() for x in names}:
                    names.append(n)
            if len(names) >= 2:
                return names[:6]
    for m in _PARTY_ALIAS.finditer(text):
        n = _clean_party(m.group("n"))
        if n and n.lower() not in {x.lower() for x in names}:
            names.append(n)
    return names[:6]


# --- type / title --------------------------------------------------------

_AGREEMENT_WORDS = re.compile(
    r"\b(agreement|contract|license|lease|covenant|indenture|charter|nda|non-?disclosure|"
    r"purchase\s+order|master\s+services?|statement\s+of\s+work)\b", re.I)


def _first_lines(text: str, n: int = 12) -> list[str]:
    lines = [ln.strip(" #*|\t") for ln in text.splitlines()]
    return [ln for ln in lines if ln][:n]


def guess_title(filename: str, first_text: str, first_heading: str | None) -> str:
    stem = PurePath(filename).stem if filename else ""
    for ln in _first_lines(first_text, 8):
        if re.fullmatch(r"(?:page\s+)?\d+(?:\s+of\s+\d+)?", ln, re.I) or len(ln) < 4 or len(ln) > 140:
            continue
        if re.match(r"^(?:exhibit|schedule|annex)\s+[\w.-]+\s*$", ln, re.I):
            continue
        letters = [c for c in ln if c.isalpha()]
        mostly_upper = letters and sum(c.isupper() for c in letters) / len(letters) > 0.7
        titled = len(ln.split()) <= 14 and all(w[:1].isupper() or not w[:1].isalpha() or w.lower() in
                                               {"of", "and", "the", "for", "to", "in", "a", "an", "on", "by", "with"}
                                               for w in ln.split())
        if (mostly_upper or titled) and not ln.endswith((".", ",", ";")) and (
            _AGREEMENT_WORDS.search(ln) or re.search(r"\b(memorand\w+|invoice|report|minutes|proposal|policy|amendment|summary)\b", ln, re.I)
        ):
            return ln[:140]
    if first_heading and len(first_heading) <= 140:
        return first_heading
    return stem.replace("_", " ").strip() or "Untitled"


def classify_doc_type(filename: str, title: str, first_text: str) -> str:
    ext = PurePath(filename).suffix.lower() if filename else ""
    if ext in _SPREADSHEET_EXT:
        return "spreadsheet"
    if ext in _EMAIL_EXT:
        return "email"
    head = first_text[:1800]
    name = PurePath(filename).stem if filename else ""
    t = f"{title} {name}"
    if re.search(r"\b(?:first|second|third|fourth|fifth|\d+(?:st|nd|rd|th)?)?\s*amendment\b|\bamended\s+and\s+restated\b|\baddendum\b", t, re.I) or re.match(
        r"\s*(?:first|second|third|fourth|fifth|sixth|no\.?\s*\d+)?\s*amendment\b", head, re.I
    ):
        return "amendment"
    if re.search(r"\binvoice\b", t, re.I) or (re.search(r"\binvoice\b", head, re.I) and re.search(r"\b(?:amount\s+due|bill\s+to|total\s+due|invoice\s+(?:no|number|#))", head, re.I)):
        return "invoice"
    if re.search(r"\bmemo(?:randum)?\b", t, re.I) or re.search(r"^\s*(?:memo(?:randum)?\b|to:\s.+\n\s*from:)", head, re.I | re.M):
        return "memo"
    if re.search(r"\bcontract\b", title, re.I) and not re.search(r"\bagreement\b", title, re.I):
        return "contract"
    if _AGREEMENT_WORDS.search(title) or _AGREEMENT_WORDS.search(name):
        return "agreement"
    if re.search(r"\bby\s+and\s+between\b|\bwhereas\b|\bhereinafter\b|\bthis\s+agreement\b", head, re.I):
        return "agreement"
    if re.search(r"\b(report|annual|quarterly|summary|findings|analysis|review|minutes)\b", t, re.I):
        return "report"
    return "other"


# --- family --------------------------------------------------------------

_VERSION_TOKENS = [
    re.compile(r"\bv(?:er(?:sion)?)?\s*\d+(?:[._]\d+)*\b", re.I),
    re.compile(r"\brev(?:ision)?\s*\d+\b", re.I),
    re.compile(r"\b(?:copy(?:\s+of)?|final|signed|executed|draft|redline|clean|countersigned|fully\s+executed|scan(?:ned)?)\b", re.I),
    re.compile(r"\(\s*\d+\s*\)"),
    re.compile(r"\b\d{4}[-_. ]\d{1,2}[-_. ]\d{1,2}\b"),
    re.compile(r"\b\d{1,2}[-_. ]\d{1,2}[-_. ]\d{2,4}\b"),
    re.compile(r"\b(?:19|20)\d{6}\b"),
    re.compile(rf"\b{_MONTH_RE}[-_. ]+(?:\d{{1,2}}[-_. ]+)?(?:19|20)?\d{{2,4}}\b", re.I),
    re.compile(r"\b(?:19|20)\d{2}\b"),
]
_FAMILY_GROUP = {"contract": "contract", "agreement": "contract", "amendment": "contract"}


def normalise_stem(filename: str) -> str:
    stem = PurePath(filename).stem.lower() if filename else ""
    stem = re.sub(r"[_\-.]+", " ", stem)
    for pat in _VERSION_TOKENS:
        stem = pat.sub(" ", stem)
    stem = re.sub(r"[^a-z0-9 ]+", " ", stem)
    stem = re.sub(r"\s+\d+$", "", stem.strip())
    return re.sub(r"\s+", " ", stem).strip()


def family_key(filename: str, doc_type: str, parent: str | None = None) -> str | None:
    stem = normalise_stem(filename)
    if len(stem) < 3:
        return None
    group = _FAMILY_GROUP.get(doc_type, doc_type)
    folder = re.sub(r"[^a-z0-9]+", " ", (parent or "").lower()).strip()
    return f"{group}|{folder}|{stem}"


# --- doc_text ------------------------------------------------------------

def build_doc_text(title: str, headings: list[str], first_text: str, words: int = 300) -> str:
    seen: set[str] = set()
    heads: list[str] = []
    for h in headings:
        h = re.sub(r"\s+", " ", (h or "").strip())
        if h and h.lower() not in seen and h != title:
            seen.add(h.lower())
            heads.append(h)
        if len(heads) >= 12:
            break
    body = " ".join(first_text.split()[:words])
    return "\n".join(p for p in [title, "; ".join(heads), body] if p)
