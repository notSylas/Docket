from __future__ import annotations

import pytest

from docket.core.db.models import Chunk, ChunkRecipe, EvidenceUnit, EvidenceVersion, SourceStatus, VersionStatus
from docket.interfaces.cli.context import AppContext
from docket.services.sources.readiness import ReadinessService, SearchReadiness


@pytest.fixture
def context(tmp_path):
    ctx = AppContext.for_testing(data_dir=tmp_path / "data")
    yield ctx
    ctx.engine.dispose()


def add_version(context, source, *, status=VersionStatus.READY, chunks=2):
    with context.session_factory() as db:
        version = EvidenceVersion(
            source_id=source.id, file_path="test.pdf", content_hash="test-hash",
            byte_size=1, parser_name="test", parser_version="1", status=status,
        )
        db.add(version)
        db.flush()
        unit = EvidenceUnit(evidence_version_id=version.id, unit_index=0, content_hash="unit")
        recipe = ChunkRecipe(id="test-recipe", chunk_size=10, overlap=0,
                             splitter="test", parser_name="test", parser_version="1")
        db.add_all([unit, recipe])
        db.flush()
        for n in range(chunks):
            db.add(Chunk(id=f"test-chunk-{n}", source_id=source.id,
                         evidence_version_id=version.id, evidence_unit_id=unit.id,
                         chunk_recipe_id=recipe.id, ordinal=n, text="Evidence", content_hash=f"{n}"))
        db.commit()


def test_fresh_context_readiness_leaves_heavy_dependencies_lazy(context):
    assert context.readiness.snapshot() == SearchReadiness(0, 0)
    assert not {"parser", "pipeline", "gateway", "vector_writer", "visual_index_writer"} & context.__dict__.keys()


@pytest.mark.parametrize("source_status", list(SourceStatus))
@pytest.mark.parametrize("version_status", list(VersionStatus))
def test_counts_follow_source_and_version_eligibility(context, tmp_path, source_status, version_status):
    folder = tmp_path / "source"
    folder.mkdir()
    source = context.source_manager.register_source(folder)
    add_version(context, source, status=version_status)
    with context.session_factory() as db:
        from docket.core.db.models import Source
        db.get(Source, source.id).status = source_status
        db.commit()
    expected = SearchReadiness(1, 2) if source_status == SourceStatus.ACTIVE and version_status == VersionStatus.READY else SearchReadiness(0, 0)
    assert context.readiness.snapshot() == expected


def test_processed_empty_file_is_not_searchable(context, tmp_path):
    source = context.source_manager.register_source(tmp_path)
    add_version(context, source, chunks=0)
    assert context.readiness.snapshot() == SearchReadiness(0, 0)


def test_disconnect_and_reconnect_update_counts_without_rebuilding(context, tmp_path):
    source = context.source_manager.register_source(tmp_path)
    add_version(context, source)
    readiness = ReadinessService(context.session_factory)
    assert readiness.snapshot() == SearchReadiness(1, 2)
    context.source_manager.deactivate_source(source.id, reason="user_disconnected")
    assert readiness.snapshot() == SearchReadiness(0, 0)
    context.source_manager.reconnect_source(source.id)
    assert readiness.snapshot() == SearchReadiness(1, 2)
