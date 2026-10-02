"""Formula-region/formula-transcription tests for the ingestion pipeline
(Phase B checkpoint 1 of "verified formula transcription"): coverage of
`EvidenceVersion.formula_regions_json`/`formula_transcriptions_json`, the
legacy-version backfill branch, and `settings.formula_transcription_enabled`
gating -- both through the full `IngestionPipeline` (orchestration-level, as
moved from the original single test file) and, separately, against
`FormulaTranscriber` constructed directly (see the bottom of this file) to
prove the split bought independent testability.
"""

from __future__ import annotations

import json
from io import BytesIO
from types import SimpleNamespace

from PIL import Image
from sqlalchemy import select as sa_select

from conftest import _write_docx
from docket.core.config import Settings
from docket.core.db.engine import get_session_factory
from docket.core.db.models import Chunk, EvidenceVersion, VersionStatus
from docket.infra.evidence.manager import EvidenceManager
from docket.infra.evidence.store import ContentAddressedStore
from docket.infra.index.fts_index import FtsIndexWriter
from docket.infra.index.manager import IndexManager
from docket.infra.index.vector_index import LanceIndexWriter
from docket.infra.inference.gateway import FakeInferenceGateway
from docket.services.ingestion.formula_transcriber import FormulaTranscriber
from docket.services.ingestion.pipeline import IngestionPipeline
from docket.infra.parsing.docling_wrapper import ParsedDocument
from docket.infra.parsing.recipes import DEFAULT_SPLITTER, ChunkRecipe
from docket.services.sources.manager import SourceManager


def test_formula_regions_persist_and_legacy_backfill_keeps_chunks(env, monkeypatch):
    path = env.folder / "formula.docx"
    _write_docx(path, "Formula", "An unreadable equation follows.")
    regions = [{"page_no": 2, "bbox": {"l": 1, "t": 2, "r": 3, "b": 4}}]
    parsed = ParsedDocument(text="Formula <!-- formula-not-decoded -->", source_path=path,
                            parser_name="fixture", parser_version="1", formula_regions=regions)
    calls = []

    def parse(source_id, source_path):
        calls.append(source_path)
        return parsed

    monkeypatch.setattr(env.pipeline._parser, "parse", parse)
    env.pipeline.run_ingestion_for_source(env.source.id)
    with env.session_factory() as session:
        version = session.execute(sa_select(EvidenceVersion)).scalar_one()
        assert json.loads(version.formula_regions_json) == regions
        version.formula_regions_json = None
        before = list(session.execute(sa_select(Chunk.id)).scalars())
        session.commit()
    result = env.pipeline.run_ingestion_for_source(env.source.id)
    with env.session_factory() as session:
        assert list(session.execute(sa_select(Chunk.id)).scalars()) == before
        assert json.loads(session.execute(sa_select(EvidenceVersion.formula_regions_json)).scalar_one()) == regions
    assert result.file_results[0].status == "unchanged"
    assert len(calls) == 2
    env.pipeline.run_ingestion_for_source(env.source.id)
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# Formula transcriptions (EvidenceVersion.formula_transcriptions_json) --
# Phase B checkpoint 1 of "verified formula transcription".
# ---------------------------------------------------------------------------


def _synthetic_page_png(width_px: int = 600, height_px: int = 800) -> bytes:
    image = Image.new("RGB", (width_px, height_px), (255, 255, 255))
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


# One region comfortably above MIN_FORMULA_REGION_AREA_PT2 (50x20=1000 pt^2)
# and one comfortably below it (9x10=90 pt^2) -- both on the same page, same
# BOTTOMLEFT-origin/page-size shape `_formula_regions` actually produces.
_ABOVE_THRESHOLD_REGION = {
    "item_ref": "#/texts/1",
    "page_no": 1,
    "coordinate_origin": "BOTTOMLEFT",
    "page_width": 300.0,
    "page_height": 400.0,
    "bbox": {"l": 100.0, "t": 300.0, "r": 150.0, "b": 280.0},
}
_BELOW_THRESHOLD_REGION = {
    "item_ref": "#/texts/2",
    "page_no": 1,
    "coordinate_origin": "BOTTOMLEFT",
    "page_width": 300.0,
    "page_height": 400.0,
    "bbox": {"l": 10.0, "t": 20.0, "r": 19.0, "b": 10.0},
}


def _formula_parsed_document(path, page_images, formula_regions):
    return ParsedDocument(
        text="# Section One\nAlpha content.\n# Section Two\nBeta content.\n",
        source_path=path,
        parser_name="fixture",
        parser_version="1",
        page_images=page_images,
        formula_regions=formula_regions,
    )


def _build_formula_pipeline(migrated_sqlite_engine, tmp_path, parser, *, formula_transcription_enabled):
    session_factory = get_session_factory(migrated_sqlite_engine)
    store_root = tmp_path / "evidence_store"
    for sub in ("objects", "manifests", "quarantine", "trash"):
        (store_root / sub).mkdir(parents=True, exist_ok=True)
    store = ContentAddressedStore(store_root)
    evidence_manager = EvidenceManager(store, session_factory)

    gateway = FakeInferenceGateway(canned_description="V = IR (Ohm's law)")
    fts = FtsIndexWriter(migrated_sqlite_engine)
    vector = LanceIndexWriter(tmp_path / "lancedb")
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
        settings=Settings(formula_transcription_enabled=formula_transcription_enabled),
    )

    source_manager = SourceManager(session_factory)
    folder = tmp_path / "docs"
    folder.mkdir()
    source = source_manager.register_source(folder)

    return SimpleNamespace(
        session_factory=session_factory,
        gateway=gateway,
        pipeline=pipeline,
        source_manager=source_manager,
        source=source,
        folder=folder,
    )


def test_formula_transcriptions_populated_above_threshold_when_enabled(
    migrated_sqlite_engine, tmp_path, parser, monkeypatch
):
    """With `formula_transcription_enabled=True`, ingesting a fixture whose
    formula regions include one above and one below
    `MIN_FORMULA_REGION_AREA_PT2` must: call the VLM exactly once (only for
    the above-threshold region), and store both a populated
    `formula_transcriptions_json` (one entry, tagged with the canned
    transcription and model) built from that one call."""
    env = _build_formula_pipeline(
        migrated_sqlite_engine, tmp_path, parser, formula_transcription_enabled=True
    )

    path = env.folder / "formulas.docx"
    _write_docx(path, "Formulas", "placeholder body")
    page_images = {1: _synthetic_page_png()}
    formula_regions = [_ABOVE_THRESHOLD_REGION, _BELOW_THRESHOLD_REGION]
    monkeypatch.setattr(
        env.pipeline._parser,
        "parse",
        lambda source_id, source_path: _formula_parsed_document(
            source_path, page_images, formula_regions
        ),
    )

    result = env.pipeline.run_ingestion_for_source(env.source.id)
    assert result.status == "succeeded"

    # Only the above-threshold region should ever reach the gateway -- the
    # below-threshold ("trivial") region must never trigger a describe_image
    # call at all.
    assert len(env.gateway.describe_image_calls) == 1

    with env.session_factory() as session:
        version = session.execute(
            sa_select(EvidenceVersion).where(EvidenceVersion.file_path == str(path))
        ).scalar_one()
        assert version.formula_transcriptions_json is not None
        transcriptions = json.loads(version.formula_transcriptions_json)

    assert len(transcriptions) == 1
    (entry,) = transcriptions
    assert entry["item_ref"] == "#/texts/1"
    assert entry["page_no"] == 1
    assert entry["transcription"] == "V = IR (Ohm's law)"
    assert entry["model"] == env.pipeline._settings.vision_model


def test_formula_transcriptions_disabled_by_default_no_gateway_calls_column_none(
    migrated_sqlite_engine, tmp_path, parser, monkeypatch
):
    """With `formula_transcription_enabled=False` (the default), ingesting
    the same fixture must make zero `describe_image` calls and leave
    `formula_transcriptions_json` as `None` -- same rigor as checkpoint 2's
    off-by-default proof (checking the actual call-count/DB-column
    evidence, not just that ingestion succeeded)."""
    env = _build_formula_pipeline(
        migrated_sqlite_engine, tmp_path, parser, formula_transcription_enabled=False
    )
    assert env.pipeline._settings.formula_transcription_enabled is False

    path = env.folder / "formulas.docx"
    _write_docx(path, "Formulas", "placeholder body")
    page_images = {1: _synthetic_page_png()}
    formula_regions = [_ABOVE_THRESHOLD_REGION, _BELOW_THRESHOLD_REGION]
    monkeypatch.setattr(
        env.pipeline._parser,
        "parse",
        lambda source_id, source_path: _formula_parsed_document(
            source_path, page_images, formula_regions
        ),
    )

    describe_calls_before = len(env.gateway.describe_image_calls)
    result = env.pipeline.run_ingestion_for_source(env.source.id)
    assert result.status == "succeeded"

    assert len(env.gateway.describe_image_calls) - describe_calls_before == 0
    assert env.gateway.describe_image_calls == []

    with env.session_factory() as session:
        version = session.execute(
            sa_select(EvidenceVersion).where(EvidenceVersion.file_path == str(path))
        ).scalar_one()
        assert version.formula_transcriptions_json is None
        # formula_regions_json is unrelated and unconditional -- still
        # populated regardless of the transcription flag.
        assert json.loads(version.formula_regions_json) == formula_regions


def test_formula_transcription_backfill_reuses_stored_page_images_no_reparse(
    migrated_sqlite_engine, tmp_path, parser, monkeypatch
):
    """Simulates turning `formula_transcription_enabled` on for a source
    that was already ingested with it off: formula_regions_json and
    page_images_json are already populated, only formula_transcriptions_json
    is missing. The backfill branch must transcribe using the already-
    stored page images/regions WITHOUT calling `parser.parse` again (no
    re-render)."""
    env = _build_formula_pipeline(
        migrated_sqlite_engine, tmp_path, parser, formula_transcription_enabled=False
    )

    path = env.folder / "formulas.docx"
    _write_docx(path, "Formulas", "placeholder body")
    page_images = {1: _synthetic_page_png()}
    formula_regions = [_ABOVE_THRESHOLD_REGION, _BELOW_THRESHOLD_REGION]
    parse_calls = {"count": 0}

    def _fake_parse(source_id, source_path):
        parse_calls["count"] += 1
        return _formula_parsed_document(source_path, page_images, formula_regions)

    monkeypatch.setattr(env.pipeline._parser, "parse", _fake_parse)

    # First run: flag off. formula_regions_json/page_images_json get
    # populated; formula_transcriptions_json stays None.
    result = env.pipeline.run_ingestion_for_source(env.source.id)
    assert result.status == "succeeded"
    assert parse_calls["count"] == 1
    assert env.gateway.describe_image_calls == []

    # Flip the flag on and re-run against the *same unchanged file* --
    # EvidenceManager sees identical bytes, so this exercises the
    # unchanged-file backfill branch, not a fresh ingest.
    env.pipeline._settings.formula_transcription_enabled = True
    result2 = env.pipeline.run_ingestion_for_source(env.source.id)
    assert result2.status == "succeeded"
    assert result2.file_results[0].status == "unchanged"

    # The backfill must reuse the already-stored regions/page images --
    # parser.parse must NOT have been called again.
    assert parse_calls["count"] == 1

    assert len(env.gateway.describe_image_calls) == 1

    with env.session_factory() as session:
        version = session.execute(
            sa_select(EvidenceVersion).where(EvidenceVersion.file_path == str(path))
        ).scalar_one()
        assert version.formula_transcriptions_json is not None
        transcriptions = json.loads(version.formula_transcriptions_json)
    assert len(transcriptions) == 1
    assert transcriptions[0]["item_ref"] == "#/texts/1"


# ---------------------------------------------------------------------------
# Standalone `FormulaTranscriber` tests -- constructed directly, no full
# `IngestionPipeline`, no real Docling parser: proves this collaborator is
# independently testable, not just file-size relief from the split.
# ---------------------------------------------------------------------------


def test_formula_transcriber_transcribes_only_above_threshold_region(
    migrated_sqlite_engine, tmp_path
):
    """`FormulaTranscriber.transcribe`, exercised on its own with a
    `FakeInferenceGateway` and a real `ContentAddressedStore`: given one
    above-threshold and one below-threshold region, it must call the
    gateway exactly once (skipping the trivial region), persist exactly one
    transcription entry onto `EvidenceVersion.formula_transcriptions_json`,
    and never touch `IngestionPipeline`, `EvidenceManager`, or a parser at
    all."""
    session_factory = get_session_factory(migrated_sqlite_engine)
    store_root = tmp_path / "evidence_store"
    for sub in ("objects", "manifests", "quarantine", "trash"):
        (store_root / sub).mkdir(parents=True, exist_ok=True)
    store = ContentAddressedStore(store_root)
    gateway = FakeInferenceGateway(canned_description="V = IR (Ohm's law)")
    settings = Settings(formula_transcription_enabled=True)

    transcriber = FormulaTranscriber(
        session_factory=session_factory, store=store, gateway=gateway, settings=settings
    )

    source_manager = SourceManager(session_factory)
    folder = tmp_path / "docs"
    folder.mkdir()
    source = source_manager.register_source(folder)

    with session_factory() as session:
        version = EvidenceVersion(
            source_id=source.id,
            file_path=str(folder / "formulas.docx"),
            content_hash="deadbeef",
            byte_size=0,
            status=VersionStatus.READY,
            parser_name="fixture",
            parser_version="1",
        )
        session.add(version)
        session.commit()
        session.refresh(version)
        version_id = version.id

    page_bytes = _synthetic_page_png()
    content_hash = store.put(page_bytes)
    page_image_hashes = {1: content_hash}

    transcriber.transcribe(
        evidence_version_id=version_id,
        formula_regions=[_ABOVE_THRESHOLD_REGION, _BELOW_THRESHOLD_REGION],
        page_image_hashes=page_image_hashes,
    )

    assert len(gateway.describe_image_calls) == 1

    with session_factory() as session:
        reloaded = session.get(EvidenceVersion, version_id)
        assert reloaded.formula_transcriptions_json is not None
        transcriptions = json.loads(reloaded.formula_transcriptions_json)
    assert len(transcriptions) == 1
    assert transcriptions[0]["item_ref"] == "#/texts/1"
    assert transcriptions[0]["transcription"] == "V = IR (Ohm's law)"
