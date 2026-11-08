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

from typing import Iterable, Sequence

from sqlalchemy import Connection, Engine, bindparam, text

from docket.infra.index.base import ChunkRecord

# Keep IN-lists well under SQLite's variable limit.
_BATCH = 500

# `fts_chunks.chunk_id` is an UNINDEXED FTS5 column: filtering on it (or on
# source_id / evidence_version_id) is a full scan of the FTS table, which made
# every per-file DELETE-before-INSERT and existence check O(library size).
# `fts_chunk_map` is a shadow table with a PRIMARY KEY on chunk_id (and indexes
# on the membership columns) mapping each chunk to its FTS rowid, so writers
# delete/look up by rowid in O(log n). It is written in the same transaction as
# `fts_chunks` and created by migration 0015 (and lazily here for databases
# opened without migrations).
_MAP_DDL = (
    "CREATE TABLE IF NOT EXISTS fts_chunk_map ("
    "chunk_id TEXT PRIMARY KEY, fts_rowid INTEGER NOT NULL, "
    "source_id TEXT, evidence_version_id TEXT)",
    "CREATE INDEX IF NOT EXISTS ix_fts_chunk_map_source ON fts_chunk_map(source_id)",
    "CREATE INDEX IF NOT EXISTS ix_fts_chunk_map_version ON fts_chunk_map(evidence_version_id)",
    "CREATE INDEX IF NOT EXISTS ix_fts_chunk_map_rowid ON fts_chunk_map(fts_rowid)",
)


def rebuild_fts_chunk_map(conn: Connection) -> None:
    """Drop duplicate FTS rows (keeping the newest per chunk_id) and rebuild
    the whole map from `fts_chunks`. Safe to re-run."""
    conn.execute(text("DELETE FROM fts_chunk_map"))
    conn.execute(
        text(
            "DELETE FROM fts_chunks WHERE rowid NOT IN "
            "(SELECT MAX(rowid) FROM fts_chunks GROUP BY chunk_id)"
        )
    )
    conn.execute(
        text(
            "INSERT INTO fts_chunk_map (chunk_id, fts_rowid, source_id, evidence_version_id) "
            "SELECT chunk_id, rowid, source_id, evidence_version_id FROM fts_chunks"
        )
    )


def ensure_fts_chunk_map_in(conn: Connection) -> None:
    """Create the map if needed and rebuild it from `fts_chunks` (for code
    that rewrites the whole FTS table inside its own transaction)."""
    for ddl in _MAP_DDL:
        conn.execute(text(ddl))
    rebuild_fts_chunk_map(conn)


def ensure_fts_chunk_map(engine: Engine) -> None:
    """Create the map if missing and backfill it from `fts_chunks`."""
    with engine.begin() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM sqlite_master WHERE type='table' AND name='fts_chunk_map'")
        ).first()
        if exists:
            return
        has_fts = conn.execute(
            text("SELECT 1 FROM sqlite_master WHERE name='fts_chunks'")
        ).first()
        if has_fts:
            ensure_fts_chunk_map_in(conn)
        else:
            for ddl in _MAP_DDL:
                conn.execute(text(ddl))


def _batches(values: Sequence[str]) -> Iterable[list[str]]:
    for i in range(0, len(values), _BATCH):
        yield list(values[i : i + _BATCH])


_MAP_IDS = text(
    "SELECT chunk_id, fts_rowid FROM fts_chunk_map WHERE chunk_id IN :ids"
).bindparams(bindparam("ids", expanding=True))


class FtsIndexWriter:
    """`IndexWriter` for the `fts_chunks` FTS5 virtual table."""

    def __init__(self, engine: Engine):
        self._engine = engine
        ensure_fts_chunk_map(engine)

    # -- helpers (all inside one caller-owned transaction) --

    @staticmethod
    def _delete_rowids(conn: Connection, rowids: Sequence[int]) -> None:
        for r in rowids:
            conn.execute(text("DELETE FROM fts_chunks WHERE rowid = :r"), {"r": r})

    def _delete_ids(self, conn: Connection, chunk_ids: Sequence[str]) -> None:
        for batch in _batches(list(chunk_ids)):
            rows = conn.execute(_MAP_IDS, {"ids": batch}).all()
            self._delete_rowids(conn, [r[1] for r in rows])
            conn.execute(
                text("DELETE FROM fts_chunk_map WHERE chunk_id IN :ids").bindparams(
                    bindparam("ids", expanding=True)
                ),
                {"ids": batch},
            )

    def _delete_where(self, conn: Connection, column: str, value: str) -> None:
        rows = conn.execute(
            text(f"SELECT fts_rowid FROM fts_chunk_map WHERE {column} = :v"), {"v": value}
        ).all()
        self._delete_rowids(conn, [r[0] for r in rows])
        conn.execute(text(f"DELETE FROM fts_chunk_map WHERE {column} = :v"), {"v": value})

    def upsert(
        self, records: Sequence[ChunkRecord], embeddings: Sequence[list[float]] | None = None
    ) -> None:
        # embeddings is accepted (per the IndexWriter protocol) but unused --
        # FTS5 is a lexical index, it has no notion of vectors.
        if not records:
            return
        with self._engine.begin() as conn:
            self.upsert_in(conn, records)

    def upsert_in(self, conn: Connection, records: Sequence[ChunkRecord]) -> None:
        """`upsert` inside a caller-owned transaction (tiered indexing replaces
        a version's chunks and FTS rows atomically)."""
        if not records:
            return
        # FTS5 has no native upsert-by-key: delete (by rowid) then re-insert.
        self._delete_ids(conn, [r.chunk_id for r in records])
        for record in records:
            result = conn.execute(
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
            conn.execute(
                text(
                    "INSERT OR REPLACE INTO fts_chunk_map "
                    "(chunk_id, fts_rowid, source_id, evidence_version_id) "
                    "VALUES (:chunk_id, :rowid, :source_id, :evidence_version_id)"
                ),
                {
                    "chunk_id": record.chunk_id,
                    "rowid": result.lastrowid,
                    "source_id": record.source_id,
                    "evidence_version_id": record.evidence_version_id,
                },
            )

    def delete_version_in(self, conn: Connection, evidence_version_id: str) -> None:
        """`delete_by_version` inside a caller-owned transaction."""
        self._delete_where(conn, "evidence_version_id", evidence_version_id)

    def delete(self, chunk_ids: Sequence[str]) -> None:
        chunk_ids = list(chunk_ids)
        if not chunk_ids:
            return
        with self._engine.begin() as conn:
            self._delete_ids(conn, chunk_ids)

    def delete_by_source(self, source_id: str) -> None:
        with self._engine.begin() as conn:
            self._delete_where(conn, "source_id", source_id)

    def delete_by_version(self, evidence_version_id: str) -> None:
        with self._engine.begin() as conn:
            self._delete_where(conn, "evidence_version_id", evidence_version_id)

    def existing_chunk_ids(self, chunk_ids: Sequence[str]) -> set[str]:
        chunk_ids = list(chunk_ids)
        if not chunk_ids:
            return set()
        found: set[str] = set()
        with self._engine.connect() as conn:
            for batch in _batches(chunk_ids):
                found.update(row[0] for row in conn.execute(_MAP_IDS, {"ids": batch}))
        return found

    def chunk_ids_for_source(self, source_id: str) -> set[str]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                text("SELECT chunk_id FROM fts_chunk_map WHERE source_id = :source_id"),
                {"source_id": source_id},
            )
            return {row[0] for row in rows}
