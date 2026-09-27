"""Store per-chunk page provenance (page_start/page_end) without a re-chunk.

Nullable on purpose, same posture as 0004's `formula_regions_json`: existing
`Chunk` rows stay NULL until the next natural re-ingest re-derives them from
page-marker-annotated text (see `docket.parsing.chunker`); nothing backfills
them retroactively.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "d9149687aa25"
down_revision: Union[str, Sequence[str], None] = "c84a9d115a40"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("chunks", sa.Column("page_start", sa.Integer(), nullable=True))
    op.add_column("chunks", sa.Column("page_end", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("chunks", "page_end")
    op.drop_column("chunks", "page_start")
