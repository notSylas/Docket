"""`IndexWriter` backed by the `fts_chunks` SQLite FTS5 virtual table.

`fts_chunks` is created by CP1's Alembic migration as
``fts5(chunk_id UNINDEXED, text)`` -- a standalone virtual table, not mapped
by the ORM (FTS5 virtual tables don't map cleanly onto SQLAlchemy models), so
this module talks to it via raw SQL through a SQLAlchemy `Engine`.

Design note -- `delete_by_source` / `chunk_ids_for_source`:
The `fts_chunks` schema has no `source_id` column (by design -- it only
exists to power `MATCH` lookups keyed by `chunk_id`), so this class cannot
answer "which chunk_ids belong to source X" on its own. Rather than smuggle a
join against the `chunks` metadata table in here (which would make
`FtsIndexWriter` depend on the ORM schema it's explicitly decoupled from, and
duplicate the same lookup `LanceIndexWriter`/`IndexManager` already need),
these two methods raise `NotImplementedError` pointing callers at
`IndexManager`, which resolves source membership via
`LanceIndexWriter.chunk_ids_for_source` (the vector table *does* carry
`source_id`) and then calls `delete()` here with the resolved chunk_ids. That
keeps `FtsIndexWriter` a thin, single-responsibility wrapper around the FTS5
table and keeps the "what belongs to a source" logic in one place
(`IndexManager`).
"""

from __future__ import annotations

from typing import Sequence

from sqlalchemy import Engine, bindparam, text

from docket.infra.index.base import ChunkRecord

_NOT_IMPLEMENTED_MSG = (
    "FtsIndexWriter.{method}() is not implemented: the fts_chunks FTS5 table "
    "has no source_id column, so it cannot resolve source membership on its "
    "own. Use IndexManager.{entrypoint}(), which resolves the relevant "
    "chunk_ids via the vector index and calls FtsIndexWriter.delete() with "
    "them directly."
)

_DELETE_BY_IDS = text("DELETE FROM fts_chunks WHERE chunk_id IN :chunk_ids").bindparams(
    bindparam("chunk_ids", expanding=True)
)
_SELECT_EXISTING_IDS = text(
    "SELECT chunk_id FROM fts_chunks WHERE chunk_id IN :chunk_ids"
).bindparams(bindparam("chunk_ids", expanding=True))


class FtsIndexWriter:
    """`IndexWriter` for the `fts_chunks` FTS5 virtual table."""

    def __init__(self, engine: Engine):
        self._engine = engine

    def upsert(
        self, records: Sequence[ChunkRecord], embeddings: Sequence[list[float]] | None = None
    ) -> None:
        # embeddings is accepted (per the IndexWriter protocol) but unused --
        # FTS5 is a lexical index, it has no notion of vectors.
        if not records:
            return
        with self._engine.begin() as conn:
            for record in records:
                # FTS5 has no native upsert-by-key: delete then re-insert.
                conn.execute(
                    text("DELETE FROM fts_chunks WHERE chunk_id = :chunk_id"),
                    {"chunk_id": record.chunk_id},
                )
                conn.execute(
                    text("INSERT INTO fts_chunks (chunk_id, text) VALUES (:chunk_id, :text)"),
                    {"chunk_id": record.chunk_id, "text": record.text},
                )

    def delete(self, chunk_ids: Sequence[str]) -> None:
        chunk_ids = list(chunk_ids)
        if not chunk_ids:
            return
        with self._engine.begin() as conn:
            conn.execute(_DELETE_BY_IDS, {"chunk_ids": chunk_ids})

    def delete_by_source(self, source_id: str) -> None:
        raise NotImplementedError(
            _NOT_IMPLEMENTED_MSG.format(method="delete_by_source", entrypoint="delete_source")
        )

    def existing_chunk_ids(self, chunk_ids: Sequence[str]) -> set[str]:
        chunk_ids = list(chunk_ids)
        if not chunk_ids:
            return set()
        with self._engine.connect() as conn:
            rows = conn.execute(_SELECT_EXISTING_IDS, {"chunk_ids": chunk_ids})
            return {row[0] for row in rows}

    def chunk_ids_for_source(self, source_id: str) -> set[str]:
        raise NotImplementedError(
            _NOT_IMPLEMENTED_MSG.format(
                method="chunk_ids_for_source", entrypoint="reconcile_source"
            )
        )
