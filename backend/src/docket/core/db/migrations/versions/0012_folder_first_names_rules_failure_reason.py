"""Folder-first foundation: source names/rules, version failure reason and mtime

Adds four nullable columns (no backfill, no data rewritten):

- `sources.display_name`: optional user-facing name; NULL means "derive from
  the folder name" (see `services/sources/resolve.py`).
- `sources.rules_json`: JSON text, `{"version": 1, "exclude": [...],
  "include": [...], "ignored_files": [...]}` -- per-source discovery rules
  layered over the built-in ignore rules and `.docketignore`.
- `evidence_versions.failure_reason`: short plain-language reason recorded
  when a version becomes FAILED.
- `evidence_versions.source_mtime`: the file's mtime (epoch seconds) when the
  version was ingested, for cheap change detection.

Revision ID: b8e1f3a45c92
Revises: a7d2e4c18b5f
Create Date: 2026-10-10 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b8e1f3a45c92"
down_revision: Union[str, Sequence[str], None] = "a7d2e4c18b5f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("sources", sa.Column("display_name", sa.String(), nullable=True))
    op.add_column("sources", sa.Column("rules_json", sa.Text(), nullable=True))
    op.add_column("evidence_versions", sa.Column("failure_reason", sa.String(), nullable=True))
    op.add_column("evidence_versions", sa.Column("source_mtime", sa.Float(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("evidence_versions") as batch:
        batch.drop_column("source_mtime")
        batch.drop_column("failure_reason")
    with op.batch_alter_table("sources") as batch:
        batch.drop_column("rules_json")
        batch.drop_column("display_name")
