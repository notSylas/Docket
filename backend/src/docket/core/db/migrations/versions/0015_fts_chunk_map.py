"""Add `fts_chunk_map` so FTS5 deletes and existence checks are O(log n).

`fts_chunks.chunk_id` is an UNINDEXED FTS5 column, so deleting or looking up a
chunk by id scanned the whole FTS table (ingest cost grew quadratically with
library size). This additive migration creates a shadow table with a PRIMARY
KEY on chunk_id mapping to the FTS rowid (plus indexes on source/version) and
backfills it from the existing FTS rows. Search results are untouched: the
FTS table itself is not modified, except that duplicate rows for one chunk_id
(which the writer never produces) are collapsed to the newest.

Rehearse on a copy: the backfill is a single INSERT ... SELECT and the whole
migration is idempotent (CREATE ... IF NOT EXISTS, map is cleared first).
"""
from alembic import op

revision = "e5b8d2f07a31"
down_revision = "d1a7c3e58b14"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE TABLE IF NOT EXISTS fts_chunk_map ("
        "chunk_id TEXT PRIMARY KEY, fts_rowid INTEGER NOT NULL, "
        "source_id TEXT, evidence_version_id TEXT)"
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_fts_chunk_map_source ON fts_chunk_map(source_id)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_fts_chunk_map_version ON fts_chunk_map(evidence_version_id)"
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_fts_chunk_map_rowid ON fts_chunk_map(fts_rowid)")
    op.execute("DELETE FROM fts_chunk_map")
    op.execute(
        "DELETE FROM fts_chunks WHERE rowid NOT IN "
        "(SELECT MAX(rowid) FROM fts_chunks GROUP BY chunk_id)"
    )
    op.execute(
        "INSERT INTO fts_chunk_map (chunk_id, fts_rowid, source_id, evidence_version_id) "
        "SELECT chunk_id, rowid, source_id, evidence_version_id FROM fts_chunks"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS fts_chunk_map")
