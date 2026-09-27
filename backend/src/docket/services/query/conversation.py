"""Conversation history support for multi-turn `QueryService.ask()`."""

from __future__ import annotations

from pydantic import BaseModel

DEFAULT_MAX_TURNS = 6
DEFAULT_MAX_CHARS = 6000


class ConversationTurn(BaseModel):
    question: str
    answer: str


def _size(turn: ConversationTurn) -> int:
    return len(turn.question) + len(turn.answer)


def trim_history(
    history: list[ConversationTurn] | None,
    max_turns: int = DEFAULT_MAX_TURNS,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> list[ConversationTurn]:
    """Return the most recent turns fitting BOTH `max_turns` and `max_chars`
    (question + answer characters summed), oldest dropped first, order kept.

    Choice: if even the single most recent turn exceeds `max_chars`, it is
    kept but its answer is truncated (trailing "...") rather than the whole
    history being dropped, since the latest turn is the most likely target of
    a follow-up reference. The question is kept intact where possible; if the
    question alone exceeds the budget the answer becomes just "...".
    """
    if not history or max_turns <= 0:
        return []

    kept: list[ConversationTurn] = []
    total = 0
    for turn in reversed(history):
        if len(kept) >= max_turns:
            break
        size = _size(turn)
        if total + size > max_chars:
            if not kept:
                room = max(max_chars - len(turn.question) - 3, 0)
                kept.append(
                    ConversationTurn(question=turn.question, answer=turn.answer[:room] + "...")
                )
            break
        kept.append(turn)
        total += size
    kept.reverse()
    return kept


def format_history_block(history: list[ConversationTurn]) -> str:
    """Render turns as alternating `User:` / `Assistant:` lines."""
    lines: list[str] = []
    for turn in history:
        lines.append(f"User: {turn.question}")
        lines.append(f"Assistant: {turn.answer}")
    return "\n".join(lines)
