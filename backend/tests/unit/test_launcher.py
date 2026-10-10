"""Launcher tests: subprocess/shutil mocked, tmp XDG_DATA_HOME, no real windows."""

from __future__ import annotations

import os
import sys

import pytest
from typer.testing import CliRunner

from docket.interfaces.cli import launcher
from docket.interfaces.cli import main as cli_main

DOCKET = ["/opt/bin/docket"]
GRAPHICAL = {"DISPLAY": ":0", "TERM": "xterm-256color"}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    for var in list(os.environ):
        if var.startswith("DOCKET_") and var != "DOCKET_DATA_DIR":
            monkeypatch.delenv(var)
    for var in ("TERMINAL", "SSH_CONNECTION", "SSH_TTY", "WAYLAND_DISPLAY", "DISPLAY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


def _which_map(monkeypatch, mapping):
    monkeypatch.setattr(launcher.shutil, "which", lambda name: mapping.get(name))


# -- detection ---------------------------------------------------------------


def test_find_terminal_prefers_docket_terminal(monkeypatch):
    _which_map(monkeypatch, {"kitty": "/usr/bin/kitty", "foot": "/usr/bin/foot", "xterm": "/usr/bin/xterm"})
    env = {"DOCKET_TERMINAL": "kitty", "TERMINAL": "foot"}
    assert launcher.find_terminal(env) == ["/usr/bin/kitty"]


def test_find_terminal_order_terminal_var_then_alternatives_then_known(monkeypatch):
    m = {"foot": "/usr/bin/foot", "x-terminal-emulator": "/usr/bin/x-terminal-emulator", "xterm": "/usr/bin/xterm"}
    _which_map(monkeypatch, m)
    assert launcher.find_terminal({"TERMINAL": "foot"}) == ["/usr/bin/foot"]
    assert launcher.find_terminal({}) == ["/usr/bin/foot"]
    del m["foot"]
    assert launcher.find_terminal({}) == ["/usr/bin/xterm"]
    del m["xterm"]
    assert launcher.find_terminal({}) == ["/usr/bin/x-terminal-emulator"]
    m.clear()
    assert launcher.find_terminal({}) is None


def test_find_terminal_known_order(monkeypatch):
    _which_map(monkeypatch, {"konsole": "/usr/bin/konsole", "xterm": "/usr/bin/xterm", "kitty": "/usr/bin/kitty"})
    assert launcher.find_terminal({}) == ["/usr/bin/konsole"]


def test_terminal_var_with_extra_args(monkeypatch):
    _which_map(monkeypatch, {"foot": "/usr/bin/foot"})
    assert launcher.find_terminal({"TERMINAL": "foot --fullscreen"}) == ["/usr/bin/foot", "--fullscreen"]


# -- argv --------------------------------------------------------------------


CMD = ["env", "DOCKET_IN_WINDOW=1", "/opt/bin/docket", "ui", "--in-window"]


@pytest.mark.parametrize(
    ("name", "expected_tail"),
    [
        ("gnome-terminal", ["--title=Docket", "--geometry=110x34", "--working-directory=/w", "--", *CMD]),
        ("ptyxis", ["--new-window", "--title=Docket", "--working-directory=/w", "--", *CMD]),
        ("konsole", ["--workdir", "/w", "-p", "tabtitle=Docket", "-e", *CMD]),
        ("xfce4-terminal", ["--title=Docket", "--working-directory=/w", "-x", *CMD]),
        ("kitty", ["--title", "Docket", "--directory", "/w", *CMD]),
        ("alacritty", ["--title", "Docket", "--working-directory", "/w", "-e", *CMD]),
        ("wezterm", ["start", "--cwd", "/w", "--", *CMD]),
        ("foot", ["--title=Docket", "--working-directory=/w", *CMD]),
        ("xterm", ["-T", "Docket", "-e", *CMD]),
        ("x-terminal-emulator-unknown", ["-e", *CMD]),
    ],
)
def test_argv_per_terminal(name, expected_tail):
    exe = f"/nonexistent/{name}"
    argv = launcher.build_terminal_argv([exe], CMD, "/w")
    assert argv == [exe, *expected_tail]


def test_argv_kgx_and_tilix_use_single_command_string():
    for name in ("kgx", "tilix"):
        argv = launcher.build_terminal_argv([f"/nonexistent/{name}"], CMD, "/w")
        assert argv[-2] == "-e"
        assert argv[-1] == " ".join(CMD)


def test_argv_is_a_list_with_unsafe_paths_untouched():
    cmd = ["/home/a b/it's/docket", "chat"]
    argv = launcher.build_terminal_argv(["/nonexistent/gnome-terminal"], cmd, "/w x")
    assert "--working-directory=/w x" in argv
    assert argv[-2:] == cmd


def test_build_spawn_argv_absolute_path_and_env_forwarding():
    env = {"DOCKET_DATA_DIR": "/scratch/d", "DOCKET_GEN_MODEL": "m", "PATH": "/bin", "HOME": "/h"}
    argv = launcher.build_spawn_argv(["/nonexistent/xterm"], "/w", env, DOCKET)
    inner = argv[argv.index("-e") + 1 :]
    assert inner[0] == "env"
    assert "DOCKET_DATA_DIR=/scratch/d" in inner
    assert "DOCKET_GEN_MODEL=m" in inner
    assert "DOCKET_IN_WINDOW=1" in inner
    assert not any(a.startswith(("PATH=", "HOME=")) for a in inner)
    assert inner[-3:] == ["/opt/bin/docket", "ui", "--in-window"]


def test_resolve_docket_command_absolute(monkeypatch, tmp_path):
    script = tmp_path / "docket"
    script.write_text("#!/bin/sh\n")
    script.chmod(0o755)
    monkeypatch.setattr(sys, "argv", [str(script)])
    assert launcher.resolve_docket_command() == [str(script)]
    monkeypatch.setattr(sys, "argv", ["pytest"])
    _which_map(monkeypatch, {"docket": "/usr/local/bin/docket"})
    assert launcher.resolve_docket_command() == ["/usr/local/bin/docket"]
    _which_map(monkeypatch, {})
    assert launcher.resolve_docket_command() == [sys.executable, "-m", "docket"]


def test_dunder_main_exists():
    import importlib.util

    assert importlib.util.find_spec("docket.__main__") is not None


# -- spawn -------------------------------------------------------------------


class _Popen:
    calls: list = []

    def __init__(self, argv, **kw):
        type(self).calls.append((argv, kw))


@pytest.fixture
def popen(monkeypatch):
    _Popen.calls = []
    monkeypatch.setattr(launcher.subprocess, "Popen", _Popen)
    monkeypatch.setattr(launcher, "resolve_docket_command", lambda: DOCKET)
    return _Popen


def test_spawn_window_detaches(monkeypatch, popen, tmp_path, capsys):
    _which_map(monkeypatch, {"gnome-terminal": "/usr/bin/gnome-terminal"})
    monkeypatch.chdir(tmp_path)
    env = {**GRAPHICAL, "DOCKET_DATA_DIR": "/scratch"}
    assert launcher.spawn_window(env, "linux") is True
    (argv, kw), = popen.calls
    assert argv[0] == "/usr/bin/gnome-terminal"
    assert f"--working-directory={os.getcwd()}" in argv
    assert kw["start_new_session"] is True
    assert kw["cwd"] == os.getcwd()
    assert kw["stdin"] == kw["stdout"] == kw["stderr"] == launcher.subprocess.DEVNULL
    assert "DOCKET_DATA_DIR=/scratch" in argv
    assert "Opened Docket in a new window." in capsys.readouterr().out


@pytest.mark.parametrize(
    ("env", "platform"),
    [
        ({"TERM": "xterm"}, "linux"),  # no display
        ({"DISPLAY": ":0", "SSH_CONNECTION": "1 2 3 4"}, "linux"),
        ({"DISPLAY": ":0", "SSH_TTY": "/dev/pts/1"}, "linux"),
        ({"DISPLAY": ":0", "TERM": "dumb"}, "linux"),
        ({"DISPLAY": ":0"}, "darwin"),
        ({"DISPLAY": ":0", "DOCKET_IN_WINDOW": "1"}, "linux"),
        ({"DISPLAY": ":0", "DOCKET_NO_WINDOW": "1"}, "linux"),
    ],
)
def test_fallbacks_do_not_spawn(monkeypatch, popen, env, platform):
    _which_map(monkeypatch, {"xterm": "/usr/bin/xterm"})
    assert launcher.spawn_window(env, platform) is False
    assert popen.calls == []


def test_wayland_display_counts(monkeypatch, popen):
    _which_map(monkeypatch, {"foot": "/usr/bin/foot"})
    assert launcher.spawn_window({"WAYLAND_DISPLAY": "wayland-0"}, "linux") is True


def test_no_terminal_found_falls_back(monkeypatch, popen):
    _which_map(monkeypatch, {})
    assert launcher.spawn_window(dict(GRAPHICAL), "linux") is False
    assert popen.calls == []


def test_popen_oserror_falls_back(monkeypatch):
    _which_map(monkeypatch, {"xterm": "/usr/bin/xterm"})

    def boom(*a, **k):
        raise OSError("nope")

    monkeypatch.setattr(launcher.subprocess, "Popen", boom)
    monkeypatch.setattr(launcher, "resolve_docket_command", lambda: DOCKET)
    assert launcher.spawn_window(dict(GRAPHICAL), "linux") is False


# -- desktop entry / install -------------------------------------------------


def test_desktop_entry_content():
    text = launcher.desktop_entry(["/opt/my bin/docket"])
    assert 'Exec="/opt/my bin/docket" launch\n' in text
    for line in ("Name=Docket", "Terminal=false", "Icon=docket", "Categories=Utility;", "StartupWMClass=Docket"):
        assert line in text.splitlines()
    assert any(line.startswith("Comment=") for line in text.splitlines())


def test_install_uninstall_idempotent(monkeypatch, tmp_path):
    monkeypatch.setattr(launcher.shutil, "which", lambda n: None)
    xdg = tmp_path / "xdg"
    desktop, icon = launcher.install_launcher(DOCKET)
    assert desktop == xdg / "applications" / "docket.desktop"
    assert icon == xdg / "icons" / "hicolor" / "scalable" / "apps" / "docket.svg"
    first = desktop.read_text()
    assert icon.read_text().lstrip().startswith("<svg")
    launcher.install_launcher(DOCKET)
    assert desktop.read_text() == first
    assert sorted(p.name for p in desktop.parent.iterdir()) == ["docket.desktop"]
    assert launcher.uninstall_launcher() == [desktop, icon]
    assert not desktop.exists() and not icon.exists()
    assert launcher.uninstall_launcher() == []
    assert not (tmp_path / "home").exists()


def test_update_desktop_database_failure_ignored(monkeypatch):
    monkeypatch.setattr(launcher.shutil, "which", lambda n: "/usr/bin/update-desktop-database")

    def boom(*a, **k):
        raise OSError("x")

    monkeypatch.setattr(launcher.subprocess, "run", boom)
    launcher.install_launcher(DOCKET)
    assert launcher.desktop_file_path().exists()


def test_packaged_icon_exists():
    assert launcher.packaged_icon_path().is_file()


# -- opt-in offer ------------------------------------------------------------


def test_offer_noop_under_pytest(tmp_path):
    asked = []
    assert launcher.maybe_offer_launcher(tmp_path, ask=lambda p: asked.append(p) or "y") is False
    assert asked == []


def test_offer_asked_once_and_installs_only_on_yes(monkeypatch, tmp_path):
    monkeypatch.delenv("PYTEST_CURRENT_TEST")
    monkeypatch.setattr(launcher.shutil, "which", lambda n: None)
    monkeypatch.setattr(launcher, "resolve_docket_command", lambda: DOCKET)
    data = tmp_path / "data"
    assert launcher.maybe_offer_launcher(data, ask=lambda p: "", out=lambda m: None) is False
    assert (data / "launcher_prompt").read_text().strip() == "no"
    assert not launcher.desktop_file_path().exists()
    # Never asked again.
    assert launcher.maybe_offer_launcher(data, ask=lambda p: "y", out=lambda m: None) is False
    data2 = tmp_path / "data2"
    assert launcher.maybe_offer_launcher(data2, ask=lambda p: "y", out=lambda m: None) is True
    assert launcher.desktop_file_path().exists()


# -- CLI ---------------------------------------------------------------------


def _fake_tty(monkeypatch):
    # CliRunner swaps sys.stdin/stdout, so stub the module's `sys` reference instead.
    from types import SimpleNamespace

    tty = SimpleNamespace(isatty=lambda: True)
    monkeypatch.setattr(cli_main, "sys", SimpleNamespace(stdin=tty, stdout=tty))


def test_bare_docket_with_tty_spawns(monkeypatch):
    calls = []
    _fake_tty(monkeypatch)
    monkeypatch.setattr(cli_main.launcher, "spawn_window", lambda: calls.append("spawn") or True)
    monkeypatch.setattr(cli_main, "_run_interactive", lambda: calls.append("inplace"))
    result = CliRunner().invoke(cli_main.app, [])
    assert result.exit_code == 0
    assert calls == ["spawn"]


def test_bare_docket_falls_back_in_place(monkeypatch):
    calls = []
    _fake_tty(monkeypatch)
    monkeypatch.setattr(cli_main.launcher, "spawn_window", lambda: False)
    monkeypatch.setattr(cli_main, "_run_interactive", lambda: calls.append("inplace"))
    CliRunner().invoke(cli_main.app, [])
    assert calls == ["inplace"]


def test_chat_stays_in_place(monkeypatch):
    calls = []
    monkeypatch.setattr(cli_main.launcher, "spawn_window", lambda: calls.append("spawn") or True)
    monkeypatch.setattr(cli_main, "_run_interactive", lambda: calls.append("inplace"))
    CliRunner().invoke(cli_main.app, ["chat"])
    assert calls == ["inplace"]


def test_in_window_error_waits_for_enter(monkeypatch):
    def boom():
        raise RuntimeError("kaput")

    monkeypatch.setattr(cli_main, "_run_interactive", boom)
    result = CliRunner().invoke(cli_main.app, ["chat", "--in-window"], input="\n")
    assert result.exit_code == 1
    assert "kaput" in result.output
    assert "Press Enter" in result.output


def test_install_uninstall_commands(monkeypatch):
    monkeypatch.setattr(launcher.shutil, "which", lambda n: None)
    monkeypatch.setattr(launcher, "resolve_docket_command", lambda: DOCKET)
    r = CliRunner()
    assert r.invoke(cli_main.app, ["install-launcher"]).exit_code == 0
    assert launcher.desktop_file_path().exists()
    assert r.invoke(cli_main.app, ["uninstall-launcher"]).exit_code == 0
    assert not launcher.desktop_file_path().exists()


def test_spawn_argv_uses_ui_by_default_and_chat_when_classic():
    term = ["/usr/bin/gnome-terminal"]
    default = launcher.build_spawn_argv(term, "/tmp", env={}, docket_command=["/opt/bin/docket"])
    assert default[-2:] == ["ui", "--in-window"]
    classic = launcher.build_spawn_argv(
        term, "/tmp", env={"DOCKET_CLASSIC": "1"}, docket_command=["/opt/bin/docket"]
    )
    assert classic[-2:] == ["chat", "--in-window"]
