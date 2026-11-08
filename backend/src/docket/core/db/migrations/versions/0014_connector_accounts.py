"""Add read-only mail connector accounts (additive; no secrets stored)."""
from alembic import op
import sqlalchemy as sa

revision = "d1a7c3e58b14"
down_revision = "c9f2a4b56d03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "connector_accounts",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("account_label", sa.String(), nullable=False),
        sa.Column("source_id", sa.String(), sa.ForeignKey("sources.id"), nullable=True),
        sa.Column("config_json", sa.Text(), nullable=False),
        sa.Column("cursor_json", sa.Text(), nullable=True),
        sa.Column("credential_ref", sa.String(), nullable=True),
        sa.Column("last_sync_at", sa.DateTime(), nullable=True),
        sa.Column("consent_at", sa.DateTime(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.UniqueConstraint("provider", "account_label", name="uq_connector_provider_label"),
    )
    op.create_index("ix_connector_accounts_source_id", "connector_accounts", ["source_id"])


def downgrade() -> None:
    op.drop_index("ix_connector_accounts_source_id", table_name="connector_accounts")
    op.drop_table("connector_accounts")
