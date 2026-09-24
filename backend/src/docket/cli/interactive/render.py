"""Rendering of query results for the interactive session."""

from __future__ import annotations

from typing import Any

from rich.console import Console
from rich.markdown import Markdown


def footer_text(mode: str, elapsed: float | None = None) -> str:
    phrase = "investigated with agent" if mode == "agent" else "quick search"
    if elapsed is not None and elapsed >= 0.05:
        return f"{phrase} \u00b7 {elapsed:.1f}s"
    return phrase


def render(console: Console, result: Any, elapsed: float | None = None) -> None:
    def say(message: str = "", **kw: Any) -> None:
        console.print(message, markup=False, highlight=False, **kw)

    say()
    console.print(Markdown(result.answer))
    if result.citations:
        say()
        say("Citations:")
        for citation in result.citations:
            say(f"  {citation.citation_label}")
    for warning in result.validation_warnings:
        say(f"warning: {warning}", style="yellow dim")
    say(footer_text(str(result.mode), elapsed), style="dim")
    say()
