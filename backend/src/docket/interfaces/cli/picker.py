"""Native folder picker: a desktop dialog for choosing a folder to add.

Docket runs in a terminal, so the dialog is a separate GUI process:
`zenity`, then `kdialog`, then `yad` (first one installed wins). The call is
blocking and meant for a worker thread; the UI never waits on it.

Only argv lists are ever run (no shell), the executable is an absolute path
found with `shutil.which`, and the environment passes through unchanged so
the dialog can reach the display and session bus (DISPLAY, WAYLAND_DISPLAY,
DBUS_SESSION_BUS_ADDRESS, XAUTHORITY).

The picker is skipped, with a plain reason, over SSH, without a display, with
TERM=dumb, or when none of the tools is installed. The typed path always
works.

Note on the alternative: the xdg-desktop-portal `FileChooser` (D-Bus
`org.freedesktop.portal.FileChooser.OpenFile` with the directory option)
would give a sandbox-aware native dialog on every desktop with one code path,
but needs a D-Bus client and the async Response signal. It is deliberately
not implemented; zenity/kdialog/yad are plain subprocesses and cover the
common Linux desktops.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

DIALOG_TITLE = "Choose a folder for Docket"
DEFAULT_TIMEOUT_S = 300

# Preference order of the dialog tools.
PICKER_TOOLS: tuple[str, ...] = ("zenity", "kdialog", "yad")

_SSH_VARS = ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY")


@dataclass(frozen=True)
class PickResult:
    """Outcome of one dialog. Exactly one of `path`, `cancelled`, `error` or
    `unavailable` is meaningful."""

    path: str | None = None
    cancelled: bool = False
    error: str = ""
    unavailable: str = ""  # why no dialog could be shown (not an error)

    @property
    def ok(self) -> bool:
        return self.path is not None


def unavailable_reason(
    env: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> str:
    """Plain reason a native dialog cannot be shown here; "" when it can."""
    env = os.environ if env is None else env
    if any(env.get(v) for v in _SSH_VARS):
        return "This session is over SSH, so there is no desktop to show a dialog on."
    if (env.get("TERM") or "").lower() == "dumb":
        return "This terminal cannot open a dialog."
    if not (env.get("DISPLAY") or env.get("WAYLAND_DISPLAY")):
        return "There is no desktop display."
    if _find_tool(which) is None:
        return "No folder dialog is installed (zenity, kdialog or yad)."
    return ""


def available(env: Mapping[str, str] | None = None, which: Callable[[str], str | None] = shutil.which) -> bool:
    """Cheap check for the UI: can a dialog be shown right now?"""
    return unavailable_reason(env, which) == ""


def _find_tool(which: Callable[[str], str | None]) -> tuple[str, str] | None:
    for name in PICKER_TOOLS:
        found = which(name)
        if found:
            return name, os.path.abspath(found)
    return None


def build_argv(tool: str, exe: str, start: str | None) -> list[str]:
    """The argv list for one tool. `start` is an existing folder or None."""
    if tool == "zenity":
        argv = [exe, "--file-selection", "--directory", f"--title={DIALOG_TITLE}"]
        if start:
            argv.append(f"--filename={start.rstrip('/')}/")
        return argv
    if tool == "kdialog":
        argv = [exe, "--title", DIALOG_TITLE, "--getexistingdirectory"]
        argv.append(start or str(Path.home()))
        return argv
    if tool == "yad":
        argv = [exe, "--file", "--directory", f"--title={DIALOG_TITLE}"]
        if start:
            argv.append(f"--filename={start.rstrip('/')}/")
        return argv
    raise ValueError(f"unknown picker tool: {tool}")


def _start_dir(start: str | os.PathLike[str] | None) -> str | None:
    if not start:
        return None
    try:
        p = Path(start).expanduser()
        if p.is_dir():
            return str(p.resolve())
    except (OSError, RuntimeError):
        pass
    return None


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def pick_folder(
    start: str | os.PathLike[str] | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
    *,
    env: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> PickResult:
    """Show the native folder dialog and block until it closes.

    Exit code 1 means the user cancelled; anything else non-zero is an error
    carrying the first line of stderr. Call this from a worker thread.
    """
    env_map = os.environ if env is None else env
    reason = unavailable_reason(env_map, which)
    if reason:
        return PickResult(unavailable=reason)
    tool = _find_tool(which)
    assert tool is not None  # unavailable_reason checked it
    name, exe = tool
    argv = build_argv(name, exe, _start_dir(start))
    try:
        proc = run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=dict(env_map),
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return PickResult(error="The folder dialog did not answer in time.")
    except OSError as exc:
        return PickResult(error=f"The folder dialog could not start: {exc}")
    code = proc.returncode
    if code == 1:
        return PickResult(cancelled=True)
    if code != 0:
        return PickResult(error=_first_line(proc.stderr) or f"The folder dialog failed (exit {code}).")
    chosen = _first_line(proc.stdout)
    if not chosen:
        return PickResult(cancelled=True)
    if name == "zenity" and "|" in chosen and not os.path.isdir(chosen):
        chosen = chosen.split("|", 1)[0]
    return PickResult(path=chosen)


class NativePicker:
    """The picker as a small object the UI holds (so tests can swap a stub)."""

    def available(self) -> bool:
        return available()

    def unavailable_reason(self) -> str:
        return unavailable_reason()

    def pick(self, start: str | None = None) -> PickResult:
        return pick_folder(start)


__all__ = [
    "DIALOG_TITLE",
    "NativePicker",
    "PICKER_TOOLS",
    "PickResult",
    "available",
    "build_argv",
    "pick_folder",
    "unavailable_reason",
]
