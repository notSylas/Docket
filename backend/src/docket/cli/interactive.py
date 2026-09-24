"""Interactive chat-like session for the `docket` CLI (bare `docket` in a TTY,
or `docket chat`). One `AppContext` per session, multi-turn memory via
`ConversationTurn` history, and slash-commands for source management.

`run_session` takes an injectable `input_fn` and rich `Console` so it is
testable without a TTY, and a `query_service_factory` so tests can supply a
fake `QueryService`.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

from rich.console import Console
from rich.markdown import Markdown

from docket import __version__
from docket.db.models import SourceStatus
from docket.inference.gateway import InferenceError, ModelNotFoundError
from docket.ingestion.pipeline import SourceNotActiveError
from docket.query.classifier import QueryMode
from docket.query.conversation import ConversationTurn
from docket.query.service import QueryService
from docket.sources.manager import SourceNotFoundError

PROMPT = "docket> "

HELP_TEXT = """\
Type a question to ask over your indexed evidence. Commands:

  /help                      show this help
  /sources                   list registered sources
  /add <folder>              register a folder as a source
  /ingest [<source-id>|all]  index a source (default: all active sources)
  /mode [auto|fast|agent]    show or set the query mode (default: auto)
  /clear                     forget the conversation so far
  /exit, /quit               leave (Ctrl-D also works)

Note: `docket watch <source-id>` (auto re-ingest on changes) blocks, so run it
in a separate terminal rather than here."""

QueryServiceFactory = Callable[[Any, Any], Any]


def _default_factory(context: Any, table: Any) -> QueryService:
    return QueryService(
        engine=context.engine,
        table=table,
        gateway=context.gateway,
        resolver=context.resolver,
        settings=context.settings,
    )


def _setup_readline() -> None:
    try:
        import readline  # noqa: F401
    except ImportError:
        pass


class _Session:
    def __init__(
        self,
        context: Any,
        input_fn: Callable[[str], str],
        console: Console,
        factory: QueryServiceFactory,
    ) -> None:
        self.context = context
        self.input_fn = input_fn
        self.console = console
        self.factory = factory
        self.history: list[ConversationTurn] = []
        self.mode: QueryMode | None = None
        self._service: Any | None = None

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
        if count == 0:
            self.say("No sources yet. Get started: /add <folder>, then /ingest.", style="cyan")

    # -- main loop ---------------------------------------------------------

    def loop(self) -> None:
        while True:
            try:
                line = self.input_fn(PROMPT)
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
                if line.startswith("/"):
                    if self.command(line):
                        return
                else:
                    self.ask(line)
            except KeyboardInterrupt:
                self.say()
                self.say("(interrupted)", style="dim")
            except Exception as exc:  # last resort: never crash the session
                self.handle_error(exc)

    def handle_error(self, exc: BaseException) -> None:
        if os.environ.get("DOCKET_DEBUG"):
            import traceback

            self.error(traceback.format_exc())
        if isinstance(exc, ModelNotFoundError):
            self.error(f"Model not found: {exc}")
        elif isinstance(exc, InferenceError):
            self.error(f"Inference error: {exc}")
        elif isinstance(exc, (SourceNotFoundError, SourceNotActiveError)):
            self.error(f"Error: {exc}")
        elif isinstance(exc, ValueError):
            self.error(f"Error: {exc}")
        else:
            self.error(f"Unexpected error ({type(exc).__name__}): {exc}")

    # -- commands ----------------------------------------------------------

    def command(self, line: str) -> bool:
        """Run a slash command. Returns True to exit the session."""
        parts = line[1:].split(maxsplit=1)
        name = parts[0].lower() if parts else ""
        arg = parts[1].strip() if len(parts) > 1 else ""
        if name in ("exit", "quit"):
            return True
        handlers = {
            "help": self.cmd_help,
            "sources": self.cmd_sources,
            "add": self.cmd_add,
            "ingest": self.cmd_ingest,
            "mode": self.cmd_mode,
            "clear": self.cmd_clear,
        }
        handler = handlers.get(name)
        if handler is None:
            self.say(f"Unknown command: /{name}. Type /help for the list of commands.")
        else:
            handler(arg)
        return False

    def cmd_help(self, arg: str) -> None:
        self.say(HELP_TEXT)

    def cmd_sources(self, arg: str) -> None:
        sources = self.context.source_manager.list_sources()
        if not sources:
            self.say("No sources registered. Use /add <folder>.")
            return
        self.say(f"{'ID':<40}{'STATUS':<12}PATH")
        for source in sources:
            self.say(f"{source.id:<40}{source.status.value:<12}{source.path}")

    def cmd_add(self, arg: str) -> None:
        if not arg:
            self.say("Usage: /add <folder>")
            return
        path = Path(arg).expanduser()
        try:
            source = self.context.source_manager.register_source(path)
        except ValueError as exc:
            self.error(f"Error: {exc}")
            return
        self.say(f"Registered source {source.id}")
        self.say(f"Next: /ingest {source.id}", style="dim")

    def cmd_ingest(self, arg: str) -> None:
        if not arg or arg.lower() == "all":
            target_ids = [
                s.id
                for s in self.context.source_manager.list_sources()
                if s.status == SourceStatus.ACTIVE
            ]
            if not target_ids:
                self.say("No active sources to ingest. Use /add <folder> first.")
                return
        else:
            target_ids = [arg]

        for target_id in target_ids:
            try:
                with self.console.status(f"Ingesting {target_id}..."):
                    result = self.context.pipeline.run_ingestion_for_source(target_id)
            except (SourceNotFoundError, SourceNotActiveError) as exc:
                self.error(f"Error ingesting {target_id}: {exc}")
                continue
            chunks = sum(r.chunks_written for r in result.file_results)
            self.say(
                f"[{result.source_id}] status={result.status} "
                f"files_processed={result.files_processed} "
                f"files_failed={result.files_failed} chunks_written={chunks}"
            )
            for fr in result.file_results:
                if fr.status == "failed":
                    self.error(f"  FAILED: {fr.path} -- {fr.error}")

    def cmd_mode(self, arg: str) -> None:
        arg = arg.lower()
        if not arg:
            self.say(f"Mode: {self.mode.value if self.mode else 'auto'}")
            return
        if arg == "auto":
            self.mode = None
        elif arg in ("fast", "agent"):
            self.mode = QueryMode(arg)
        else:
            self.say("Usage: /mode [auto|fast|agent]")
            return
        self.say(f"Mode set to {arg}.")

    def cmd_clear(self, arg: str) -> None:
        self.history.clear()
        self.say("Conversation history cleared.")

    # -- questions ---------------------------------------------------------

    def get_service(self) -> Any | None:
        if self._service is not None:
            return self._service
        table = self.context.vector_writer.table
        if table is None:
            return None
        self._service = self.factory(self.context, table)
        return self._service

    def ask(self, question: str) -> None:
        service = self.get_service()
        if service is None:
            self.say("Nothing indexed yet -- /add a folder and /ingest it first.")
            return
        with self.console.status("Thinking..."):
            result = service.ask(question, mode=self.mode, history=list(self.history))
        self.render(result)
        self.history.append(ConversationTurn(question=question, answer=result.answer))

    def render(self, result: Any) -> None:
        self.say()
        self.console.print(Markdown(result.answer))
        if result.citations:
            self.say()
            self.say("Citations:")
            for citation in result.citations:
                self.say(f"  {citation.citation_label}")
        for warning in result.validation_warnings:
            self.say(f"warning: {warning}", style="yellow dim")
        self.say(f"[{result.mode}]", style="dim")
        self.say()


def run_session(
    context: Any,
    *,
    input_fn: Callable[[str], str] = input,
    console: Console | None = None,
    query_service_factory: QueryServiceFactory | None = None,
) -> None:
    if input_fn is input:
        _setup_readline()
    session = _Session(
        context,
        input_fn,
        console or Console(),
        query_service_factory or _default_factory,
    )
    session.banner()
    session.loop()
