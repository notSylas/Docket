"""`reindex` -- rebuild FTS5 and the LanceDB `chunks`/`pages` tables from
SQLite under the currently configured embedding model, then write the index
manifest (see `docket.infra.index.manifest`).

What is indexed: the chunks of READY evidence versions whose source is ACTIVE
or MISSING -- what ingestion indexes (chunks are indexed before a version is
marked READY, and superseded versions' entries are removed). REVOKED,
TOMBSTONED, HARD_DELETE_PENDING and DELETED sources are left out: they can't
return to ACTIVE and retrieval never serves them. A MISSING source can come
back without re-ingestion, so it stays indexed. `pages` rows are re-embedded
from their stored `description` (no VLM calls) for the same set of versions;
rows of any other version are dropped.

Safety (LanceDB OSS has no `rename_table`): everything slow and fallible --
reading SQLite and embedding -- happens first, into scratch tables
`chunks__reindex`/`pages__reindex`, leaving the live index untouched. Only
then are the live tables swapped: each is replaced by one atomic
`create_table(mode="overwrite")` commit (LanceDB keeps the previous version),
then FTS5 is rewritten in a single SQLite transaction, and the manifest is
written LAST. If any swap step fails, the live LanceDB tables are restored
to their previous versions and the old manifest is untouched. A hard crash
(kill -9) between swap steps is not rolled back automatically, but the
manifest still names the old model, so queries refuse (rather than serve
mixed spaces) until `docket reindex` is re-run.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import pyarrow as pa
from sqlalchemy import Engine, bindparam, text

from docket.core.db.models import SourceStatus, VersionStatus
from docket.infra.index.manifest import new_manifest, read_manifest, write_manifest
from docket.infra.inference.gateway import InferenceGateway, embed_texts

_CHUNKS = "chunks"
_PAGES = "pages"
_STAGING_SUFFIX = "__reindex"

_ELIGIBLE_CHUNKS_SQL = text(
    "SELECT chunks.id, chunks.source_id, chunks.evidence_version_id, chunks.text "
    "FROM chunks "
    "JOIN evidence_versions ON evidence_versions.id = chunks.evidence_version_id "
    "JOIN sources ON sources.id = chunks.source_id "
    "WHERE evidence_versions.status = :ready AND sources.status IN :source_statuses "
    "ORDER BY chunks.id"
).bindparams(bindparam("source_statuses", expanding=True))

_ELIGIBLE_VERSIONS_SQL = text(
    "SELECT evidence_versions.id FROM evidence_versions "
    "JOIN sources ON sources.id = evidence_versions.source_id "
    "WHERE evidence_versions.status = :ready AND sources.status IN :source_statuses"
).bindparams(bindparam("source_statuses", expanding=True))

_INDEXED_SOURCE_STATUSES = [SourceStatus.ACTIVE.name, SourceStatus.MISSING.name]


@dataclass(frozen=True)
class ReindexResult:
    chunks: int
    pages: int
    embed_model: str
    embed_dimension: int


def _open(db: Any, name: str):
    try:
        return db.open_table(name)
    except ValueError:  # lancedb has no dedicated "table not found" exception
        return None


def _build_staging(
    db: Any,
    name: str,
    rows: list[dict],
    texts: list[str],
    gateway: InferenceGateway,
    batch_size: int,
    sleep: Callable[[float], None],
) -> int | None:
    """Embed `texts` batch by batch into a fresh scratch table `name`
    (rows[i] gets the vector of texts[i]). Returns the vector dimension, or
    `None` if there was nothing to embed (no table is created)."""
    dimension: int | None = None
    staging = None
    for start in range(0, len(rows), batch_size):
        batch_rows = rows[start : start + batch_size]
        vectors = embed_texts(
            gateway,
            texts[start : start + batch_size],
            batch_size=batch_size,
            sleep=sleep,
        )
        if dimension is None:
            dimension = len(vectors[0])
        if any(len(v) != dimension for v in vectors):
            raise ValueError("embedding model returned vectors of differing dimensions")
        data = [{**row, "vector": vector} for row, vector in zip(batch_rows, vectors)]
        if staging is None:
            staging = db.create_table(name, data=data, mode="overwrite")
        else:
            staging.add(data)
    return dimension


def reindex(
    *,
    engine: Engine,
    db_path: str | Path,
    gateway: InferenceGateway,
    manifest_path: Path,
    embed_model: str,
    batch_size: int = 32,
    sleep: Callable[[float], None] | None = None,
    tokenizer: str | None = None,
) -> ReindexResult:
    import lancedb

    sleep = sleep or time.sleep
    db = lancedb.connect(str(db_path))
    chunks_staging, pages_staging = _CHUNKS + _STAGING_SUFFIX, _PAGES + _STAGING_SUFFIX

    with engine.connect() as conn:
        chunk_rows = conn.execute(
            _ELIGIBLE_CHUNKS_SQL,
            {"ready": VersionStatus.READY.name, "source_statuses": _INDEXED_SOURCE_STATUSES},
        ).all()
        version_ids = {
            row[0]
            for row in conn.execute(
                _ELIGIBLE_VERSIONS_SQL,
                {"ready": VersionStatus.READY.name, "source_statuses": _INDEXED_SOURCE_STATUSES},
            )
        }

    page_rows: list[dict] = []
    live_pages = _open(db, _PAGES)
    if live_pages is not None:
        columns = ["evidence_version_id", "source_id", "page_no", "description"]
        page_rows = [
            row
            for row in live_pages.to_arrow().select(columns).to_pylist()
            if row["evidence_version_id"] in version_ids
        ]

    try:
        # Phase 1: build scratch tables. The live index is not touched.
        for name in (chunks_staging, pages_staging):
            if _open(db, name) is not None:
                db.drop_table(name)
        chunk_dicts = [
            {
                "chunk_id": r[0],
                "source_id": r[1],
                "text": r[3],
                "evidence_version_id": r[2],
            }
            for r in chunk_rows
        ]
        dimension = _build_staging(
            db, chunks_staging, chunk_dicts, [r[3] for r in chunk_rows], gateway, batch_size, sleep
        )
        page_dimension = _build_staging(
            db,
            pages_staging,
            page_rows,
            [r["description"] for r in page_rows],
            gateway,
            batch_size,
            sleep,
        )
        if dimension is None:
            dimension = page_dimension
        if dimension is None:
            # Nothing to embed: probe the model for its dimension.
            dimension = len(embed_texts(gateway, ["dimension probe"], sleep=sleep)[0])
        if page_dimension is not None and page_dimension != dimension:
            raise ValueError("embedding model returned vectors of differing dimensions")

        # Phase 2: swap. Each live table is replaced by one atomic commit;
        # `undo` restores whatever was already swapped if a later step fails.
        undo: list[Callable[[], None]] = []
        try:
            vector_type = pa.list_(pa.float32(), dimension)
            _swap(
                db,
                _CHUNKS,
                chunks_staging,
                pa.schema(
                    [
                        pa.field("chunk_id", pa.string()),
                        pa.field("source_id", pa.string()),
                        pa.field("text", pa.string()),
                        pa.field("evidence_version_id", pa.string()),
                        pa.field("vector", vector_type),
                    ]
                ),
                always=True,
                undo=undo,
            )
            _swap(
                db,
                _PAGES,
                pages_staging,
                pa.schema(
                    [
                        pa.field("evidence_version_id", pa.string()),
                        pa.field("source_id", pa.string()),
                        pa.field("page_no", pa.int64()),
                        pa.field("description", pa.string()),
                        pa.field("vector", vector_type),
                    ]
                ),
                always=live_pages is not None,
                undo=undo,
            )
            _rewrite_fts(engine, chunk_dicts)
            previous = read_manifest(manifest_path)
            write_manifest(
                manifest_path,
                new_manifest(
                    embed_model,
                    dimension,
                    created_at=previous.created_at if previous else None,
                    tokenizer=tokenizer or (previous.tokenizer if previous else None),
                ),
            )
        except BaseException:
            for restore in reversed(undo):
                restore()
            raise
    finally:
        for name in (chunks_staging, pages_staging):
            if _open(db, name) is not None:
                db.drop_table(name)

    return ReindexResult(
        chunks=len(chunk_dicts),
        pages=len(page_rows),
        embed_model=embed_model,
        embed_dimension=dimension,
    )


def _swap(
    db: Any,
    name: str,
    staging_name: str,
    empty_schema: pa.Schema,
    *,
    always: bool,
    undo: list[Callable[[], None]],
) -> None:
    """Replace live table `name` with the scratch table's rows (or an empty
    table when there are none), registering how to undo it. With `always`
    False and no scratch rows, the live table is left alone (it may not
    exist at all)."""
    staging = _open(db, staging_name)
    if staging is None and not always:
        return
    live = _open(db, name)
    old_version = live.version if live is not None else None
    if staging is not None:
        db.create_table(name, data=staging.to_arrow(), mode="overwrite")
    else:
        db.create_table(name, schema=empty_schema, mode="overwrite")
    if old_version is None:
        undo.append(lambda: db.drop_table(name))
    else:
        undo.append(lambda: db.open_table(name).restore(old_version))


def _rewrite_fts(engine: Engine, chunk_dicts: list[dict]) -> None:
    """Replace the whole FTS5 table contents in one SQLite transaction."""
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM fts_chunks"))
        for row in chunk_dicts:
            conn.execute(
                text(
                    "INSERT INTO fts_chunks (chunk_id, text, evidence_version_id, source_id) "
                    "VALUES (:chunk_id, :text, :evidence_version_id, :source_id)"
                ),
                row,
            )
