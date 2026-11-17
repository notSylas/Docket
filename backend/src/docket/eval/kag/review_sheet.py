"""Human-review sheets for the KAG items that need human judgement (plain CSV + instructions .txt).

Built from a scratch library's graph; the system's proposals are shown so a reviewer can confirm,
reject or add. These sheets contain document text: files are 0600 in a 0700 directory and must not be
committed. Nothing here fills a label; the label columns are left empty.
"""

from __future__ import annotations

import csv
import io
import random
import re
import sqlite3
from pathlib import Path

from docket.eval.kag.graph import GraphSnapshot
from docket.eval.outputs import ensure_private_dir, write_private_text

ENTITY_COLUMNS = ("item_id", "kind", "file", "passage", "entity", "system_role", "entity_correct", "role_correct",
                  "missing_entities", "notes")
RESOLUTION_COLUMNS = ("item_id", "entity_a", "a_documents", "entity_b", "b_documents", "same", "notes")
RELATION_COLUMNS = ("item_id", "file_a", "title_a", "dates_a", "file_b", "title_b", "dates_b", "relation", "notes")

INSTRUCTIONS = """KAG REVIEW INSTRUCTIONS (plain text)

These sheets ask you to check what a document-graph extractor produced. Fill ONLY the empty columns.
Accepted values for yes/no columns: yes / no (blank = unsure; explain in notes). Judge only from the
passage and, where needed, the document itself. Do not guess; do not use the system's wording as evidence.

kag_review_entities.csv
  kind=edge   one proposed (document, entity) link.
              entity_correct: is `entity` really a company/person named in this document?
              role_correct:   system_role=party means one of the contracting parties (the signatories);
                              mention means named in the text but not a party. yes if the role is right.
  kind=doc    one row per document: list in missing_entities (semicolon separated) every contracting party
              the system did NOT propose.
kag_review_resolution.csv
  same: yes if entity_a and entity_b are the SAME legal entity (a name variant, abbreviation or former
  name of one company), no if they are different companies (including parent/subsidiary or affiliates).
kag_review_relations.csv
  relation: amends if document A amends document B, supersedes if A replaces B, none if unrelated
  or merely both involve the same company.

Independence: label as someone other than the person who wrote the gold. Write your name in the file
name or tell the maintainer so reviewed_by can be recorded. Keep these files private (they contain
document text). Return the filled CSVs unchanged in structure.
"""


def _csv_text(columns: tuple[str, ...], rows: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=columns, lineterminator="\n")
    w.writeheader()
    w.writerows({c: r.get(c, "") for c in columns} for r in rows)
    return buf.getvalue()


def _snippet(text: str, needles: set[str], width: int = 420) -> str:
    flat = re.sub(r"\s+", " ", text)
    low = flat.lower()
    for n in sorted(needles, key=len, reverse=True):
        i = low.find(n.lower())
        if i >= 0:
            start = max(0, i - width // 3)
            return flat[start:start + width]
    return flat[:width]


def export_review_sheets(
    conn: sqlite3.Connection, snap: GraphSnapshot, out_dir: Path, *, max_pairs: int = 60, seed: int = 0
) -> dict:
    out = ensure_private_dir(out_dir)
    chunk_text = {cid: t for cid, t in conn.execute("SELECT id, text FROM chunks")}
    info = {d.doc_id: d for d in snap.docs_by_file.values()}
    rows: list[dict] = []
    n = 0
    for doc_id in sorted(info):
        d = info[doc_id]
        for did, eid, role, chunk_id in sorted(x for x in snap.doc_entities if x[0] == doc_id):
            n += 1
            norm, display, _k = snap.entities[eid]
            rows.append({"item_id": f"E{n:03d}", "kind": "edge", "file": d.file_name,
                         "passage": _snippet(chunk_text.get(chunk_id or "", ""), {display, norm}),
                         "entity": display, "system_role": role})
        n += 1
        first = conn.execute("SELECT text FROM chunks WHERE evidence_version_id = ? ORDER BY ordinal LIMIT 1", (d.version_id,)).fetchone()
        rows.append({"item_id": f"E{n:03d}", "kind": "doc", "file": d.file_name, "passage": _snippet(first[0] if first else "", set(), 700)})
    docs_of: dict[int, list[str]] = {}
    for doc_id, eid, _r, _c in snap.doc_entities:
        docs_of.setdefault(eid, []).append(info[doc_id].file_name if doc_id in info else "?")
    ents = sorted(snap.entities)
    core = {e: {t for t in snap.entities[e][0].split() if len(t) > 2} for e in ents}
    near = [(a, b) for i, a in enumerate(ents) for b in ents[i + 1:] if core[a] & core[b]]
    rng = random.Random(seed)
    far = [(a, b) for i, a in enumerate(ents) for b in ents[i + 1:] if not core[a] & core[b]]
    rng.shuffle(far)
    chosen = near[: max_pairs * 2 // 3] + far[: max_pairs - min(len(near), max_pairs * 2 // 3)]
    rng.shuffle(chosen)
    pair_rows = [{
        "item_id": f"R{i:03d}", "entity_a": snap.entities[a][1], "a_documents": "; ".join(sorted(set(docs_of.get(a, [])))),
        "entity_b": snap.entities[b][1], "b_documents": "; ".join(sorted(set(docs_of.get(b, [])))),
    } for i, (a, b) in enumerate(chosen, 1)]
    by_party: dict[int, list[int]] = {}
    for doc_id, eid, role, _c in snap.doc_entities:
        if role == "party":
            by_party.setdefault(eid, []).append(doc_id)
    doc_meta = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT id, title, dates_json FROM documents")}
    seen: set[tuple[int, int]] = set()
    rel_rows: list[dict] = []
    for eid, dids in sorted(by_party.items()):
        for i, a in enumerate(sorted(set(dids))):
            for b in sorted(set(dids))[i + 1:]:
                if (a, b) in seen or len(rel_rows) >= max_pairs:
                    continue
                seen.add((a, b))
                rel_rows.append({"item_id": f"D{len(rel_rows) + 1:03d}", "file_a": info[a].file_name, "title_a": doc_meta[a][0] or "",
                                 "dates_a": doc_meta[a][1] or "", "file_b": info[b].file_name, "title_b": doc_meta[b][0] or "",
                                 "dates_b": doc_meta[b][1] or ""})
    paths = {
        "entities": out / "kag_review_entities.csv", "resolution": out / "kag_review_resolution.csv",
        "relations": out / "kag_review_relations.csv", "instructions": out / "KAG_REVIEW_INSTRUCTIONS.txt",
    }
    write_private_text(paths["entities"], _csv_text(ENTITY_COLUMNS, rows))
    write_private_text(paths["resolution"], _csv_text(RESOLUTION_COLUMNS, pair_rows))
    write_private_text(paths["relations"], _csv_text(RELATION_COLUMNS, rel_rows))
    write_private_text(paths["instructions"], INSTRUCTIONS)
    return {"entity_rows": len(rows), "resolution_rows": len(pair_rows), "relation_rows": len(rel_rows), "paths": paths}
