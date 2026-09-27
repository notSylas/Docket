"""Page-image/visual-index/page-span tests for the ingestion pipeline --
visual retrieval checkpoints 1 and 2: `Chunk.page_start`/`page_end`
provenance, `EvidenceVersion.page_images_json` storage, and the
`settings.visual_index_enabled`-gated VLM description + `pages` LanceDB
table write. Covers both the full `IngestionPipeline` orchestration
(moved from the original single test file) and, separately, `VisualIndexer`
constructed directly (see the bottom of this file) to prove the split
bought independent testability.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from sqlalchemy import select as sa_select

from conftest import _write_docx
from docket.core.config import Settings
from docket.core.db.engine import get_session_factory
from docket.core.db.models import Chunk, EvidenceVersion
from docket.infra.evidence.manager import EvidenceManager
from docket.infra.evidence.store import ContentAddressedStore
from docket.infra.index.fts_index import FtsIndexWriter
from docket.infra.index.manager import IndexManager
from docket.infra.index.vector_index import LanceIndexWriter
from docket.infra.index.visual_index import LancePageIndexWriter
from docket.infra.inference.gateway import FakeInferenceGateway
from docket.services.ingestion.pipeline import IngestionPipeline
from docket.services.ingestion.visual_indexer import VisualIndexer
from docket.infra.parsing.docling_wrapper import ParsedDocument
from docket.infra.parsing.recipes import DEFAULT_SPLITTER, ChunkRecipe
from docket.services.sources.manager import SourceManager

# ---------------------------------------------------------------------------
# Page provenance (Chunk.page_start/page_end) -- visual retrieval checkpoint 1.
# ---------------------------------------------------------------------------


def test_chunk_page_start_and_page_end_persist_and_survive_readback(env, monkeypatch):
    """A mocked multi-page parse (same fixture-injection technique as the
    formula-regions test) proves page_start/page_end make it all the way
    from `ParsedDocument.text_with_page_markers` through `chunk_document`
    into real `Chunk` rows in the database, and that they read back
    correctly from a *fresh* session (not just an in-memory artifact of the
    write)."""
    path = env.folder / "multipage.docx"
    _write_docx(path, "Multi", "placeholder body")

    # Markers sit right before each heading (mirroring how docling_wrapper
    # inserts a marker right before the first matched text item on a new
    # page), so each section here lands entirely on one page.
    annotated = (
        "<!--PAGE:1-->\n"
        "# Section One\n"
        "Alpha bravo charlie delta echo foxtrot golf hotel india juliet.\n\n"
        "<!--PAGE:2-->\n"
        "# Section Two\n"
        "Kilo lima mike november oscar papa quebec romeo sierra tango.\n"
    )
    plain = annotated.replace("<!--PAGE:1-->", "").replace("<!--PAGE:2-->", "")
    parsed = ParsedDocument(
        text=plain,
        source_path=path,
        parser_name="fixture",
        parser_version="1",
        text_with_page_markers=annotated,
    )

    def parse(source_id, source_path):
        return parsed

    monkeypatch.setattr(env.pipeline._parser, "parse", parse)
    result = env.pipeline.run_ingestion_for_source(env.source.id)
    assert result.status == "succeeded"

    with env.session_factory() as session:
        rows = session.execute(
            sa_select(Chunk.heading, Chunk.page_start, Chunk.page_end).order_by(Chunk.ordinal)
        ).all()
    assert rows == [("Section One", 1, 1), ("Section Two", 2, 2)]
    for heading, page_start, page_end in rows:
        assert page_start is not None and page_end is not None

    # Fresh session: proves this round-tripped through the DB, not just an
    # artifact of the write session's identity map.
    with env.session_factory() as session:
        readback = session.execute(
            sa_select(Chunk.page_start, Chunk.page_end).order_by(Chunk.ordinal)
        ).all()
    assert readback == [(1, 1), (2, 2)]


def test_chunk_page_span_is_none_when_parser_gives_no_marker_info(env, monkeypatch):
    """A `ParsedDocument` built without `text_with_page_markers` (its
    default) must chunk exactly as before this checkpoint: every chunk gets
    page_start=page_end=None rather than a guessed page number."""
    path = env.folder / "nopage.docx"
    _write_docx(path, "NoPage", "placeholder body")
    parsed = ParsedDocument(
        text="# Heading\nSome ordinary content with no page info at all.",
        source_path=path,
        parser_name="fixture",
        parser_version="1",
    )

    def parse(source_id, source_path):
        return parsed

    monkeypatch.setattr(env.pipeline._parser, "parse", parse)
    result = env.pipeline.run_ingestion_for_source(env.source.id)
    assert result.status == "succeeded"

    with env.session_factory() as session:
        rows = session.execute(sa_select(Chunk.page_start, Chunk.page_end)).all()
    assert rows
    assert all(page_start is None and page_end is None for page_start, page_end in rows)


# ---------------------------------------------------------------------------
# Page images (EvidenceVersion.page_images_json + visual index) --
# visual retrieval checkpoint 2.
# ---------------------------------------------------------------------------


def _multipage_parsed_document(path, page_images: dict[int, bytes]):
    return ParsedDocument(
        text="# Section One\nAlpha content.\n# Section Two\nBeta content.\n",
        source_path=path,
        parser_name="fixture",
        parser_version="1",
        page_images=page_images,
    )


def test_page_images_stored_and_indexed_when_visual_index_enabled(
    migrated_sqlite_engine, tmp_path, parser, monkeypatch
):
    """With `visual_index_enabled=True` and a `FakeInferenceGateway`,
    ingesting a small multi-page fixture must: populate
    `EvidenceVersion.page_images_json`, make each page's bytes retrievable
    from the `ContentAddressedStore`, and write rows (with the fake
    gateway's canned description) to the pages LanceDB table."""
    session_factory = get_session_factory(migrated_sqlite_engine)
    store_root = tmp_path / "evidence_store"
    for sub in ("objects", "manifests", "quarantine", "trash"):
        (store_root / sub).mkdir(parents=True, exist_ok=True)
    store = ContentAddressedStore(store_root)
    evidence_manager = EvidenceManager(store, session_factory)

    gateway = FakeInferenceGateway(canned_description="a page describing penguins")
    fts = FtsIndexWriter(migrated_sqlite_engine)
    vector = LanceIndexWriter(tmp_path / "lancedb")
    visual_writer = LancePageIndexWriter(tmp_path / "lancedb")
    index_manager = IndexManager(fts, vector, gateway)

    chunk_recipe = ChunkRecipe(
        chunk_size=200,
        overlap=40,
        splitter=DEFAULT_SPLITTER,
        parser_name=parser.parser_name,
        parser_version=parser.parser_version,
    )

    pipeline = IngestionPipeline(
        session_factory=session_factory,
        evidence_manager=evidence_manager,
        parser=parser,
        index_manager=index_manager,
        chunk_recipe=chunk_recipe,
        gateway=gateway,
        visual_index_writer=visual_writer,
        settings=Settings(visual_index_enabled=True),
    )

    source_manager = SourceManager(session_factory)
    folder = tmp_path / "docs"
    folder.mkdir()
    source = source_manager.register_source(folder)

    path = folder / "multipage.docx"
    _write_docx(path, "Multi", "placeholder body")
    page_images = {1: b"fake-png-bytes-page-1", 2: b"fake-png-bytes-page-2"}
    monkeypatch.setattr(
        pipeline._parser, "parse", lambda source_id, source_path: _multipage_parsed_document(
            source_path, page_images
        )
    )

    result = pipeline.run_ingestion_for_source(source.id)
    assert result.status == "succeeded"

    with session_factory() as session:
        version = session.execute(sa_select(EvidenceVersion)).scalar_one()
        assert version.page_images_json is not None
        hashes = json.loads(version.page_images_json)
        assert set(hashes.keys()) == {"1", "2"}

    for page_no, image_bytes in page_images.items():
        content_hash = hashes[str(page_no)]
        assert store.get(content_hash) == image_bytes

    assert len(gateway.describe_image_calls) == 2
    described_images = {call["image_bytes"] for call in gateway.describe_image_calls}
    assert described_images == set(page_images.values())
    for call in gateway.describe_image_calls:
        assert call["model"] == "qwen2.5vl:7b"

    table = visual_writer.table
    assert table is not None
    rows = {row["page_no"]: row["description"] for row in table.search().to_list()}
    assert rows == {1: "a page describing penguins", 2: "a page describing penguins"}
    for row in table.search().to_list():
        assert row["evidence_version_id"] == version.id
        assert row["source_id"] == source.id


def test_page_images_disabled_by_default_no_vlm_or_index_calls(env, monkeypatch):
    """With `visual_index_enabled=False` (the default -- `env`'s pipeline was
    built without passing `gateway`/`visual_index_writer`/`settings` at all,
    same as every other test in this file), none of the visual-index
    machinery runs: no `describe_image`/extra `embed` calls, no pages table
    writes. Page images are still captured/stored (item 3/4 of the
    checkpoint is unconditional) -- only the VLM+embedding+LanceDB-write step
    is gated."""
    path = env.folder / "multipage.docx"
    _write_docx(path, "Multi", "placeholder body")
    page_images = {1: b"fake-png-bytes-page-1", 2: b"fake-png-bytes-page-2"}
    monkeypatch.setattr(
        env.pipeline._parser,
        "parse",
        lambda source_id, source_path: _multipage_parsed_document(source_path, page_images),
    )

    embed_calls_before = len(env.gateway.embed_calls)
    result = env.pipeline.run_ingestion_for_source(env.source.id)
    assert result.status == "succeeded"

    assert env.gateway.describe_image_calls == []
    # Chunk-text embedding still happens (unrelated to the visual index);
    # what must NOT happen is any *extra* embed call for a page description.
    # With a fake gateway and no page-description embeds, the count should
    # equal exactly the number of chunk-text embed calls made this run.
    assert len(env.gateway.embed_calls) - embed_calls_before == sum(
        r.chunks_written for r in result.file_results
    )

    with env.session_factory() as session:
        version = session.execute(
            sa_select(EvidenceVersion).where(EvidenceVersion.file_path == str(path))
        ).scalar_one()
        assert version.page_images_json is not None
        assert json.loads(version.page_images_json) != {}

    # No "pages" table created at all under the same lancedb dir the
    # pipeline's own vector writer uses.
    assert "pages" not in env.vector._db.list_tables().tables


# ---------------------------------------------------------------------------
# Standalone `VisualIndexer` tests -- constructed directly, no full
# `IngestionPipeline`, no real Docling parser: proves this collaborator is
# independently testable, not just file-size relief from the split.
# ---------------------------------------------------------------------------


def test_visual_indexer_save_and_index_pages_directly(migrated_sqlite_engine, tmp_path):
    """`VisualIndexer.save_page_images` + `.index_pages`, exercised on their
    own with a `FakeInferenceGateway`/real `ContentAddressedStore`/real
    `LancePageIndexWriter`: given two pages' raw bytes, storage must persist
    a `{page_no: content_hash}` mapping retrievable from the store, and
    indexing must write one row per page (with the gateway's canned
    description) to the `pages` LanceDB table -- all without touching
    `IngestionPipeline`, `EvidenceManager`, or a parser at all."""
    session_factory = get_session_factory(migrated_sqlite_engine)
    store_root = tmp_path / "evidence_store"
    for sub in ("objects", "manifests", "quarantine", "trash"):
        (store_root / sub).mkdir(parents=True, exist_ok=True)
    store = ContentAddressedStore(store_root)
    gateway = FakeInferenceGateway(canned_description="a page describing volcanoes")
    visual_writer = LancePageIndexWriter(tmp_path / "lancedb")
    settings = Settings(visual_index_enabled=True)

    indexer = VisualIndexer(
        session_factory=session_factory,
        store=store,
        gateway=gateway,
        visual_index_writer=visual_writer,
        settings=settings,
    )

    source_manager = SourceManager(session_factory)
    folder = tmp_path / "docs"
    folder.mkdir()
    source = source_manager.register_source(folder)

    with session_factory() as session:
        version = EvidenceVersion(
            source_id=source.id,
            file_path=str(folder / "multipage.docx"),
            content_hash="deadbeef",
            byte_size=0,
            is_current=True,
            parser_name="fixture",
            parser_version="1",
        )
        session.add(version)
        session.commit()
        session.refresh(version)
        version_id = version.id

    page_images = {1: b"fake-png-bytes-page-1", 2: b"fake-png-bytes-page-2"}
    hashes = indexer.save_page_images(version_id, page_images)
    assert set(hashes.keys()) == {1, 2}
    for page_no, image_bytes in page_images.items():
        assert store.get(hashes[page_no]) == image_bytes

    with session_factory() as session:
        reloaded = session.get(EvidenceVersion, version_id)
        assert json.loads(reloaded.page_images_json) == {str(k): v for k, v in hashes.items()}

    indexer.index_pages(
        evidence_version_id=version_id, source_id=source.id, page_images=page_images
    )

    assert len(gateway.describe_image_calls) == 2
    table = visual_writer.table
    assert table is not None
    rows = {row["page_no"]: row["description"] for row in table.search().to_list()}
    assert rows == {1: "a page describing volcanoes", 2: "a page describing volcanoes"}
