"""`reindex` -- rebuild FTS5 and the LanceDB `chunks`/`pages` tables from
SQLite under the currently configured embedding model, then write the index
manifest (see `docket.infra.index.manifest`). Embedded and FTS-indexed chunk
text gets the file-name/heading context prefix (`docket.infra.index.context`);
this is how an index built before the prefix existed is upgraded.

What is indexed: the chunks of READY evidence versions whose source is ACTIVE
or MISSING -- what ingestion indexes (chunks are indexed before a version is
marked READY, and superseded versions' entries are removed). REVOKED,
TOMBSTONED, HARD_DELETE_PENDING and DELETED sources are left out: they can't
return to ACTIVE and retrieval never serves them. A MISSING source can come
back without re-ingestion, so it stays indexed. `pages` rows are re-embedded
from their stored `description` (no VLM calls) for the same set of versions;
rows of any other version are dropped.

Safety (LanceDB OSS has no `rename_table`): everything slow and fallible --
reading SQLite and embedding -- happens first, into scratch tables
`chunks__reindex`/`pages__reindex`, leaving the live index untouched. Only
then are the live tables swapped: each is replaced by one atomic
`create_table(mode="overwrite")` commit (LanceDB keeps the previous version),
then FTS5 is rewritten in a single SQLite transaction, and the manifest is
written LAST. If any swap step fails, the live LanceDB tables are restored
to their previous versions and the old manifest is untouched. A hard crash
(kill -9) between swap steps is not rolled back automatically, but the
manifest still names the old model, so queries refuse (rather than serve
mixed spaces) until `docket reindex` is re-run.

Resumable: the scratch tables live in a separate LanceDB directory next to the
live one (`<lancedb>-reindex`), with a small checkpoint file beside it
(`<lancedb>-reindex.json`). After each embedded batch the checkpoint records
how many rows are staged and a digest of exactly what was embedded (model, row
keys and index texts). An interrupted run (Ctrl-C, crash, Ollama down) leaves
both in place; the next `docket reindex` re-verifies the staged prefix against
the current rows and continues after it instead of re-embedding from zero. A
changed model or any difference in the staged prefix discards the scratch and
starts clean. They are removed after a successful swap.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import pyarrow as pa
from sqlalchemy import Engine, bindparam, text

from docket.core.writer_lock import WriterLock
from docket.core.db.models import SourceStatus, VersionStatus
from docket.infra.index.context import INDEX_TEXT_VERSION, index_text_for_chunk
from docket.infra.index.fts_index import ensure_fts_chunk_map_in
from docket.infra.index.manifest import new_manifest, read_manifest, write_manifest
from docket.infra.index.vector_index import LanceIndexWriter
from docket.infra.inference.gateway import InferenceGateway, embed_texts

_CHUNKS = "chunks"
_PAGES = "pages"
_STAGING_SUFFIX = "__reindex"

_ELIGIBLE_CHUNKS_SQL = text(
    "SELECT chunks.id, chunks.source_id, chunks.evidence_version_id, chunks.text, "
    "evidence_versions.file_path, evidence_units.locator_json, chunks.heading "
    "FROM chunks "
    "JOIN evidence_versions ON evidence_versions.id = chunks.evidence_version_id "
    "LEFT JOIN evidence_units ON evidence_units.id = chunks.evidence_unit_id "
    "JOIN sources ON sources.id = chunks.source_id "
    "WHERE evidence_versions.status = :ready AND sources.status IN :source_statuses "
    "ORDER BY chunks.id"
).bindparams(bindparam("source_statuses", expanding=True))

_ELIGIBLE_VERSIONS_SQL = text(
    "SELECT evidence_versions.id FROM evidence_versions "
    "JOIN sources ON sources.id = evidence_versions.source_id "
    "WHERE evidence_versions.status = :ready AND sources.status IN :source_statuses"
).bindparams(bindparam("source_statuses", expanding=True))

_INDEXED_SOURCE_STATUSES = [SourceStatus.ACTIVE.name, SourceStatus.MISSING.name]


@dataclass(frozen=True)
class ReindexResult:
    chunks: int
    pages: int
    embed_model: str
    embed_dimension: int
    resumed_rows: int = 0  # rows taken from an interrupted run's scratch tables


def _open(db: Any, name: str):
    try:
        return db.open_table(name)
    except ValueError:  # lancedb has no dedicated "table not found" exception
        return None


class _Checkpoint:
    """Progress of the scratch tables, persisted atomically after every batch."""

    def __init__(self, path: Path, embed_model: str) -> None:
        self.path = path
        self.embed_model = embed_model
        self.tables: dict[str, dict] = {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("embed_model") == embed_model and isinstance(data.get("tables"), dict):
                self.tables = data["tables"]
        except (OSError, ValueError, AttributeError):
            pass

    def record(self, table: str, rows: int, digest: str) -> None:
        self.tables[table] = {"rows": rows, "digest": digest}
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps({"embed_model": self.embed_model, "tables": self.tables}), encoding="utf-8")
        with open(tmp, "rb") as handle:
            os.fsync(handle.fileno())
        os.replace(tmp, self.path)

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


def _row_digest(hasher: Any, key: tuple, index_text: str) -> None:
    hasher.update(json.dumps([list(key), index_text], ensure_ascii=False).encode("utf-8"))
    hasher.update(b"\n")


def _build_staging(
    db: Any,
    name: str,
    rows: list[dict],
    texts: list[str],
    gateway: InferenceGateway,
    batch_size: int,
    sleep: Callable[[float], None],
    *,
    key_columns: tuple[str, ...] = ("chunk_id",),
    checkpoint: _Checkpoint | None = None,
) -> tuple[int | None, int]:
    """Embed `texts` batch by batch into a scratch table `name` (rows[i] gets
    the vector of texts[i]). Returns `(vector dimension or None if there was
    nothing to embed, rows resumed from an earlier interrupted run)`.

    With a `checkpoint`, a scratch table left by an interrupted run is reused
    when its recorded prefix still matches `rows`/`texts` exactly; otherwise it
    is dropped and the build starts from the first row."""
    hasher = hashlib.sha256()
    keys = [tuple(row[c] for c in key_columns) for row in rows]
    dimension: int | None = None
    staging = None
    start = 0
    saved = checkpoint.tables.get(name) if checkpoint is not None else None
    existing = _open(db, name)
    if existing is not None:
        usable = False
        if saved and 0 < saved["rows"] <= len(rows):
            n = saved["rows"]
            prefix = hashlib.sha256()
            for key, index_text in zip(keys[:n], texts[:n]):
                _row_digest(prefix, key, index_text)
            if prefix.hexdigest() == saved["digest"] and existing.count_rows() == n:
                staged = existing.to_arrow().select(list(key_columns)).to_pylist()
                usable = [tuple(r[c] for c in key_columns) for r in staged] == keys[:n]
                if usable:
                    staging, start = existing, n
                    dimension = existing.schema.field("vector").type.list_size
                    hasher = prefix
        if not usable:
            db.drop_table(name)
    for start_at in range(start, len(rows), batch_size):
        batch_rows = rows[start_at : start_at + batch_size]
        vectors = embed_texts(
            gateway,
            texts[start_at : start_at + batch_size],
            batch_size=batch_size,
            sleep=sleep,
        )
        if dimension is None:
            dimension = len(vectors[0])
        if any(len(v) != dimension for v in vectors):
            raise ValueError("embedding model returned vectors of differing dimensions")
        data = [{**row, "vector": vector} for row, vector in zip(batch_rows, vectors)]
        if staging is None:
            staging = db.create_table(name, data=data, mode="overwrite")
        else:
            staging.add(data)
        end = start_at + len(batch_rows)
        for key, index_text in zip(keys[start_at:end], texts[start_at:end]):
            _row_digest(hasher, key, index_text)
        if checkpoint is not None:
            checkpoint.record(name, end, hasher.copy().hexdigest())
    return dimension, start


def reindex(
    *,
    engine: Engine,
    db_path: str | Path,
    gateway: InferenceGateway,
    manifest_path: Path,
    embed_model: str,
    batch_size: int = 32,
    sleep: Callable[[float], None] | None = None,
    tokenizer: str | None = None,
    writer_lock: WriterLock | None = None,
) -> ReindexResult:
    with (writer_lock or WriterLock(manifest_path.parent)).hold("rebuilding the search index"):
        return _reindex_unlocked(
            engine=engine, db_path=db_path, gateway=gateway, manifest_path=manifest_path,
            embed_model=embed_model, batch_size=batch_size, sleep=sleep, tokenizer=tokenizer,
        )


def _reindex_unlocked(
    *,
    engine: Engine,
    db_path: str | Path,
    gateway: InferenceGateway,
    manifest_path: Path,
    embed_model: str,
    batch_size: int = 32,
    sleep: Callable[[float], None] | None = None,
    tokenizer: str | None = None,
) -> ReindexResult:
    import lancedb

    sleep = sleep or time.sleep
    db = lancedb.connect(str(db_path))
    # Scratch tables live in their own directory so the live database never lists
    # them and an interrupted run can be resumed (see the module docstring).
    scratch_dir = Path(db_path).with_name(Path(db_path).name + _STAGING_SUFFIX.replace("__", "-"))
    checkpoint = _Checkpoint(scratch_dir.with_name(scratch_dir.name + ".json"), embed_model)
    chunks_staging, pages_staging = _CHUNKS + _STAGING_SUFFIX, _PAGES + _STAGING_SUFFIX
    scratch = lancedb.connect(str(scratch_dir))
    completed = False

    with engine.connect() as conn:
        chunk_rows = conn.execute(
            _ELIGIBLE_CHUNKS_SQL,
            {"ready": VersionStatus.READY.name, "source_statuses": _INDEXED_SOURCE_STATUSES},
        ).all()
        # Tiered indexing: keyword-only (TEXT_READY) chunks have no vectors but
        # keep their FTS rows through a rebuild.
        provisional_rows = conn.execute(
            _ELIGIBLE_CHUNKS_SQL,
            {"ready": VersionStatus.TEXT_READY.name, "source_statuses": _INDEXED_SOURCE_STATUSES},
        ).all()
        version_ids = {
            row[0]
            for row in conn.execute(
                _ELIGIBLE_VERSIONS_SQL,
                {"ready": VersionStatus.READY.name, "source_statuses": _INDEXED_SOURCE_STATUSES},
            )
        }

    page_rows: list[dict] = []
    live_pages = _open(db, _PAGES)
    if live_pages is not None:
        columns = ["evidence_version_id", "source_id", "page_no", "description"]
        page_rows = [
            row
            for row in live_pages.to_arrow().select(columns).to_pylist()
            if row["evidence_version_id"] in version_ids
        ]

    try:
        # Phase 1: build scratch tables. The live index is not touched.
        chunk_dicts = [
            {
                "chunk_id": r[0],
                "source_id": r[1],
                "text": r[3],
                "evidence_version_id": r[2],
            }
            for r in chunk_rows
        ]
        # What is embedded and FTS-indexed carries the context prefix (same
        # function as ingestion); the stored `text` columns stay verbatim.
        index_texts = [index_text_for_chunk(r[4], r[5], r[6], r[3]) for r in chunk_rows]
        dimension, resumed_chunks = _build_staging(
            scratch, chunks_staging, chunk_dicts, index_texts, gateway, batch_size, sleep,
            checkpoint=checkpoint,
        )
        page_dimension, resumed_pages = _build_staging(
            scratch,
            pages_staging,
            page_rows,
            [r["description"] for r in page_rows],
            gateway,
            batch_size,
            sleep,
            key_columns=("evidence_version_id", "page_no"),
            checkpoint=checkpoint,
        )
        if dimension is None:
            dimension = page_dimension
        if dimension is None:
            # Nothing to embed: probe the model for its dimension.
            dimension = len(embed_texts(gateway, ["dimension probe"], sleep=sleep)[0])
        if page_dimension is not None and page_dimension != dimension:
            raise ValueError("embedding model returned vectors of differing dimensions")

        # Phase 2: swap. Each live table is replaced by one atomic commit;
        # `undo` restores whatever was already swapped if a later step fails.
        undo: list[Callable[[], None]] = []
        try:
            vector_type = pa.list_(pa.float32(), dimension)
            _swap(
                db,
                scratch,
                _CHUNKS,
                chunks_staging,
                pa.schema(
                    [
                        pa.field("chunk_id", pa.string()),
                        pa.field("source_id", pa.string()),
                        pa.field("text", pa.string()),
                        pa.field("evidence_version_id", pa.string()),
                        pa.field("vector", vector_type),
                    ]
                ),
                always=True,
                undo=undo,
            )
            _swap(
                db,
                scratch,
                _PAGES,
                pages_staging,
                pa.schema(
                    [
                        pa.field("evidence_version_id", pa.string()),
                        pa.field("source_id", pa.string()),
                        pa.field("page_no", pa.int64()),
                        pa.field("description", pa.string()),
                        pa.field("vector", vector_type),
                    ]
                ),
                always=live_pages is not None,
                undo=undo,
            )
            prov_dicts = [
                {"chunk_id": r[0], "source_id": r[1], "text": r[3], "evidence_version_id": r[2]}
                for r in provisional_rows
            ]
            prov_texts = [index_text_for_chunk(r[4], r[5], r[6], r[3]) for r in provisional_rows]
            _rewrite_fts(engine, chunk_dicts + prov_dicts, index_texts + prov_texts)
            previous = read_manifest(manifest_path)
            write_manifest(
                manifest_path,
                new_manifest(
                    embed_model,
                    dimension,
                    created_at=previous.created_at if previous else None,
                    tokenizer=tokenizer or (previous.tokenizer if previous else None),
                ),
            )
            # The swapped-in table is a fresh flat one: rebuild the ANN index
            # when it is big enough. Best effort -- search is correct without it.
            try:
                LanceIndexWriter(db_path).maintain(manifest_path=manifest_path)
            except Exception:  # noqa: BLE001
                pass
            completed = True
        except BaseException:
            for restore in reversed(undo):
                restore()
            raise
    finally:
        if completed:
            checkpoint.clear()
            shutil.rmtree(scratch_dir, ignore_errors=True)

    return ReindexResult(
        chunks=len(chunk_dicts),
        pages=len(page_rows),
        embed_model=embed_model,
        embed_dimension=dimension,
        resumed_rows=resumed_chunks + resumed_pages,
    )


def _swap(
    db: Any,
    scratch: Any,
    name: str,
    staging_name: str,
    empty_schema: pa.Schema,
    *,
    always: bool,
    undo: list[Callable[[], None]],
) -> None:
    """Replace live table `name` with the scratch table's rows (or an empty
    table when there are none), registering how to undo it. With `always`
    False and no scratch rows, the live table is left alone (it may not
    exist at all)."""
    staging = _open(scratch, staging_name)
    if staging is None and not always:
        return
    live = _open(db, name)
    old_version = live.version if live is not None else None
    if staging is not None:
        db.create_table(name, data=staging.to_arrow(), mode="overwrite")
    else:
        db.create_table(name, schema=empty_schema, mode="overwrite")
    if old_version is None:
        undo.append(lambda: db.drop_table(name))
    else:
        undo.append(lambda: db.open_table(name).restore(old_version))


def _rewrite_fts(engine: Engine, chunk_dicts: list[dict], index_texts: list[str]) -> None:
    """Replace the whole FTS5 table contents in one SQLite transaction, indexing
    the prefixed `index_texts` rather than the verbatim chunk text."""
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM fts_chunks"))
        for row, index_text in zip(chunk_dicts, index_texts):
            conn.execute(
                text(
                    "INSERT INTO fts_chunks (chunk_id, text, evidence_version_id, source_id) "
                    "VALUES (:chunk_id, :text, :evidence_version_id, :source_id)"
                ),
                {**row, "text": index_text},
            )
        ensure_fts_chunk_map_in(conn)
