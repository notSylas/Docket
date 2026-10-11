"""Native folder picker: argv per tool, fallbacks, exit-code mapping. Every
subprocess is mocked; no dialog is ever opened."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

from docket.interfaces.cli import picker

GOOD_ENV = {"DISPLAY": ":0", "TERM": "xterm-256color", "DBUS_SESSION_BUS_ADDRESS": "unix:path=/x", "XAUTHORITY": "/xa"}


def which_only(*names):
    return lambda n: f"/usr/bin/{n}" if n in names else None


class Runner:
    def __init__(self, rc=0, out="", err="", exc=None):
        self.calls = []
        self.rc, self.out, self.err, self.exc = rc, out, err, exc

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        if self.exc:
            raise self.exc
        return SimpleNamespace(returncode=self.rc, stdout=self.out, stderr=self.err)


def test_zenity_argv_and_path(tmp_path):
    r = Runner(out=f"{tmp_path}/Docs\n")
    res = picker.pick_folder(tmp_path, env=GOOD_ENV, which=which_only("zenity", "kdialog"), run=r)
    assert res.path == f"{tmp_path}/Docs" and not res.cancelled and not res.error
    argv, kw = r.calls[0]
    assert argv[0] == "/usr/bin/zenity"
    assert argv[1:3] == ["--file-selection", "--directory"]
    assert f"--filename={tmp_path.resolve()}/" in argv
    assert any(a.startswith("--title=") for a in argv)
    assert isinstance(argv, list) and "shell" not in kw
    assert kw["timeout"] == 300


def test_kdialog_then_yad_fallback_order(tmp_path):
    r = Runner(out="/x\n")
    picker.pick_folder(tmp_path, env=GOOD_ENV, which=which_only("kdialog", "yad"), run=r)
    argv = r.calls[0][0]
    assert argv[0] == "/usr/bin/kdialog" and "--getexistingdirectory" in argv and str(tmp_path.resolve()) in argv
    r = Runner(out="/x\n")
    picker.pick_folder(tmp_path, env=GOOD_ENV, which=which_only("yad"), run=r)
    argv = r.calls[0][0]
    assert argv[0] == "/usr/bin/yad" and argv[1:3] == ["--file", "--directory"]


def test_missing_tools_ssh_no_display_dumb():
    r = Runner()
    res = picker.pick_folder(env=GOOD_ENV, which=which_only(), run=r)
    assert res.unavailable and "zenity" in res.unavailable and not r.calls
    assert not picker.available(GOOD_ENV, which_only())
    assert picker.available(GOOD_ENV, which_only("zenity"))
    ssh = dict(GOOD_ENV, SSH_CONNECTION="1 2 3 4")
    assert "SSH" in picker.unavailable_reason(ssh, which_only("zenity"))
    assert "display" in picker.unavailable_reason({"TERM": "xterm"}, which_only("zenity"))
    assert picker.unavailable_reason(dict(GOOD_ENV, TERM="dumb"), which_only("zenity"))
    assert picker.available({"WAYLAND_DISPLAY": "wayland-0"}, which_only("kdialog"))
    assert not r.calls


def test_cancel_and_error_mapping():
    res = picker.pick_folder(env=GOOD_ENV, which=which_only("zenity"), run=Runner(rc=1))
    assert res.cancelled and res.path is None and not res.error
    res = picker.pick_folder(env=GOOD_ENV, which=which_only("zenity"), run=Runner(rc=5, err="\nGtk-WARNING boom\nmore"))
    assert res.error == "Gtk-WARNING boom" and not res.cancelled
    res = picker.pick_folder(env=GOOD_ENV, which=which_only("zenity"), run=Runner(rc=2))
    assert "exit 2" in res.error
    res = picker.pick_folder(env=GOOD_ENV, which=which_only("zenity"), run=Runner(rc=0, out="  \n"))
    assert res.cancelled


def test_timeout_and_oserror():
    t = Runner(exc=subprocess.TimeoutExpired("zenity", 3))
    res = picker.pick_folder(timeout=3, env=GOOD_ENV, which=which_only("zenity"), run=t)
    assert "in time" in res.error and t.calls[0][1]["timeout"] == 3
    res = picker.pick_folder(env=GOOD_ENV, which=which_only("zenity"), run=Runner(exc=OSError("nope")))
    assert "nope" in res.error


def test_environment_passes_through():
    env = dict(GOOD_ENV, WAYLAND_DISPLAY="w0")
    r = Runner(out="/x\n")
    picker.pick_folder(env=env, which=which_only("zenity"), run=r)
    passed = r.calls[0][1]["env"]
    for k in ("DISPLAY", "WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS", "XAUTHORITY"):
        assert passed[k] == env[k]


def test_bad_start_is_dropped():
    r = Runner(out="/x\n")
    picker.pick_folder("/definitely/not/here", env=GOOD_ENV, which=which_only("zenity"), run=r)
    assert not any(a.startswith("--filename=") for a in r.calls[0][0])
