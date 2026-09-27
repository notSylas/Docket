"""Store unverified formula-region VLM transcriptions (Phase B checkpoint 1)."""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "e9d307126406"
down_revision: Union[str, Sequence[str], None] = "f3a6b8c1d204"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "evidence_versions", sa.Column("formula_transcriptions_json", sa.Text(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("evidence_versions", "formula_transcriptions_json")
