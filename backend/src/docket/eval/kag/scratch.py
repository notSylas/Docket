"""Build a SCRATCH KAG library for evaluation from chunks already stored in another library.

No model, no GPU, no embeddings, no parsing: it copies the stored chunk rows (one servable version per
file name) into a new SQLite DB and runs the deterministic document + KAG builder
(`builder.build_all(kag=True)`) there. The destination must not be the real data directory.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path, PurePosixPath

from docket.eval.kag.graph import refuse_real_data_dir
from docket.eval.outputs import ensure_private_dir

COPY_TABLES = ("alembic_version", "chunk_recipes", "workspaces", "authorized_sources", "sources",
               "evidence_versions", "evidence_units", "chunks", "documents", "document_fields",
               "kag_entities", "kag_entity_variants", "kag_document_entities", "kag_document_clauses")


class ScratchError(RuntimeError):
    pass


def build_scratch_library(source_db: Path, file_names: list[str], dest_dir: Path) -> dict:
    refuse_real_data_dir(dest_dir)
    refuse_real_data_dir(source_db)
    dest_dir = Path(dest_dir)
    dest = dest_dir / "docket.sqlite3"
    if dest.exists():
        raise ScratchError(f"{dest} already exists; choose a new scratch directory")
    ensure_private_dir(dest_dir)
    wanted = [PurePosixPath(f).name for f in file_names]
    src = sqlite3.connect(f"file:{Path(source_db).resolve()}?mode=ro", uri=True)
    try:
        versions: dict[str, tuple[str, str]] = {}
        for name in wanted:
            row = src.execute(
                "SELECT ev.id, ev.source_id FROM evidence_versions ev JOIN sources s ON s.id = ev.source_id "
                "WHERE upper(ev.status) = 'READY' AND upper(s.status) = 'ACTIVE' AND ev.file_path LIKE ? "
                "ORDER BY ev.source_id, ev.id LIMIT 1", ("%/" + name,)).fetchone()
            if row is None:
                raise ScratchError(f"no servable version of {name} in {source_db}")
            versions[name] = (row[0], row[1])
        ddl = {n: s for n, s in src.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table'")}
    finally:
        src.close()
    out = sqlite3.connect(f"file:{dest}", uri=True)
    for t in COPY_TABLES:
        if t in ddl:
            out.execute(ddl[t])
    out.execute("ATTACH DATABASE ? AS src", (f"file:{Path(source_db).resolve()}?mode=ro",))
    try:
        vids = [v for v, _s in versions.values()]
        sids = sorted({s for _v, s in versions.values()})
        qv = ",".join("?" for _ in vids)
        qs = ",".join("?" for _ in sids)
        out.execute("INSERT INTO alembic_version SELECT * FROM src.alembic_version")
        out.execute("INSERT INTO chunk_recipes SELECT * FROM src.chunk_recipes")
        out.execute(f"INSERT INTO sources SELECT * FROM src.sources WHERE id IN ({qs})", sids)
        out.execute("INSERT INTO workspaces SELECT * FROM src.workspaces WHERE id IN (SELECT workspace_id FROM sources)")
        out.execute("INSERT INTO authorized_sources SELECT * FROM src.authorized_sources WHERE id IN (SELECT authorized_source_id FROM sources)")
        out.execute(f"INSERT INTO evidence_versions SELECT * FROM src.evidence_versions WHERE id IN ({qv})", vids)
        out.execute(f"INSERT INTO evidence_units SELECT * FROM src.evidence_units WHERE evidence_version_id IN ({qv})", vids)
        out.execute(f"INSERT INTO chunks SELECT * FROM src.chunks WHERE evidence_version_id IN ({qv})", vids)
        out.commit()
        chunks = out.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        out.execute("DETACH DATABASE src")
    finally:
        out.close()
    import os

    os.chmod(dest, 0o600)
    from docket.core.db.engine import get_engine, get_session_factory
    from docket.services.documents import builder

    engine = get_engine(dest)
    try:
        stats = builder.build_all(get_session_factory(engine), force=True, kag=True)
    finally:
        engine.dispose()
    return {"db": str(dest), "documents": len(versions), "chunks": chunks, "built": stats.built, "fields": stats.fields}
