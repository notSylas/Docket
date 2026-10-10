"""Open Docket in its own detached terminal window, and manage the app-menu launcher.

Everything here is deliberately small and side-effect-free until a function is
explicitly called, so it can be unit-tested with `subprocess`/`shutil.which`
mocked. Linux only; other platforms fall back to running in place.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

WINDOW_TITLE = "Docket"
IN_WINDOW_ENV = "DOCKET_IN_WINDOW"
NO_WINDOW_ENV = "DOCKET_NO_WINDOW"
TERMINAL_ENV = "DOCKET_TERMINAL"

KNOWN_TERMINALS: tuple[str, ...] = (
    "gnome-terminal",
    "ptyxis",
    "kgx",
    "konsole",
    "xfce4-terminal",
    "tilix",
    "kitty",
    "alacritty",
    "wezterm",
    "foot",
    "xterm",
)

DESKTOP_FILE_NAME = "docket.desktop"
ICON_FILE_NAME = "docket.svg"


# -- terminal detection -------------------------------------------------------


def find_terminal(env: dict[str, str] | None = None) -> list[str] | None:
    """Return `[absolute terminal path, *extra args]` or None.

    Order: DOCKET_TERMINAL, $TERMINAL, known emulators, then x-terminal-emulator.
    The first two may carry extra arguments (split shell-style).
    """
    env = os.environ if env is None else env
    for var in (TERMINAL_ENV, "TERMINAL"):
        raw = (env.get(var) or "").strip()
        if not raw:
            continue
        try:
            parts = shlex.split(raw)
        except ValueError:
            continue
        if not parts:
            continue
        resolved = shutil.which(parts[0])
        if resolved:
            return [resolved, *parts[1:]]
    # Known emulators first: Debian/Ubuntu's `x-terminal-emulator` can resolve to
    # a wrapper (e.g. gnome-terminal.wrapper) that did not run a `-- cmd`
    # invocation in testing, whereas the real emulator does. It is the fallback.
    for name in (*KNOWN_TERMINALS, "x-terminal-emulator"):
        resolved = shutil.which(name)
        if resolved:
            return [resolved]
    return None


def _terminal_kind(path: str) -> str:
    """Classify a terminal executable by its real basename (alternatives symlinks)."""
    candidates = [os.path.basename(path)]
    try:
        candidates.append(os.path.basename(os.path.realpath(path)))
    except OSError:
        pass
    for base in candidates:
        for name in KNOWN_TERMINALS:
            if base == name or base.startswith(name + "."):
                return name
    return "generic"


def build_terminal_argv(
    terminal: list[str], command: list[str], cwd: str, title: str = WINDOW_TITLE
) -> list[str]:
    """Wrap `command` (an argv list) in the terminal's own "run this" syntax."""
    exe, extra = terminal[0], terminal[1:]
    kind = _terminal_kind(exe)
    if kind == "gnome-terminal":
        tail = [f"--title={title}", "--geometry=110x34", f"--working-directory={cwd}", "--", *command]
    elif kind == "ptyxis":
        tail = ["--new-window", f"--title={title}", f"--working-directory={cwd}", "--", *command]
    elif kind == "kgx":
        tail = ["--title", title, "--working-directory", cwd, "-e", shlex.join(command)]
    elif kind == "konsole":
        tail = ["--workdir", cwd, "-p", f"tabtitle={title}", "-e", *command]
    elif kind == "xfce4-terminal":
        tail = [f"--title={title}", f"--working-directory={cwd}", "-x", *command]
    elif kind == "tilix":
        tail = ["--title", title, "--working-directory", cwd, "-e", shlex.join(command)]
    elif kind == "kitty":
        tail = ["--title", title, "--directory", cwd, *command]
    elif kind == "alacritty":
        tail = ["--title", title, "--working-directory", cwd, "-e", *command]
    elif kind == "wezterm":
        tail = ["start", "--cwd", cwd, "--", *command]
    elif kind == "foot":
        tail = [f"--title={title}", f"--working-directory={cwd}", *command]
    elif kind == "xterm":
        tail = ["-T", title, "-e", *command]
    else:  # x-terminal-emulator or an unknown $TERMINAL: the near-universal -e
        tail = ["-e", *command]
    return [exe, *extra, *tail]


# -- docket executable + environment -----------------------------------------


def resolve_docket_command() -> list[str]:
    """Absolute argv prefix that runs this Docket, independent of the new window's PATH."""
    argv0 = sys.argv[0] if sys.argv else ""
    if argv0 and os.path.basename(argv0) == "docket":
        candidate = os.path.abspath(argv0)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return [candidate]
    found = shutil.which("docket")
    if found:
        return [os.path.abspath(found)]
    return [sys.executable, "-m", "docket"]


def forwarded_env(env: dict[str, str] | None = None) -> list[str]:
    """`NAME=value` pairs for every DOCKET_* variable, plus DOCKET_IN_WINDOW=1."""
    env = os.environ if env is None else env
    pairs = {k: v for k, v in env.items() if k.startswith("DOCKET_")}
    pairs[IN_WINDOW_ENV] = "1"
    return [f"{k}={v}" for k, v in sorted(pairs.items())]


def build_spawn_argv(
    terminal: list[str],
    cwd: str,
    env: dict[str, str] | None = None,
    docket_command: list[str] | None = None,
) -> list[str]:
    docket = docket_command if docket_command is not None else resolve_docket_command()
    inner = ["env", *forwarded_env(env), *docket, "chat", "--in-window"]
    return build_terminal_argv(terminal, inner, cwd)


# -- spawning ---------------------------------------------------------------


def fallback_reason(env: dict[str, str] | None = None, platform: str | None = None) -> str | None:
    """Why a window cannot/should not be opened, or None if it can.

    An empty string means "fall back silently" (explicit opt-out / already in a window).
    """
    env = os.environ if env is None else env
    platform = sys.platform if platform is None else platform
    if env.get(IN_WINDOW_ENV) == "1" or env.get(NO_WINDOW_ENV) == "1":
        return ""
    if not platform.startswith("linux"):
        return "Opening a separate window is only supported on Linux; running here."
    if env.get("SSH_CONNECTION") or env.get("SSH_TTY"):
        return "SSH session detected; running here instead of opening a window."
    if not (env.get("DISPLAY") or env.get("WAYLAND_DISPLAY")):
        return "No display available; running here instead of opening a window."
    if env.get("TERM") == "dumb":
        return "TERM=dumb; running here instead of opening a window."
    return None


def spawn_window(env: dict[str, str] | None = None, platform: str | None = None) -> bool:
    """Open Docket in a detached terminal window. True if spawned, False to run in place."""
    env = os.environ if env is None else env
    reason = fallback_reason(env, platform)
    if reason is not None:
        if reason:
            print(reason, file=sys.stderr)
        return False
    terminal = find_terminal(env)
    if terminal is None:
        print(
            "No terminal emulator found (set DOCKET_TERMINAL); running here.",
            file=sys.stderr,
        )
        return False
    cwd = os.getcwd()
    argv = build_spawn_argv(terminal, cwd, env)
    try:
        subprocess.Popen(
            argv,
            cwd=cwd,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        print(f"Could not open a window ({exc}); running here.", file=sys.stderr)
        return False
    print("Opened Docket in a new window.")
    return True


# -- app-menu launcher ------------------------------------------------------


def _data_home() -> Path:
    raw = os.environ.get("XDG_DATA_HOME")
    if raw and os.path.isabs(raw):
        return Path(raw)
    return Path.home() / ".local" / "share"


def desktop_file_path() -> Path:
    return _data_home() / "applications" / DESKTOP_FILE_NAME


def icon_file_path() -> Path:
    return _data_home() / "icons" / "hicolor" / "scalable" / "apps" / ICON_FILE_NAME


def packaged_icon_path() -> Path:
    return Path(__file__).resolve().parents[2] / "assets" / ICON_FILE_NAME


def _desktop_quote(arg: str) -> str:
    """Always double-quote one Exec argument, escaping per the Desktop Entry spec."""
    escaped = (
        arg.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$")
    ).replace("%", "%%")
    return f'"{escaped}"'


def desktop_entry(docket_command: list[str] | None = None) -> str:
    docket = docket_command if docket_command is not None else resolve_docket_command()
    exec_line = " ".join([*(_desktop_quote(a) for a in docket), "launch"])
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Docket\n"
        "Comment=Local-first, evidence-backed work assistant\n"
        f"Exec={exec_line}\n"
        "Terminal=false\n"
        "Icon=docket\n"
        "Categories=Utility;\n"
        "StartupWMClass=Docket\n"
    )


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _update_desktop_database(directory: Path) -> None:
    tool = shutil.which("update-desktop-database")
    if not tool:
        return
    try:
        subprocess.run(
            [tool, str(directory)],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )
    except Exception:
        pass


def install_launcher(docket_command: list[str] | None = None) -> tuple[Path, Path]:
    """Write the .desktop entry and icon under the user's XDG data dir (idempotent)."""
    desktop, icon = desktop_file_path(), icon_file_path()
    _atomic_write(icon, packaged_icon_path().read_bytes())
    _atomic_write(desktop, desktop_entry(docket_command).encode("utf-8"))
    _update_desktop_database(desktop.parent)
    return desktop, icon


def uninstall_launcher() -> list[Path]:
    """Remove what install_launcher wrote; returns the paths actually removed."""
    removed: list[Path] = []
    for path in (desktop_file_path(), icon_file_path()):
        try:
            path.unlink()
            removed.append(path)
        except FileNotFoundError:
            pass
    if removed:
        _update_desktop_database(desktop_file_path().parent)
    return removed


# -- one-time opt-in offer --------------------------------------------------


def maybe_offer_launcher(data_dir: Path, ask=input, out=print) -> bool:
    """Ask once whether to add Docket to the app menu. Returns True if installed.

    The answer is recorded in `<data_dir>/launcher_prompt` so it is never asked
    again. Only call this on an interactive TTY; it is a no-op under pytest,
    off Linux, or when a launcher already exists.
    """
    if os.environ.get("PYTEST_CURRENT_TEST") or not sys.platform.startswith("linux"):
        return False
    marker = Path(data_dir) / "launcher_prompt"
    if marker.exists() or desktop_file_path().exists():
        return False
    try:
        answer = ask("Add Docket to your app menu? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    installed = answer in ("y", "yes")
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("yes\n" if installed else "no\n", encoding="utf-8")
    except OSError:
        pass
    if installed:
        try:
            desktop, _ = install_launcher()
            out(f"Added Docket to your app menu ({desktop}).")
        except OSError as exc:
            out(f"Could not add the launcher: {exc}")
            return False
    return installed
