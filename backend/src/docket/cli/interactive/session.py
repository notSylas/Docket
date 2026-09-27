"""Interactive chat-like session for the `docket` CLI (bare `docket` in a TTY,
or `docket chat`). One `AppContext` per session, multi-turn memory via
`ConversationTurn` history, and slash-commands for source management.

`run_session` takes an injectable `input_fn` and rich `Console` so it is
testable without a TTY, and a `query_service_factory` so tests can supply a
fake `QueryService`.

This module owns session lifecycle/wiring, the REPL loop, and command
dispatch. The actual command *bodies* live in sibling collaborator modules
(`source_commands`, `ingestion_ui`, `query_flow`) that take the `_Session`
instance as an argument -- `_Session` remains the single owner of all shared
mutable state (`context`, `reader`, `console`, `state`, `history`, `mode`,
`last_citations`, `last_question`); collaborators read/write it through the
`session` reference they're handed rather than holding their own copies.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from rich.console import Console
from rich.table import Table

from docket import __version__
from docket.inference.health import HealthReport, format_health_warning
from docket.query.classifier import QueryMode
from docket.query.conversation import ConversationTurn
from docket.query.service import QueryService

from . import ingestion_ui, query_flow, source_commands
from .commands import BARE_WORDS, HELP_FOOTER, CommandRegistry, default_registry
from .errors import handle_error
from .reader import LineReader, make_reader
from .state import SessionState

PROMPT = "docket> "

QueryServiceFactory = Callable[[Any, Any], Any]


def _default_factory(context: Any, table: Any) -> QueryService:
    # `None` (without ever opening the `pages` LanceDB table) unless
    # `settings.visual_index_enabled` is True -- see `AppContext.page_table_for_query`.
    page_table = context.page_table_for_query
    return QueryService(
        engine=context.engine,
        table=table,
        gateway=context.gateway,
        resolver=context.resolver,
        settings=context.settings,
        page_table=page_table,
    )


class _Session:
    def __init__(
        self,
        context: Any,
        reader: LineReader,
        console: Console,
        factory: QueryServiceFactory,
        registry: CommandRegistry | None = None,
        state: SessionState | None = None,
        health_check: Callable[[], HealthReport] | None = None,
    ) -> None:
        self.health_check = health_check
        self.context = context
        self.reader = reader
        self.registry = registry or default_registry()
        self.state = state or SessionState()
        self.console = console
        self.factory = factory
        self.history: list[ConversationTurn] = []
        self.mode: QueryMode | None = None
        self.last_citations: list[Any] = []
        self.last_question: str | None = None
        self._service: Any | None = None
        self.state.model = getattr(getattr(context, "settings", None), "gen_model", "") or ""
        self.sync_state()

    def sync_state(self) -> None:
        """Refresh cheap toolbar fields. Main thread only; may touch vector_writer."""
        self.state.turns = len(self.history)
        try:
            self.state.indexed = self.context.vector_writer.table is not None
        except Exception:
            pass

    # -- output helpers ----------------------------------------------------

    def say(self, message: str = "", **kw: Any) -> None:
        self.console.print(message, markup=False, highlight=False, **kw)

    def error(self, message: str) -> None:
        self.console.print(message, style="red", markup=False, highlight=False)

    # -- banner ------------------------------------------------------------

    def banner(self) -> None:
        settings = self.context.settings
        count = len(self.context.source_manager.list_sources())
        self.console.print(f"docket {__version__}", style="bold", markup=False)
        self.say(
            f"{count} source(s) registered | model: {settings.gen_model} | "
            f"embeddings: {settings.embed_model}"
        )
        self.say("Type /help for commands, /exit to leave.", style="dim")
        for line in self._health_lines():
            self.console.print(line, style="yellow", markup=False, highlight=False)
        if count == 0:
            self.say("No sources yet. Get started: /add <folder>, then /ingest.", style="cyan")

    def _health_lines(self) -> list[str]:
        if self.health_check is None:
            return []
        try:
            return format_health_warning(self.health_check(), self.context.settings)
        except Exception:  # noqa: BLE001 -- health is advisory only
            return []

    # -- main loop ---------------------------------------------------------

    def loop(self) -> None:
        while True:
            try:
                line = self.reader.read(PROMPT)
            except EOFError:
                self.say()
                return
            except KeyboardInterrupt:
                self.say()
                self.say("(cancelled -- use /exit or Ctrl-D to leave)", style="dim")
                continue
            line = line.strip()
            if not line:
                continue
            try:
                bare = BARE_WORDS.get(line.lower())
                if bare is not None:
                    line = "/" + bare
                if line.startswith("/"):
                    if self.command(line):
                        return
                else:
                    query_flow.ask(self, line)
            except KeyboardInterrupt:
                self.say()
                self.say("(interrupted)", style="dim")
            except Exception as exc:  # last resort: never crash the session
                handle_error(self, exc)

    # -- commands ----------------------------------------------------------

    def command(self, line: str) -> bool:
        """Run a slash command. Returns True to exit the session."""
        parts = line[1:].split(maxsplit=1)
        name = parts[0].lower() if parts else ""
        arg = parts[1].strip() if len(parts) > 1 else ""
        cmd = self.registry.resolve(name)
        if cmd is None:
            ambiguous = self.registry.prefix_matches(name)
            if len(ambiguous) > 1:
                names = ", ".join(c.name for c in ambiguous)
                self.say(f"Ambiguous command /{name}: {names}. Type /help for the list of commands.")
                return False
            self.say(f"Unknown command: /{name}.")
            suggestion = self.registry.suggest(name)
            if suggestion:
                self.say(f"Did you mean /{suggestion}?")
            self.say("Type /help for the list of commands.")
            return False
        return bool(cmd.handler(self, arg))

    # -- thin forwarders to collaborators -----------------------------------
    #
    # `commands.py`'s registry dispatches by looking up a method name on
    # whatever session object `command()` hands it (see `_call()` there), so
    # these one-line forwarders are what let the registry keep resolving
    # `/sources` -> `session.cmd_sources(arg)` etc. without any change to
    # `commands.py` itself -- only the handler *bodies* moved out.

    def cmd_help(self, arg: str) -> None:
        self.say(self.registry.render_help(HELP_FOOTER))

    def cmd_sources(self, arg: str) -> None:
        return source_commands.cmd_sources(self, arg)

    def cmd_add(self, arg: str) -> None:
        return source_commands.cmd_add(self, arg)

    def cmd_remove(self, arg: str) -> None:
        return source_commands.cmd_remove(self, arg)

    def cmd_ingest(self, arg: str) -> None:
        return ingestion_ui.cmd_ingest(self, arg)

    def cmd_mode(self, arg: str) -> None:
        return query_flow.cmd_mode(self, arg)

    def cmd_clear(self, arg: str) -> None:
        return query_flow.cmd_clear(self, arg)

    def cmd_show(self, arg: str) -> None:
        return query_flow.cmd_show(self, arg)

    def cmd_retry(self, arg: str) -> None:
        return query_flow.cmd_retry(self, arg)

    def first_run_offer(self) -> None:
        return source_commands.first_run_offer(self)

    # -- status --------------------------------------------------------------

    def cmd_status(self, arg: str) -> None:
        settings = self.context.settings
        sources = self.state.sources
        active = sum(1 for s in sources if s.status == "active")
        table = Table(show_header=False, box=None, pad_edge=False)
        table.add_column(style="bold", no_wrap=True)
        table.add_column(overflow="fold")
        table.add_row("Data dir", str(settings.data_dir))
        table.add_row("Sources", f"{active} active / {len(sources)} total")
        table.add_row("Indexed", "yes" if self.state.indexed else "no")
        table.add_row("Model", str(settings.gen_model))
        table.add_row("Embeddings", str(settings.embed_model))
        table.add_row("Mode", self.mode.value if self.mode else "auto")
        table.add_row("Turns", str(len(self.history)))
        history_file = Path(str(settings.data_dir)) / "history"
        if history_file.exists():
            table.add_row("History", str(history_file))
        self.console.print(table)


def run_session(
    context: Any,
    *,
    input_fn: Callable[[str], str] = input,
    console: Console | None = None,
    query_service_factory: QueryServiceFactory | None = None,
    reader: LineReader | None = None,
    state: SessionState | None = None,
    health_check: Callable[[], HealthReport] | None = None,
    offer_first_run: bool = False,
) -> None:
    registry = default_registry()
    state = state if state is not None else SessionState()
    state.refresh_sources(context.source_manager)
    console = console or Console()
    if reader is None:
        reader = make_reader(input_fn, context, state, registry, console)
    session = _Session(
        context,
        reader,
        console,
        query_service_factory or _default_factory,
        registry=registry,
        state=state,
        health_check=health_check,
    )
    session.banner()
    if offer_first_run:
        session.first_run_offer()
    session.loop()
