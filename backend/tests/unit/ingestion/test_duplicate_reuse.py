"""Step 1b-E: identical content under another path reuses parsed chunks/vectors."""

from __future__ import annotations

import shutil

from sqlalchemy import select

from docket.core.config import Settings
from docket.core.db.models import Chunk, EvidenceVersion, VersionStatus
from conftest import _write_docx
from pool_fakes import FakeParser
from test_page_images_optional import _pipeline


class CountingParser(FakeParser):
    def __init__(self):
        super().__init__()
        self.calls = []

    def parse(self, source_id, path):
        self.calls.append(path.name)
        return super().parse(source_id, path)


def _run(engine, tmp_path, settings=None):
    parser = CountingParser()
    sf, pipeline = _pipeline(engine, tmp_path, parser, settings or Settings())
    from docket.services.sources.manager import SourceManager

    folder = tmp_path / "docs"
    (folder / "sub").mkdir(parents=True)
    source = SourceManager(sf).register_source(folder)
    _write_docx(folder / "a.docx", "Title", "alpha beta gamma delta " * 30)
    shutil.copy(folder / "a.docx", folder / "sub" / "a.docx")      # same name, other dir
    shutil.copy(folder / "a.docx", folder / "b_copy.docx")          # other name
    return sf, pipeline, parser, source, pipeline._index_manager


def test_duplicates_are_cloned_not_reparsed(migrated_sqlite_engine, tmp_path):
    sf, pipeline, parser, source, im = _run(migrated_sqlite_engine, tmp_path)
    gateway = pipeline._gateway
    result = pipeline.run_ingestion_for_source(source.id)
    assert result.status == "succeeded"
    assert len(parser.calls) == 1, parser.calls  # parsed once, two clones
    with sf() as s:
        versions = s.execute(select(EvidenceVersion)).scalars().all()
        assert len(versions) == 3 and all(v.status == VersionStatus.READY for v in versions)
        assert len({v.file_path for v in versions}) == 3  # per-path provenance kept
        per_version = {
            v.id: [(c.ordinal, c.text, c.content_hash) for c in s.execute(
                select(Chunk).where(Chunk.evidence_version_id == v.id).order_by(Chunk.ordinal)).scalars()]
            for v in versions
        }
        assert len({tuple(x) for x in per_version.values()}) == 1 and len(next(iter(per_version.values()))) > 0
        chunk_ids = [c for (c,) in s.execute(select(Chunk.id)).all()]
        assert len(chunk_ids) == len(set(chunk_ids)) == 3 * len(next(iter(per_version.values())))
    n_chunks = len(next(iter(per_version.values())))
    # embeddings: 1x for the parsed original; the same-basename copy reuses the vector,
    # the differently named copy re-embeds (its index text carries the file name)
    assert len(gateway.embed_calls) == 2 * n_chunks
    # all three versions are findable in both indexes
    assert len(im._fts.existing_chunk_ids(chunk_ids)) == len(chunk_ids)
    assert len(im._vector.existing_chunk_ids(chunk_ids)) == len(chunk_ids)


def test_reuse_can_be_disabled(migrated_sqlite_engine, tmp_path):
    sf, pipeline, parser, source, _ = _run(
        migrated_sqlite_engine, tmp_path, Settings(reuse_duplicate_content=False)
    )
    pipeline.run_ingestion_for_source(source.id)
    assert len(parser.calls) == 3


def test_reuse_skipped_when_visual_index_on(migrated_sqlite_engine, tmp_path):
    sf, pipeline, parser, source, _ = _run(
        migrated_sqlite_engine, tmp_path, Settings(visual_index_enabled=True)
    )
    pipeline.run_ingestion_for_source(source.id)
    assert len(parser.calls) == 3
