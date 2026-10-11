"""Pasted or dragged paths: quotes, escapes, file:// URIs, ~, file vs folder,
and sentences that merely contain a path."""

from __future__ import annotations

from urllib.parse import quote

from docket.interfaces.cli.dropped import parse_dropped_path


def test_plain_and_quoted_folder(tmp_path):
    d = tmp_path / "My Docs"
    d.mkdir()
    for text in (str(d), f"'{d}'", f'"{d}"', str(d).replace(" ", "\\ "), f"  {d}\n"):
        got = parse_dropped_path(text)
        assert got is not None and got.is_dir and got.path == str(d), text


def test_file_uri_percent_encoded(tmp_path):
    d = tmp_path / "Q3 reports"
    d.mkdir()
    uri = "file://" + quote(str(d))
    assert parse_dropped_path(uri).path == str(d)
    assert parse_dropped_path("file://localhost" + quote(str(d))).path == str(d)
    assert parse_dropped_path("file://otherhost" + quote(str(d))) is None


def test_tilde(tmp_path, monkeypatch):
    (tmp_path / "docs").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    got = parse_dropped_path("~/docs")
    assert got.path == str(tmp_path / "docs") and got.is_dir


def test_file_gives_parent(tmp_path):
    f = tmp_path / "a b.pdf"
    f.write_bytes(b"%PDF-")
    got = parse_dropped_path(str(f).replace(" ", "\\ "))
    assert got.kind == "file" and not got.is_dir and got.parent == str(tmp_path)


def test_questions_are_not_paths(tmp_path):
    (tmp_path / "docs").mkdir()
    for text in (
        f"what is in {tmp_path}?",
        f"{tmp_path} is where the files are",
        "docs",
        "what is revenue",
        f"{tmp_path} {tmp_path}",
        str(tmp_path / "missing"),
        "",
        f"{tmp_path}\nand a second line",
    ):
        assert parse_dropped_path(text) is None, text


def test_relative_needs_dot_prefix(tmp_path):
    (tmp_path / "docs").mkdir()
    assert parse_dropped_path("./docs", cwd=tmp_path).path == str(tmp_path / "docs")
    assert parse_dropped_path("docs", cwd=tmp_path) is None
