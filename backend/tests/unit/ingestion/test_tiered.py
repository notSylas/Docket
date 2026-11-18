"""Tiered indexing: Tier-1 TEXT_READY (keyword only) -> Tier-2 READY (full).

Fake inference gateway throughout; a fake Docling stand-in for Tier 2 so the
suite stays fast. Covers the tier-1 write, the atomic upgrade, crash/resume at
each transition, eligibility, citation behaviour on replacement and that the
end state equals one-pass ingestion.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text

from docket.core.config import Settings
from docket.core.db.engine import get_engine
from docket.core.db.models import Chunk, ChunkAlias, EvidenceVersion, VersionStatus
from docket.infra.index.fts_index import FtsIndexWriter
from docket.infra.parsing import fast_text
from docket.infra.retrieval.hybrid import fts_search, hybrid_search, vector_search
from docket.infra.retrieval.resolver import ChunkNotFoundError, EvidenceResolver
from docket.services.ingestion.queue import IngestQueue
from docket.services.ingestion.tiered import TieredIndexer
from docket.services.sources.manager import SourceManager

from conftest import _write_docx, _write_pptx, _write_xlsx
from pool_fakes import FakeParser
from test_page_images_optional import _pipeline

REPO = Path(__file__).resolve().parents[3]
BODY = "alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima mike november " * 12


class CountingParser(FakeParser):
    def __init__(self):
        super().__init__()
        self.calls: list[str] = []

    def parse(self, source_id, path):
        self.calls.append(path.name)
        return super().parse(source_id, path)


def _env(engine, tmp_path, settings=None, parser=None):
    parser = parser or CountingParser()
    sf, pipeline = _pipeline(engine, tmp_path, parser, settings or Settings())
    folder = tmp_path / "docs"
    folder.mkdir(exist_ok=True)
    source = SourceManager(sf).register_source(folder)
    queue = IngestQueue(sf)
    return sf, pipeline, parser, source, folder, queue


def _context(pipeline, source):
    return pipeline.tier_context(source.id)


def _tier1(pipeline, source, path):
    _, root, rules = _context(pipeline, source)
    return pipeline.ingest_tier1_path(source.id, Path(path), root, rules)


def _tier2(pipeline, source, path):
    _, root, rules = _context(pipeline, source)
    return pipeline.ingest_tier2_path(source.id, Path(path), root, rules)


def _statuses(sf):
    with sf() as s:
        return {Path(v.file_path).name: v.status for v in s.execute(select(EvidenceVersion)).scalars()}


def _fts_ids(engine):
    with engine.connect() as c:
        return {r[0] for r in c.execute(text("SELECT chunk_id FROM fts_chunks"))}


def _map_ids(engine):
    with engine.connect() as c:
        return {r[0] for r in c.execute(text("SELECT chunk_id FROM fts_chunk_map"))}


def _chunk_ids(sf):
    with sf() as s:
        return {c for (c,) in s.execute(select(Chunk.id)).all()}


def _vector_ids(pipeline):
    table = pipeline._index_manager._vector.table
    if table is None:
        return set()
    return {r["chunk_id"] for r in table.search().limit(100000).to_list()}


def assert_consistent(engine, sf, pipeline):
    """No duplicate or orphan rows: chunks == FTS rows == FTS map rows."""
    chunks = _chunk_ids(sf)
    assert _fts_ids(engine) == chunks
    assert _map_ids(engine) == chunks
    with engine.connect() as c:
        n_fts = c.execute(text("SELECT COUNT(*) FROM fts_chunks")).scalar_one()
    assert n_fts == len(chunks)


# -- fast text readers -------------------------------------------------------


def test_fast_text_born_digital_pdf_scan_and_docx(tmp_path):
    parsed = fast_text.parse_fast(REPO.parent / "evals" / "suites" / "corpus-synthetic-g6" / "contract.pdf")
    assert parsed.text.strip() and "<!--PAGE:1-->" in parsed.text_with_page_markers
    assert parsed.parser_name == fast_text.TIER1_PARSER_NAME

    import pypdfium2 as pdfium

    blank = tmp_path / "scan.pdf"
    doc = pdfium.PdfDocument.new()
    doc.new_page(200, 200)
    doc.save(str(blank))
    with pytest.raises(fast_text.NoTextLayer):
        fast_text.parse_fast(blank)

    d = tmp_path / "a.docx"
    import docx

    document = docx.Document()
    document.add_heading("Title", level=1)
    document.add_paragraph("intro words")
    table = document.add_table(rows=2, cols=2)
    for r, row in enumerate([["k", "v"], ["rent", "900"]]):
        for c, val in enumerate(row):
            table.cell(r, c).text = val
    document.save(str(d))
    text_ = fast_text.parse_fast(d).text
    assert text_.startswith("# Title") and "intro words" in text_ and "| rent | 900 |" in text_


# -- tier 1 ------------------------------------------------------------------


def test_tier1_makes_files_keyword_searchable_without_embeddings(migrated_sqlite_engine, tmp_path):
    engine = migrated_sqlite_engine
    sf, pipeline, parser, source, folder, _ = _env(engine, tmp_path)
    _write_docx(folder / "a.docx", "Lease", "the quarterly rent is nine hundred zebracorn dollars")
    _write_xlsx(folder / "b.xlsx", "Sheet1", ["name", "amt"], [["quokka", 5]])
    (folder / "c.txt").write_text("plain note about a wombat colony " * 5)
    shutil.copy(REPO.parent / "evals" / "suites" / "corpus-synthetic-g6" / "contract.pdf", folder / "d.pdf")
    for name in ("a.docx", "b.xlsx", "c.txt", "d.pdf"):
        assert _tier1(pipeline, source, folder / name).status == "ingested"
    assert set(_statuses(sf).values()) == {VersionStatus.TEXT_READY}
    assert pipeline._gateway.embed_calls == [] and parser.calls == []
    assert _vector_ids(pipeline) == set()
    with sf() as s:
        chunks = s.execute(select(Chunk)).scalars().all()
        assert chunks and all(c.provisional for c in chunks)
    assert_consistent(engine, sf, pipeline)
    assert fts_search(engine, "zebracorn", 5) and fts_search(engine, "quokka", 5)
    assert fts_search(engine, "wombat", 5)
    # file-name search works: the index text carries the file name
    assert fts_search(engine, "d.pdf", 5) or fts_search(engine, "contract", 5)
    # unchanged on a second pass
    assert _tier1(pipeline, source, folder / "a.docx").status == "unchanged"


def test_scan_pdf_has_no_fast_path_and_creates_no_version(migrated_sqlite_engine, tmp_path):
    import pypdfium2 as pdfium

    sf, pipeline, _, source, folder, _ = _env(migrated_sqlite_engine, tmp_path)
    doc = pdfium.PdfDocument.new()
    doc.new_page(200, 200)
    doc.save(str(folder / "scan.pdf"))
    assert _tier1(pipeline, source, folder / "scan.pdf").status == "no_fast_path"
    assert _statuses(sf) == {}


# -- eligibility ---------------------------------------------------------------


def test_vector_leg_skips_provisional_and_results_are_flagged(migrated_sqlite_engine, tmp_path):
    engine = migrated_sqlite_engine
    sf, pipeline, parser, source, folder, _ = _env(engine, tmp_path)
    _write_docx(folder / "full.docx", "Full", "fully indexed document about gizmo assembly " * 8)
    _write_docx(folder / "quick.docx", "Quick", "keyword only document about zebracorn migration " * 8)
    _tier2(pipeline, source, folder / "full.docx")
    _tier1(pipeline, source, folder / "quick.docx")
    gateway = pipeline._gateway
    table = pipeline._index_manager._vector.table
    # the vector leg returns nothing from the provisional file even for its own text
    ids = vector_search(table, engine, gateway, "keyword only document about zebracorn migration", 10)
    with sf() as s:
        prov = {c for (c,) in s.execute(select(Chunk.id).where(Chunk.provisional.is_(True)))}
    assert prov and not (set(ids) & prov)
    # the keyword leg finds it, and hybrid flags it
    assert set(fts_search(engine, "zebracorn", 10)) & prov
    ranked = hybrid_search(engine=engine, table=table, gateway=gateway, query="zebracorn migration", top_k=5)
    flagged = {r.chunk_id for r in ranked if r.provisional}
    assert flagged and flagged <= prov
    assert all(not r.provisional for r in ranked if r.chunk_id not in prov)


# -- upgrade --------------------------------------------------------------------


def test_upgrade_replaces_provisional_chunks_atomically_no_orphans(migrated_sqlite_engine, tmp_path):
    engine = migrated_sqlite_engine
    sf, pipeline, parser, source, folder, _ = _env(engine, tmp_path)
    _write_docx(folder / "a.docx", "Lease", BODY)
    _write_xlsx(folder / "b.xlsx", "S", ["n", "v"], [["quokka", 5], ["emu", 6]])
    (folder / "c.txt").write_text("wombat colony " * 80)
    for n in ("a.docx", "b.xlsx", "c.txt"):
        _tier1(pipeline, source, folder / n)
    tier1_ids = _chunk_ids(sf)
    assert pipeline._gateway.embed_calls == []
    for n in ("a.docx", "b.xlsx", "c.txt"):
        assert _tier2(pipeline, source, folder / n).status == "ingested"
    assert set(_statuses(sf).values()) == {VersionStatus.READY}
    with sf() as s:
        assert not any(c.provisional for c in s.execute(select(Chunk)).scalars())
        recipes = set(s.execute(select(Chunk.chunk_recipe_id)).scalars())
    assert pipeline.tier1_recipe().id not in recipes
    assert_consistent(engine, sf, pipeline)
    assert _vector_ids(pipeline) == _chunk_ids(sf)
    # docx (different recipe) got new ids; xlsx/txt kept theirs (same recipe + text)
    assert _chunk_ids(sf) != tier1_ids and tier1_ids & _chunk_ids(sf)
    assert parser.calls == ["a.docx"]


def test_docling_family_upgrade_embeds_only_in_tier2(migrated_sqlite_engine, tmp_path):
    engine = migrated_sqlite_engine
    sf, pipeline, parser, source, folder, _ = _env(engine, tmp_path)
    _write_docx(folder / "a.docx", "T", BODY)
    _tier1(pipeline, source, folder / "a.docx")
    assert pipeline._gateway.embed_calls == []
    _tier2(pipeline, source, folder / "a.docx")
    n = len(_chunk_ids(sf))
    assert len(pipeline._gateway.embed_calls) == n and n > 0


# -- crash / resume ----------------------------------------------------------------


def test_crash_during_tier1_transaction_rolls_back_and_retry_succeeds(
    migrated_sqlite_engine, tmp_path, monkeypatch
):
    engine = migrated_sqlite_engine
    sf, pipeline, _, source, folder, _ = _env(engine, tmp_path)
    _write_docx(folder / "a.docx", "T", BODY)
    fts = pipeline._index_manager.fts_writer
    real = fts.upsert_in
    monkeypatch.setattr(fts, "upsert_in", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        _tier1(pipeline, source, folder / "a.docx")
    assert _statuses(sf)["a.docx"] == VersionStatus.FAILED
    assert _chunk_ids(sf) == set() and _fts_ids(engine) == set()
    monkeypatch.setattr(fts, "upsert_in", real)
    assert _tier1(pipeline, source, folder / "a.docx").status == "ingested"
    assert _statuses(sf)["a.docx"] == VersionStatus.TEXT_READY
    assert_consistent(engine, sf, pipeline)


def test_tier2_failure_keeps_text_ready_and_provisional_intact(migrated_sqlite_engine, tmp_path):
    engine = migrated_sqlite_engine
    sf, pipeline, parser, source, folder, _ = _env(engine, tmp_path)
    _write_docx(folder / "a.docx", "T", BODY)
    _tier1(pipeline, source, folder / "a.docx")
    before = _chunk_ids(sf)

    def boom(*a, **k):
        raise RuntimeError("embedding service down")

    pipeline._gateway.embed_batch = boom
    with pytest.raises(RuntimeError):
        _tier2(pipeline, source, folder / "a.docx")
    assert _statuses(sf)["a.docx"] == VersionStatus.TEXT_READY
    assert _chunk_ids(sf) == before and fts_search(engine, "alpha", 3)
    with sf() as s:
        assert s.execute(select(EvidenceVersion)).scalar_one().failure_reason
    assert_consistent(engine, sf, pipeline)


def test_crash_inside_replace_transaction_rolls_back_then_retry_completes(
    migrated_sqlite_engine, tmp_path, monkeypatch
):
    engine = migrated_sqlite_engine
    sf, pipeline, parser, source, folder, _ = _env(engine, tmp_path)
    _write_docx(folder / "a.docx", "T", BODY)
    _tier1(pipeline, source, folder / "a.docx")
    before = _chunk_ids(sf)
    fts = pipeline._index_manager.fts_writer
    real = fts.upsert_in

    def late_boom(conn, records):
        real(conn, records)  # rows written, then the process "dies" before commit
        raise RuntimeError("killed mid-transaction")

    monkeypatch.setattr(fts, "upsert_in", late_boom)
    with pytest.raises(RuntimeError):
        _tier2(pipeline, source, folder / "a.docx")
    # everything rolled back: provisional chunks, units, FTS rows and status as before
    assert _statuses(sf)["a.docx"] == VersionStatus.TEXT_READY
    assert _chunk_ids(sf) == before
    assert_consistent(engine, sf, pipeline)
    orphan_vectors = _vector_ids(pipeline) - _chunk_ids(sf)
    assert orphan_vectors  # appended before the transaction; unreachable (version not READY)
    embedded = len(pipeline._gateway.embed_calls)
    monkeypatch.setattr(fts, "upsert_in", real)
    _tier2(pipeline, source, folder / "a.docx")
    assert _statuses(sf)["a.docx"] == VersionStatus.READY
    assert len(pipeline._gateway.embed_calls) == embedded  # vectors reused, not recomputed
    assert_consistent(engine, sf, pipeline)
    assert _vector_ids(pipeline) == _chunk_ids(sf)


def test_orphan_vectors_from_a_crash_are_removed_by_reconcile(migrated_sqlite_engine, tmp_path, monkeypatch):
    engine = migrated_sqlite_engine
    sf, pipeline, _, source, folder, _ = _env(engine, tmp_path)
    _write_docx(folder / "a.docx", "T", BODY)
    _tier1(pipeline, source, folder / "a.docx")
    fts = pipeline._index_manager.fts_writer
    monkeypatch.setattr(fts, "upsert_in", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    with pytest.raises(RuntimeError):
        _tier2(pipeline, source, folder / "a.docx")
    assert _vector_ids(pipeline) - _chunk_ids(sf)
    pipeline.finish_source_pass(source.id)
    assert _vector_ids(pipeline) <= _chunk_ids(sf)
    assert fts_search(engine, "alpha", 3)  # provisional chunks survived the reconcile


def test_superseded_while_deepening_is_not_resurrected(migrated_sqlite_engine, tmp_path):
    engine = migrated_sqlite_engine
    sf, pipeline, _, source, folder, _ = _env(engine, tmp_path)
    _write_docx(folder / "a.docx", "T", BODY)
    _tier1(pipeline, source, folder / "a.docx")
    with sf() as s:
        vid = s.execute(select(EvidenceVersion.id)).scalar_one()
    pipeline._evidence_manager.mark_version_status(vid, VersionStatus.SUPERSEDED)
    units, chunks = pipeline._chunk_family(
        "docling", vid, pipeline._parser.parse(source.id, folder / "a.docx"), pipeline._recipes["docling"]
    )
    built = pipeline._chunk_writer.build_rows(
        source_id=source.id, evidence_version_id=vid, file_path=str(folder / "a.docx"),
        units=units, chunks=chunks, recipe=pipeline._recipes["docling"],
    )
    assert (
        pipeline._chunk_writer.replace_version_content(
            fts=pipeline._index_manager.fts_writer, evidence_version_id=vid, built=built,
            new_status=VersionStatus.READY,
        )
        is None
    )
    assert _statuses(sf)["a.docx"] == VersionStatus.SUPERSEDED


# -- citations across replacement ------------------------------------------------------


def test_provisional_citation_resolves_then_maps_to_replacing_chunk(migrated_sqlite_engine, tmp_path):
    engine = migrated_sqlite_engine
    sf, pipeline, _, source, folder, _ = _env(engine, tmp_path)
    _write_docx(folder / "a.docx", "T", BODY)
    _tier1(pipeline, source, folder / "a.docx")
    resolver = EvidenceResolver(sf)
    old = sorted(_chunk_ids(sf))
    ev = resolver.resolve_many(old)
    assert all(e.provisional for e in ev)
    _tier2(pipeline, source, folder / "a.docx")
    new = _chunk_ids(sf)
    assert not (set(old) & new)  # docling-family ids differ across the swap
    out = resolver.resolve_many(old)  # old ids still resolve, to the replacing chunks
    assert len(out) == len(old) and all(not e.provisional and e.chunk_id in new for e in out)
    with sf() as s:
        aliases = s.execute(select(ChunkAlias)).scalars().all()
    assert {a.old_chunk_id for a in aliases} == set(old)
    # text moved too far (no overlap): degrades to "not found"
    with sf() as s:
        a = s.execute(select(ChunkAlias)).scalars().first()
        a.new_chunk_id = None
        s.commit()
        gone = a.old_chunk_id
    with pytest.raises(ChunkNotFoundError):
        resolver.resolve_many([gone])


def test_native_family_chunk_ids_survive_replacement(migrated_sqlite_engine, tmp_path):
    engine = migrated_sqlite_engine
    sf, pipeline, _, source, folder, _ = _env(engine, tmp_path)
    (folder / "c.txt").write_text("wombat colony counts " * 90)
    _tier1(pipeline, source, folder / "c.txt")
    before = _chunk_ids(sf)
    _tier2(pipeline, source, folder / "c.txt")
    assert _chunk_ids(sf) == before
    with sf() as s:
        assert s.execute(select(ChunkAlias)).scalars().all() == []
    assert_consistent(engine, sf, pipeline)


# -- end state equals one-pass ----------------------------------------------------------


def _fresh_engine(tmp_path, name):
    path = tmp_path / f"{name}.sqlite3"
    cfg = Config(str(REPO / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO / "src" / "docket" / "core" / "db" / "migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    command.upgrade(cfg, "head")
    return get_engine(path)


def _snapshot(sf, corpus):
    with sf() as s:
        rows = s.execute(
            select(EvidenceVersion.file_path, Chunk.ordinal, Chunk.text, Chunk.provisional, EvidenceVersion.status)
            .join(Chunk, Chunk.evidence_version_id == EvidenceVersion.id)
        ).all()
    return sorted((str(Path(p).relative_to(corpus)), o, t, bool(pr), st.name) for p, o, t, pr, st in rows)


def test_tiered_end_state_equals_one_pass_on_synthetic_corpora(tmp_path):
    corpus = tmp_path / "corpus"
    for src in ("corpus-synthetic", "corpus-synthetic-g1", "corpus-synthetic-g4"):
        for f in (REPO.parent / "evals" / "suites" / src).rglob("*"):
            if f.is_file() and f.suffix.lower() in {".docx", ".xlsx", ".pptx", ".txt", ".html", ".csv"}:
                dest = corpus / src / f.relative_to(REPO.parent / "evals" / "suites" / src)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(f, dest)
    files = sorted(p for p in corpus.rglob("*") if p.is_file())
    assert len(files) > 10

    def run(name, tiered):
        engine = _fresh_engine(tmp_path, name)
        work = tmp_path / name
        work.mkdir()
        sf, pipeline = _pipeline(engine, work, CountingParser(), Settings())
        source = SourceManager(sf).register_source(corpus)
        if tiered:
            queue = IngestQueue(sf)
            indexer = TieredIndexer(pipeline=pipeline, queue=queue)
            indexer.enqueue_source(source.id)
            s1 = indexer.run(tier2=False)
            assert s1.tier1_done == len(files) and s1.tier2_done == 0
            assert VersionStatus.TEXT_READY in set(_statuses_by_path(sf).values())
            indexer.run()
        else:
            pipeline.run_ingestion_for_source(source.id)
        assert_consistent(engine, sf, pipeline)
        return _snapshot(sf, corpus), _vector_ids(pipeline) == _chunk_ids(sf)

    one, ok1 = run("one", False)
    two, ok2 = run("two", True)
    assert ok1 and ok2
    assert one == two and len(one) > 20
    assert {r[4] for r in two} == {"READY"} and not any(r[3] for r in two)


def _statuses_by_path(sf):
    with sf() as s:
        return {v.file_path: v.status for v in s.execute(select(EvidenceVersion)).scalars()}
