"""`IndexWriter` backed by the `fts_chunks` SQLite FTS5 virtual table.

`fts_chunks` is created by Alembic migrations as
``fts5(chunk_id UNINDEXED, text, evidence_version_id UNINDEXED, source_id
UNINDEXED)`` -- a standalone virtual table, not mapped by the ORM (FTS5
virtual tables don't map cleanly onto SQLAlchemy models), so this module talks
to it via raw SQL through a SQLAlchemy `Engine`.

Design note -- source/version membership: every row carries the
`evidence_version_id` and `source_id` of the chunk it mirrors (migration
0011), so this writer can enumerate and delete by source or by version on its
own, without a join to the `chunks` table. That matters for orphans: an
FTS-only row (crash between the FTS and vector writes) has no vector row to
be found through, and after a purge its `chunks`/`evidence_versions` rows are
already gone, so the membership has to live in the FTS row itself.
"""

from __future__ import annotations

from typing import Sequence

from sqlalchemy import Engine, bindparam, text

from docket.infra.index.base import ChunkRecord

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
                    text(
                        "INSERT INTO fts_chunks (chunk_id, text, evidence_version_id, source_id) "
                        "VALUES (:chunk_id, :text, :evidence_version_id, :source_id)"
                    ),
                    {
                        "chunk_id": record.chunk_id,
                        "text": record.index_text or record.text,
                        "evidence_version_id": record.evidence_version_id,
                        "source_id": record.source_id,
                    },
                )

    def delete(self, chunk_ids: Sequence[str]) -> None:
        chunk_ids = list(chunk_ids)
        if not chunk_ids:
            return
        with self._engine.begin() as conn:
            conn.execute(_DELETE_BY_IDS, {"chunk_ids": chunk_ids})

    def delete_by_source(self, source_id: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                text("DELETE FROM fts_chunks WHERE source_id = :source_id"),
                {"source_id": source_id},
            )

    def delete_by_version(self, evidence_version_id: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                text("DELETE FROM fts_chunks WHERE evidence_version_id = :evidence_version_id"),
                {"evidence_version_id": evidence_version_id},
            )

    def existing_chunk_ids(self, chunk_ids: Sequence[str]) -> set[str]:
        chunk_ids = list(chunk_ids)
        if not chunk_ids:
            return set()
        with self._engine.connect() as conn:
            rows = conn.execute(_SELECT_EXISTING_IDS, {"chunk_ids": chunk_ids})
            return {row[0] for row in rows}

    def chunk_ids_for_source(self, source_id: str) -> set[str]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                text("SELECT chunk_id FROM fts_chunks WHERE source_id = :source_id"),
                {"source_id": source_id},
            )
            return {row[0] for row in rows}
