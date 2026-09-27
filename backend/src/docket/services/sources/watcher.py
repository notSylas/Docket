"""`SourceWatcher` -- re-runs a source's ingestion pipeline on filesystem
changes.

Deliberately coarse-grained for this checkpoint: any create/modify/delete
event anywhere under the watched path re-runs
`IngestionPipeline.run_ingestion_for_source` for the *whole* source, rather
than dispatching a single-file re-ingest. This is correct (every unchanged
file is a cheap no-op thanks to `EvidenceManager`'s content-hash idempotency
and `IndexManager`'s skip-if-already-indexed behavior) even though it isn't
the most efficient possible response to one file changing -- building
fine-grained single-file dispatch is an optimization for a later checkpoint,
not something correctness requires here.
"""

from __future__ import annotations

import threading
from pathlib import Path

import watchfiles

from docket.ingestion.pipeline import IngestionPipeline


class SourceWatcher:
    def __init__(self, pipeline: IngestionPipeline) -> None:
        self._pipeline = pipeline

    def watch(
        self,
        source_id: str,
        path: Path,
        *,
        stop_event: threading.Event | None = None,
    ) -> None:
        """Block, watching `path` for changes. On every batch of filesystem
        change events `watchfiles.watch` yields, re-run ingestion for
        `source_id`. Returns when `stop_event` is set (or the underlying
        `watchfiles.watch` generator otherwise stops)."""
        for _changes in watchfiles.watch(path, stop_event=stop_event):
            self._pipeline.run_ingestion_for_source(source_id)
