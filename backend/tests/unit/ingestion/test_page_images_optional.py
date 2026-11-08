"""Step 1b-B: page PNGs are generated/stored only when a consumer needs them."""

from __future__ import annotations

import json

from sqlalchemy import select as sa_select

from docket.core.config import Settings
from docket.core.db.engine import get_session_factory
from docket.core.db.models import EvidenceVersion
from docket.infra.evidence.manager import EvidenceManager
from docket.infra.evidence.store import ContentAddressedStore
from docket.infra.index.fts_index import FtsIndexWriter
from docket.infra.index.manager import IndexManager
from docket.infra.index.vector_index import LanceIndexWriter
from docket.infra.index.visual_index import LancePageIndexWriter
from docket.infra.inference.gateway import FakeInferenceGateway
from docket.infra.parsing.recipes import DEFAULT_SPLITTER, ChunkRecipe
from docket.services.ingestion.pipeline import IngestionPipeline
from docket.services.sources.manager import SourceManager
from tests.unit.ingestion.conftest import _write_docx
from tests.unit.ingestion.test_visual import _multipage_parsed_document

PAGES = {1: b"png-1", 2: b"png-2"}


def _pipeline(engine, tmp_path, parser, settings):
    sf = get_session_factory(engine)
    root = tmp_path / "evidence_store"
    for sub in ("objects", "manifests", "quarantine", "trash"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    gateway = FakeInferenceGateway(canned_description="d")
    pipeline = IngestionPipeline(
        session_factory=sf,
        evidence_manager=EvidenceManager(ContentAddressedStore(root), sf),
        parser=parser,
        index_manager=IndexManager(
            FtsIndexWriter(engine), LanceIndexWriter(tmp_path / "lancedb"), gateway
        ),
        chunk_recipe=ChunkRecipe(
            chunk_size=200, overlap=40, splitter=DEFAULT_SPLITTER,
            parser_name=parser.parser_name, parser_version=parser.parser_version,
        ),
        gateway=gateway,
        visual_index_writer=LancePageIndexWriter(tmp_path / "lancedb"),
        settings=settings,
    )
    return sf, pipeline


def _setup(tmp_path, sf):
    folder = tmp_path / "docs"
    folder.mkdir(exist_ok=True)
    source = SourceManager(sf).register_source(folder)
    _write_docx(folder / "a.docx", "H", "body text")
    return source


def test_settings_page_images_needed():
    assert not Settings().page_images_needed
    assert Settings(store_page_images=True).page_images_needed
    assert Settings(visual_index_enabled=True).page_images_needed
    assert Settings(formula_transcription_enabled=True).page_images_needed


def test_default_does_not_store_images_and_later_flag_backfills(
    migrated_sqlite_engine, tmp_path, parser, monkeypatch
):
    sf, pipeline = _pipeline(migrated_sqlite_engine, tmp_path, parser, Settings())
    source = _setup(tmp_path, sf)
    calls = []

    def fake_parse(source_id, path):
        calls.append(path)
        return _multipage_parsed_document(path, PAGES)

    monkeypatch.setattr(pipeline._parser, "parse", fake_parse)
    assert pipeline.run_ingestion_for_source(source.id).status == "succeeded"
    with sf() as s:
        v = s.execute(sa_select(EvidenceVersion)).scalar_one()
        assert v.page_images_json is None
    assert len(calls) == 1
    # unchanged re-run: no re-parse just because images are absent
    pipeline.run_ingestion_for_source(source.id)
    assert len(calls) == 1

    # turning a consumer on later backfills the images for the unchanged file
    sf2, pipeline2 = _pipeline(
        migrated_sqlite_engine, tmp_path, parser, Settings(store_page_images=True)
    )
    monkeypatch.setattr(pipeline2._parser, "parse", fake_parse)
    pipeline2.run_ingestion_for_source(source.id)
    assert len(calls) == 2
    with sf2() as s:
        v = s.execute(sa_select(EvidenceVersion)).scalar_one()
        assert set(json.loads(v.page_images_json)) == {"1", "2"}


def test_docling_parser_flag_reaches_pipeline_options(parser):
    assert parser._generate_page_images is True  # constructor default keeps old behaviour
    from docket.infra.parsing.docling_wrapper import DoclingParser

    off = DoclingParser(generate_page_images=False)
    assert off._generate_page_images is False
    opts = off._converter.format_to_options[
        __import__("docling.datamodel.base_models", fromlist=["InputFormat"]).InputFormat.PDF
    ].pipeline_options
    assert opts.generate_page_images is False
