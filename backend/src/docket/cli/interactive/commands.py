"""Slash-command registry for the interactive session (pure Python)."""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class SlashCommand:
    name: str
    summary: str
    handler: Callable[[Any, str], bool | None]  # (session, arg) -> True means exit
    aliases: tuple[str, ...] = ()
    arg_hint: str = ""
    arg_completer: Any = None  # reserved for CP1

    @property
    def usage(self) -> str:
        return f"/{self.name} {self.arg_hint}".rstrip()


class CommandRegistry:
    def __init__(self) -> None:
        self._commands: list[SlashCommand] = []

    def register(self, cmd: SlashCommand) -> None:
        taken = {t for c in self._commands for t in (c.name, *c.aliases)}
        for token in (cmd.name, *cmd.aliases):
            if token.lower() in taken:
                raise ValueError(f"duplicate command token: {token}")
        self._commands.append(cmd)

    def all(self) -> list[SlashCommand]:
        return list(self._commands)

    def resolve(self, token: str) -> SlashCommand | None:
        token = token.lower()
        if not token:
            return None
        for cmd in self._commands:
            if token == cmd.name.lower() or token in (a.lower() for a in cmd.aliases):
                return cmd
        matches = self.prefix_matches(token)
        return matches[0] if len(matches) == 1 else None

    def prefix_matches(self, token: str) -> list[SlashCommand]:
        token = token.lower()
        if not token:
            return []
        return [c for c in self._commands if c.name.lower().startswith(token)]

    def suggest(self, token: str) -> str | None:
        token = token.lower()
        lookup: dict[str, str] = {}
        for cmd in self._commands:
            for t in (cmd.name, *cmd.aliases):
                lookup[t.lower()] = cmd.name
        close = difflib.get_close_matches(token, list(lookup), n=1, cutoff=0.6)
        return lookup[close[0]] if close else None

    def render_help(self, footer: str = "") -> str:
        cmds = self._commands
        width = max((len(c.usage) for c in cmds), default=0) + 2
        lines = ["Type a question to ask over your indexed evidence. Commands:", ""]
        for c in cmds:
            summary = c.summary
            if c.aliases:
                summary += " (also: " + ", ".join(f"/{a}" for a in c.aliases) + ")"
            lines.append(f"  {c.usage:<{width}}{summary}")
        text = "\n".join(lines)
        if footer:
            text += "\n\n" + footer
        return text


HELP_FOOTER = (
    "Note: `docket watch <source-id>` (auto re-ingest on changes) blocks, so run it\n"
    "in a separate terminal rather than here."
)

# Bare words (no slash) that trigger a command when they are the whole line.
BARE_WORDS = {"?": "help", "help": "help", "exit": "exit", "quit": "exit"}


def _call(method: str) -> Callable[[Any, str], bool | None]:
    def handler(session: Any, arg: str) -> bool | None:
        return getattr(session, method)(arg)

    return handler


def default_registry() -> CommandRegistry:
    reg = CommandRegistry()
    reg.register(SlashCommand("help", "show this help", _call("cmd_help"), aliases=("h", "?")))
    reg.register(
        SlashCommand("sources", "list registered sources", _call("cmd_sources"), aliases=("ls",))
    )
    reg.register(
        SlashCommand(
            "add", "register a folder as a source", _call("cmd_add"), arg_hint="<folder>"
        )
    )
    reg.register(
        SlashCommand(
            "ingest",
            "index a source (default: all active sources)",
            _call("cmd_ingest"),
            arg_hint="[<source-id>|all]",
        )
    )
    reg.register(
        SlashCommand(
            "mode",
            "show or set the query mode (default: auto)",
            _call("cmd_mode"),
            arg_hint="[auto|fast|agent]",
        )
    )
    reg.register(SlashCommand("clear", "forget the conversation so far", _call("cmd_clear")))
    reg.register(
        SlashCommand(
            "exit",
            "leave (Ctrl-D works too)",
            lambda session, arg: True,
            aliases=("quit", "q"),
        )
    )
    return reg
