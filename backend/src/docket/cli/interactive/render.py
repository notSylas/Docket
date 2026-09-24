"""Rendering of query results for the interactive session."""

from __future__ import annotations

import re
from typing import Any

from rich.console import Console
from rich.markdown import Markdown


def footer_text(mode: str, elapsed: float | None = None) -> str:
    phrase = "investigated with agent" if mode == "agent" else "quick search"
    if elapsed is not None and elapsed >= 0.05:
        return f"{phrase} \u00b7 {elapsed:.1f}s"
    return phrase


def number_citations(answer: str, citations: list[Any]) -> tuple[str, list[Any]]:
    """Rewrite known citation labels to `[n]` (numbered by first appearance).

    Returns the rewritten text and the citations in number order. Citations
    whose label never appears are appended last; unknown tags are untouched.
    """
    by_label: dict[str, Any] = {}
    for c in citations:
        by_label.setdefault(c.citation_label, c)
    if not by_label:
        return answer, []
    labels = sorted(by_label, key=len, reverse=True)
    pattern = re.compile("|".join(re.escape(label) for label in labels))
    order: list[str] = []
    numbers: dict[str, int] = {}

    def repl(m: "re.Match[str]") -> str:
        label = m.group(0)
        if label not in numbers:
            numbers[label] = len(order) + 1
            order.append(label)
        return f"[{numbers[label]}]"

    text = pattern.sub(repl, answer)
    for label in by_label:
        if label not in numbers:
            numbers[label] = len(order) + 1
            order.append(label)
    return text, [by_label[label] for label in order]


def render(
    console: Console,
    result: Any,
    elapsed: float | None = None,
    text: str | None = None,
    numbered: list[Any] | None = None,
) -> None:
    def say(message: str = "", **kw: Any) -> None:
        console.print(message, markup=False, highlight=False, **kw)

    say()
    if text is None or numbered is None:
        text, numbered = number_citations(result.answer, list(result.citations))
    console.print(Markdown(text))
    if numbered:
        say()
        say("Sources:", style="dim")
        for n, citation in enumerate(numbered, 1):
            say(f"  [{n}] {citation.source_display_name}", style="dim")
        say("/show <n> to read the evidence", style="dim")
    for warning in result.validation_warnings:
        say(f"warning: {warning}", style="yellow dim")
    say(footer_text(str(result.mode), elapsed), style="dim")
    say()
