from __future__ import annotations

from pathlib import Path

import typer

from docket import __version__
from docket.cli.context import build_context
from docket.db.models import SourceStatus
from docket.ingestion.pipeline import SourceNotActiveError
from docket.query.service import QueryService
from docket.sources.manager import SourceNotFoundError
from docket.sources.watcher import SourceWatcher

app = typer.Typer(name="docket", help="Local-first, evidence-backed work intelligence assistant.")
sources_app = typer.Typer(help="Manage registered sources (local folders).")
app.add_typer(sources_app, name="sources")


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    version: bool = typer.Option(False, "--version", help="Show the version and exit."),
) -> None:
    if version:
        typer.echo(f"docket {__version__}")
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())


# -- sources -----------------------------------------------------------


@sources_app.command("add")
def sources_add(
    path: Path = typer.Argument(..., help="Local folder to register as a source."),
) -> None:
    """Register a local folder as a source."""
    context = build_context()
    try:
        source = context.source_manager.register_source(path)
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(source.id)


@sources_app.command("list")
def sources_list() -> None:
    """List registered sources."""
    context = build_context()
    sources = context.source_manager.list_sources()
    if not sources:
        typer.echo("No sources registered.")
        return
    typer.echo(f"{'ID':<40}{'STATUS':<12}PATH")
    for source in sources:
        typer.echo(f"{source.id:<40}{source.status.value:<12}{source.path}")


# -- ingest --------------------------------------------------------------


@app.command("ingest")
def ingest(
    source_id: str = typer.Argument(None, help="Source id to ingest."),
    all_sources: bool = typer.Option(False, "--all", help="Ingest every active source."),
) -> None:
    """Run (incremental) ingestion for one source, or every active source."""
    if bool(source_id) == bool(all_sources):
        typer.echo("Error: pass exactly one of a source_id or --all.", err=True)
        raise typer.Exit(code=1)

    context = build_context()

    if all_sources:
        target_ids = [
            source.id
            for source in context.source_manager.list_sources()
            if source.status == SourceStatus.ACTIVE
        ]
        if not target_ids:
            typer.echo("No active sources to ingest.")
            raise typer.Exit(code=0)
    else:
        target_ids = [source_id]

    exit_code = 0
    for target_id in target_ids:
        try:
            result = context.pipeline.run_ingestion_for_source(target_id)
        except (SourceNotFoundError, SourceNotActiveError) as exc:
            typer.echo(f"Error ingesting {target_id}: {exc}", err=True)
            exit_code = 1
            continue

        chunks_written = sum(r.chunks_written for r in result.file_results)
        typer.echo(
            f"[{result.source_id}] job={result.job_id} status={result.status} "
            f"files_processed={result.files_processed} files_failed={result.files_failed} "
            f"chunks_written={chunks_written}"
        )
        for file_result in result.file_results:
            if file_result.status == "failed":
                typer.echo(f"  FAILED: {file_result.path} -- {file_result.error}")

        if result.status != "succeeded":
            exit_code = 1

    raise typer.Exit(code=exit_code)


# -- query -----------------------------------------------------------------


@app.command("query")
def query(question: str = typer.Argument(..., help="Question to ask over ingested evidence.")) -> None:
    """Ask a question, answered (with citations) from ingested evidence."""
    context = build_context()

    table = context.vector_writer.table
    if table is None:
        typer.echo("No content has been indexed yet. Run `docket ingest` first.")
        raise typer.Exit(code=1)

    query_service = QueryService(
        engine=context.engine,
        table=table,
        gateway=context.gateway,
        resolver=context.resolver,
        settings=context.settings,
    )
    result = query_service.ask(question)

    typer.echo(f"[{result.mode}] {result.answer}")
    if result.citations:
        typer.echo("\nCitations:")
        for citation in result.citations:
            typer.echo(f"  {citation.citation_label}")
    if result.validation_warnings:
        typer.echo("\nWarnings:")
        for warning in result.validation_warnings:
            typer.echo(f"  {warning}")


# -- watch -------------------------------------------------------------


@app.command("watch")
def watch(source_id: str = typer.Argument(..., help="Source id to watch for changes.")) -> None:
    """Watch a source's folder and re-ingest on any filesystem change.

    Long-running foreground command -- blocks until interrupted (Ctrl+C).
    """
    context = build_context()
    try:
        source = context.source_manager.get_source(source_id)
    except SourceNotFoundError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(f"Watching {source.path} (source_id={source_id})... press Ctrl+C to stop.")
    watcher = SourceWatcher(context.pipeline)
    try:
        watcher.watch(source_id, Path(source.path))
    except KeyboardInterrupt:
        typer.echo("Stopped watching.")


if __name__ == "__main__":
    app()
