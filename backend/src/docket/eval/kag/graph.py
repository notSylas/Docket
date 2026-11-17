"""Read-only view of the KAG tables in a scratch library (no model, no writes)."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from sqlalchemy import Engine, create_engine

DEFAULT_DATA_DIR_MARKER = ".local/share/docket"


class RealDataDirError(RuntimeError):
    pass


def refuse_real_data_dir(path: str | Path) -> None:
    """Eval tooling only ever touches scratch libraries."""
    resolved = str(Path(path).expanduser().resolve())
    if DEFAULT_DATA_DIR_MARKER in resolved:
        raise RealDataDirError(f"refusing to use the real data directory ({resolved}); copy to a scratch dir")


def connect_ro(db_path: str | Path) -> sqlite3.Connection:
    refuse_real_data_dir(db_path)
    return sqlite3.connect(f"file:{Path(db_path).resolve()}?mode=ro", uri=True)


def engine_ro(db_path: str | Path) -> Engine:
    refuse_real_data_dir(db_path)
    return create_engine(f"sqlite:///file:{Path(db_path).resolve()}?mode=ro&uri=true")


@dataclass
class DocInfo:
    doc_id: int
    version_id: str
    file_name: str
    servable: bool


@dataclass
class GraphSnapshot:
    docs_by_file: dict[str, DocInfo] = field(default_factory=dict)  # by base file name
    entities: dict[int, tuple[str, str, str]] = field(default_factory=dict)  # id -> (norm, display, kind)
    variants: dict[str, set[int]] = field(default_factory=dict)  # variant -> entity ids
    entity_variants: dict[int, set[str]] = field(default_factory=dict)
    doc_entities: list[tuple[int, int, str, str | None]] = field(default_factory=list)  # doc, entity, role, chunk
    clauses: list[tuple[int, str, str, float]] = field(default_factory=list)  # doc, type, chunk, conf

    def doc_for(self, file: str) -> DocInfo | None:
        return self.docs_by_file.get(PurePosixPath(file).name)

    def entity_ids_of_doc(self, doc_id: int) -> set[int]:
        return {e for d, e, _r, _c in self.doc_entities if d == doc_id}

    def roles_of_doc(self, doc_id: int) -> dict[int, set[str]]:
        out: dict[int, set[str]] = {}
        for d, e, r, _c in self.doc_entities:
            if d == doc_id:
                out.setdefault(e, set()).add(r)
        return out


def load_snapshot(conn: sqlite3.Connection) -> GraphSnapshot:
    snap = GraphSnapshot()
    rows = conn.execute(
        "SELECT d.id, d.evidence_version_id, d.file_name, "
        "(upper(ev.status) = 'READY' AND upper(s.status) = 'ACTIVE') "
        "FROM documents d JOIN evidence_versions ev ON ev.id = d.evidence_version_id "
        "JOIN sources s ON s.id = ev.source_id"
    ).fetchall()
    for doc_id, vid, name, ok in rows:
        # several documents with one file name: keep the servable one, else the first seen
        old = snap.docs_by_file.get(name or "")
        if old is None or (bool(ok) and not old.servable):
            snap.docs_by_file[name or ""] = DocInfo(doc_id, vid, name or "", bool(ok))
    for eid, norm, display, kind in conn.execute("SELECT id, norm, display, kind FROM kag_entities"):
        snap.entities[eid] = (norm, display, kind)
    for eid, variant in conn.execute("SELECT entity_id, variant FROM kag_entity_variants"):
        snap.variants.setdefault(variant, set()).add(eid)
        snap.entity_variants.setdefault(eid, set()).add(variant)
    snap.doc_entities = list(conn.execute("SELECT document_id, entity_id, role, chunk_id FROM kag_document_entities"))
    snap.clauses = list(conn.execute("SELECT document_id, clause_type, chunk_id, confidence FROM kag_document_clauses"))
    return snap
