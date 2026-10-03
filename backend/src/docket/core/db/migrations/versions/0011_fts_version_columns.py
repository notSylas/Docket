"""fts_chunks carries source_id and evidence_version_id

Upgrade doc 04 section 6: derived index rows must be attributable to exactly
one evidence version (and source), so superseded versions can be removed
directly and reconcile/purge can enumerate FTS rows without going through the
vector table.

FTS5 cannot ALTER a virtual table, so this drops and recreates `fts_chunks`
(same tokenizer as 0003) with two extra `UNINDEXED` columns -- they are
stored for filtering/deletion, never tokenized -- and backfills them from
`chunks`, the source of truth. Rows in `fts_chunks` with no matching `chunks`
row (FTS-only orphans) are not carried over, which is the desired cleanup.

`source_id` is stored alongside `evidence_version_id` (rather than resolved by
a join to `evidence_versions`) so a purge can still enumerate a source's FTS
rows after its version rows have been deleted.

Revision ID: a7d2e4c18b5f
Revises: c4753b731e9c
Create Date: 2026-10-03 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "a7d2e4c18b5f"
down_revision: Union[str, Sequence[str], None] = "c4753b731e9c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("DROP TABLE fts_chunks")
    op.execute(
        "CREATE VIRTUAL TABLE fts_chunks USING fts5("
        "chunk_id UNINDEXED, text, evidence_version_id UNINDEXED, source_id UNINDEXED, "
        "tokenize='porter unicode61 remove_diacritics 2')"
    )
    op.execute(
        "INSERT INTO fts_chunks (chunk_id, text, evidence_version_id, source_id) "
        "SELECT id, text, evidence_version_id, source_id FROM chunks"
    )


def downgrade() -> None:
    op.execute("DROP TABLE fts_chunks")
    op.execute(
        "CREATE VIRTUAL TABLE fts_chunks USING fts5("
        "chunk_id UNINDEXED, text, "
        "tokenize='porter unicode61 remove_diacritics 2')"
    )
    op.execute("INSERT INTO fts_chunks (chunk_id, text) SELECT id, text FROM chunks")
