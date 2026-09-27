"""`LancePageIndexWriter` -- a LanceDB table of per-page VLM descriptions,
used only as a retrieval-ranking signal (visual retrieval checkpoint 2).

Table schema: ``evidence_version_id: str, source_id: str, page_no: int,
description: str, vector: list[float]``. Modeled closely on
`docket.infra.index.vector_index.LanceIndexWriter`, but keyed by the composite
``(evidence_version_id, page_no)`` pair instead of a single ``chunk_id`` --
LanceDB's ``merge_insert`` accepts a list of column names as its match key
(verified against the installed lancedb version), so no synthetic
concatenated key column is needed.

Nothing in this module -- or anything it writes -- is citable evidence. The
``description`` column is generative VLM output that has never been verified
against the source document (unlike Docling's deterministic text
extraction); it must never reach `Chunk.text`, `validate_citations`, or
`EvidenceResolver`'s output. See `docket.services.ingestion.pipeline` for where rows
here actually get written (gated behind `settings.visual_index_enabled`).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

_VECTOR_COLUMN = "vector"
_KEY_COLUMNS = ["evidence_version_id", "page_no"]


@dataclass(frozen=True)
class PageRecord:
    """The subset of page-level data this index needs to write one row."""

    evidence_version_id: str
    source_id: str
    page_no: int
    description: str


def _sql_quote(value: str) -> str:
    """Same escaping as `vector_index._sql_quote` -- see there for why."""
    return "'" + value.replace("'", "''") + "'"


class LancePageIndexWriter:
    """`IndexWriter`-shaped writer for the LanceDB "pages" table."""

    def __init__(self, db_path: str | Path, table_name: str = "pages"):
        import lancedb

        self._db = lancedb.connect(str(db_path))
        self._table_name = table_name

    def _open_table(self):
        """Return the table, or `None` if it hasn't been created yet."""
        try:
            return self._db.open_table(self._table_name)
        except ValueError:
            # lancedb raises ValueError("Table '<name>' was not found") --
            # there is no dedicated "table not found" exception type.
            return None

    @property
    def table(self):
        """Public accessor for the underlying LanceDB table object (or
        `None` if it hasn't been created yet)."""
        return self._open_table()

    def upsert(
        self, records: Sequence[PageRecord], embeddings: Sequence[list[float]] | None
    ) -> None:
        if not records:
            return
        if embeddings is None or len(embeddings) != len(records):
            raise ValueError(
                "LancePageIndexWriter.upsert requires one embedding per record "
                f"(got {len(records)} records, "
                f"{0 if embeddings is None else len(embeddings)} embeddings)"
            )

        rows = [
            {
                "evidence_version_id": record.evidence_version_id,
                "source_id": record.source_id,
                "page_no": record.page_no,
                "description": record.description,
                _VECTOR_COLUMN: embedding,
            }
            for record, embedding in zip(records, embeddings)
        ]

        table = self._open_table()
        if table is None:
            self._db.create_table(self._table_name, data=rows)
            return

        table.merge_insert(_KEY_COLUMNS).when_matched_update_all().when_not_matched_insert_all().execute(
            rows
        )

    def delete(self, keys: Sequence[tuple[str, int]]) -> None:
        """Remove rows for the given `(evidence_version_id, page_no)` pairs."""
        keys = list(keys)
        if not keys:
            return
        table = self._open_table()
        if table is None:
            return
        predicate = " OR ".join(
            f"(evidence_version_id = {_sql_quote(ev_id)} AND page_no = {page_no})"
            for ev_id, page_no in keys
        )
        table.delete(predicate)

    def delete_by_source(self, source_id: str) -> None:
        table = self._open_table()
        if table is None:
            return
        table.delete(f"source_id = {_sql_quote(source_id)}")
