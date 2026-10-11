from __future__ import annotations

import sys
import time
from pathlib import Path

import click
import typer
import typer.core
import typer.main

from docket import __version__
from docket.core.writer_lock import LibraryReplacedError, WriterLockBusyError
from docket.infra.index.manifest import IndexManifestMismatchError
from docket.infra.index.reindex import reindex as run_reindex
from docket.infra.inference.gateway import InferenceError
from docket.infra.parsing.tokens import get_token_counter
from docket.interfaces.cli.context import build_context
from docket.interfaces.cli import launcher
from docket.interfaces.cli.quiet import (
    FailureReporter,
    condense_error,
    ignore_notices,
    index_format_notice,
    quiet_ingest,
    stale_recipe_notice,
)
from docket.interfaces.cli.interactive import run_session
from docket.core.db.models import SourceStatus
from docket.services.ingestion.pipeline import SourceNotActiveError
from docket.services.query.service import QueryService
from docket.services.sources.manager import SourceCoveredError, SourceNotFoundError
from docket.services.sources.resolve import (
    SourceRefError,
    display_names,
    ordered_sources,
    resolve_source,
)
from docket.services.sources.watcher import SourceWatcher

_EVAL_COMMAND_NAME = "eval"
_PATH_META = "docket.folder_path"
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

    def invoke(self, ctx: click.Context):
        try:
            return super().invoke(ctx)
        except (WriterLockBusyError, LibraryReplacedError) as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=1) from None

    def list_commands(self, ctx: click.Context) -> list[str]:
        return sorted({*super().list_commands(ctx), _EVAL_COMMAND_NAME})

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        if cmd_name == _EVAL_COMMAND_NAME:
            return _eval_click()
        return super().get_command(ctx, cmd_name)

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        """`docket [PATH]`: a first bare word that is not a command name is a
        folder, so `docket ~/work/proj` works next to `docket chat`. (A folder
        that shares a command's name is reached as `docket ./chat`.)"""
        args = list(args)
        commands = set(self.list_commands(ctx))
        for i, word in enumerate(args):
            if word.startswith("-"):
                continue
            if word not in commands:
                ctx.meta[_PATH_META] = word
                del args[i]
            break
        return super().parse_args(ctx, args)


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
library_app = typer.Typer(help="Maintain stored library data.")
app.add_typer(library_app, name="library")
feedback_app = typer.Typer(help="Review private feedback and export draft evaluation cases.")
app.add_typer(feedback_app, name="feedback")


@feedback_app.command("review")
def feedback_review(
    item_id: str = typer.Argument(..., help="Full saved feedback ID."),
    expected: str = typer.Option("", "--expected", help="Manually checked expected fact."),
    spans: list[str] = typer.Option([], "--span", help="Verbatim cited quote, 20–60 characters; repeatable."),
    unanswerable: bool = typer.Option(False, "--unanswerable", help="Explicitly review this question as unanswerable."),
) -> None:
    """Review one capture against its stored evidence; this does not accept gold."""
    from docket.core.writer_lock import lock_for_sessions
    from docket.services.feedback import FeedbackError, FeedbackStore

    context = build_context()
    try:
        FeedbackStore(context.settings.data_dir, writer_lock=lock_for_sessions(context.session_factory)).review(
            item_id, context.resolver, expected=expected, spans=spans, answerable=not unanswerable)
    except FeedbackError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None
    typer.echo("Feedback reviewed. Any export remains DRAFT with reviewed: false; no training was performed.")


@feedback_app.command("export")
def feedback_export(
    gold_draft: bool = typer.Option(False, "--gold-draft", help="Export explicitly reviewed items as unaccepted gold drafts."),
    out: Path | None = typer.Option(None, "--out", help="New private YAML file; otherwise write to standard output."),
) -> None:
    """Export DRAFT cases only, never raw feedback or automatic training data."""
    import os
    from docket.services.feedback import FeedbackError, FeedbackStore

    if not gold_draft:
        typer.echo("Choose --gold-draft to export reviewed items. Raw feedback is kept private.", err=True)
        raise typer.Exit(1)
    context = build_context()
    try:
        content, skipped = FeedbackStore(context.settings.data_dir).export_gold(context.resolver)
        if out is None:
            typer.echo(content, nl=False)
        else:
            fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            typer.echo(f"DRAFT gold saved to {out}.")
    except FeedbackError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None
    except OSError:
        typer.echo("Draft could not be written. Choose a new file in a writable folder.", err=True)
        raise typer.Exit(1) from None
    typer.echo(f"DRAFT only: reviewed: false. {skipped} reviewed items skipped because evidence is unavailable or invalid.", err=True)


@app.command("doctor")
def doctor_command() -> None:
    """Check local setup without changing the library or loading models."""
    from docket.core.config import Settings
    from docket.services.doctor import checks

    results = checks(Settings())
    for result in results:
        typer.echo(f"{result.status} · {result.name}: {result.detail}")
        if result.fix:
            typer.echo(f"  Fix: {result.fix}")
    if any(result.status == "BLOCK" for result in results):
        raise typer.Exit(1)


def _cleanup(context):
    from docket.services.library_cleanup import LibraryCleanup

    return LibraryCleanup(context.session_factory, context.source_manager, context.purge_service,
                          context.store, context.settings)


@sources_app.command("purge")
def sources_purge(
    name: str = typer.Argument(..., help="Disconnected folder name, number or ID."),
    yes: bool = typer.Option(False, "--yes", help="Skip the deletion confirmation."),
) -> None:
    """Delete a disconnected folder's stored evidence and index entries."""
    from docket.services.library_cleanup import CleanupError
    from docket.services.sources.manager import InvalidSourceTransitionError
    from docket.services.sources.purge import SourceNotPurgeableError

    context = build_context()
    source = _resolve_ref(context, name)
    if source.status not in (SourceStatus.REVOKED, SourceStatus.HARD_DELETE_PENDING):
        typer.echo("Disconnect this folder before purging it. Original user files are never touched.", err=True)
        raise typer.Exit(1)
    typer.echo("Deletes stored passages, versions and index entries for this folder. "
               "Unshared stored originals enter trash before permanent cleanup; shared originals remain. "
               "Job history is retained. Original user files are never touched.")
    if not yes and not typer.confirm(f"Purge stored evidence for {name}?", default=False):
        typer.echo("Cancelled. Nothing was purged.")
        return
    try:
        result = _cleanup(context).purge_disconnected(source.id)
    except (CleanupError, InvalidSourceTransitionError, SourceNotPurgeableError, SourceNotFoundError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None
    except (WriterLockBusyError, LibraryReplacedError):
        raise
    except Exception:
        typer.echo("The purge could not finish. Check data-folder permissions and disk space, then retry.", err=True)
        raise typer.Exit(1) from None
    typer.echo(f"Purged {result.versions_deleted} stored versions; {len(result.blobs_trashed)} originals moved to trash.")


@library_app.command("clean")
def library_clean(
    apply: bool = typer.Option(False, "--apply", help="Apply the previewed retention and trash cleanup."),
) -> None:
    """Preview expired retention and evidence trash cleanup by default."""
    try:
        plan = _cleanup(build_context()).clean(apply=apply)
    except (WriterLockBusyError, LibraryReplacedError):
        raise
    except Exception:
        typer.echo("Cleanup could not finish. Check data-folder permissions and disk space, then retry.", err=True)
        raise typer.Exit(1) from None
    typer.echo(f"{'Cleaned' if apply else 'Preview'}: {len(plan.source_ids)} expired or pending folders; "
               f"{len(plan.trash_objects)} trash objects {'deleted' if apply else 'eligible for deletion'}.")
    typer.echo("Original user files are never touched. Disconnected folders are retained unless explicitly purged.")
    if not apply:
        typer.echo("No evidence was deleted. Run docket library clean --apply to apply this cleanup.")


@app.command("backup")
def backup_command(destination: Path | None = typer.Argument(None, help="New backup directory or .zip archive.")) -> None:
    """Save a verified snapshot of this local library."""
    from docket.core.config import Settings
    from docket.services.backup import BackupError, backup

    try:
        saved = backup(Settings().data_dir, destination)
    except BackupError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None
    typer.echo(f"Library backup saved to {saved}")


@app.command("restore")
def restore_command(
    path: Path = typer.Argument(..., help="Backup directory or .zip archive."),
    force: bool = typer.Option(False, "--force", help="Keep the current library as a dated side copy and replace it."),
) -> None:
    """Verify and restore a local library snapshot."""
    from docket.core.config import Settings
    from docket.services.backup import BackupError, restore

    try:
        previous = restore(Settings().data_dir, path, force=force)
    except BackupError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None
    typer.echo(f"Library restored. Previous data kept at {previous}")
    typer.echo("Restart any other Docket windows to use the restored library.")


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    version: bool = typer.Option(False, "--version", help="Show the version and exit."),
    all_folders: bool = typer.Option(
        False, "--all", help="Open the library of all folders instead of the current folder."
    ),
) -> None:
    if version:
        typer.echo(f"docket {__version__}")
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        if sys.stdin.isatty() and sys.stdout.isatty():
            folder = _folder_arg(ctx.meta.get(_PATH_META), all_folders)
            # Own window first; fall back to running in place (no display, SSH, ...).
            if not launcher.spawn_window(
                folder=str(folder) if folder is not None else None, all_folders=all_folders
            ):
                _run_interactive(folder)
        else:
            typer.echo(ctx.get_help())


def _folder_arg(path: str | Path | None, all_folders: bool = False) -> Path | None:
    """The folder Docket opens on: PATH, else the current folder; None for
    `--all` (library mode). Exits with a plain message for a bad path."""
    if all_folders:
        return None
    try:
        target = Path(path).expanduser() if path else Path.cwd()
        resolved = target.resolve()
    except (OSError, RuntimeError):
        typer.echo(f"Error: cannot use that folder: {path}", err=True)
        raise typer.Exit(code=2)
    if not resolved.is_dir():
        typer.echo(f"Error: not a folder: {path}", err=True)
        raise typer.Exit(code=2)
    return resolved


def _set_window_title(folder: Path | None) -> None:
    sys.stdout.write(f"\033]0;{launcher.window_title(str(folder) if folder else None)}\007")
    sys.stdout.flush()


def _run_interactive(folder: Path | None = None) -> None:
    from docket.infra.inference.health import check_ollama

    context = build_context()
    settings = context.settings
    if sys.stdin.isatty() and sys.stdout.isatty():
        launcher.maybe_offer_launcher(settings.data_dir)
    run_session(
        context,
        health_check=lambda: check_ollama([settings.gen_model, settings.embed_model]),
        # Only prompt when a human is on a TTY; never swallow piped commands.
        offer_first_run=sys.stdin.isatty() and sys.stdout.isatty(),
        folder=folder,
    )


@app.command("chat")
def chat(
    path: Path = typer.Argument(None, help="Folder to open on (default: the current folder)."),
    all_folders: bool = typer.Option(False, "--all", help="Open the library of all folders."),
    in_window: bool = typer.Option(False, "--in-window", hidden=True),
    folder_opt: Path = typer.Option(None, "--folder", hidden=True),
) -> None:
    """Start an interactive session (multi-turn questions and /commands)."""
    folder = _folder_arg(folder_opt or path, all_folders)
    if not in_window:
        _run_interactive(folder)
        return
    _set_window_title(folder)
    try:
        _run_interactive(folder)
    except (KeyboardInterrupt, SystemExit, typer.Exit):
        raise
    except Exception as exc:  # the window would otherwise vanish unread
        import traceback

        traceback.print_exc()
        print(f"\nDocket stopped unexpectedly: {exc}")
        try:
            input("Press Enter to close this window...")
        except (EOFError, KeyboardInterrupt):
            pass
        raise typer.Exit(code=1) from exc


@app.command("launch")
def launch() -> None:
    """Open Docket in its own terminal window (used by the app-menu icon)."""
    # An icon launch has no meaningful folder: it opens the library.
    if launcher.spawn_window(all_folders=True):
        return
    if sys.stdin.isatty() and sys.stdout.isatty():
        _run_interactive(None)
        return
    typer.echo("Could not open a terminal window; run `docket` from a terminal.", err=True)
    raise typer.Exit(code=1)


@app.command("tui-demo", hidden=True)
def tui_demo(
    state: str = typer.Option("chat", "--state", help="welcome | welcome-blocked | chat | indexing | sources | evidence | settings"),
    reduced_motion: bool = typer.Option(False, "--reduced-motion", help="No timed progress; press N in the indexing panel to advance."),
    ascii_mode: bool = typer.Option(False, "--ascii", help="Force ASCII borders (also DOCKET_ASCII=1)."),
) -> None:
    """Full-screen Screen A prototype with fake data only (design review; no backend)."""
    from docket.interfaces.cli.tui import STATES, run_demo

    if state not in STATES:
        typer.echo(f"Unknown state {state!r}. Choose from: {', '.join(STATES)}", err=True)
        raise typer.Exit(code=2)
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        typer.echo("tui-demo needs an interactive terminal.", err=True)
        raise typer.Exit(code=1)
    run_demo(state, reduced_motion=reduced_motion, ascii_mode=True if ascii_mode else None)


@app.command("ui")
def ui_command(
    reduced_motion: bool = typer.Option(False, "--reduced-motion", help="Fewer redraws while working."),
    ascii_mode: bool = typer.Option(False, "--ascii", help="Force ASCII borders (also DOCKET_ASCII=1)."),
    in_window: bool = typer.Option(False, "--in-window", hidden=True),
    folder_opt: Path = typer.Option(None, "--folder", help="Open on this folder (default: the current folder)."),
    all_folders: bool = typer.Option(False, "--all", help="Open the library of all folders."),
) -> None:
    """Full-screen terminal UI. `docket` opens it in its own window; `docket chat` is the classic prompt."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        typer.echo("docket ui needs an interactive terminal. Use `docket chat` or `docket query`.", err=True)
        raise typer.Exit(code=1)
    from docket.interfaces.cli.tui import run_ui

    folder = _folder_arg(folder_opt, all_folders)
    if not in_window:
        run_ui(build_context(), reduced_motion=reduced_motion, ascii_mode=True if ascii_mode else None, folder=folder)
        return
    _set_window_title(folder)
    try:
        run_ui(build_context(), reduced_motion=reduced_motion, ascii_mode=True if ascii_mode else None, folder=folder)
    except (KeyboardInterrupt, SystemExit, typer.Exit):
        raise
    except Exception as exc:  # the window would otherwise vanish unread
        import traceback

        traceback.print_exc()
        print(f"\nDocket stopped unexpectedly: {exc}")
        print("Run `docket chat` for the classic prompt.")
        try:
            input("Press Enter to close this window...")
        except (EOFError, KeyboardInterrupt):
            pass
        raise typer.Exit(code=1) from exc


@app.command("install-launcher")
def install_launcher_cmd() -> None:
    """Add Docket to the desktop app menu (user-level, no system files touched)."""
    desktop, icon = launcher.install_launcher()
    typer.echo(f"Installed {desktop}")
    typer.echo(f"Installed {icon}")


@app.command("uninstall-launcher")
def uninstall_launcher_cmd() -> None:
    """Remove the app-menu entry and icon added by `install-launcher`."""
    removed = launcher.uninstall_launcher()
    if not removed:
        typer.echo("No launcher installed.")
    for path in removed:
        typer.echo(f"Removed {path}")


# -- sources -----------------------------------------------------------


def _resolve_ref(context, ref: str):
    """Resolve a name, list number, ID prefix or ID to a source (exit 1 with
    the candidates listed when unknown or ambiguous)."""
    try:
        return resolve_source(ref, context.source_manager.list_sources())
    except SourceRefError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc


@sources_app.command("add")
def sources_add(
    path: Path = typer.Argument(..., help="Local folder to register as a source."),
    name: str = typer.Option(None, "--name", help="Display name (default: the folder name)."),
) -> None:
    """Register a local folder as a source."""
    context = build_context()
    try:
        source = context.source_manager.register_source(path, display_name=name)
    except SourceCoveredError as exc:
        typer.echo(f"Already covered by {exc.covering_name}.", err=True)
        raise typer.Exit(code=1) from exc
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
    ordered = ordered_sources(sources)
    names = display_names(ordered)
    try:
        counts = context.readiness.files_by_source()
    except Exception:  # noqa: BLE001 -- the count is a nicety
        counts = {}
    name_width = max(8, min(32, max(len(n) for n in names.values()) + 2))
    typer.echo(f"{'#':<4}{'NAME':<{name_width}}{'STATUS':<12}{'DOCUMENTS':<11}PATH  ID")
    for number, source in enumerate(ordered, start=1):
        docs = str(counts[source.id]) if source.id in counts else "-"
        typer.echo(
            f"{number:<4}{names[source.id]:<{name_width}}{source.status.value:<12}"
            f"{docs:<11}{source.path}  {source.id}"
        )


@sources_app.command("prune")
def sources_prune(
    source_ref: str = typer.Argument(..., help="Source name, list number or id to prune."),
    apply: bool = typer.Option(
        False, "--apply", help="Actually remove them from the index (default: dry run)."
    ),
) -> None:
    """Remove indexed files that the current ignore rules would skip
    (virtualenvs, site-packages, hidden folders, .docketignore).

    Dry run by default. Never deletes files on disk."""
    context = build_context()
    source_id = _resolve_ref(context, source_ref).id
    try:
        service = context.prune_service
        candidates = service.apply(source_id) if apply else service.find(source_id)
    except SourceNotFoundError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    if not candidates:
        typer.echo("Nothing to prune: no indexed files are in ignored folders or invalid.")
        return
    verb = "Pruned" if apply else "Would prune"
    typer.echo(f"{verb} {len(candidates)} file(s):")
    for candidate in candidates:
        typer.echo(f"  {candidate.relative_path.as_posix()} ({candidate.status})")
    if not apply:
        typer.echo("Dry run: nothing changed. Re-run with --apply to remove them from the index.")


# -- ingest --------------------------------------------------------------


@app.command("ingest")
def ingest(
    source_id: str = typer.Argument(None, help="Source name, list number or id to ingest."),
    all_sources: bool = typer.Option(False, "--all", help="Ingest active and temporarily missing sources."),
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
    """Run incremental ingestion, including recovery of temporarily missing sources."""
    if bool(source_id) == bool(all_sources):
        typer.echo("Error: pass exactly one of a source (name, number or id) or --all.", err=True)
        raise typer.Exit(code=1)
    if dry_run and not rechunk:
        typer.echo("Error: --dry-run only applies together with --rechunk.", err=True)
        raise typer.Exit(code=1)

    context = build_context()

    if all_sources:
        target_ids = [
            source.id
            for source in context.source_manager.list_sources()
            if source.status in (SourceStatus.ACTIVE, SourceStatus.MISSING)
        ]
        if not target_ids:
            typer.echo("No active or missing sources to ingest.")
            raise typer.Exit(code=0)
    else:
        target_ids = [_resolve_ref(context, source_id).id]

    if rechunk:
        raise typer.Exit(code=_rechunk_sources(context, target_ids, dry_run))

    exit_code = 0
    stale_total = 0
    for target_id in target_ids:
        try:
            with quiet_ingest():
                result = context.pipeline.run_ingestion_for_source(target_id)
        except (SourceNotFoundError, SourceNotActiveError) as exc:
            typer.echo(f"Error ingesting {target_id}: {exc}", err=True)
            exit_code = 1
            continue
        try:
            root = Path(context.source_manager.get_source(target_id).path)
        except Exception:  # noqa: BLE001 -- display nicety only
            root = None
        reporter = FailureReporter(root)

        chunks_written = sum(r.chunks_written for r in result.file_results)
        typer.echo(
            f"[{result.source_id}] job={result.job_id} status={result.status} "
            f"files_processed={result.files_processed} files_failed={result.files_failed} "
            f"chunks_written={chunks_written}"
        )
        for file_result in result.file_results:
            if file_result.status == "failed":
                line = reporter.line(file_result.path, file_result.error)
                if line is not None:
                    typer.echo(f"  {line}")
        for notice in ignore_notices(result, getattr(context, "prune_service", None), target_id):
            typer.echo(typer.style(notice, dim=True))

        if result.status != "succeeded":
            exit_code = 1
        stale_total += len(context.pipeline.find_stale_versions(target_id))

    if stale_total:
        typer.echo(typer.style(stale_recipe_notice(stale_total), dim=True))

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
                typer.echo(f"  FAILED {result.path}: {condense_error(result.error)}")
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
            typer.echo(typer.style(index_format_notice(), dim=True))
    return exit_code


# -- sync ----------------------------------------------------------------


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _print_plan(plan) -> None:
    typer.echo(plan.headline + ".")
    for step in plan.steps:
        typer.echo(f"  - {step.label}")
        for rel in step.detail[:3]:
            typer.echo(typer.style(f"      {rel}", dim=True))
        if step.count > len(step.detail) > 0 and step.kind in ("prune", "rechunk"):
            typer.echo(typer.style(f"      and {step.count - len(step.detail)} more", dim=True))


def _run_plan(context, plan, label: str) -> bool:
    """Run `plan`, printing one line per stage; True when it finished cleanly."""
    if plan.empty:
        return True
    seen: set[str] = set()

    def on_progress(p) -> None:
        key = f"{p.stage}:{p.source_name}"
        if key in seen:
            return
        seen.add(key)
        where = f" ({p.source_name})" if p.source_name else ""
        typer.echo(f"{p.label}{where}...")

    try:
        with quiet_ingest():
            result = context.maintenance.run(plan, progress=on_progress)
    except KeyboardInterrupt:
        typer.echo("Stopped. Anything that finished was kept.")
        return False
    for path, reason in result.failures:
        from docket.services.ingestion.failure import plain_failure_reason

        typer.echo(f"  Could not read {Path(path).name}: {plain_failure_reason(condense_error(reason), path)}")
    typer.echo(result.summary())
    return result.ok


@app.command("sync")
def sync(
    refs: list[str] = typer.Argument(
        None, help="Sources to bring up to date (name, number or id). Default: all."
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be done and change nothing."),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Also run the slower steps without asking."
    ),
) -> None:
    """Bring the library up to date: remove what is no longer read, read new
    folders, re-prepare older documents and rebuild the search index when
    needed. Quick steps run straight away; slower ones ask once with a time
    estimate. Your original files are never touched."""
    context = build_context()
    ids = [_resolve_ref(context, r).id for r in refs] if refs else None
    plan = context.maintenance.plan(ids)
    _print_plan(plan)
    if plan.empty:
        raise typer.Exit(code=0)
    if dry_run:
        typer.echo("Dry run: nothing changed.")
        raise typer.Exit(code=0)

    ok = True
    cheap = plan.only({"prune", "refresh"})
    if not cheap.empty:
        ok = _run_plan(context, cheap, "quick")
    costly = plan.costly_steps
    if costly:
        costly_plan = plan.only({"rechunk", "reindex"})
        go = yes
        if not go:
            if _interactive():
                go = typer.confirm(
                    f"Run the slower updates now? This takes {costly_plan.duration_text}.",
                    default=False,
                )
            else:
                typer.echo(
                    f"The slower updates ({costly_plan.duration_text}) were not run. "
                    "Pass --yes to run them without asking."
                )
        if go:
            ok = _run_plan(context, costly_plan, "slower") and ok
    raise typer.Exit(code=0 if ok else 1)


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
def query(
    question: str = typer.Argument(..., help="Question to ask over ingested evidence."),
    here: bool = typer.Option(
        False, "--here", help="Search only the current folder (and below). Default: all sources."
    ),
    folder: Path = typer.Option(
        None, "--folder", help="Search only this folder (and below). Implies --here."
    ),
) -> None:
    """Ask a question, answered (with citations) from ingested evidence."""
    scope = None
    if here or folder is not None:
        from docket.services.query.scope import QueryScope

        target = (folder if folder is not None else Path.cwd()).expanduser().resolve()
        scope = QueryScope.for_folder(target, label=target.name or str(target))
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
        result = (
            query_service.ask(question, scope=scope) if scope is not None else query_service.ask(question)
        )
    except IndexManifestMismatchError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    if scope is not None and result.scope_label is not None:
        typer.echo(f"Scope: {result.scope_label} ({result.scope_file_count} documents)")
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
def watch(
    source_ref: str = typer.Argument(..., help="Source name, list number or id to watch for changes."),
) -> None:
    """Watch a source's folder and re-ingest just the files that change
    (new, edited or deleted); ignored paths never trigger anything.

    Long-running foreground command -- blocks until interrupted (Ctrl+C).
    """
    context = build_context()
    source = _resolve_ref(context, source_ref)
    source_id = source.id

    typer.echo(f"Watching {source.path} (source_id={source_id})... press Ctrl+C to stop.")
    watcher = SourceWatcher(context.pipeline, session_factory=context.session_factory)
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
