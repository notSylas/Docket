"""Document layer: `documents`, `document_fields` and a document-level FTS5 index.

Additive. `fts_documents` uses the rowid pattern (rowid = documents.id) so no
lookup or delete ever scans an UNINDEXED column; triggers keep it in step with
`documents` for every insert/update/delete path (ORM cascade, purge, rebuild).
"""
from alembic import op

revision = "f6c9e3a18b42"
down_revision = "e5b8d2f07a31"
branch_labels = None
depends_on = None

FTS_TOKENIZE = "porter unicode61 remove_diacritics 2"


def upgrade() -> None:
    op.execute(
        "CREATE TABLE IF NOT EXISTS documents ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "evidence_version_id TEXT NOT NULL UNIQUE REFERENCES evidence_versions(id) ON DELETE CASCADE, "
        "source_id TEXT NOT NULL, file_name TEXT, doc_type TEXT NOT NULL DEFAULT 'other', "
        "title TEXT, parties_json TEXT, dates_json TEXT, page_count INTEGER, family_key TEXT, "
        "source_mtime FLOAT, doc_text TEXT NOT NULL DEFAULT '', summary TEXT, "
        "supersedes_document_id INTEGER, supersedes_kind TEXT, extractor_version TEXT NOT NULL DEFAULT '1')"
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_documents_source_id ON documents(source_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_documents_family_key ON documents(family_key)")
    op.execute(
        "CREATE TABLE IF NOT EXISTS document_fields ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE, "
        "field TEXT NOT NULL, value TEXT, normalized TEXT, evidence_chunk_id TEXT, char_span TEXT, "
        "confidence FLOAT NOT NULL DEFAULT 0, extractor_version TEXT NOT NULL DEFAULT '1')"
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_document_fields_document_id ON document_fields(document_id)")
    op.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS fts_documents USING fts5(doc_text, meta, tokenize='{FTS_TOKENIZE}')"
    )
    op.execute(
        "CREATE TRIGGER IF NOT EXISTS documents_fts_ai AFTER INSERT ON documents BEGIN "
        "INSERT INTO fts_documents(rowid, doc_text, meta) VALUES (new.id, new.doc_text, "
        "coalesce(new.file_name,'') || ' ' || coalesce(new.title,'') || ' ' || coalesce(new.parties_json,'')); END"
    )
    op.execute(
        "CREATE TRIGGER IF NOT EXISTS documents_fts_ad AFTER DELETE ON documents BEGIN "
        "DELETE FROM fts_documents WHERE rowid = old.id; END"
    )
    op.execute(
        "CREATE TRIGGER IF NOT EXISTS documents_fts_au AFTER UPDATE ON documents BEGIN "
        "DELETE FROM fts_documents WHERE rowid = old.id; "
        "INSERT INTO fts_documents(rowid, doc_text, meta) VALUES (new.id, new.doc_text, "
        "coalesce(new.file_name,'') || ' ' || coalesce(new.title,'') || ' ' || coalesce(new.parties_json,'')); END"
    )


def downgrade() -> None:
    for trig in ("documents_fts_ai", "documents_fts_ad", "documents_fts_au"):
        op.execute(f"DROP TRIGGER IF EXISTS {trig}")
    op.execute("DROP TABLE IF EXISTS fts_documents")
    op.execute("DROP TABLE IF EXISTS document_fields")
    op.execute("DROP TABLE IF EXISTS documents")
