"""Interactive chat-like session for the `docket` CLI (bare `docket` in a TTY,
or `docket chat`). One `AppContext` per session, multi-turn memory via
`ConversationTurn` history, and slash-commands for source management.

`run_session` takes an injectable `input_fn` and rich `Console` so it is
testable without a TTY, and a `query_service_factory` so tests can supply a
fake `QueryService`.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any, Callable

from rich.console import Console
from rich.table import Table

from docket import __version__
from docket.db.models import SourceStatus
from docket.inference.gateway import (
    InferenceError,
    InferenceUnavailableError,
    ModelNotFoundError,
)
from docket.inference.health import HealthReport, format_health_warning
from docket.ingestion.pipeline import SUPPORTED_EXTENSIONS, ProgressEvent, SourceNotActiveError
from docket.query.classifier import QueryMode
from docket.query.conversation import ConversationTurn
from docket.query.service import QueryService
from docket.retrieval.resolver import ChunkNotFoundError
from docket.sources.manager import SourceNotFoundError

from .commands import BARE_WORDS, HELP_FOOTER, CommandRegistry, default_registry
from .reader import LineReader, make_reader
from .state import SessionState
from .render import number_citations, render

PROMPT = "docket> "

QueryServiceFactory = Callable[[Any, Any], Any]


def _default_factory(context: Any, table: Any) -> QueryService:
    return QueryService(
        engine=context.engine,
        table=table,
        gateway=context.gateway,
        resolver=context.resolver,
        settings=context.settings,
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
        if isinstance(exc, InferenceUnavailableError):
            self.error("Can't reach Ollama \u2014 is it running? (ollama serve)")
            self.say(str(exc), style="dim")
        elif isinstance(exc, ModelNotFoundError):
            match = re.search(r"[Mm]odel '([^']+)'", str(exc))
            model = match.group(1) if match else getattr(
                getattr(self.context, "settings", None), "gen_model", "<model>"
            )
            self.error(f"Model not found: {model} \u2014 run: ollama pull {model}")
            self.say(str(exc), style="dim")
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

    def cmd_help(self, arg: str) -> None:
        self.say(self.registry.render_help(HELP_FOOTER))

    def cmd_sources(self, arg: str) -> None:
        sources = self.context.source_manager.list_sources()
        if not sources:
            self.say("No sources registered. Use /add <folder>.")
            return
        table = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
        table.add_column("ID", no_wrap=True, overflow="fold")
        table.add_column("Status", no_wrap=True)
        table.add_column("Path", overflow="fold")
        for source in sources:
            table.add_row(str(source.id), source.status.value, str(source.path))
        self.console.print(table)

    def cmd_add(self, arg: str) -> None:
        if not arg:
            self.say("Usage: /add <folder>")
            return
        self._register(Path(arg).expanduser().resolve())

    def _register(self, path: Path) -> Any | None:
        """Register `path` (or report it's already registered). Returns the
        source id on success or when already registered, else None."""
        for existing in self.context.source_manager.list_sources():
            if Path(existing.path) == path:
                self.say(f"Already registered: {existing.id}")
                return existing.id
        try:
            source = self.context.source_manager.register_source(path)
        except ValueError as exc:
            self.error(f"Error: {exc}")
            return None
        self.state.refresh_sources(self.context.source_manager)
        self.sync_state()
        self.say(f"Registered source {source.id}")
        self.say(f"Next: /ingest {source.id}", style="dim")
        return source.id

    def first_run_offer(self) -> None:
        """Opt-in guided start: offer to register the cwd when nothing is set up."""
        if self.context.source_manager.list_sources():
            return
        cwd = Path.cwd().resolve()
        if cwd == Path("/") or cwd == Path.home().resolve():
            return
        if not self._confirm(f"No sources yet. Add the current directory ({cwd})? [y/N] "):
            return
        source_id = self._register(cwd)
        if source_id is None:
            return
        if self._confirm("Ingest it now? [y/N] "):
            try:
                self.cmd_ingest(str(source_id))
            except KeyboardInterrupt:
                self.say()
                self.say("(interrupted)", style="dim")
            except Exception as exc:  # noqa: BLE001
                self.handle_error(exc)

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

        try:
            self._ingest_targets(target_ids)
        finally:
            self.state.refresh_sources(self.context.source_manager)
            self.sync_state()

    def _ingest_targets(self, target_ids: list[str]) -> None:
        ctx = self.context
        vars_ = vars(ctx) if hasattr(ctx, "__dict__") else {}
        # Constructing the pipeline builds the Docling parser, which loads
        # models (slow the first time). Say so before it happens.
        cold = "pipeline" not in vars_ and "parser" not in vars_
        for target_id in target_ids:
            status = None
            try:
                with self.console.status(f"Ingesting {target_id}...") as status:
                    if cold:
                        self.say(
                            "Loading document parser (first time only \u2014 "
                            "this can take a moment)\u2026",
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
                            self.error(f"FAILED: {event.path} -- {event.result.error}")

                    result = pipeline.run_ingestion_for_source(target_id, progress=on_progress)
            except (SourceNotFoundError, SourceNotActiveError) as exc:
                self.error(f"Error ingesting {target_id}: {exc}")
                continue
            self._summarize(target_id, result)

    def _summarize(self, target_id: str, result: Any) -> None:
        if result.files_processed == 0 and result.status == "failed":
            folder = next(
                (str(s.path) for s in self.context.source_manager.list_sources()
                 if s.id == result.source_id),
                result.source_id,
            )
            exts = ", ".join(sorted(SUPPORTED_EXTENSIONS))
            self.say(f"No supported files found in {folder} (supported: {exts}).")
            return
        ingested = sum(1 for r in result.file_results if r.status == "ingested")
        unchanged = sum(1 for r in result.file_results if r.status == "unchanged")
        chunks = sum(r.chunks_written for r in result.file_results)
        self.say(
            f"{result.source_id}: {ingested} ingested, {unchanged} unchanged, "
            f"{result.files_failed} failed \u2014 {chunks} chunks written"
        )

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
        self.state.mode = arg
        self.say(f"Mode set to {arg}.")

    def cmd_clear(self, arg: str) -> None:
        self.history.clear()
        self._set_citations([], None)
        self.sync_state()
        self.say("Conversation history cleared.")

    def _set_citations(self, citations: list[Any], question: str | None) -> None:
        self.last_citations = citations
        self.last_question = question
        self.state.citations = tuple(
            (n, c.source_display_name) for n, c in enumerate(citations, 1)
        )

    def cmd_show(self, arg: str) -> None:
        if not self.last_citations:
            self.say("No answer yet \u2014 ask a question first.")
            return
        try:
            n = int(arg)
        except ValueError:
            self.say("Usage: /show <n>")
            return
        total = len(self.last_citations)
        if not 1 <= n <= total:
            self.say(f"No citation {n} in the last answer (it has {total}).")
            return
        citation = self.last_citations[n - 1]
        try:
            evidence = self.context.resolver.resolve(citation.chunk_id)
        except ChunkNotFoundError:
            self.say("That evidence is no longer available (source changed or removed).")
            return
        header = f"[{n}] {evidence.source_display_name}"
        if evidence.heading:
            header += f" \u2014 {evidence.heading}"
        self.console.print(header, style="bold", markup=False, highlight=False)
        self.say()
        self.say(evidence.text)
        self.say()
        self.say(f"chunk {evidence.chunk_id}", style="dim")

    def cmd_retry(self, arg: str) -> None:
        question = self.last_question
        if not question:
            self.say("Nothing to retry yet.")
            return
        if self.history and self.history[-1].question == question:
            self.history.pop()
        self.ask(question)

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

    def _confirm(self, prompt: str) -> bool:
        read = getattr(self.reader, "confirm", None) or self.reader.read
        try:
            reply = read(prompt)
        except (EOFError, KeyboardInterrupt):
            self.say()
            return False
        return reply.strip().lower() in ("y", "yes")

    def cmd_remove(self, arg: str) -> None:
        if not arg:
            self.say("Usage: /remove <source-id>")
            return
        manager = self.context.source_manager
        match = next((s for s in manager.list_sources() if s.id == arg), None)
        if match is None:
            self.error(f"Error: source not found: {arg}")
            return
        if not self._confirm(f"Remove source {arg} ({match.path})? [y/N] "):
            self.say("Cancelled.", style="dim")
            return
        try:
            manager.deactivate_source(arg)
        except SourceNotFoundError:
            self.error(f"Error: source not found: {arg}")
            return
        finally:
            self.state.refresh_sources(manager)
            self.sync_state()
        self.say(f"Removed {arg}. Its evidence is no longer searched.")

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
        start = time.perf_counter()
        with self.console.status("Thinking..."):
            result = service.ask(question, mode=self.mode, history=list(self.history))
        elapsed = time.perf_counter() - start
        text, numbered = number_citations(result.answer, list(result.citations))
        render(self.console, result, elapsed, text, numbered)
        # History keeps the ORIGINAL tagged answer so follow-ups stay consistent.
        self.history.append(ConversationTurn(question=question, answer=result.answer))
        self._set_citations(numbered, question)
        self.sync_state()


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
