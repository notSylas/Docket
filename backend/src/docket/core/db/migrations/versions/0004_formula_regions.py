"""Store detected formula page regions without promoting OCR equations to evidence."""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "c84a9d115a40"
down_revision: Union[str, Sequence[str], None] = "777be29455a6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("evidence_versions", sa.Column("formula_regions_json", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("evidence_versions", "formula_regions_json")
