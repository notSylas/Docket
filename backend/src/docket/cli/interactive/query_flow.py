"""Query/citation-review flow: asking questions, `/mode`, `/clear`, `/show`,
and `/retry`.

Plain functions taking `session` first, matching `source_commands`/
`ingestion_ui`. Kept together per the real coupling to
`session.history`/`session.last_citations`/`session.last_question` -- these
all read and write that shared, ordered state on the session itself.
"""

from __future__ import annotations

import time
from typing import Any

from docket.query.classifier import QueryMode
from docket.query.conversation import ConversationTurn
from docket.retrieval.resolver import ChunkNotFoundError

from .render import number_citations, render


def get_service(session: Any) -> Any | None:
    if session._service is not None:
        return session._service
    table = session.context.vector_writer.table
    if table is None:
        return None
    session._service = session.factory(session.context, table)
    return session._service


def ask(session: Any, question: str) -> None:
    service = get_service(session)
    if service is None:
        session.say("Nothing indexed yet -- /add a folder and /ingest it first.")
        return
    start = time.perf_counter()
    with session.console.status("Thinking..."):
        result = service.ask(question, mode=session.mode, history=list(session.history))
    elapsed = time.perf_counter() - start
    text, numbered = number_citations(result.answer, list(result.citations))
    render(session.console, result, elapsed, text, numbered)
    # History keeps the ORIGINAL tagged answer so follow-ups stay consistent.
    session.history.append(ConversationTurn(question=question, answer=result.answer))
    _set_citations(session, numbered, question)
    session.sync_state()


def cmd_mode(session: Any, arg: str) -> None:
    arg = arg.lower()
    if not arg:
        session.say(f"Mode: {session.mode.value if session.mode else 'auto'}")
        return
    if arg == "auto":
        session.mode = None
    elif arg in ("fast", "agent"):
        session.mode = QueryMode(arg)
    else:
        session.say("Usage: /mode [auto|fast|agent]")
        return
    session.state.mode = arg
    session.say(f"Mode set to {arg}.")


def cmd_clear(session: Any, arg: str) -> None:
    session.history.clear()
    _set_citations(session, [], None)
    session.sync_state()
    session.say("Conversation history cleared.")


def _set_citations(session: Any, citations: list[Any], question: str | None) -> None:
    session.last_citations = citations
    session.last_question = question
    session.state.citations = tuple(
        (n, c.source_display_name) for n, c in enumerate(citations, 1)
    )


def cmd_show(session: Any, arg: str) -> None:
    if not session.last_citations:
        session.say("No answer yet — ask a question first.")
        return
    try:
        n = int(arg)
    except ValueError:
        session.say("Usage: /show <n>")
        return
    total = len(session.last_citations)
    if not 1 <= n <= total:
        session.say(f"No citation {n} in the last answer (it has {total}).")
        return
    citation = session.last_citations[n - 1]
    try:
        evidence = session.context.resolver.resolve(citation.chunk_id)
    except ChunkNotFoundError:
        session.say("That evidence is no longer available (source changed or removed).")
        return
    header = f"[{n}] {evidence.source_display_name}"
    if evidence.heading:
        header += f" — {evidence.heading}"
    session.console.print(header, style="bold", markup=False, highlight=False)
    session.say()
    session.say(evidence.text)
    session.say()
    session.say(f"chunk {evidence.chunk_id}", style="dim")


def cmd_retry(session: Any, arg: str) -> None:
    question = session.last_question
    if not question:
        session.say("Nothing to retry yet.")
        return
    if session.history and session.history[-1].question == question:
        session.history.pop()
    ask(session, question)
