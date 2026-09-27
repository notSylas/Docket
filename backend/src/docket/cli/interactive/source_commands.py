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

from docket.services.sources.manager import SourceNotFoundError

from .errors import handle_error


def cmd_sources(session: Any, arg: str) -> None:
    sources = session.context.source_manager.list_sources()
    if not sources:
        session.say("No sources registered. Use /add <folder>.")
        return
    table = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
    table.add_column("ID", no_wrap=True, overflow="fold")
    table.add_column("Status", no_wrap=True)
    table.add_column("Path", overflow="fold")
    for source in sources:
        table.add_row(str(source.id), source.status.value, str(source.path))
    session.console.print(table)


def cmd_add(session: Any, arg: str) -> None:
    if not arg:
        session.say("Usage: /add <folder>")
        return
    _register(session, Path(arg).expanduser().resolve())


def _register(session: Any, path: Path) -> Any | None:
    """Register `path` (or report it's already registered). Returns the
    source id on success or when already registered, else None."""
    for existing in session.context.source_manager.list_sources():
        if Path(existing.path) == path:
            session.say(f"Already registered: {existing.id}")
            return existing.id
    try:
        source = session.context.source_manager.register_source(path)
    except ValueError as exc:
        session.error(f"Error: {exc}")
        return None
    session.state.refresh_sources(session.context.source_manager)
    session.sync_state()
    session.say(f"Registered source {source.id}")
    session.say(f"Next: /ingest {source.id}", style="dim")
    return source.id


def first_run_offer(session: Any) -> None:
    """Opt-in guided start: offer to register the cwd when nothing is set up."""
    if session.context.source_manager.list_sources():
        return
    cwd = Path.cwd().resolve()
    if cwd == Path("/") or cwd == Path.home().resolve():
        return
    if not _confirm(session, f"No sources yet. Add the current directory ({cwd})? [y/N] "):
        return
    source_id = _register(session, cwd)
    if source_id is None:
        return
    if _confirm(session, "Ingest it now? [y/N] "):
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
        session.say("Usage: /remove <source-id>")
        return
    manager = session.context.source_manager
    match = next((s for s in manager.list_sources() if s.id == arg), None)
    if match is None:
        session.error(f"Error: source not found: {arg}")
        return
    if not _confirm(session, f"Remove source {arg} ({match.path})? [y/N] "):
        session.say("Cancelled.", style="dim")
        return
    try:
        manager.deactivate_source(arg)
    except SourceNotFoundError:
        session.error(f"Error: source not found: {arg}")
        return
    finally:
        session.state.refresh_sources(manager)
        session.sync_state()
    session.say(f"Removed {arg}. Its evidence is no longer searched.")
