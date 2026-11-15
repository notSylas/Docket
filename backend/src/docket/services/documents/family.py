"""Version families and amendment links (deterministic, conservative).

* A *family* is the set of documents sharing `family_key`; ordering inside it
  is by source mtime (then document id).
* An *explicit* link (`supersedes_document_id` + `supersedes_kind`) is only
  written when the document's own preamble names the earlier agreement
  ("Amendment No. 2 to ...", "amends and restates ...", "supersedes and
  replaces ... dated ...") AND exactly one other document matches that
  reference (title + date or party). Anything ambiguous stays unlinked.
* Query time (`DOCKET_FAMILY_LATEST=on`): when chunks of an older and a newer
  member of one family are both retrieved, the older ones are dropped from the
  evidence and the answer gets a one-line note. Partial amendments ('amends')
  never hide the base agreement's chunks; they only add a note.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from docket.core.db.models import Document, EvidenceVersion, Source, SourceStatus, VersionStatus
from docket.services.documents import classify

_TITLE = r"(?P<title>[A-Z][A-Za-z0-9&,'’\- ]{3,90}?(?:Agreement|Contract|Lease|License|Addendum))"
_DATE = r"(?:\s*,?\s*(?:dated|made)(?:\s+as\s+of|\s+on)?\s+(?P<date>[A-Za-z0-9 ,./-]{6,30}?\d{4}))?"
_LINKS = [
    ("amends", re.compile(rf"\bAmendment\s+(?:No\.?\s*\d+\s+)?(?:to|of)\s+(?:the\s+|that\s+certain\s+)?{_TITLE}{_DATE}", re.I)),
    ("replaces", re.compile(rf"\bamends?\s+and\s+restates?\s+(?:in\s+its\s+entirety\s+)?(?:the\s+|that\s+certain\s+)?{_TITLE}{_DATE}", re.I)),
    ("replaces", re.compile(rf"\b(?:supersedes?|replaces?)(?:\s+and\s+(?:supersedes?|replaces?))?\s+(?:in\s+its\s+entirety\s+)?(?:the\s+|that\s+certain\s+)?{_TITLE}{_DATE}", re.I)),
]


@dataclass(frozen=True)
class Reference:
    kind: str
    title: str
    date_iso: str | None


def find_reference(preamble: str) -> Reference | None:
    text = re.sub(r"\s+", " ", preamble)[:3000]
    for kind, pat in _LINKS:
        m = pat.search(text)
        if not m:
            continue
        iso = None
        if m.groupdict().get("date"):
            ds = classify.find_dates(m.group("date"))
            iso = ds[0][0] if ds else None
        return Reference(kind, m.group("title").strip(), iso)
    return None


_STOP = {"the", "a", "an", "of", "and", "agreement", "contract", "inc", "llc", "corp", "corporation", "company", "ltd", "co"}


def _toks(s: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", s.lower()) if t not in _STOP}


def _row_dates(d: Document) -> dict:
    return json.loads(d.dates_json) if d.dates_json else {}


def _row_parties(d: Document) -> list[str]:
    return json.loads(d.parties_json) if d.parties_json else []


def resolve_reference(session: Session, doc: Document, ref: Reference, preamble: str) -> Document | None:
    cands = session.execute(
        select(Document)
        .join(EvidenceVersion, EvidenceVersion.id == Document.evidence_version_id)
        .join(Source, Source.id == EvidenceVersion.source_id)
        .where(
            Document.id != doc.id,
            Document.doc_type.in_(("contract", "agreement")),
            EvidenceVersion.status == VersionStatus.READY,
            Source.status == SourceStatus.ACTIVE,
        )
    ).scalars().all()
    want = _toks(ref.title)
    own_parties = _toks(" ".join(_row_parties(doc))) | _toks(preamble[:1500])
    hits = []
    for c in cands:
        ct = _toks(c.title or "") | _toks(c.file_name or "")
        title_ok = bool(want) and want <= ct if want else False
        generic_title = not want
        if not (title_ok or generic_title):
            continue
        date_ok = ref.date_iso is not None and _row_dates(c).get("effective") == ref.date_iso
        party_ok = bool(_toks(" ".join(_row_parties(c))) & own_parties)
        if (ref.date_iso and date_ok and party_ok) or (not ref.date_iso and party_ok and title_ok):
            hits.append(c)
    return hits[0] if len(hits) == 1 else None


def link_document(session: Session, doc: Document) -> bool:
    """Set (or clear) the explicit supersedes link of one document."""
    if doc.doc_type not in ("amendment", "agreement", "contract"):
        return False
    ev = session.get(EvidenceVersion, doc.evidence_version_id)
    from docket.core.db.models import Chunk

    first = session.execute(
        select(Chunk.text).where(Chunk.evidence_version_id == doc.evidence_version_id).order_by(Chunk.ordinal).limit(3)
    ).scalars().all()
    preamble = "\n".join(first)
    ref = find_reference(preamble)
    doc.supersedes_document_id = None
    doc.supersedes_kind = None
    if ref is None or ev is None:
        return False
    target = resolve_reference(session, doc, ref, preamble)
    if target is None:
        return False
    doc.supersedes_document_id = target.id
    doc.supersedes_kind = ref.kind
    return True


def relink_all(session_factory: sessionmaker, source_id: str | None = None) -> int:
    n = 0
    with session_factory() as session:
        q = select(Document).where(Document.doc_type.in_(("amendment", "agreement", "contract")))
        if source_id:
            q = q.where(Document.source_id == source_id)
        for d in session.execute(q).scalars().all():
            if link_document(session, d):
                n += 1
        session.commit()
    return n


# --- query-time --------------------------------------------------------------

@dataclass(frozen=True)
class FamilyDecision:
    drop_version_ids: frozenset[str]
    note: str | None


def _mtime_key(d: Document) -> tuple[float, int]:
    return (d.source_mtime or 0.0, d.id)


def apply_latest(session_factory: sessionmaker, chunks: list, text_of) -> FamilyDecision:
    """`chunks`: resolved evidence (needs `.evidence_version_id`, `.chunk_id`).
    `text_of(chunk) -> str`. Returns which versions' chunks to drop and a note."""
    vids = list(dict.fromkeys(c.evidence_version_id for c in chunks))
    if len(vids) < 2:
        return FamilyDecision(frozenset(), None)
    with session_factory() as session:
        docs = {
            d.evidence_version_id: d
            for d in session.execute(select(Document).where(Document.evidence_version_id.in_(vids))).scalars()
        }
        by_id = {d.id: d for d in docs.values()}
        older: dict[str, Document] = {}  # version id -> newer doc that replaces it
        amended: list[tuple[Document, Document]] = []
        groups: dict[str, list[Document]] = {}
        for d in docs.values():
            if d.family_key:
                groups.setdefault(d.family_key, []).append(d)
        for members in groups.values():
            if len(members) > 1:
                ordered = sorted(members, key=_mtime_key)
                if (ordered[-1].source_mtime or 0) == (ordered[-2].source_mtime or 0):
                    continue  # cannot order them: leave alone
                for old in ordered[:-1]:
                    older[old.evidence_version_id] = ordered[-1]
        for d in docs.values():
            tgt = by_id.get(d.supersedes_document_id) if d.supersedes_document_id else None
            if tgt is not None:
                if d.supersedes_kind == "replaces":
                    older[tgt.evidence_version_id] = d
                else:
                    amended.append((d, tgt))
        drop = frozenset(older)
        notes: list[str] = []
        newer_texts: dict[str, set[str]] = {}
        for c in chunks:
            newer_texts.setdefault(c.evidence_version_id, set()).add(" ".join(text_of(c).split()))
        for old_vid, new_doc in older.items():
            old_doc = docs[old_vid]
            new_set = newer_texts.get(new_doc.evidence_version_id, set())
            differs = any(" ".join(text_of(c).split()) not in new_set for c in chunks if c.evidence_version_id == old_vid)
            if differs:
                notes.append(f"An earlier version ({old_doc.file_name}) is also in your library and differs; this answer uses the latest ({new_doc.file_name}).")
        for am, base in amended:
            notes.append(f"Note: {am.file_name} amends {base.file_name}; check the amendment for changes.")
    return FamilyDecision(drop, " ".join(dict.fromkeys(notes)) or None)
