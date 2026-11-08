"""Tiered indexing: provisional chunks, replacement aliases, persisted work queue.

Additive only. `evidence_versions.status` gains the value TEXT_READY, which
needs no schema change (SQLAlchemy's Enum is stored as VARCHAR without a CHECK
constraint on SQLite). Existing rows keep `provisional = 0`.

Downgrade removes the three additions; any TEXT_READY versions are first
returned to PENDING and their provisional chunks/FTS rows dropped, so an older
build never sees a status or chunk it cannot serve.
"""
from alembic import op
import sqlalchemy as sa

revision = "f6c9e3a18d25"
down_revision = "e5b8d2f07a31"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chunks",
        sa.Column("provisional", sa.Boolean(), nullable=False, server_default="0"),
    )
    op.create_table(
        "chunk_aliases",
        sa.Column("old_chunk_id", sa.String(), primary_key=True),
        sa.Column("new_chunk_id", sa.String(), nullable=True),
        sa.Column("evidence_version_id", sa.String(), nullable=False),
        sa.Column("overlap", sa.Float(), nullable=False, server_default="0"),
    )
    op.create_index("ix_chunk_aliases_evidence_version_id", "chunk_aliases", ["evidence_version_id"])
    op.create_table(
        "ingest_queue",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("source_id", sa.String(), sa.ForeignKey("sources.id"), nullable=False),
        sa.Column("file_path", sa.Text(), nullable=False),
        sa.Column("tier", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(), nullable=False, server_default="queued"),
        sa.Column("prio_class", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("mtime", sa.Float(), nullable=False, server_default="0"),
        sa.Column("recency_day", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("byte_size", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("not_before", sa.Float(), nullable=False, server_default="0"),
        sa.Column("enqueued_at", sa.Float(), nullable=False, server_default="0"),
        sa.Column("started_at", sa.Float(), nullable=True),
        sa.Column("finished_at", sa.Float(), nullable=True),
        sa.Column("duration_s", sa.Float(), nullable=True),
        sa.UniqueConstraint("source_id", "file_path", "tier", name="uq_ingest_queue_item"),
    )
    op.create_index("ix_ingest_queue_source_id", "ingest_queue", ["source_id"])
    # Mixed-direction index matching the claim ORDER BY exactly.
    op.execute(
        "CREATE INDEX ix_ingest_queue_claim ON ingest_queue "
        "(tier, state, prio_class, recency_day DESC, byte_size, mtime DESC, id)"
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM fts_chunks WHERE rowid IN (SELECT fts_rowid FROM fts_chunk_map WHERE "
        "chunk_id IN (SELECT id FROM chunks WHERE provisional))"
    )
    op.execute(
        "DELETE FROM fts_chunk_map WHERE chunk_id IN (SELECT id FROM chunks WHERE provisional)"
    )
    op.execute(
        "UPDATE evidence_versions SET status = 'PENDING' WHERE status = 'TEXT_READY'"
    )
    op.execute("DELETE FROM chunks WHERE provisional")
    op.execute(
        "DELETE FROM evidence_units WHERE evidence_version_id IN "
        "(SELECT id FROM evidence_versions WHERE status = 'PENDING') AND id NOT IN "
        "(SELECT evidence_unit_id FROM chunks)"
    )
    op.execute("DROP INDEX IF EXISTS ix_ingest_queue_claim")
    op.drop_index("ix_ingest_queue_source_id", table_name="ingest_queue")
    op.drop_table("ingest_queue")
    op.drop_index("ix_chunk_aliases_evidence_version_id", table_name="chunk_aliases")
    op.drop_table("chunk_aliases")
    with op.batch_alter_table("chunks") as batch:
        batch.drop_column("provisional")
