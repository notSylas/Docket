"""fts_chunks porter stemming

M2 of the accuracy milestone (see the plan's "Keyword search fix" section):
the baseline eval showed 11/11 classifiable failures were `retrieval_miss`,
partly because `fts_chunks` used FTS5's bare default tokenizer (`unicode61`,
no stemming). A question phrased "journeys" doesn't match indexed text that
only contains "journey", and vice versa.

This migration drops and recreates `fts_chunks` with
``tokenize='porter unicode61 remove_diacritics 2'`` -- the `porter` wrapper
stems tokens (journey/journeys/journeying all index to the same root) before
handing them to `unicode61` (with accent-folding) for the actual
tokenization/matching. FTS5 has no `ALTER ... tokenize`, so the only way to
change a virtual table's tokenizer is drop-and-recreate; hyphenated ids like
"J-01" or "NFR-003" already split into separate tokens ("j"/"01",
"nfr"/"003") under plain `unicode61` (hyphen is a separator, not a token
char, and this migration doesn't change that), so re-tokenizing preserves the
same splitting behavior application code already assumes -- see
`docket.retrieval.hybrid._sanitize_fts_query`, which tokenizes queries the
same way (`\\w+`, splitting on the hyphen too) so ingest-side and query-side
tokenization stay symmetric.

`fts_chunks` has no `source_id`/foreign keys of its own (see
`docket.index.fts_index`'s module docstring), so the safe backfill is a
straight copy from the `chunks` table (the source of truth for chunk text)
keyed by `id`/`text` -- every row in `fts_chunks` before this migration is,
by construction, a 1:1 mirror of a `chunks` row (`FtsIndexWriter.upsert`
never diverges from it), so re-deriving `fts_chunks` from `chunks` loses
nothing.

Caveat for existing installs: `docket.cli.context.AppContext` only runs
Alembic migrations when the SQLite file doesn't exist yet
(`_ensure_schema`); an existing user's DB is not auto-upgraded to this
revision by any current CLI command. That's a pre-existing gap (it applies
equally to 0002's `file_path` column), not something this migration
introduces or fixes.

Revision ID: 777be29455a6
Revises: b3f1c9a02d17
Create Date: 2026-09-26 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '777be29455a6'
down_revision: Union[str, Sequence[str], None] = 'b3f1c9a02d17'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("DROP TABLE fts_chunks")
    op.execute(
        "CREATE VIRTUAL TABLE fts_chunks USING fts5("
        "chunk_id UNINDEXED, text, "
        "tokenize='porter unicode61 remove_diacritics 2')"
    )
    # Backfill from `chunks`, the source of truth for chunk text -- every
    # pre-migration `fts_chunks` row is a 1:1 mirror of one `chunks` row.
    op.execute("INSERT INTO fts_chunks (chunk_id, text) SELECT id, text FROM chunks")


def downgrade() -> None:
    op.execute("DROP TABLE fts_chunks")
    op.execute("CREATE VIRTUAL TABLE fts_chunks USING fts5(chunk_id UNINDEXED, text)")
    op.execute("INSERT INTO fts_chunks (chunk_id, text) SELECT id, text FROM chunks")
