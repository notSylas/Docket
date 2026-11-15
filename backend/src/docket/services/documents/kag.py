"""KAG-lite: a small deterministic entity graph over the document layer.

Build side (ingest, flag DOCKET_KAG_SCOPING=on): parties and corporate names
read from a document's preamble become shared `kag_entities` (with normalised
variants: case, punctuation and Inc/Corp/LLC/Ltd stripped); every chunk is
tagged with the clause types it mentions (`kag_document_clauses`). Every edge
keeps its source chunk id, so answers still cite real text.

Query side: `plan(engine, question)` finds entities named in the question by
exact variant match, maps them to documents, and detects the clause type asked
about. It never raises and never decides alone: the caller fuses the scoped
candidates with the global pool (soft scoping); with no confident match the
plan is empty and behaviour is exactly the unflagged path.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlalchemy import Engine, text

from docket.core.db.models import SourceStatus, VersionStatus

# --- name normalisation --------------------------------------------------

_SUFFIX_WORDS = {
    "inc", "incorporated", "corp", "corporation", "co", "company", "llc", "ltd", "limited", "plc", "lp",
    "llp", "sa", "nv", "bv", "gmbh", "ag", "lc", "pllc", "pty", "holdings", "holding",
}
_STOP = {
    "the", "a", "an", "of", "and", "or", "for", "to", "in", "on", "by", "with", "under", "what", "which", "who",
    "when", "where", "how", "does", "do", "is", "are", "was", "were", "this", "that", "any", "all", "each",
    "company", "party", "parties", "agreement", "contract", "distributor", "licensee", "licensor", "customer",
    "supplier", "seller", "buyer", "client", "vendor", "manufacturer", "agent", "lessee", "lessor", "employee",
    "employer", "contractor", "consultant", "borrower", "lender", "purchaser", "provider", "partner", "us", "u",
    "s", "state", "states", "united", "new", "master", "services", "service", "license", "general", "amendment",
}


def _clean(s: str) -> list[str]:
    s = s.lower().replace("&", " and ")
    s = re.sub(r"[’'`]", "", s)  # Reed's -> reeds
    return re.findall(r"[a-z0-9]+", s)


def normalize_name(s: str) -> str:
    """Case/punctuation-folded full name (suffixes kept): 'Reed's, Inc.' -> 'reeds inc'."""
    toks = _clean(s)
    if toks and toks[0] == "the":
        toks = toks[1:]
    return " ".join(toks)


def core_name(s: str) -> str:
    """Normalised name without trailing corporate suffixes: 'Skype Communications, S.A.' -> 'skype communications'."""
    toks = _clean(s)
    if toks and toks[0] == "the":
        toks = toks[1:]
    # 'S.A.' cleans to ['s', 'a']; fold the dotted forms before stripping
    joined = " ".join(toks)
    joined = re.sub(r"\bs a\b$", "sa", joined)
    joined = re.sub(r"\bl l c\b$", "llc", joined)
    joined = re.sub(r"\bn v\b$", "nv", joined)
    joined = re.sub(r"\bb v\b$", "bv", joined)
    toks = joined.split()
    while toks and toks[-1] in _SUFFIX_WORDS:
        toks.pop()
    return " ".join(toks)


def usable_variant(v: str) -> bool:
    toks = v.split()
    if not toks or all(t in _STOP or t in _SUFFIX_WORDS for t in toks):
        return False
    if len(toks) == 1 and (len(v) < 4 or v in _STOP):
        return False
    return len(v) >= 3


def name_variants(display: str) -> set[str]:
    """All lookup keys for one name. Empty when the name is too generic to match on."""
    out = {normalize_name(display), core_name(display)}
    return {v for v in out if usable_variant(v)}


_CORP_IN_TEXT = re.compile(
    r"\b((?:[A-Z][\w'’&.\-]*)(?:\s+(?:[A-Z][\w'’&.\-]*|of|and|&|de|the))*?,?\s+"
    r"(?i:Inc\.?|Incorporated|Corp\.?|Corporation|LLC|L\.L\.C\.|Ltd\.?|Limited|Co\.|Company|PLC|GmbH|S\.A\.|N\.V\.|B\.V\.|LP|L\.P\.|LLP))(?![\w])"
)


def plausible_name(display: str) -> bool:
    """A company/party name, not a sentence fragment: short, mostly capitalised words, no field labels."""
    words = display.split()
    if not (1 <= len(words) <= 8) or re.search(r"[:;\[\]]|\d{3,}", display):
        return False
    alpha = [w for w in words if re.search(r"[A-Za-z]", w)]
    if not alpha:
        return False
    # a sentence end inside the name ("... Property. Company") means two phrases were glued together
    if any(w.endswith(".") and len(w) > 4 and w[:-1].lower() not in _SUFFIX_WORDS for w in words[:-1]):
        return False
    caps = sum(1 for w in alpha if w[:1].isupper() or w.lower() in ("of", "and", "&", "de", "the", "for"))
    return caps / len(alpha) >= 0.8


def filename_entity(file_name: str | None) -> str | None:
    """Company-like leading segment of a file name ('ReedsInc_2019_..pdf' -> 'Reeds Inc'); None when it is a
    generic word or does not look like a name."""
    if not file_name:
        return None
    stem = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", file_name)
    seg = re.split(r"[_\-]\s*\d|_|\s+-\s+|\s\(", stem)[0].strip()
    seg = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", seg)  # camel case -> words
    seg = re.sub(r"\s+", " ", seg).strip(" ,.-")
    if not seg or len(seg) < 4 or len(seg.split()) > 5 or re.search(r"\d", seg):
        return None
    key = core_name(seg)
    return seg if usable_variant(key) else None


def preamble_entities(parties: list[str], preamble: str, file_name: str | None = None) -> list[tuple[str, str]]:
    """[(display, role)] : parties first, then other corporate names in the preamble, then the file-name company."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for p in parties:
        k = core_name(p)
        if k and k not in seen and plausible_name(p) and usable_variant(k):
            seen.add(k)
            out.append((p, "party"))
    for m in _CORP_IN_TEXT.finditer(re.sub(r"\s+", " ", preamble)[:3000]):
        d = m.group(1).strip(" ,")
        words = d.split()
        while words and words[0].lower() in _STOP:
            words.pop(0)
        d = " ".join(words)
        k = core_name(d)
        if k and usable_variant(k) and k not in seen and plausible_name(d):
            seen.add(k)
            out.append((d, "mention"))
    fe = filename_entity(file_name)
    if fe:
        k = core_name(fe)
        if k not in seen:
            out.append((fe, "filename"))
    return out


# --- clause types --------------------------------------------------------

@dataclass(frozen=True)
class ClauseType:
    name: str
    label: str
    chunk_re: re.Pattern[str]
    question_re: re.Pattern[str]
    hint: str  # extra terms for the scoped search
    can_be_absent: bool  # True: no chunk with these keywords => the contract has no such clause


def _c(p: str) -> re.Pattern[str]:
    return re.compile(p, re.I)


# Order matters for question matching: the first matching type wins.
CLAUSE_TYPES: tuple[ClauseType, ...] = (
    ClauseType("insurance", "insurance", _c(r"\binsur(?:ance|ed|er|ers|e)\b"),
               _c(r"\binsur"), "insurance coverage maintain policy insured", True),
    ClauseType("audit_rights", "audit rights", _c(r"\baudit|\binspect(?:ion)?\s+(?:of\s+)?(?:the\s+)?(?:books|records)"),
               _c(r"\baudit"), "audit books records inspect examine", True),
    ClauseType("exclusivity", "exclusivity", _c(r"exclusiv"),
               _c(r"exclusiv"), "exclusive exclusivity sole", True),
    ClauseType("non_compete", "non-compete", _c(r"\bcompet(?:e|es|ing|ition|itor|itors|itive)\b"),
               _c(r"\bcompet|non-?compet"), "compete competing competitor non-compete restrict", True),
    ClauseType("anti_assignment", "assignment restriction", _c(r"\bassign(?:s|ed|ing|ment|ments|able)?\b"),
               _c(r"\bassign"), "assign assignment consent transfer", True),
    ClauseType("liability_cap", "liability cap", _c(r"limitation\s+of\s+liabilit|liabilit\w*[^.]{0,200}(?:exceed|cap|limit|aggregate|maximum)|(?:exceed|cap|limit|aggregate|maximum)[^.]{0,200}liabilit|in\s+no\s+event"),
               _c(r"(?:cap|caps|capped|limit\w*)\b[^?]{0,40}liabilit|liabilit\w*\s+cap|maximum\s+liabilit"), "limitation of liability aggregate exceed cap", True),
    ClauseType("termination_for_convenience", "termination for convenience", _c(r"\bconvenience\b|without\s+(?:cause|reason)|any\s+reason|no\s+reason|at\s+will"),
               _c(r"terminat\w*[^?]{0,80}convenience|convenience[^?]{0,80}terminat|notice[^?]{0,40}(?:is\s+)?(?:required|needed)[^?]{0,20}terminat|how\s+(?:much|many)[^?]{0,30}notice[^?]{0,40}terminat"),
               "terminate termination convenience without cause written notice days", True),
    ClauseType("license_grant", "license grant", _c(r"\bgrants?\b|licen[sc]e"),
               _c(r"licen[sc]e\s+grant|grants?\b[^?]{0,40}licen|scope\s+of\s+(?:the\s+)?licen"), "grant license licensed rights scope", True),
    ClauseType("termination", "termination", _c(r"\bterminat"),
               _c(r"\bterminat"), "terminate termination notice", True),
    ClauseType("renewal", "renewal", _c(r"\brenew|\bextend|\bextension|successive"),
               _c(r"\brenew|auto-?renew"), "renew renewal extended successive period", True),
    ClauseType("governing_law", "governing law", _c(r"governing\s+law|governed\s+(?:by|in)|laws?\s+of\s+the\s+(?:state|commonwealth|republic)|construed\s+in\s+accordance"),
               _c(r"governing\s+law|\bgoverned\b|\bjurisdiction\b[^?]{0,30}\b(?:law|govern)|which\s+law|what\s+law|\bgoverns\b|law\s+govern"), "governed by laws of the state governing law", False),
    ClauseType("effective_date", "effective date", _c(r"effective\s+date|entered\s+into\s+as\s+of|dated\s+as\s+of|made\s+(?:and\s+entered\s+into\s+)?as\s+of"),
               _c(r"effective\s+date|become\s+effective|take\s+effect|enter(?:ed)?\s+into\s+effect"), "effective date entered into as of dated", False),
    ClauseType("expiration", "term and expiration", _c(r"\bexpir|\binitial\s+term\b|\bterm\s+of\s+this\s+agreement"),
               _c(r"\bexpir|initial\s+term|term\s+of\s+the\s+(?:agreement|contract)|how\s+long\s+(?:is|does)"), "term expire expiration initial term years", False),
    ClauseType("parties", "parties", _c(r"\bbetween\b|\bby\s+and\s+between\b|\bparties\b"),
               _c(r"counterpart|who\s+(?:are|is)\s+the\s+(?:other\s+)?part|parties\s+to"), "between parties agreement entered into", False),
)
_BY_NAME = {c.name: c for c in CLAUSE_TYPES}


def clause_type_by_name(name: str) -> ClauseType | None:
    return _BY_NAME.get(name)


def detect_clause_type(question: str) -> ClauseType | None:
    for c in CLAUSE_TYPES:
        if c.question_re.search(question):
            return c
    return None


def classify_chunk(chunk_text: str, heading: str | None = None) -> list[tuple[str, float]]:
    """[(clause_type, confidence)] for one chunk. Heading hits are stronger."""
    out: list[tuple[str, float]] = []
    for c in CLAUSE_TYPES:
        if c.name == "parties":
            continue  # the preamble is found through the parties edge, not by keyword
        if heading and c.chunk_re.search(heading):
            out.append((c.name, 0.9))
        elif c.chunk_re.search(chunk_text):
            out.append((c.name, 0.6))
    return out


# --- build ---------------------------------------------------------------

def write_graph(session, document_id: int, chunks, parties: list[str], preamble: str, file_name: str | None = None) -> tuple[int, int]:
    """Replace this document's graph rows (caller commits). `chunks`: objects with id/text/heading/ordinal.
    Returns (entity_edges, clause_links)."""
    from sqlalchemy import delete, select

    from docket.core.db.models import KagDocumentClause, KagDocumentEntity, KagEntity, KagEntityVariant

    session.execute(delete(KagDocumentEntity).where(KagDocumentEntity.document_id == document_id))
    session.execute(delete(KagDocumentClause).where(KagDocumentClause.document_id == document_id))
    first_chunk = chunks[0].id if chunks else None
    edges = 0
    for display, role in preamble_entities(parties, preamble, file_name):
        key = core_name(display)
        if not usable_variant(key):
            continue
        ent = session.execute(select(KagEntity).where(KagEntity.norm == key)).scalar_one_or_none()
        if ent is None:
            ent = KagEntity(norm=key, display=display, kind="party")
            session.add(ent)
            session.flush()
        for v in name_variants(display) | {key}:
            if usable_variant(v) and session.get(KagEntityVariant, (ent.id, v)) is None:
                session.add(KagEntityVariant(entity_id=ent.id, variant=v))
        if session.get(KagDocumentEntity, (document_id, ent.id, role)) is None:
            session.add(KagDocumentEntity(document_id=document_id, entity_id=ent.id, role=role, chunk_id=first_chunk))
            edges += 1
    links = 0
    for ch in chunks:
        for ctype, conf in classify_chunk(ch.text, getattr(ch, "heading", None)):
            session.add(KagDocumentClause(document_id=document_id, clause_type=ctype, chunk_id=ch.id, confidence=conf))
            links += 1
    return edges, links


# --- query ---------------------------------------------------------------

@dataclass
class KagPlan:
    active: bool = False
    reason: str = ""
    mode: str = "none"  # none | unique | multi
    entities: list[str] = field(default_factory=list)
    version_ids: list[str] = field(default_factory=list)
    document_ids: list[int] = field(default_factory=list)
    clause: ClauseType | None = None
    clause_chunk_ids: dict[str, float] = field(default_factory=dict)  # chunk id -> confidence (the requested type)
    clause_absent: bool = False
    file_name: str | None = None
    top_version_id: str | None = None

    def to_dict(self) -> dict:
        return {
            "mode": self.mode, "reason": self.reason, "entities": self.entities,
            "documents": len(self.version_ids), "clause": self.clause.name if self.clause else None,
            "clause_chunks": len(self.clause_chunk_ids), "clause_absent": self.clause_absent,
        }


MAX_SOFT_DOCS = 3

_VARIANT_SQL = (
    "SELECT v.variant, e.id FROM kag_entity_variants v JOIN kag_entities e ON e.id = v.entity_id "
    "WHERE v.variant IN ({ph})"
)
_DOCS_SQL = (
    "SELECT de.entity_id, d.id, d.evidence_version_id, d.file_name, d.doc_text "
    "FROM kag_document_entities de JOIN documents d ON d.id = de.document_id "
    "JOIN evidence_versions ev ON ev.id = d.evidence_version_id JOIN sources s ON s.id = ev.source_id "
    "WHERE de.entity_id IN ({ph}) AND s.status = :active AND ev.status = :ready"
)


def _windows(question: str, max_n: int = 6):
    """(norm_text, start_token_idx, end_idx, first_token_capitalised) for each token window."""
    toks = [(m.group(0), m.start()) for m in re.finditer(r"[A-Za-z0-9][A-Za-z0-9'’&.\-]*", question)]
    cleaned = [(_clean(t), pos) for t, pos in toks]
    # one raw token may split into several cleaned tokens (hyphens); flatten with a capitalisation flag
    flat: list[tuple[str, bool, int]] = []
    for i, (ctoks, _pos) in enumerate(cleaned):
        raw = toks[i][0]
        for j, t in enumerate(ctoks):
            flat.append((t, raw[:1].isupper() or raw.isupper(), i))
    n = len(flat)
    for i in range(n):
        for k in range(1, max_n + 1):
            if i + k > n:
                break
            seg = flat[i:i + k]
            yield " ".join(t for t, _, _ in seg), i, i + k, seg[0][1]


def match_entities(engine: Engine, question: str) -> list[tuple[int, str, tuple[int, int]]]:
    """Entities named in the question: [(entity_id, matched variant, (start, end))], longest spans only."""
    wins = [(w, s, e, cap) for w, s, e, cap in _windows(question) if usable_variant(w)]
    if not wins:
        return []
    keys = sorted({w for w, *_ in wins})
    found: dict[str, set[int]] = {}
    with engine.connect() as conn:
        for i in range(0, len(keys), 400):
            part = keys[i:i + 400]
            rows = conn.execute(
                text(_VARIANT_SQL.format(ph=",".join(f":p{j}" for j in range(len(part))))),
                {f"p{j}": v for j, v in enumerate(part)},
            ).all()
            for v, eid in rows:
                found.setdefault(v, set()).add(eid)
    hits: list[tuple[int, str, tuple[int, int]]] = []
    spans: list[tuple[int, int, set[int], str]] = []
    for w, s, e, cap in wins:
        if w in found:
            if len(w.split()) == 1 and not cap:
                continue  # a lone lower-case word is an ordinary word, not a name
            spans.append((s, e, found[w], w))
    spans.sort(key=lambda x: (-(x[1] - x[0]), x[0]))
    kept: list[tuple[int, int, set[int], str]] = []
    for s, e, ids, w in spans:
        if any(s >= ks and e <= ke for ks, ke, _, _ in kept):
            continue  # inside a longer matched name
        kept.append((s, e, ids, w))
    for s, e, ids, w in kept:
        for eid in sorted(ids):
            hits.append((eid, w, (s, e)))
    return hits


def plan(engine: Engine, question: str, scope_version_ids: list[str] | None = None) -> KagPlan:
    """Never raises. An inactive plan means 'do exactly what happens without KAG'."""
    try:
        return _plan(engine, question, scope_version_ids)
    except Exception:  # noqa: BLE001
        import logging

        logging.getLogger(__name__).warning("kag planning failed; searching globally", exc_info=True)
        return KagPlan(reason="error")


def _plan(engine: Engine, question: str, scope_version_ids: list[str] | None) -> KagPlan:
    clause = detect_clause_type(question)
    hits = match_entities(engine, question)
    if not hits:
        return KagPlan(reason="no entity named", clause=clause)
    by_span: dict[tuple[int, int], set[int]] = {}
    for eid, _w, span in hits:
        by_span.setdefault(span, set()).add(eid)
    ent_ids = sorted({e for ids in by_span.values() for e in ids})
    with engine.connect() as conn:
        rows = conn.execute(
            text(_DOCS_SQL.format(ph=",".join(f":e{j}" for j in range(len(ent_ids))))),
            {**{f"e{j}": e for j, e in enumerate(ent_ids)}, "active": SourceStatus.ACTIVE.name, "ready": VersionStatus.READY.name},
        ).all()
        names = {r[0]: r[1] for r in conn.execute(
            text("SELECT id, display FROM kag_entities WHERE id IN (" + ",".join(str(int(e)) for e in ent_ids) + ")"))}
    scope = set(scope_version_ids) if scope_version_ids is not None else None
    docs_by_entity: dict[int, dict[int, tuple[str, str | None, str]]] = {}
    for eid, did, vid, fname, dtext in rows:
        if scope is not None and vid not in scope:
            continue
        docs_by_entity.setdefault(eid, {})[did] = (vid, fname, dtext or "")
    ent_names = [names.get(e, str(e)) for e in ent_ids]
    # each named span is satisfied by the docs of ANY of its candidate entities; all spans must hold
    per_span: list[dict[int, tuple[str, str | None, str]]] = []
    for span, ids in by_span.items():
        union: dict[int, tuple[str, str | None, str]] = {}
        for e in ids:
            union.update(docs_by_entity.get(e, {}))
        per_span.append(union)
    if any(not d for d in per_span):
        return KagPlan(reason="named entity has no document in scope", entities=ent_names, clause=clause)
    common = set(per_span[0])
    for d in per_span[1:]:
        common &= set(d)
    if not common:
        return KagPlan(reason="named entities share no document", entities=ent_names, clause=clause)
    docs = {did: per_span[0].get(did) or next(d[did] for d in per_span if did in d) for did in common}
    # exact copies of one file (same text) count as one document
    distinct_text = {t[2][:2000] for t in docs.values()}
    if len(docs) == 1 or len(distinct_text) == 1:
        mode = "unique"
    elif len(docs) <= MAX_SOFT_DOCS:
        mode = "multi"
    else:
        return KagPlan(reason=f"{len(docs)} documents match; too ambiguous", entities=ent_names, clause=clause)
    dids = sorted(docs)
    p = KagPlan(
        active=True, reason="matched", mode=mode, entities=ent_names, document_ids=dids,
        version_ids=[docs[d][0] for d in dids], clause=clause,
        file_name=docs[dids[0]][1], top_version_id=docs[dids[0]][0],
    )
    if clause is not None:
        with engine.connect() as conn:
            crow = conn.execute(
                text("SELECT chunk_id, confidence FROM kag_document_clauses WHERE clause_type = :t AND document_id IN ("
                     + ",".join(str(int(d)) for d in dids) + ")"), {"t": clause.name}).all()
        p.clause_chunk_ids = {c: float(cf) for c, cf in crow}
        p.clause_absent = bool(mode == "unique" and clause.can_be_absent and not crow)
    return p


def scoped_query(question: str, plan_: KagPlan) -> str:
    """Clause-focused wording for the in-document search."""
    if plan_.clause is None:
        return question
    return f"{question} {plan_.clause.hint}"


def fuse(global_ids: list[str], scoped_ids: list[str], limit: int, *, scoped_weight: float = 1.5, k: int = 60) -> list[tuple[str, float]]:
    """Weighted RRF of the scoped list and the global list. With these weights the first
    ~8 scoped candidates outrank any global-only candidate, but nothing global is dropped
    unless the limit cuts it."""
    scores: dict[str, float] = {}
    for r, cid in enumerate(scoped_ids):
        scores[cid] = scores.get(cid, 0.0) + scoped_weight / (k + r + 1)
    for r, cid in enumerate(global_ids):
        scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + r + 1)
    return sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:limit]


def absent_note(plan_: KagPlan) -> str:
    name = plan_.file_name or "the named document"
    label = plan_.clause.label if plan_.clause else "that clause"
    return (
        f"\n\nClause check (deterministic scan of every passage of {name}): no passage of that document mentions "
        f"{label}. If the excerpts above do not show it, say plainly that {name} contains no {label} "
        "provision, and cite the document with one of its excerpts. Do not guess a value."
    )
