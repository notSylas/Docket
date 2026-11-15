"""Document-level routing: name a document in the question -> search only it.

Deterministic. Named entities are pulled from the question (quoted strings,
Title-Case / ALL-CAPS name phrases, corporate names, IDs); documents are found
through the document-level FTS5 index (`fts_documents`, rowid = documents.id)
and scored by how fully each entity's tokens appear in the document's
title/parties/file name/first words. Routing only fires when the evidence is
unambiguous (see `route`); otherwise the caller searches globally, unchanged.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlalchemy import Engine, text

from docket.core.db.models import SourceStatus, VersionStatus

_Q_STOP = {
    "what", "which", "who", "whom", "whose", "when", "where", "why", "how", "does", "do", "did", "is", "are",
    "was", "were", "under", "for", "in", "on", "at", "to", "the", "a", "an", "of", "and", "or", "if", "can",
    "could", "should", "would", "will", "quote", "name", "list", "please", "explain", "summarize", "summarise",
    "tell", "give", "find", "show", "describe", "according", "per", "between", "within", "across", "each",
    "any", "all", "both", "this", "that", "these", "those", "my", "our", "i", "we", "it", "its", "their",
}
_GENERIC = {
    "agreement", "contract", "lease", "license", "licence", "addendum", "amendment", "memo", "memorandum",
    "report", "invoice", "document", "file", "library", "party", "parties", "clause", "section", "article",
    "exhibit", "schedule", "term", "terms", "conditions", "company", "corporation", "inc", "llc", "ltd", "corp",
    "co", "plc", "gmbh", "sa", "s.a", "lp", "llp", "master", "services", "service", "general",
}
_SUFFIX = {"inc", "inc.", "llc", "l.l.c.", "ltd", "ltd.", "corp", "corp.", "corporation", "co", "co.", "plc", "gmbh", "s.a.", "s.a", "lp", "llp", "company", "limited", "incorporated"}
_DOC_WORDS = {"agreement", "contract", "lease", "license", "licence", "addendum", "amendment", "memo", "memorandum", "report", "invoice", "policy", "proposal", "statement"}
_NOT_ENTITY = {
    # jurisdictions and calendar words are never document names
    "january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
    "november", "december", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "california", "delaware", "florida", "maryland", "nevada", "new york", "texas", "england", "wales",
    "usd", "us", "u.s.", "united states", "english",
}
_LIST_CUES = re.compile(
    r"\b(?:which\s+(?:of\s+)?(?:the\s+)?(?:contracts?|agreements?|documents?|files?|memos?|reports?)|"
    r"(?:all|every|each|any)\s+(?:of\s+the\s+|the\s+)?(?:contracts?|agreements?|documents?|files?)|"
    r"in\s+(?:this|my|the)\s+library|across\s+(?:all|the|my)|compare|list\s+(?:all|the)|how\s+many\s+(?:contracts?|documents?|agreements?))\b",
    re.I,
)

_WORD = r"[A-Z][\w'’&.\-]*"
_CAP_PHRASE = re.compile(
    rf"(?:{_WORD})(?:(?:\s+|,\s+(?=(?:Inc|LLC|L\.L\.C|Ltd|Corp|Co|S\.A|N\.V|B\.V|LP|LLP|PLC|GmbH)\b))(?:{_WORD})|\s+(?:of|and|&|the|de|for)\s+(?={_WORD}))*"
)
_QUOTED = re.compile(r"[\"“”‘']([^\"“”‘’']{3,80})[\"“”’']")
_ID = re.compile(r"\b[A-Z]{2,}-\d{2,}\b")


@dataclass(frozen=True)
class Entity:
    text: str
    kind: str  # party | title | quoted | id
    tokens: tuple[str, ...]
    weight: float


def _tok(s: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", s.lower())]


def _stem(t: str) -> str:
    return t[:-1] if len(t) > 3 and t.endswith("s") and not t.endswith("ss") else t


def _content_tokens(s: str) -> tuple[str, ...]:
    return tuple(_stem(t) for t in _tok(s) if t not in _GENERIC and t not in _Q_STOP and len(t) > 1)


def extract_entities(question: str) -> list[Entity]:
    ents: list[Entity] = []
    seen: set[tuple[str, ...]] = set()

    def add(txt: str, kind: str, weight: float) -> None:
        toks = _content_tokens(txt)
        if not toks or toks in seen:
            return
        seen.add(toks)
        ents.append(Entity(txt.strip(), kind, toks, weight))

    for m in _QUOTED.finditer(question):
        add(m.group(1), "quoted", 1.0)
    for m in _ID.finditer(question):
        add(m.group(0), "id", 1.0)
    for m in _CAP_PHRASE.finditer(question):
        words = m.group(0).split()
        # drop leading question/function words ("Under", "The", "What")
        while words and words[0].lower().strip(",.") in _Q_STOP:
            words.pop(0)
        while words and words[-1].lower().strip(",") in _Q_STOP:
            words.pop()
        if not words:
            continue
        phrase = " ".join(words)
        low = phrase.lower().rstrip(".,")
        if low in _NOT_ENTITY or all(w.lower().strip(".,'’s") in _NOT_ENTITY or w.lower() in _Q_STOP for w in words):
            continue
        last = words[-1].lower().strip(".,").rstrip("s") if words else ""
        has_suffix = any(w.lower().strip(".,") in _SUFFIX for w in words)
        ends_doc = words[-1].lower().strip(".,") in _DOC_WORDS
        if ends_doc:
            # "Distributorship Agreement": a title. Generic ones ("Master Agreement") get low weight
            toks = _content_tokens(phrase)
            if toks:
                add(phrase, "title", 0.6)
        elif has_suffix or len(words) >= 2:
            add(phrase, "party", 1.0)
        elif len(words[0]) >= 4 and low not in _NOT_ENTITY:
            add(phrase, "party", 1.0)
    return ents


_JOIN_CUES = re.compile(
    r"\b(?:and\s+(?:the|its|their)\s+\w+|budget|approval|approved|amend\w*|compar\w+|versus|vs\.?|both|together\s+with|"
    r"cross-?ref\w*|related\s+to|according\s+to\s+the\s+\w+\s+and|memo|between\s+the\s+\w+\s+and\s+the|difference)\b",
    re.I,
)


def is_list_question(question: str) -> bool:
    return bool(_LIST_CUES.search(question) or _JOIN_CUES.search(question))


@dataclass
class Candidate:
    document_id: int
    version_id: str
    source_id: str
    file_name: str | None
    title: str | None
    score: float
    bm25: float


@dataclass
class RouteDecision:
    routed: bool
    version_ids: list[str] = field(default_factory=list)
    top_version_id: str | None = None
    top_unique: bool = False
    top_score: float = 0.0
    reason: str = ""
    entities: list[str] = field(default_factory=list)
    candidates: list[Candidate] = field(default_factory=list)


_FTS_SQL = text(
    "SELECT d.id, d.evidence_version_id, d.source_id, d.file_name, d.title, "
    "d.doc_text, d.parties_json, bm25(fts_documents) AS rank "
    "FROM fts_documents JOIN documents d ON d.id = fts_documents.rowid "
    "JOIN evidence_versions v ON v.id = d.evidence_version_id "
    "JOIN sources s ON s.id = v.source_id "
    "WHERE fts_documents MATCH :q AND s.status = :active AND v.status = :ready "
    "ORDER BY rank LIMIT :limit"
)


def _fts_query(entities: list[Entity]) -> str:
    parts = []
    for e in entities:
        toks = [t for t in e.tokens if re.fullmatch(r"[a-z0-9]+", t)]
        if toks:
            parts.append("(" + " AND ".join(f'"{t}"' for t in toks) + ")")
    return " OR ".join(parts)


def _coverage(ent: Entity, doc_tokens: set[str]) -> float:
    return sum(1 for t in ent.tokens if t in doc_tokens) / len(ent.tokens)


# Tuned on the CUAD gold (12 contracts, 34 questions): see tests/unit/test_document_routing.py.
MIN_TOP_SCORE = 0.9
TIE_BAND = 0.05
FILL_MIN = 0.4


def route(engine: Engine, question: str, top_n: int = 8, *, scope_version_ids: list[str] | None = None) -> RouteDecision:
    if is_list_question(question):
        return RouteDecision(False, reason="list question")
    ents = extract_entities(question)
    if not ents:
        return RouteDecision(False, reason="no named entity")
    if any(e.kind == "id" for e in ents):
        return RouteDecision(False, reason="identifier points elsewhere", entities=[e.text for e in ents])
    if not any(e.kind in ("party", "quoted") for e in ents):
        return RouteDecision(False, reason="only generic titles named", entities=[e.text for e in ents])
    fq = _fts_query(ents)
    if not fq:
        return RouteDecision(False, reason="no searchable entity")
    with engine.connect() as conn:
        rows = conn.execute(
            _FTS_SQL,
            {"q": fq, "active": SourceStatus.ACTIVE.name, "ready": VersionStatus.READY.name, "limit": 60},
        ).all()
    scope = set(scope_version_ids) if scope_version_ids is not None else None
    total_w = sum(e.weight for e in ents)
    cands: list[Candidate] = []
    for did, vid, sid, fname, title, doc_text, parties, rank in rows:
        if scope is not None and vid not in scope:
            continue
        toks = {_stem(t) for t in _tok(f"{doc_text} {fname or ''} {title or ''} {parties or ''}")}
        score = sum(e.weight * _coverage(e, toks) for e in ents) / total_w
        cands.append(Candidate(did, vid, sid, fname, title, score, rank))
    names = [e.text for e in ents]
    if not cands:
        return RouteDecision(False, reason="no document matched", entities=names)
    cands.sort(key=lambda c: (-c.score, c.bm25))
    top = cands[0]
    if top.score < MIN_TOP_SCORE:
        return RouteDecision(False, reason=f"top score {top.score:.2f} below threshold", entities=names, candidates=cands[:top_n])
    strong = [c for c in cands if c.score >= top.score - TIE_BAND]
    if len(strong) > top_n:
        return RouteDecision(False, reason=f"{len(strong)} documents tie; too ambiguous", entities=names, candidates=cands[:top_n])
    chosen = list(strong)
    for c in cands:
        if len(chosen) >= top_n:
            break
        if c not in chosen and c.score >= FILL_MIN:
            chosen.append(c)
    unique = len(strong) == 1
    return RouteDecision(
        True, [c.version_id for c in chosen], top.version_id, unique, top.score,
        "routed", names, chosen,
    )
