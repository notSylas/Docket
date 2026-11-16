"""LegacyConverter: command shape, isolation, timeout/cleanup, availability.
Uses fake runners; no LibreOffice needed."""

from __future__ import annotations

from pathlib import Path

import pytest

from docket.infra.parsing.legacy_office import (
    LEGACY_TARGETS,
    ConversionError,
    ConversionTimeout,
    ConverterUnavailable,
    LegacyConverter,
    _run_subprocess,
)


def _arg(cmd: list[str], flag: str) -> str:
    return cmd[cmd.index(flag) + 1]


def _fake_ok(calls: list):
    def run(cmd, timeout_s):
        calls.append((cmd, timeout_s))
        fmt, outdir, src = _arg(cmd, "--convert-to"), Path(_arg(cmd, "--outdir")), Path(cmd[-1])
        (outdir / f"{src.stem}.{fmt}").write_bytes(b"converted")
        return 0
    return run


def test_targets_cover_legacy_formats() -> None:
    assert LEGACY_TARGETS == {".doc": "docx", ".rtf": "docx", ".xls": "xlsx", ".ppt": "pptx"}


def test_command_is_headless_isolated_and_cleaned_up(tmp_path: Path) -> None:
    src = tmp_path / "old.doc"
    src.write_bytes(b"x")
    calls: list = []
    conv = LegacyConverter("/fake/soffice", timeout_s=7, runner=_fake_ok(calls))
    conv._resolved, conv._resolved_done = "/fake/soffice", True
    with conv.converted(src) as out:
        assert out.name == "old.docx" and out.read_bytes() == b"converted"
        kept = out
    assert not kept.exists()  # temp copy removed
    cmd, timeout = calls[0]
    assert cmd[0] == "/fake/soffice" and timeout == 7
    assert "--headless" in cmd and _arg(cmd, "--convert-to") == "docx"
    ui = next(c for c in cmd if c.startswith("-env:UserInstallation=file://"))
    assert ".local/share" not in ui and "docket-lo-profile-" in ui  # private profile
    assert str(src) == cmd[-1]
    assert src.read_bytes() == b"x"  # original untouched


def test_unavailable_when_missing_or_disabled(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("docket.infra.parsing.legacy_office.shutil.which", lambda _n: None)
    src = tmp_path / "a.xls"
    src.write_bytes(b"x")
    conv = LegacyConverter()
    assert not conv.available()
    with pytest.raises(ConverterUnavailable):
        with conv.converted(src):
            pass
    off = LegacyConverter("/bin/true", enabled=False)
    assert not off.available() and "disabled" in off.unavailable_reason()


def test_no_output_is_a_conversion_error(tmp_path: Path) -> None:
    src = tmp_path / "a.ppt"
    src.write_bytes(b"x")
    conv = LegacyConverter(runner=lambda cmd, t: 1)
    conv._resolved, conv._resolved_done = "/fake/soffice", True
    with pytest.raises(ConversionError):
        with conv.converted(src):
            pass


def test_timeout_propagates(tmp_path: Path) -> None:
    src = tmp_path / "a.ppt"
    src.write_bytes(b"x")

    def boom(cmd, t):
        raise ConversionTimeout("timed out")

    conv = LegacyConverter(runner=boom)
    conv._resolved, conv._resolved_done = "/fake/soffice", True
    with pytest.raises(ConversionTimeout):
        with conv.converted(src):
            pass


def test_real_subprocess_timeout_kills_process_group(tmp_path: Path) -> None:
    pidfile = tmp_path / "pid"
    # A shell that spawns a child sleeper in the same group, like soffice.bin.
    cmd = ["sh", "-c", f"sleep 60 & echo $! > {pidfile}; wait"]
    with pytest.raises(ConversionTimeout):
        _run_subprocess(cmd, 0.5)
    import os, time

    child = int(pidfile.read_text())
    for _ in range(20):
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        pytest.fail("stray child process survived the timeout")


def test_real_libreoffice_smoke(tmp_path: Path) -> None:
    """Only when soffice exists: .docx -> .doc -> converted back via the
    converter, in a scratch dir (never the user's LibreOffice profile)."""
    import shutil
    import subprocess

    import docx

    exe = shutil.which("soffice")
    if exe is None:
        pytest.skip("LibreOffice not installed")
    src = tmp_path / "tiny.docx"
    d = docx.Document()
    d.add_paragraph("hello legacy walrus")
    d.save(str(src))
    legacy_dir = tmp_path / "legacy"
    legacy_dir.mkdir()
    conv = LegacyConverter(timeout_s=120)
    # Produce a real .doc with the same isolated-profile machinery.
    cmd = conv.command(exe, src, "doc", legacy_dir)
    subprocess.run(cmd, check=True, timeout=120, stdin=subprocess.DEVNULL,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    legacy = legacy_dir / "tiny.doc"
    assert legacy.is_file()
    with conv.converted(legacy) as out:
        assert out.suffix == ".docx"
        text = "\n".join(p.text for p in docx.Document(str(out)).paragraphs)
    assert "hello legacy walrus" in text
    assert not out.exists()
