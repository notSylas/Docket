from __future__ import annotations

import sys
import time
from pathlib import Path

import click
import typer
import typer.core
import typer.main

from docket import __version__
from docket.infra.index.manifest import IndexManifestMismatchError
from docket.infra.index.reindex import reindex as run_reindex
from docket.infra.inference.gateway import InferenceError
from docket.infra.parsing.tokens import get_token_counter
from docket.interfaces.cli.context import build_context
from docket.interfaces.cli.interactive import run_session
from docket.core.db.models import SourceStatus
from docket.services.ingestion.pipeline import SourceNotActiveError
from docket.services.query.service import QueryService
from docket.services.sources.manager import SourceNotFoundError
from docket.services.sources.watcher import SourceWatcher

_EVAL_COMMAND_NAME = "eval"
_eval_click_command: click.Command | None = None


def _eval_click() -> click.Command:
    """Builds the `docket eval ...` sub-app's click command, importing
    `docket.eval.cli` for the first time right here -- not at
    `docket.interfaces.cli.main` module-import time (see `_LazyEvalGroup`).

    `docket.eval.cli` itself already defers every *heavy* import (ollama,
    docling) into its own command bodies; the only thing this function
    defers is the module import of `docket.eval.cli`, which is what let
    `eval/runner.py` end up with a fragile, accidental-import-order
    dependency on `docket.interfaces.cli.context` (see that module's `for_testing`
    classmethod, and this project's Phase 5 restructuring notes).
    """
    global _eval_click_command
    if _eval_click_command is None:
        from docket.eval.cli import eval_app

        _eval_click_command = typer.main.get_command(eval_app)
        # `eval_app` (unlike `sources_app`/`formulas_app`) is never built
        # with its own `name=` -- `app.add_typer(eval_app, name="eval")`
        # used to supply it; replicate that here now that this bypasses
        # `add_typer` entirely.
        _eval_click_command.name = _EVAL_COMMAND_NAME
    return _eval_click_command


class _LazyEvalGroup(typer.core.TyperGroup):
    """`TyperGroup` that resolves the `eval` sub-app on demand instead of via
    `app.add_typer()`, so mounting it doesn't require importing
    `docket.eval.cli` merely because someone imported `docket.interfaces.cli.main`
    (e.g. to reuse `build_context`) -- only actually running `docket eval
    ...` (or `docket --help`, which needs every subcommand's one-line help)
    does.
    """

    def list_commands(self, ctx: click.Context) -> list[str]:
        return sorted({*super().list_commands(ctx), _EVAL_COMMAND_NAME})

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        if cmd_name == _EVAL_COMMAND_NAME:
            return _eval_click()
        return super().get_command(ctx, cmd_name)


app = typer.Typer(
    name="docket",
    cls=_LazyEvalGroup,
    help=(
        "Local-first, evidence-backed work intelligence assistant. "
        "Run `docket` with no arguments in a terminal (or `docket chat`) for an interactive session."
    ),
)
sources_app = typer.Typer(help="Manage registered sources (local folders).")
app.add_typer(sources_app, name="sources")
formulas_app = typer.Typer(help="Verify a sample of formula transcriptions against their source crops.")
app.add_typer(formulas_app, name="formulas")


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    version: bool = typer.Option(False, "--version", help="Show the version and exit."),
) -> None:
    if version:
        typer.echo(f"docket {__version__}")
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        if sys.stdin.isatty() and sys.stdout.isatty():
            _run_interactive()
        else:
            typer.echo(ctx.get_help())


def _run_interactive() -> None:
    from docket.infra.inference.health import check_ollama

    context = build_context()
    settings = context.settings
    run_session(
        context,
        health_check=lambda: check_ollama([settings.gen_model, settings.embed_model]),
        # Only prompt when a human is on a TTY; never swallow piped commands.
        offer_first_run=sys.stdin.isatty() and sys.stdout.isatty(),
    )


@app.command("chat")
def chat() -> None:
    """Start an interactive session (multi-turn questions and /commands)."""
    _run_interactive()


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


def _stale_notice(count: int) -> str:
    noun = "file has" if count == 1 else "files have"
    return (
        f"{count} already-ingested {noun} chunks from an older chunking recipe; "
        "run `docket ingest --rechunk` to update."
    )


@app.command("ingest")
def ingest(
    source_id: str = typer.Argument(None, help="Source id to ingest."),
    all_sources: bool = typer.Option(False, "--all", help="Ingest every active source."),
    rechunk: bool = typer.Option(
        False,
        "--rechunk",
        help="Re-chunk already-ingested files whose chunks use an older recipe "
        "(from the stored originals, not the live files).",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="With --rechunk: list what would be re-chunked, change nothing."
    ),
) -> None:
    """Run (incremental) ingestion for one source, or every active source."""
    if bool(source_id) == bool(all_sources):
        typer.echo("Error: pass exactly one of a source_id or --all.", err=True)
        raise typer.Exit(code=1)
    if dry_run and not rechunk:
        typer.echo("Error: --dry-run only applies together with --rechunk.", err=True)
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

    if rechunk:
        raise typer.Exit(code=_rechunk_sources(context, target_ids, dry_run))

    exit_code = 0
    stale_total = 0
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
        stale_total += len(context.pipeline.find_stale_versions(target_id))

    if stale_total:
        typer.echo(_stale_notice(stale_total))

    raise typer.Exit(code=exit_code)


def _rechunk_sources(context, target_ids: list[str], dry_run: bool) -> int:
    exit_code = 0
    rechunked = failed = up_to_date = 0
    for target_id in target_ids:
        try:
            report = context.pipeline.rechunk_source(target_id, dry_run=dry_run)
        except (SourceNotFoundError, SourceNotActiveError) as exc:
            typer.echo(f"Error re-chunking {target_id}: {exc}", err=True)
            exit_code = 1
            continue
        up_to_date += report.up_to_date
        if dry_run:
            typer.echo(f"[{target_id}] would re-chunk {len(report.candidates)} file(s):")
            for candidate in report.candidates:
                typer.echo(f"  {candidate.path} ({candidate.reason})")
            continue
        for result in report.results:
            if result.status == "failed":
                failed += 1
                typer.echo(f"  FAILED: {result.path} -- {result.error}")
            else:
                rechunked += 1

    if dry_run:
        typer.echo(f"Dry run: nothing changed; {up_to_date} file(s) already up to date.")
        return exit_code

    typer.echo(f"Re-chunked {rechunked}, failed {failed}, skipped {up_to_date} up to date.")
    if failed:
        exit_code = 1
    if rechunked:
        from docket.infra.index.context import INDEX_TEXT_VERSION
        from docket.infra.index.manifest import read_manifest

        manifest = read_manifest(context.settings.index_manifest_path)
        if manifest is not None and manifest.index_text_version < INDEX_TEXT_VERSION:
            typer.echo(
                "Other index rows still lack the chunk context prefix "
                f"(index_text_version {manifest.index_text_version} < {INDEX_TEXT_VERSION}); "
                "run `docket reindex` to update them."
            )
    return exit_code


# -- reindex -------------------------------------------------------------


@app.command("reindex")
def reindex() -> None:
    """Rebuild the search indexes from stored chunks under the current embedding model.

    Re-embeds every chunk (and, if present, every visual page description)
    into scratch tables first, then swaps them in and writes the index
    manifest last. The existing index stays intact if anything fails.
    """
    context = build_context()
    settings = context.settings
    typer.echo(
        f"Reindexing with embedding model {settings.embed_model!r} "
        "(the existing index is kept until the new one is fully built)..."
    )
    try:
        result = run_reindex(
            engine=context.engine,
            db_path=settings.lancedb_path,
            gateway=context.gateway,
            manifest_path=settings.index_manifest_path,
            embed_model=settings.embed_model,
            batch_size=settings.embed_batch_size,
            tokenizer=get_token_counter().name,
        )
    except (InferenceError, ValueError) as exc:
        typer.echo(f"Error: reindex failed, the existing index is unchanged: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(
        f"Reindexed chunks={result.chunks} pages={result.pages} "
        f"model={result.embed_model} dimension={result.embed_dimension}"
    )


# -- query -----------------------------------------------------------------


@app.command("query")
def query(question: str = typer.Argument(..., help="Question to ask over ingested evidence.")) -> None:
    """Ask a question, answered (with citations) from ingested evidence."""
    context = build_context()

    table = context.vector_writer.table
    if table is None:
        typer.echo("No content has been indexed yet. Run `docket ingest` first.")
        raise typer.Exit(code=1)

    # `None` (without ever opening the `pages` LanceDB table) unless
    # `settings.visual_index_enabled` is True -- see `AppContext.page_table_for_query`.
    page_table = context.page_table_for_query

    query_service = QueryService(
        engine=context.engine,
        table=table,
        gateway=context.gateway,
        resolver=context.resolver,
        settings=context.settings,
        page_table=page_table,
        manifest_guard=context.index_manifest_guard,
    )
    try:
        result = query_service.ask(question)
    except IndexManifestMismatchError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

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


# -- formulas ------------------------------------------------------------
#
# Human verification of unverified formula transcriptions (checkpoint 1's
# `EvidenceVersion.formula_transcriptions_json`) against their source crops.
# Measures agreement only -- it never writes anything back into `Chunk.text`,
# an index, or a "verified" flag anywhere (see `formula_review`'s docstring).


@formulas_app.command("export")
def formulas_export(
    source_id: str = typer.Option(None, "--source-id", help="Only this source's transcriptions (default: every source)."),
    out: Path = typer.Option(None, "--out", help="Output directory (default <data_dir>/formula_review/<time>)."),
    n: int = typer.Option(30, "--n", min=1, help="Sample size."),
    seed: int = typer.Option(0, "--seed"),
) -> None:
    """Sample transcribed formula regions, crop them, and write a labels YAML to fill in."""
    from docket.eval.formula_review import FormulaReviewError, export_labels

    context = build_context()
    out_dir = out or context.settings.data_dir / "formula_review" / time.strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        count = export_labels(
            context.session_factory, context.store, out_dir, source_id=source_id, n=n, seed=seed
        )
    except FormulaReviewError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    labels_path = out_dir / "labels.yaml"
    typer.echo(f"Exported {count} items to {labels_path} (crops under {out_dir / 'crops'}).")
    typer.echo("Open each crop_path, fill in `correct: true/false` (and `notes` when false), then run:")
    typer.echo(f"  docket formulas score --labels {labels_path}")


@formulas_app.command("score")
def formulas_score(
    labels: Path = typer.Option(..., "--labels", help="The filled-in labels YAML from `docket formulas export`."),
) -> None:
    """Aggregate agreement across your true/false labels; print every failure."""
    from docket.eval.formula_review import FormulaReviewError, format_review, score_labels

    try:
        result = score_labels(labels)
    except FormulaReviewError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(format_review(result))


if __name__ == "__main__":
    app()
