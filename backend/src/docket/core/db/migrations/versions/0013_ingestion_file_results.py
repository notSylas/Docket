"""Add durable per-file ingestion job results (no existing rows rewritten)."""
from alembic import op
import sqlalchemy as sa

revision = "c9f2a4b56d03"
down_revision = "b8e1f3a45c92"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ingestion_file_results",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("job_id", sa.String(), sa.ForeignKey("ingestion_jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("outcome", sa.String(), nullable=False),
        sa.Column("chunks_written", sa.Integer(), nullable=False),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("job_id", "path", name="uq_ingestion_file_job_path"),
    )
    op.create_index("ix_ingestion_file_results_job_id", "ingestion_file_results", ["job_id"])


def downgrade() -> None:
    op.drop_index("ix_ingestion_file_results_job_id", table_name="ingestion_file_results")
    op.drop_table("ingestion_file_results")
