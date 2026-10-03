"""Superseding a file's version removes the old version's index entries at
supersede time (FTS, vector, visual pages), not at end-of-run reconcile."""

from __future__ import annotations

from sqlalchemy import select

from conftest import _write_xlsx
from docket.core.db.engine import get_session_factory
from docket.core.db.models import EvidenceVersion, VersionStatus
from docket.infra.evidence.manager import EvidenceManager
from docket.infra.evidence.store import ContentAddressedStore
from docket.infra.index.fts_index import FtsIndexWriter
from docket.infra.index.manager import IndexManager
from docket.infra.index.vector_index import LanceIndexWriter
from docket.infra.index.visual_index import LancePageIndexWriter, PageRecord
from docket.infra.inference.gateway import FakeInferenceGateway
from docket.infra.parsing.docling_wrapper import DoclingParser
from docket.infra.parsing.recipes import DEFAULT_SPLITTER, ChunkRecipe
from docket.services.ingestion.pipeline import IngestionPipeline
from docket.services.sources.manager import SourceManager


def test_supersede_removes_old_version_fts_vector_and_pages(migrated_sqlite_engine, tmp_path):
    session_factory = get_session_factory(migrated_sqlite_engine)
    store_root = tmp_path / "evidence_store"
    for sub in ("objects", "manifests", "quarantine", "trash"):
        (store_root / sub).mkdir(parents=True, exist_ok=True)
    gateway = FakeInferenceGateway()
    fts = FtsIndexWriter(migrated_sqlite_engine)
    vector = LanceIndexWriter(tmp_path / "lancedb", engine=migrated_sqlite_engine)
    pages = LancePageIndexWriter(tmp_path / "lancedb")
    parser = DoclingParser()
    pipeline = IngestionPipeline(
        session_factory=session_factory,
        evidence_manager=EvidenceManager(ContentAddressedStore(store_root), session_factory),
        parser=parser,
        index_manager=IndexManager(fts, vector, gateway, pages),
        chunk_recipe=ChunkRecipe(
            chunk_size=200,
            overlap=40,
            splitter=DEFAULT_SPLITTER,
            parser_name=parser.parser_name,
            parser_version=parser.parser_version,
        ),
    )
    folder = tmp_path / "docs"
    folder.mkdir()
    source = SourceManager(session_factory).register_source(folder)
    path = folder / "book.xlsx"

    _write_xlsx(path, "S", ["name", "qty"], [["apples", 1]])
    pipeline.run_ingestion_for_source(source.id)
    with session_factory() as session:
        old_id = session.execute(select(EvidenceVersion.id)).scalar_one()
    old_chunks = fts.chunk_ids_for_source(source.id)
    assert old_chunks and vector.chunk_ids_for_source(source.id) == old_chunks
    # xlsx has no pages; seed one so the pages cleanup path is exercised.
    pages.upsert([PageRecord(old_id, source.id, 1, "p")], [gateway.embed("p")])

    # Change content, then ingest only the new bytes through the per-file
    # entrypoint, so end-of-run reconcile cannot be what cleans up.
    _write_xlsx(path, "S", ["name", "qty"], [["pears", 2]])
    pipeline._ingest_one_file(source.id, path)

    with session_factory() as session:
        statuses = {
            v.id: v.status for v in session.execute(select(EvidenceVersion)).scalars().all()
        }
    assert statuses[old_id] == VersionStatus.SUPERSEDED
    new_chunks = fts.chunk_ids_for_source(source.id)
    assert new_chunks and not (new_chunks & old_chunks)
    assert vector.chunk_ids_for_source(source.id) == new_chunks
    assert pages.version_ids_for_source(source.id) == set()
