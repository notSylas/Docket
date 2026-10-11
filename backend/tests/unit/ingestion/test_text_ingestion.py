import json

import pytest
from sqlalchemy import select

from docket.core.db.models import Chunk, EvidenceUnit, EvidenceVersion, VersionStatus
from docket.infra.parsing.recipes import FAMILY_TEXT, ChunkRecipe, recipe_family
from docket.infra.retrieval.resolver import EvidenceResolver
from docket.services.sources.scan import scan_folder


@pytest.mark.parametrize("suffix,content,quote,location", [
    (".txt", "Shipping reserve is 420 rupees.\n", "Shipping reserve", "lines 1-1"),
    (".md", "# Shipping\n## Reserve\nReserve is 420 rupees.\n", "Reserve is", "Shipping > Reserve"),
    (".csv", 'code,reserve\n0012,"1,23,456"\n', "1,23,456", "Record 2, column reserve"),
    (".html", "<h1>Shipping</h1><p>Reserve is 420 rupees.</p>", "Reserve is", "visible block 2"),
    (".htm", "<p>Reserve is 420 rupees.</p>", "Reserve is", "visible block 1"),
])
def test_ingest_resolve_rechunk_and_original_bytes(env, suffix, content, quote, location):
    path = env.folder / ("reserve" + suffix)
    original = content.encode("utf-16")
    path.write_bytes(original)
    binary = env.folder / "fake.txt"
    binary.write_bytes(b"binary\x00\x01")
    scan = scan_folder(env.folder)
    assert scan.documents == 1
    env.pipeline.run_ingestion_for_source(env.source.id)
    with env.session_factory() as session:
        version = session.execute(select(EvidenceVersion)).scalar_one()
        chunks = session.execute(select(Chunk)).scalars().all()
        units = session.execute(select(EvidenceUnit)).scalars().all()
    assert version.status == VersionStatus.READY
    store = env.pipeline._evidence_manager.store
    assert store.get(version.content_hash) == original == path.read_bytes()
    assert all(json.loads(u.locator_json)["encoding"] == "utf-16" for u in units)
    resolver = EvidenceResolver(env.session_factory)
    for chunk in chunks:
        assert resolver.resolve(chunk.id).text == chunk.text
    resolved = next(resolver.resolve(c.id) for c in chunks if quote in c.text)
    assert location in resolved.location
    # Same bytes are a no-op, but a recipe change makes this family stale.
    env.pipeline.run_ingestion_for_source(env.source.id)
    assert env.pipeline.find_stale_versions(env.source.id) == []
    recipe = env.pipeline._recipes[FAMILY_TEXT]
    env.pipeline._recipes[FAMILY_TEXT] = ChunkRecipe(
        recipe.chunk_size, recipe.overlap, recipe.splitter + ":test", recipe.parser_name, recipe.parser_version)
    candidates = env.pipeline.find_stale_versions(env.source.id)
    assert len(candidates) == 1 and recipe_family(str(path)) == FAMILY_TEXT
    path.unlink()  # Rechunk must read the stored original, not the live file.
    env.pipeline.rechunk_version(env.source.id, candidates[0])
    assert env.pipeline.find_stale_versions(env.source.id) == []
    assert store.get(version.content_hash) == original
