"""evidence_versions.status (replaces is_current)

Upgrade doc 03 section 4: a single `status` lifecycle enum
(PENDING/READY/FAILED/SUPERSEDED) replaces the `is_current` boolean on
`EvidenceVersion`. A boolean could only ever distinguish "current" from
"not current" -- it had no way to represent a version whose processing
hadn't finished yet (PENDING) or had raised (FAILED) as anything other
than indistinguishable from "superseded". The new enum makes those three
outcomes explicit, which `EvidenceManager`/`IngestionPipeline` need to
retry a `FAILED` version on the next ingest of the same content instead of
silently treating it as an unchanged no-op forever.

Backfill is deterministic, per doc 03 section 4/10: `is_current = True` ->
`READY` (it was already being served, so it's already known-good), and
`is_current = False` -> `SUPERSEDED` (the old boolean never distinguished a
superseded-but-failed row from a cleanly superseded one, so collapsing both
to SUPERSEDED loses no information that was actually tracked).

Revision ID: 0f0bcf448de1
Revises: e9d307126406
Create Date: 2026-10-02 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0f0bcf448de1"
down_revision: Union[str, Sequence[str], None] = "e9d307126406"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "evidence_versions",
        sa.Column(
            "status",
            sa.Enum("PENDING", "READY", "FAILED", "SUPERSEDED", name="version_status"),
            nullable=False,
            server_default="PENDING",
        ),
    )
    op.execute("UPDATE evidence_versions SET status = 'READY' WHERE is_current")
    op.execute("UPDATE evidence_versions SET status = 'SUPERSEDED' WHERE NOT is_current")
    op.drop_column("evidence_versions", "is_current")


def downgrade() -> None:
    op.add_column(
        "evidence_versions",
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.execute("UPDATE evidence_versions SET is_current = (status = 'READY')")
    op.drop_column("evidence_versions", "status")
