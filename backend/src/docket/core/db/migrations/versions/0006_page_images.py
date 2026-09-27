"""Store page-image content hashes on EvidenceVersion (visual retrieval CP2)."""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "f3a6b8c1d204"
down_revision: Union[str, Sequence[str], None] = "d9149687aa25"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("evidence_versions", sa.Column("page_images_json", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("evidence_versions", "page_images_json")
