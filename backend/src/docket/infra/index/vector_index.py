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
