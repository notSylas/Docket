"""EvidenceUnit.unit_kind/locator_json, Chunk.provenance (sub-file granularity)

Upgrade doc 03 section 7: evidence granularity below the file level needs a
typed location (cell, slide, message), not just a markdown-section heading,
and retrieval needs to distinguish a literal extracted value from a
Docket-computed derivation or a generated interpretation.

Three new, additive, nullable-or-defaulted columns -- no backfill logic
beyond a column default, since every row that exists today (and every row
written by the current Docling-markdown-section path) is exactly the
'section'/'extracted' case these defaults represent:

- `evidence_units.unit_kind` (plain string, NOT a DB enum -- this set is
  expected to grow with each future adapter, e.g. 'cell'/'range'/'slide'/
  'message' -- see section 7's closing paragraph): not null, defaults to
  'section', the only kind that exists today.
- `evidence_units.locator_json` (nullable Text, JSON-encoded, same storage
  convention as `formula_regions_json`/`page_images_json`): NULL for the
  existing 'section' kind, since a markdown heading section doesn't have a
  structured locator -- `heading` already serves as its human-readable
  label.
- `chunks.provenance` (plain string, NOT a DB enum -- 'extracted'/'derived'/
  'generated' are today's three values but are conceptual categories that
  may need refinement later): not null, defaults to 'extracted', since every
  chunk today is produced only from parsed document text (see
  `docket.infra.parsing.chunker`) -- `derived`/`generated` only become
  reachable once a future adapter actually produces them.

Revision ID: f9749e87a0e7
Revises: 0f0bcf448de1
Create Date: 2026-10-02 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "f9749e87a0e7"
down_revision: Union[str, Sequence[str], None] = "0f0bcf448de1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "evidence_units",
        sa.Column("unit_kind", sa.String(), nullable=False, server_default="section"),
    )
    op.add_column(
        "evidence_units",
        sa.Column("locator_json", sa.Text(), nullable=True),
    )
    op.add_column(
        "chunks",
        sa.Column("provenance", sa.String(), nullable=False, server_default="extracted"),
    )


def downgrade() -> None:
    op.drop_column("chunks", "provenance")
    op.drop_column("evidence_units", "locator_json")
    op.drop_column("evidence_units", "unit_kind")
