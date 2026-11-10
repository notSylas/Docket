"""Ingestion hook: document rows are written only when the flag is on, and are
removed when a changed file supersedes its earlier version."""
from __future__ import annotations

from sqlalchemy import text

from docket.core.config import settings as singleton_settings
from docket.services.documents import builder

BODY = ("DISTRIBUTOR AGREEMENT\n\nThis Distributor Agreement is made as of May 9, 2019 (the \"Effective Date\") by and between "
        "Zogenix, Inc., a Delaware corporation, and Acme Distribution LLC, whereas the parties wish to cooperate.\n\n"
        "This Agreement shall be governed by the laws of the State of New York.\n")


def _count(env, table="documents"):
    with env.session_factory() as s:
        return s.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()


def test_hook_off_by_default_then_on_and_supersede(env, monkeypatch):
    f = env.folder / "Zogenix Distributor Agreement.txt"
    f.write_text(BODY)
    assert not singleton_settings.documents_build_enabled
    env.pipeline.run_ingestion_for_source(env.source.id)
    assert _count(env) == 0  # flag off: no rows, ingestion unchanged

    monkeypatch.setattr(singleton_settings, "documents_build", True)
    f.write_text(BODY + "\nExtra clause.\n")
    env.pipeline.run_ingestion_for_source(env.source.id)
    assert _count(env) == 1 and _count(env, "fts_documents") == 1
    with env.session_factory() as s:
        row = s.execute(text("SELECT doc_type, parties_json FROM documents")).one()
        gl = s.execute(text("SELECT normalized FROM document_fields WHERE field='governing_law'")).scalar_one()
    assert row[0] == "agreement" and "Zogenix" in row[1] and gl == "New York"

    # content change supersedes the old version: still exactly one row, for the new one
    f.write_text(BODY + "\nAnother change.\n")
    env.pipeline.run_ingestion_for_source(env.source.id)
    assert _count(env) == 1
    # backfill is idempotent
    assert builder.build_all(env.session_factory).built == 0
