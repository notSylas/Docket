"""`SourceWatcher` -- the foreground `docket watch` loop: ingest only the
paths that changed.

Every batch of filesystem events `watchfiles.watch` yields is filtered by the
source's ignore rules and the supported extensions (`freshness.classify_event`)
and handed to `IngestionPipeline.run_ingestion_for_paths`, the same
path-level entry the in-window freshness monitor uses: changed and new files
are hashed (unchanged content is a cheap no-op), deleted files are superseded
and leave the index. The whole source is no longer re-run on every event.

A folder that appears or disappears triggers one bounded `catch_up` for that
source (when a `session_factory` is given) so the files inside are picked up
or retired. Debouncing is `watchfiles`' own (its `debounce` setting).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path

import watchfiles

from docket.services.ingestion.ignore import IgnoreRules
from docket.services.ingestion.pipeline import IngestionPipeline, IngestionJobResult
from docket.services.sources.freshness import catch_up, classify_event

logger = logging.getLogger(__name__)


class SourceWatcher:
    def __init__(self, pipeline: IngestionPipeline, *, session_factory=None) -> None:
        self._pipeline = pipeline
        self._session_factory = session_factory

    def _rules_json(self, source_id: str) -> object:
        if self._session_factory is None:
            return None
        from docket.core.db.models import Source

        with self._session_factory() as session:
            source = session.get(Source, source_id)
            return None if source is None else source.rules_json

    def watch(
        self,
        source_id: str,
        path: Path,
        *,
        stop_event: threading.Event | None = None,
        on_result: Callable[[IngestionJobResult], None] | None = None,
    ) -> None:
        """Block, watching `path`. Each event batch re-ingests just the
        affected paths. Returns when `stop_event` is set (or the underlying
        generator stops)."""
        root = Path(path)
        rules = IgnoreRules.for_root(root, self._rules_json(source_id))
        for changes in watchfiles.watch(root, stop_event=stop_event):
            paths: set[Path] = set()
            rescan = False
            for change, raw in changes:
                keep, scan = classify_event(root, rules, change, raw)
                if keep:
                    paths.add(Path(raw))
                rescan = rescan or scan
            if rescan and self._session_factory is not None:
                try:
                    paths.update(catch_up(source_id, session_factory=self._session_factory).paths)
                except Exception:  # noqa: BLE001 -- never end the watch for a scan problem
                    logger.debug("catch-up after a folder change failed", exc_info=True)
            if not paths:
                continue
            result = self._pipeline.run_ingestion_for_paths(
                source_id, sorted(paths), missing="skip"
            )
            if on_result is not None:
                on_result(result)
