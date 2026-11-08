"""Step 1b-C: streaming hashing and per-file size cap."""

from __future__ import annotations

import hashlib

from sqlalchemy import select

from docket.core.config import Settings
from docket.core.db.models import EvidenceVersion
from docket.infra.evidence.manager import EvidenceManager
from docket.infra.evidence.store import ContentAddressedStore
from conftest import _write_docx
from pool_fakes import FakeParser
from test_page_images_optional import _pipeline, _setup


def test_streaming_hash_and_put_file_match_bytes_api(tmp_path):
    data = b"x" * 3_000_001
    f = tmp_path / "big.bin"
    f.write_bytes(data)
    store = ContentAddressedStore(tmp_path / "store")
    expect = hashlib.sha256(data).hexdigest()
    assert store.hash_file(f, chunk_size=1 << 16) == (expect, len(data))
    assert store.put_file(f, chunk_size=1 << 16) == expect
    assert store.get(expect) == data
    assert store.put_file(f) == expect  # idempotent
    assert not [p for p in (tmp_path / "store" / "objects").rglob(".tmp-*")]


def test_ingest_file_records_same_hash_and_size_as_before(migrated_sqlite_engine, tmp_path):
    from docket.core.db.engine import get_session_factory
    from docket.services.sources.manager import SourceManager

    sf = get_session_factory(migrated_sqlite_engine)
    mgr = EvidenceManager(ContentAddressedStore(tmp_path / "store"), sf)
    folder = tmp_path / "d"
    folder.mkdir()
    src = SourceManager(sf).register_source(folder)
    f = folder / "a.txt"
    f.write_bytes(b"hello world")
    v = mgr.ingest_file(src.id, f, parser_name="p", parser_version="1")
    assert v.content_hash == hashlib.sha256(b"hello world").hexdigest() and v.byte_size == 11
    assert mgr.get_evidence_bytes(v) == b"hello world"
    assert mgr.ingest_file(src.id, f, parser_name="p", parser_version="1").id == v.id


def test_oversized_file_fails_only_that_file(migrated_sqlite_engine, tmp_path):
    sf, pipeline = _pipeline(migrated_sqlite_engine, tmp_path, FakeParser(), Settings(max_file_bytes=60_000))
    source = _setup(tmp_path, sf)  # a.docx (small)
    big = tmp_path / "docs" / "big.docx"
    _write_docx(big, "H", "word " * 20)
    big.write_bytes(big.read_bytes() + b"\0" * 100_000)  # still a valid zip, but over the cap
    result = pipeline.run_ingestion_for_source(source.id)
    by = {r.path.name: r for r in result.file_results}
    assert by["a.docx"].status == "ingested"
    assert by["big.docx"].status == "failed" and "too large" in by["big.docx"].error
    with sf() as s:
        assert len(s.execute(select(EvidenceVersion)).scalars().all()) == 1  # nothing stored for the big one
