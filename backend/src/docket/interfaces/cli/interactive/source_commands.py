"""Source-management slash-commands: `/sources`, `/add`, `/remove`, and the
opt-in first-run onboarding flow.

Plain functions taking the `_Session` instance as their first argument
(rather than a class wrapping it) -- these handlers reach across into other
collaborators too (`session.cmd_ingest` during first-run onboarding,
`errors.handle_error` on failure), so there's no meaningfully smaller state
to hold privately; a thin class would just forward everything to `session`
anyway. Module-level functions make that reuse of `session` explicit instead
of hiding it behind `self`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rich.table import Table

from docket.core.db.models import SourceStatus
from docket.services.sources.manager import (
    InvalidSourceTransitionError,
    SourceCoveredError,
    SourceNotFoundError,
)
from docket.services.sources.resolve import (
    SourceRefError,
    display_names,
    ordered_sources,
    resolve_source,
)

from .errors import handle_error


def cmd_sources(session: Any, arg: str) -> None:
    sources = session.context.source_manager.list_sources()
    if not sources:
        session.say("No sources registered. Use /add <folder>.")
        return
    table = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
    table.add_column("#", no_wrap=True)
    table.add_column("Name", no_wrap=True)
    table.add_column("ID", no_wrap=True, overflow="fold")
    table.add_column("Status", no_wrap=True)
    table.add_column("Path", overflow="fold")
    ordered = ordered_sources(sources)
    names = display_names(ordered)
    for number, source in enumerate(ordered, start=1):
        table.add_row(
            str(number), names[source.id], str(source.id), source.status.value, str(source.path)
        )
    session.console.print(table)


def cmd_add(session: Any, arg: str) -> None:
    if not arg:
        session.say("Usage: /add <folder>")
        return
    # A single pasted path can be quoted without treating it as a shell command.
    if len(arg) >= 2 and arg[0] == arg[-1] and arg[0] in ("'", '"'):
        arg = arg[1:-1]
    _register(session, Path(arg).expanduser().resolve())


def _register(session: Any, path: Path) -> Any | None:
    """Register `path` (or report it's already registered). Returns the
    source id on success or when already registered, else None."""
    for existing in session.context.source_manager.list_sources():
        if Path(existing.path).expanduser().resolve() == path:
            if getattr(existing, "status", SourceStatus.ACTIVE) == SourceStatus.REVOKED:
                session.say(f"Source {existing.id} is disconnected. Use /reconnect {existing.id} to reconnect and index it.")
                return None
            if getattr(existing, "status", SourceStatus.ACTIVE) not in (SourceStatus.ACTIVE, SourceStatus.MISSING):
                session.error(f"Source {existing.id} is {existing.status.value}; it cannot be registered again here.")
                return None
            session.say(f"Already registered: {existing.id}")
            return existing.id
    try:
        source = session.context.source_manager.register_source(path)
    except SourceCoveredError as exc:
        session.say(f"Already covered by {exc.covering_name}.")
        return exc.covering.id
    except ValueError as exc:
        session.error(f"Error: {exc}")
        return None
    session.state.refresh_sources(session.context.source_manager)
    session.sync_state()
    session.say(f"Registered source {source.id}")
    session.say(f"Next: /ingest {source.id}", style="dim")
    return source.id


def first_run_offer(session: Any) -> None:
    """Folder-first start for the classic prompt: act on the folder Docket was
    opened in. A folder already covered by a source is searched as is; a new
    folder with documents is offered for indexing (asked once per data
    directory, then done unasked); home, a broad root or no folder does
    nothing. The bare current directory is never offered blindly."""
    from docket.services.sources.session import (
        KIND_COVERED,
        KIND_EMPTY,
        KIND_NEW,
        has_consent,
        record_consent,
        resolve_session,
        start_folder,
    )

    folder = getattr(session, "folder", None)
    if folder is None:
        return
    ctx = session.context
    try:
        plan = resolve_session(
            folder,
            ctx.source_manager,
            session_factory=getattr(ctx, "session_factory", None),
            suggest_for_library=False,
        )
    except Exception:  # noqa: BLE001 -- a scan problem must never block the prompt
        return
    if plan.kind == KIND_COVERED:
        session.scope = plan.scope
        where = plan.source_name + (f" / {plan.relative_path}" if plan.relative_path else "")
        session.say(f"Using {where}, already indexed. Questions here search this folder only.")
        return
    if plan.kind == KIND_EMPTY:
        session.say(f"No supported documents in {plan.name} ({plan.reason}).")
        return
    if plan.kind != KIND_NEW:
        return

    n = plan.documents
    noun = "document" if n == 1 else "documents"
    data_dir = ctx.settings.data_dir
    if plan.needs_preview:
        scan = plan.scan
        session.say(
            f"{plan.name} is large: {n} {noun}"
            + (" or more" if scan and scan.incomplete else "")
            + ".",
        )
        if scan is not None:
            for name, count in scan.subfolders[:5]:
                session.say(f"  {name}: {count}", style="dim")
        if not _confirm(session, "Index everything here anyway? [y/N] "):
            return
    elif not has_consent(data_dir):
        if not _confirm(
            session, f"Docket will read {n} {noun} in {plan.name}. Index them now? [y/N] "
        ):
            return
        try:
            record_consent(data_dir)
        except OSError:
            pass
    else:
        session.say(f"Reading {n} {noun} in {plan.name}.")
    try:
        source = start_folder(ctx.source_manager, plan)
    except SourceCoveredError as exc:
        session.say(f"Already covered by {exc.covering_name}.")
        return
    except ValueError as exc:
        session.error(f"Error: {exc}")
        return
    from docket.services.query.scope import QueryScope

    session.scope = QueryScope.for_source(source.id, label=plan.name)
    session.state.refresh_sources(ctx.source_manager)
    session.sync_state()
    try:
        session.cmd_ingest(str(source.id))
    except KeyboardInterrupt:
        session.say()
        session.say("(interrupted)", style="dim")
    except Exception as exc:  # noqa: BLE001
        handle_error(session, exc)


def offer_dropped_path(session: Any, dropped: Any) -> None:
    """A pasted or dragged path at the classic prompt: offer to add it
    (y/N) instead of asking it as a question. A file offers its folder."""
    from docket.interfaces.cli.dropped import FILE_OFFER
    from docket.services.sources.session import broad_location_reason

    path = Path(dropped.path)
    if not dropped.is_dir:
        path = path.parent
        if not _confirm(session, f"{FILE_OFFER} {path} [y/N] "):
            session.say("Cancelled.", style="dim")
            return
    broad = broad_location_reason(path)
    if broad:
        session.say(f"That is {broad}. Choose a folder inside it, or use /add {path} to add it anyway.")
        return
    if dropped.is_dir and not _confirm(session, f"Add {path} and index it? [y/N] "):
        session.say("Cancelled.", style="dim")
        return
    source_id = _register(session, path.resolve())
    if source_id is None:
        return
    try:
        session.cmd_ingest(str(source_id))
    except KeyboardInterrupt:
        session.say()
        session.say("(interrupted)", style="dim")
    except Exception as exc:  # noqa: BLE001
        handle_error(session, exc)


def _confirm(session: Any, prompt: str) -> bool:
    read = getattr(session.reader, "confirm", None) or session.reader.read
    try:
        reply = read(prompt)
    except (EOFError, KeyboardInterrupt):
        session.say()
        return False
    return reply.strip().lower() in ("y", "yes")


def cmd_remove(session: Any, arg: str) -> None:
    if not arg:
        session.say("Usage: /remove <name, number or source-id>")
        return
    manager = session.context.source_manager
    try:
        match = resolve_source(arg, manager.list_sources())
    except SourceRefError as exc:
        session.error(f"Error: {exc}")
        return
    arg = match.id
    if match.status == SourceStatus.REVOKED:
        session.say(f"Source {arg} is already disconnected.")
        return
    if not _confirm(session, f"Remove source {arg} ({match.path})? [y/N] "):
        session.say("Cancelled.", style="dim")
        return
    try:
        manager.deactivate_source(arg, reason="user_disconnected")
    except (SourceNotFoundError, InvalidSourceTransitionError) as exc:
        session.error(f"Error: {exc}")
        return
    finally:
        session.invalidate_query_service()
        session.state.refresh_sources(manager)
        session.sync_state()
    session.say(f"Removed {arg}. Its evidence is no longer searched.")


def cmd_reconnect(session: Any, arg: str) -> None:
    if not arg:
        session.say("Usage: /reconnect <name, number or source-id>")
        return
    manager = session.context.source_manager
    try:
        target = resolve_source(arg, manager.list_sources())
        source = manager.reconnect_source(target.id)
    except (SourceNotFoundError, InvalidSourceTransitionError, ValueError) as exc:
        session.error(f"Error: {exc}")
        return
    session.invalidate_query_service()
    session.state.refresh_sources(manager)
    session.sync_state()
    session.say(f"Reconnected {source.id}. Starting indexing.")
    session.cmd_ingest(source.id)
