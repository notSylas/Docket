"""Ingestion UX for `/ingest`: progress spinner wired to `ProgressEvent`, plus
the per-source result summary.

Plain functions taking `session` first, matching `source_commands`/
`query_flow` -- these need `session.console.status(...)`, `session.context`,
and `session.state`, so a wrapping class would hold no state of its own.
"""

from __future__ import annotations

from typing import Any

from docket.core.db.models import SourceStatus
from docket.services.ingestion.pipeline import SUPPORTED_EXTENSIONS, ProgressEvent, SourceNotActiveError
from docket.services.sources.manager import SourceNotFoundError


def cmd_ingest(session: Any, arg: str) -> None:
    if not arg or arg.lower() == "all":
        target_ids = [
            s.id
            for s in session.context.source_manager.list_sources()
            if s.status == SourceStatus.ACTIVE
        ]
        if not target_ids:
            session.say("No active sources to ingest. Use /add <folder> first.")
            return
    else:
        target_ids = [arg]

    try:
        _ingest_targets(session, target_ids)
    finally:
        session.state.refresh_sources(session.context.source_manager)
        session.sync_state()


def _ingest_targets(session: Any, target_ids: list[str]) -> None:
    ctx = session.context
    vars_ = vars(ctx) if hasattr(ctx, "__dict__") else {}
    # Constructing the pipeline builds the Docling parser, which loads
    # models (slow the first time). Say so before it happens.
    cold = "pipeline" not in vars_ and "parser" not in vars_
    for target_id in target_ids:
        status = None
        try:
            with session.console.status(f"Ingesting {target_id}...") as status:
                if cold:
                    session.say(
                        "Loading document parser (first time only — "
                        "this can take a moment)…",
                        style="dim",
                    )
                pipeline = ctx.pipeline
                cold = False

                def on_progress(event: ProgressEvent, status=status) -> None:
                    if event.kind == "start":
                        status.update(
                            f"Ingesting {event.index}/{event.total}: {event.path.name}"
                        )
                    elif event.result is not None and event.result.status == "failed":
                        session.error(f"FAILED: {event.path} -- {event.result.error}")

                result = pipeline.run_ingestion_for_source(target_id, progress=on_progress)
        except (SourceNotFoundError, SourceNotActiveError) as exc:
            session.error(f"Error ingesting {target_id}: {exc}")
            continue
        _summarize(session, target_id, result)
        stale = len(pipeline.find_stale_versions(target_id))
        if stale:
            session.say(
                f"{stale} already-ingested file(s) have chunks from an older recipe; "
                "run `docket ingest --rechunk` to update.",
                style="dim",
            )


def _summarize(session: Any, target_id: str, result: Any) -> None:
    if result.files_processed == 0 and result.status == "failed":
        folder = next(
            (str(s.path) for s in session.context.source_manager.list_sources()
             if s.id == result.source_id),
            result.source_id,
        )
        exts = ", ".join(sorted(SUPPORTED_EXTENSIONS))
        session.say(f"No supported files found in {folder} (supported: {exts}).")
        return
    ingested = sum(1 for r in result.file_results if r.status == "ingested")
    unchanged = sum(1 for r in result.file_results if r.status == "unchanged")
    chunks = sum(r.chunks_written for r in result.file_results)
    session.say(
        f"{result.source_id}: {ingested} ingested, {unchanged} unchanged, "
        f"{result.files_failed} failed — {chunks} chunks written"
    )
