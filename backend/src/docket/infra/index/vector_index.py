"""`IndexWriter` backed by a LanceDB table (the semantic/vector index).

Table schema: ``chunk_id: str, source_id: str, text: str, vector: list[float],
evidence_version_id: str``. Tables written before `evidence_version_id` existed
are upgraded in place on first open (see `LanceIndexWriter._open_table`).
Unlike `spike/ingest.py`, which nuked and fully re-created the table on every
run (``mode="overwrite"``), this writer does true incremental upsert via
LanceDB's ``merge_insert`` (match-and-update-or-insert keyed by `chunk_id`),
so re-ingesting unchanged content never touches rows that are already
correct.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Sequence

from sqlalchemy import Engine, text

from docket.infra.index.base import ChunkRecord

_VECTOR_COLUMN = "vector"
_VERSION_COLUMN = "evidence_version_id"


def _sql_quote(value: str) -> str:
    """Quote a value for embedding in a LanceDB SQL-like predicate string.

    LanceDB's `delete`/`where` take a raw SQL predicate string rather than a
    parameterized query, so values have to be embedded directly. Escape
    embedded single quotes the standard SQL way (double them) so a chunk_id
    or source_id containing a quote can't break out of the string literal.
    """
    return "'" + value.replace("'", "''") + "'"


def _sql_in_list(values: Sequence[str]) -> str:
    return ", ".join(_sql_quote(v) for v in values)


class LanceIndexWriter:
    """`IndexWriter` for the LanceDB vector table."""

    def __init__(
        self, db_path: str | Path, table_name: str = "chunks", *, engine: Engine | None = None
    ):
        import lancedb

        self._db = lancedb.connect(str(db_path))
        self._table_name = table_name
        # SQLite engine, used only to backfill `evidence_version_id` on a
        # legacy table (from `chunks`, the source of truth).
        self._engine = engine

    def _open_table(self):
        """Return the table, or `None` if it hasn't been created yet."""
        try:
            table = self._db.open_table(self._table_name)
        except ValueError:
            # lancedb raises ValueError("Table '<name>' was not found") --
            # there is no dedicated "table not found" exception type.
            return None
        if _VERSION_COLUMN not in table.schema.names:
            self._add_version_column(table)
        return table

    def _add_version_column(self, table) -> None:
        """Upgrade a table written before `evidence_version_id` existed: add
        the column and backfill it from SQLite `chunks`. Rows with no
        matching `chunks` row stay NULL, so they never pass the version
        prefilter in `vector_search` (reconcile removes them). Refuses to
        run without an engine rather than serve a table that would silently
        mix versions."""
        if self._engine is None:
            raise RuntimeError(
                f"LanceDB table '{self._table_name}' has no {_VERSION_COLUMN} column and "
                "this LanceIndexWriter has no SQLite engine to backfill it from"
            )
        table.add_columns({_VERSION_COLUMN: "cast(NULL as string)"})
        with self._engine.connect() as conn:
            rows = conn.execute(text("SELECT id, evidence_version_id FROM chunks")).all()
        chunk_ids_by_version: dict[str, list[str]] = defaultdict(list)
        for chunk_id, version_id in rows:
            chunk_ids_by_version[version_id].append(chunk_id)
        for version_id, chunk_ids in chunk_ids_by_version.items():
            table.update(
                where=f"chunk_id IN ({_sql_in_list(chunk_ids)})",
                values={_VERSION_COLUMN: version_id},
            )

    @property
    def table(self):
        """Public accessor for the underlying LanceDB table object (or
        `None` if it hasn't been created yet -- e.g. before any chunks have
        been indexed). Needed by callers outside this module (e.g. the CLI,
        wiring a table into `docket.infra.retrieval.hybrid.hybrid_search` via
        `QueryService`) that shouldn't reach into the `_open_table` internal."""
        return self._open_table()

    def maintain(self, **kwargs) -> dict | None:
        """Post-ingest ANN-index build / optimize / compaction (see
        `maintain_table`). Caller must hold the writer lock."""
        return _writer_maintain(self, **kwargs)

    def vector_dimension(self) -> int | None:
        """Dimension of the stored vectors, or `None` if the table doesn't
        exist yet."""
        table = self._open_table()
        if table is None:
            return None
        return table.schema.field(_VECTOR_COLUMN).type.list_size

    def upsert(
        self, records: Sequence[ChunkRecord], embeddings: Sequence[list[float]] | None
    ) -> None:
        if not records:
            return
        if embeddings is None or len(embeddings) != len(records):
            raise ValueError(
                "LanceIndexWriter.upsert requires one embedding per record "
                f"(got {len(records)} records, "
                f"{0 if embeddings is None else len(embeddings)} embeddings)"
            )

        rows = [
            {
                "chunk_id": record.chunk_id,
                "source_id": record.source_id,
                "text": record.text,
                _VERSION_COLUMN: record.evidence_version_id,
                _VECTOR_COLUMN: embedding,
            }
            for record, embedding in zip(records, embeddings)
        ]

        table = self._open_table()
        if table is None:
            self._db.create_table(self._table_name, data=rows)
            return

        table.merge_insert("chunk_id").when_matched_update_all().when_not_matched_insert_all().execute(
            rows
        )

    def delete(self, chunk_ids: Sequence[str]) -> None:
        chunk_ids = list(chunk_ids)
        if not chunk_ids:
            return
        table = self._open_table()
        if table is None:
            return
        table.delete(f"chunk_id IN ({_sql_in_list(chunk_ids)})")

    def delete_by_source(self, source_id: str) -> None:
        table = self._open_table()
        if table is None:
            return
        table.delete(f"source_id = {_sql_quote(source_id)}")

    def delete_by_version(self, evidence_version_id: str) -> None:
        table = self._open_table()
        if table is None:
            return
        table.delete(f"{_VERSION_COLUMN} = {_sql_quote(evidence_version_id)}")

    def existing_chunk_ids(self, chunk_ids: Sequence[str]) -> set[str]:
        chunk_ids = list(chunk_ids)
        if not chunk_ids:
            return set()
        table = self._open_table()
        if table is None:
            return set()
        rows = table.search().where(f"chunk_id IN ({_sql_in_list(chunk_ids)})").to_list()
        return {row["chunk_id"] for row in rows}

    def chunk_ids_for_source(self, source_id: str) -> set[str]:
        table = self._open_table()
        if table is None:
            return set()
        rows = table.search().where(f"source_id = {_sql_quote(source_id)}").to_list()
        return {row["chunk_id"] for row in rows}


# --- ANN index + compaction -------------------------------------------------


def pq_sub_vectors(dim: int) -> int:
    """PQ sub-vector count: dim/8 when divisible (1024 -> 128), otherwise the
    divisor of `dim` giving 4..16-wide sub-vectors closest to 8 (PQ requires
    `dim % num_sub_vectors == 0`)."""
    if dim % 8 == 0:
        return dim // 8
    candidates = [d for d in range(1, dim + 1) if dim % d == 0 and 4 <= dim // d <= 16]
    if candidates:
        return min(candidates, key=lambda d: abs(dim // d - 8))
    return 1


def ann_num_partitions(rows: int) -> int:
    """About sqrt(rows), kept in a sane range for IVF."""
    return max(1, min(8192, int(round(rows**0.5))))


def vector_index_stats(table) -> dict | None:
    """State of the vector ANN index on `table`: None when it is flat."""
    for cfg in table.list_indices():
        if list(cfg.columns) == [_VECTOR_COLUMN]:
            stats = table.index_stats(cfg.name)
            return {
                "name": cfg.name,
                "index_type": stats.index_type,
                "indexed_rows": stats.num_indexed_rows,
                "unindexed_rows": stats.num_unindexed_rows,
                "size_bytes": getattr(cfg, "size_bytes", None),
            }
    return None


def _build_ann_index(table, rows: int, dim: int) -> dict:
    from lancedb.index import IvfPq

    params = {
        "index_type": "IVF_PQ",
        "metric": "l2",  # the table has always been searched with LanceDB's default (l2)
        "num_partitions": ann_num_partitions(rows),
        "num_sub_vectors": pq_sub_vectors(dim),
        "rows_at_build": rows,
    }
    table.create_index(
        _VECTOR_COLUMN,
        config=IvfPq(
            distance_type="l2",
            num_partitions=params["num_partitions"],
            num_sub_vectors=params["num_sub_vectors"],
        ),
        replace=True,
    )
    return params


def maintain_table(
    table,
    *,
    min_rows: int,
    compact: bool,
    keep_hours: int,
    previous_params: dict | None = None,
) -> dict:
    """Post-ingest maintenance of a vector table. Returns a report:
    ``{"rows", "action", "ann": params-or-None, "compacted": bool}``.

    - rows < `min_rows` and no index: nothing but compaction (flat scan).
    - rows >= `min_rows` and no index: build IVF_PQ.
    - index exists and the table has since grown >2x past the build size:
      rebuild it (partition count / PQ codebooks are stale).
    - otherwise `optimize()` folds unindexed rows into the index (until then
      they are still found: LanceDB flat-scans the unindexed tail and merges).
    Compaction and old-version cleanup (keeping `keep_hours` of history) run
    via the same `optimize()` call when `compact` is set.
    """
    from datetime import timedelta

    report: dict = {"rows": 0, "action": "none", "ann": previous_params, "compacted": False}
    rows = table.count_rows()
    report["rows"] = rows
    stats = vector_index_stats(table)
    dim = table.schema.field(_VECTOR_COLUMN).type.list_size
    if stats is None and rows >= min_rows and rows > 0:
        report["ann"] = _build_ann_index(table, rows, dim)
        report["action"] = "built"
    elif stats is not None:
        built_at = (previous_params or {}).get("rows_at_build") or stats["indexed_rows"]
        if rows > 2 * max(built_at, 1):
            report["ann"] = _build_ann_index(table, rows, dim)
            report["action"] = "rebuilt"
        elif stats["unindexed_rows"] > 0:
            report["action"] = "optimized"
    if compact or report["action"] == "optimized":
        table.optimize(cleanup_older_than=timedelta(hours=keep_hours))
        report["compacted"] = True
    return report


def _writer_maintain(self, *, manifest_path=None, min_rows=None, compact=None, keep_hours=None):
    from docket.infra.index import tuning
    from docket.infra.index.manifest import read_manifest, record_ann_index

    table = self._open_table()
    if table is None:
        return None
    previous = None
    if manifest_path is not None:
        manifest = read_manifest(manifest_path)
        previous = manifest.ann_index if manifest else None
    report = maintain_table(
        table,
        min_rows=tuning.ann_min_rows() if min_rows is None else min_rows,
        compact=tuning.compact_after_ingest() if compact is None else compact,
        keep_hours=tuning.compact_keep_hours() if keep_hours is None else keep_hours,
        previous_params=previous,
    )
    if manifest_path is not None and report["ann"] != previous:
        record_ann_index(manifest_path, report["ann"])
    return report

