"""evidence_versions.file_path

CP8 introduces "local_folder" sources that can contain many files under one
`Source` row (see `docket.services.sources.manager.SourceManager.register_source` /
`docket.services.ingestion.pipeline.IngestionPipeline`). `EvidenceManager`'s
"current version" lookup was previously scoped only by `source_id`, which is
correct for a source that maps to exactly one file but silently wrong for a
multi-file folder source: ingesting file B under the same source would flip
file A's still-current, still-unchanged version to `is_current=False`
(whichever file was ingested last "wins" the source's one `is_current` slot),
breaking idempotent re-ingestion for every file but the most recently seen
one.

This migration adds a nullable `file_path` column to `evidence_versions` so
`EvidenceManager` can scope "current version" lookups by (source_id,
file_path) instead of `source_id` alone, while staying backward compatible
with any row that predates this column (nullable, no backfill needed --
CP1-CP7 only ever exercised one-file-per-source, so no existing row's
"current" semantics change).

Revision ID: b3f1c9a02d17
Revises: 9432eff2dba5
Create Date: 2026-09-22 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b3f1c9a02d17'
down_revision: Union[str, Sequence[str], None] = '9432eff2dba5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('evidence_versions', sa.Column('file_path', sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column('evidence_versions', 'file_path')
