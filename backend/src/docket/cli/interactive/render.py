"""Rendering of query results for the interactive session."""

from __future__ import annotations

from typing import Any

from rich.console import Console
from rich.markdown import Markdown


def render(console: Console, result: Any) -> None:
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
    say(f"[{result.mode}]", style="dim")
    say()
