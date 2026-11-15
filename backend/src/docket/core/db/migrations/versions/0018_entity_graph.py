"""Entity graph (KAG-lite): entities, document->entity edges, document->clause->chunk links.

Additive. Every edge carries the source chunk id so answers cite real text.
Rows hang off `documents` with ON DELETE CASCADE, so purge/supersede paths that
already delete a document row clean the graph up too. Entities are shared across
documents (one row per normalised name); unreferenced entities are harmless.
"""
from alembic import op

revision = "f6c9e3a18e36"
down_revision = "f6c9e3a18d25"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE TABLE IF NOT EXISTS kag_entities ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, norm TEXT NOT NULL UNIQUE, display TEXT NOT NULL, "
        "kind TEXT NOT NULL DEFAULT 'party')"
    )
    op.execute(
        "CREATE TABLE IF NOT EXISTS kag_entity_variants ("
        "entity_id INTEGER NOT NULL REFERENCES kag_entities(id) ON DELETE CASCADE, "
        "variant TEXT NOT NULL, PRIMARY KEY (entity_id, variant))"
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_kag_entity_variants_variant ON kag_entity_variants(variant)")
    op.execute(
        "CREATE TABLE IF NOT EXISTS kag_document_entities ("
        "document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE, "
        "entity_id INTEGER NOT NULL REFERENCES kag_entities(id) ON DELETE CASCADE, "
        "role TEXT NOT NULL DEFAULT 'party', chunk_id TEXT, "
        "PRIMARY KEY (document_id, entity_id, role))"
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_kag_document_entities_entity ON kag_document_entities(entity_id)")
    op.execute(
        "CREATE TABLE IF NOT EXISTS kag_document_clauses ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE, "
        "clause_type TEXT NOT NULL, chunk_id TEXT NOT NULL, confidence FLOAT NOT NULL DEFAULT 0.6)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_kag_document_clauses_doc_type ON kag_document_clauses(document_id, clause_type)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS kag_document_clauses")
    op.execute("DROP TABLE IF EXISTS kag_document_entities")
    op.execute("DROP TABLE IF EXISTS kag_entity_variants")
    op.execute("DROP TABLE IF EXISTS kag_entities")
