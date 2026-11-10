"""Deterministic field lookup: question intent -> stored field -> cited answer.

An answer is produced only when ALL hold: the question's intent maps to one
field, routing named exactly one document unambiguously, that document has the
field with confidence >= LOOKUP_MIN_CONFIDENCE, and the supporting chunk is
still resolvable. Anything else returns None and normal retrieval runs; the
lookup never claims that something is absent.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date

from sqlalchemy import Engine, text

from docket.core.db.models import SourceStatus, VersionStatus
from docket.services.documents.fields import LOOKUP_MIN_CONFIDENCE

# (field, kind, regex). kind: value = a value question; presence = "does X ... ?"
_INTENTS: list[tuple[str, str, re.Pattern[str]]] = [
    ("governing_law", "value", re.compile(r"\b(?:which|what)\s+(?:jurisdiction|state|country|law|laws)\b[^?]{0,40}\b(?:govern|appl)|\bgoverning\s+law\b|\bgoverned\s+by\s+(?:the\s+)?(?:laws?|which)|\bwhich\s+law\b|\bunder\s+what\s+law\b", re.I)),
    ("renewal_term", "value", re.compile(r"\brenew(?:s|al)?\s+(?:each\s+time|for\s+how)|\bfor\s+how\s+long\b[^?]{0,40}\brenew|\brenewal\s+(?:term|period)\b|\bhow\s+long\b[^?]{0,40}\brenew", re.I)),
    ("termination_notice_period", "value", re.compile(r"\bhow\s+(?:much|many\s+days?'?)\s+(?:prior\s+)?notice\b|\bnotice\s+period\b|\bnotice\s+(?:is\s+)?(?:required|needed)\s+to\s+terminate|\bhow\s+many\s+days'?\s+notice", re.I)),
    ("effective_date", "value", re.compile(r"\beffective\s+date\b|\bwhen\s+(?:did|does|was)\b[^?]{0,50}\b(?:become\s+effective|take\s+effect|commence|begin)", re.I)),
    ("expiration_or_term", "value", re.compile(r"\b(?:initial|primary)\s+term\b[^?]{0,30}\b(?:expire|end|last)|\bwhen\s+does\b[^?]{0,60}\b(?:expire|terminate|end)\b|\bterm\s+of\s+the\s+(?:agreement|contract)\b|\bhow\s+long\s+is\s+the\s+(?:initial\s+)?term", re.I)),
    ("liability_cap", "value", re.compile(r"\b(?:cap|caps|capped)\b[^?]{0,30}\bliabilit|\bliabilit\w*\s+cap\b|\b(?:maximum|limit(?:ation)?)\s+(?:of\s+)?(?:total\s+)?liabilit|\bdollar\s+amount\b[^?]{0,30}\bliabilit", re.I)),
    ("insurance", "amount", re.compile(r"\bminimum\s+(?:amount\s+of\s+)?(?:insurance|coverage)|\binsurance\s+coverage\b[^?]{0,40}\b(?:amount|minimum)|\bhow\s+much\s+insurance\b", re.I)),
    ("parties", "value", re.compile(r"\bwho\s+(?:are|is)\s+the\s+(?:parties|counterpart\w+|other\s+party)\b|\bparties\s+to\s+the\b", re.I)),
    ("exclusivity", "presence", re.compile(r"\b(?:contain|include|have|impose|grant)\w*\b[^?]{0,60}\bexclusiv", re.I)),
    ("non_compete", "presence", re.compile(r"\b(?:restrict|prohibit|prevent|bar)\w*\b[^?]{0,60}\bcompet|\bnon-?compet", re.I)),
    ("anti_assignment", "presence", re.compile(r"\b(?:restrict|prohibit|prevent|limit)\w*\b[^?]{0,60}\bassign|\banti-?assign", re.I)),
    ("audit_rights", "presence", re.compile(r"\b(?:right\s+to\s+audit|give\w*\b[^?]{0,40}\baudit|audit\s+rights?)\b", re.I)),
    ("insurance", "presence", re.compile(r"\b(?:require|must|obligat)\w*\b[^?]{0,60}\binsurance|\bmaintain\s+insurance", re.I)),
]
# quantitative follow-ups on presence fields are not answerable from "present"
_QUANT = re.compile(r"\bhow\s+(?:often|frequently|many|much)\b", re.I)


def classify_intent(question: str) -> tuple[str, str] | None:
    q = question.strip()
    for field, kind, pat in _INTENTS:
        if pat.search(q):
            if kind == "presence" and _QUANT.search(q):
                return None
            return field, kind
    return None


@dataclass(frozen=True)
class StoredField:
    field: str
    value: str
    normalized: str | None
    chunk_id: str | None
    span: tuple[int, int] | None
    confidence: float
    file_name: str | None
    document_id: int


_SQL = text(
    "SELECT f.field, f.value, f.normalized, f.evidence_chunk_id, f.char_span, f.confidence, d.file_name, d.id "
    "FROM document_fields f JOIN documents d ON d.id = f.document_id "
    "JOIN evidence_versions v ON v.id = d.evidence_version_id JOIN sources s ON s.id = v.source_id "
    "WHERE d.evidence_version_id = :vid AND f.field = :field AND s.status = :active AND v.status = :ready "
    "ORDER BY f.confidence DESC LIMIT 1"
)


def stored_field(engine: Engine, version_id: str, field: str) -> StoredField | None:
    with engine.connect() as conn:
        row = conn.execute(_SQL, {"vid": version_id, "field": field, "active": SourceStatus.ACTIVE.name, "ready": VersionStatus.READY.name}).first()
    if row is None:
        return None
    span = tuple(int(x) for x in row[4].split(":")) if row[4] else None
    return StoredField(row[0], row[1] or "", row[2], row[3], span, float(row[5]), row[6], row[7])  # type: ignore[arg-type]


def _quote(sf: StoredField, chunk_text: str) -> str:
    if sf.span and sf.span[1] <= len(chunk_text):
        q = chunk_text[sf.span[0]:sf.span[1]]
    else:
        q = sf.value
    q = " ".join(q.split()).replace("[", "(").replace("]", ")").replace('"', "'")
    return q if len(q) <= 420 else q[:417].rstrip() + "..."


def _pretty_date(iso: str) -> str:
    try:
        d = date.fromisoformat(iso)
        return f"{d.strftime('%B')} {d.day}, {d.year}"
    except ValueError:
        return iso


def compose(sf: StoredField, kind: str, chunk_text: str, label: str, question: str = "") -> str | None:
    """The answer sentence (with its citation tag), or None if this field value
    cannot be stated safely."""
    cite = f"[{label}]"
    quote = _quote(sf, chunk_text)
    n = sf.normalized or ""
    f = sf.field
    if f == "governing_law" and n:
        return f"The agreement is governed by the laws of {n} {cite}. The clause reads: \"{quote}\" {cite}"
    if f == "effective_date" and re.fullmatch(r"\d{4}-\d{2}-\d{2}", n):
        return f"The effective date is {_pretty_date(n)} {cite}."
    if f == "expiration_or_term" and n:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", n):
            return f"The agreement runs until {_pretty_date(n)} {cite}. The clause reads: \"{quote}\" {cite}"
        if re.search(r"\bwhen\b|\bexpir", question, re.I):
            return None  # a duration alone does not tell when the term ends
        return f"The term is {n} {cite}. The clause reads: \"{quote}\" {cite}"
    if f == "renewal_term" and n:
        return f"The agreement renews for {n} each time {cite}. The clause reads: \"{quote}\" {cite}"
    if f == "termination_notice_period" and n:
        return f"The notice required to terminate is {n} {cite}. The clause reads: \"{quote}\" {cite}"
    if f == "liability_cap" and n.startswith("$"):
        return f"Liability is capped at {n} {cite}. The clause reads: \"{quote}\" {cite}"
    if f == "insurance" and kind == "amount" and n.startswith("$"):
        return f"The minimum insurance coverage stated is {n} {cite}. The clause reads: \"{quote}\" {cite}"
    if f == "parties" and sf.normalized and " | " in sf.normalized:
        return f"The parties are {sf.normalized.replace(' | ', ' and ')} {cite}."
    if kind == "presence" and n == "present":
        return f"Yes. The clause reads: \"{quote}\" {cite}"
    return None


def usable(sf: StoredField | None, kind: str) -> bool:
    if sf is None or sf.chunk_id is None or sf.confidence < LOOKUP_MIN_CONFIDENCE:
        return False
    if kind == "presence" and sf.normalized != "present":
        return False  # absence is never asserted
    return True
