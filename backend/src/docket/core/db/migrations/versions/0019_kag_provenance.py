"""KAG graph provenance: chunk-relative spans and an evidence kind on graph edges.

Additive. Adds three NULLable columns to kag_document_entities and kag_document_clauses
(span_start, span_end, evidence_kind). No defaults and no backfill: rows written before this
migration keep NULL = legacy / unverified, which readers must never present as a span.
Existing readers select explicit columns and are unaffected.

Downgrade drops the six columns (SQLite >= 3.35 ALTER TABLE DROP COLUMN; falls back to a batch
rebuild on older SQLite). Graph rows and their chunk ids are kept.
"""
from alembic import op
import sqlalchemy as sa

revision = "f6c9e3a18f47"
down_revision = "f6c9e3a18e36"
branch_labels = None
depends_on = None

_TABLES = ("kag_document_entities", "kag_document_clauses")
_COLUMNS = (("span_start", sa.Integer()), ("span_end", sa.Integer()), ("evidence_kind", sa.String()))


def upgrade() -> None:
    for table in _TABLES:
        for name, type_ in _COLUMNS:
            op.add_column(table, sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    for table in _TABLES:
        try:
            for name, _t in _COLUMNS:
                op.execute(f"ALTER TABLE {table} DROP COLUMN {name}")
        except Exception:  # noqa: BLE001 - SQLite older than 3.35
            with op.batch_alter_table(table) as batch:
                for name, _t in _COLUMNS:
                    batch.drop_column(name)
