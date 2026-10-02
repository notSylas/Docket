"""Source lifecycle fields + evidence_blob_references

Upgrade doc 03 sections 6 and 8: implements the rest of the `SourceStatus`
transition graph (ACTIVE/MISSING/REVOKED/TOMBSTONED/HARD_DELETE_PENDING/
DELETED) and blob reference counting for safe garbage collection.

Adds:

- `sources.sync_paused_at` (nullable DateTime): orthogonal "paused" flag --
  a paused source is still ACTIVE and servable on its last-synced content,
  so this deliberately is NOT a `SourceStatus` value (section 6).
- `sources.retention_deadline` (nullable DateTime): set to
  `now + <configured retention window>` on entry to TOMBSTONED; a
  `SourceManager.sweep_expired_retentions()` sweep advances expired rows to
  HARD_DELETE_PENDING.
- `sources.status_reason` (nullable String): records *why* the current
  status was set, e.g. distinguishing a user-initiated disconnect from a
  connector-detected access-loss REVOKED, or the tombstone reason left
  behind once a purge completes -- without a new `SourceStatus` value per
  cause (section 6's explicit decision to reuse REVOKED for both disconnect
  and access-loss).
- `evidence_blob_references` (new table): one row per live reference from
  some other row into the `ContentAddressedStore`, keyed by `content_hash`.
  Turns "is this blob still referenced" into a single indexed COUNT query
  instead of a full-table JSON scan of `page_images_json` (section 8).

No backfill needed: every column is nullable (or, for the new table, simply
starts empty) -- no existing row has ever been paused, tombstoned, or had a
blob reference tracked before this checkpoint.

Revision ID: c4753b731e9c
Revises: f9749e87a0e7
Create Date: 2026-10-03 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c4753b731e9c"
down_revision: Union[str, Sequence[str], None] = "f9749e87a0e7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("sources", sa.Column("sync_paused_at", sa.DateTime(), nullable=True))
    op.add_column("sources", sa.Column("retention_deadline", sa.DateTime(), nullable=True))
    op.add_column("sources", sa.Column("status_reason", sa.String(), nullable=True))

    op.create_table(
        "evidence_blob_references",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("content_hash", sa.String(), nullable=False),
        sa.Column("referencing_table", sa.String(), nullable=False),
        sa.Column("referencing_id", sa.String(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "content_hash",
            "referencing_table",
            "referencing_id",
            "role",
            name="uq_evidence_blob_references_tuple",
        ),
    )
    op.create_index(
        op.f("ix_evidence_blob_references_content_hash"),
        "evidence_blob_references",
        ["content_hash"],
        unique=False,
    )
    op.create_index(
        "ix_evidence_blob_references_referencing",
        "evidence_blob_references",
        ["referencing_table", "referencing_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_evidence_blob_references_referencing", table_name="evidence_blob_references"
    )
    op.drop_index(
        op.f("ix_evidence_blob_references_content_hash"), table_name="evidence_blob_references"
    )
    op.drop_table("evidence_blob_references")
    op.drop_column("sources", "status_reason")
    op.drop_column("sources", "retention_deadline")
    op.drop_column("sources", "sync_paused_at")
