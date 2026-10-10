"""Discovery ignore rules (hidden dirs, virtualenvs, site-packages, ...,
`.docketignore`, the settings escape hatch) -- evaluated relative to the
registered root."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import _write_xlsx
from docket.core.config import Settings
from docket.services.ingestion.ignore import (
    IgnoreRules,
    discover,
    is_ignored_dir_name,
    is_ignored_file_name,
    skipped_summary,
)
from docket.services.ingestion.pipeline import IngestionPipeline

EXT = {".pdf", ".docx", ".txt"}


def _touch(root: Path, rel: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x")
    return p


def _names(report, root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in report.files)


@pytest.mark.parametrize(
    "name",
    [
        ".git", ".venv", ".venv-docket", ".hidden", ".tox", "node_modules", "__pycache__",
        "site-packages", "dist-packages", "venv", "venv3", "venv-x", "my-venv", "proj_venv",
        "foo.egg-info", "__MACOSX", "$RECYCLE.BIN", "Node_Modules",
    ],
)
def test_ignored_dir_names(name):
    assert is_ignored_dir_name(name)


@pytest.mark.parametrize(
    "name",
    ["tests", "fixtures", "docs", "build", "dist", "env", "venvironment", "src", "Reports 2024"],
)
def test_not_ignored_dir_names(name):
    assert not is_ignored_dir_name(name)


def test_ignored_file_names():
    assert is_ignored_file_name(".hidden.pdf")
    assert is_ignored_file_name("~$Budget.docx")
    assert not is_ignored_file_name("budget.docx")


def test_discover_prunes_directories_and_files(tmp_path):
    _touch(tmp_path, "a.pdf")
    _touch(tmp_path, "sub/b.docx")
    _touch(tmp_path, "tests/fixtures/c.pdf")  # NOT ignored
    _touch(tmp_path, "docs/d.txt")  # NOT ignored
    _touch(tmp_path, "env/e.txt")  # NOT ignored
    _touch(tmp_path, ".venv/lib/python3/site-packages/docx/templates/default.docx")
    _touch(tmp_path, "proj/venv/x.pdf")
    _touch(tmp_path, "proj/node_modules/pkg/readme.txt")
    _touch(tmp_path, ".git/notes.txt")
    _touch(tmp_path, "~$lock.docx")
    _touch(tmp_path, ".hidden.pdf")
    _touch(tmp_path, "sub/__pycache__/z.txt")
    _touch(tmp_path, "pkg.egg-info/PKG.txt")
    _touch(tmp_path, "ignored.bin")  # unsupported ext: not counted at all

    report = discover(tmp_path, extensions=EXT)

    assert _names(report, tmp_path) == [
        "a.pdf", "docs/d.txt", "env/e.txt", "sub/b.docx", "tests/fixtures/c.pdf",
    ]
    # 8 pruned/ignored supported files: default.docx, venv x.pdf, readme.txt,
    # .git notes, ~$lock, .hidden.pdf, z.txt, PKG.txt
    assert report.files_skipped == 8
    # .venv, proj/venv, proj/node_modules, .git, sub/__pycache__, pkg.egg-info
    assert report.dirs_skipped == 6


def test_dirs_skipped_count_is_exact(tmp_path):
    _touch(tmp_path, ".venv/a.pdf")
    _touch(tmp_path, "x/venv/b.pdf")
    _touch(tmp_path, "keep.pdf")
    report = discover(tmp_path, extensions=EXT)
    assert report.dirs_skipped == 2
    assert report.files_skipped == 2
    assert _names(report, tmp_path) == ["keep.pdf"]


def test_root_inside_hidden_and_venv_path_still_works(tmp_path):
    root = tmp_path / ".cache" / "venv" / "site-packages" / "myproject"
    _touch(root, "readme.txt")
    _touch(root, "sub/doc.pdf")
    _touch(root, ".venv/lib.pdf")
    report = discover(root, extensions=EXT)
    assert _names(report, root) == ["readme.txt", "sub/doc.pdf"]
    assert report.files_skipped == 1


def test_disabled_walks_everything(tmp_path):
    _touch(tmp_path, ".venv/a.pdf")
    _touch(tmp_path, "~$b.docx")
    _touch(tmp_path, "keep.pdf")
    (tmp_path / ".docketignore").write_text("keep.pdf\n")
    report = discover(tmp_path, extensions=EXT, enabled=False)
    assert _names(report, tmp_path) == [".venv/a.pdf", "keep.pdf", "~$b.docx"]
    assert report.dirs_skipped == 0 and report.files_skipped == 0


def test_docketignore_patterns(tmp_path):
    (tmp_path / ".docketignore").write_text(
        "# comment\n\narchive/\n*.txt\nreports/drafts\nscratch.pdf\n"
    )
    _touch(tmp_path, "keep.pdf")
    _touch(tmp_path, "archive/old.pdf")  # name/
    _touch(tmp_path, "deep/archive/old2.pdf")  # name/ matches at any depth
    _touch(tmp_path, "notes.txt")  # *.ext
    _touch(tmp_path, "deep/more.txt")
    _touch(tmp_path, "reports/drafts/d.pdf")  # path/segment
    _touch(tmp_path, "reports/final.pdf")
    _touch(tmp_path, "other/drafts/e.pdf")  # path/segment is anchored
    _touch(tmp_path, "sub/scratch.pdf")  # bare name matches any depth
    report = discover(tmp_path, extensions=EXT)
    assert _names(report, tmp_path) == ["keep.pdf", "other/drafts/e.pdf", "reports/final.pdf"]


def test_docketignore_dir_only_pattern_does_not_hit_a_file_of_that_name(tmp_path):
    (tmp_path / ".docketignore").write_text("data/\n")
    _touch(tmp_path, "data")  # a FILE named data (no ext, unsupported anyway)
    _touch(tmp_path, "x/data.pdf")
    report = discover(tmp_path, extensions=EXT)
    assert _names(report, tmp_path) == ["x/data.pdf"]


def test_path_ignored_checks_ancestors(tmp_path):
    rules = IgnoreRules.for_root(tmp_path)
    assert rules.path_ignored(Path(".venv/lib/site-packages/x.pdf"))
    assert rules.path_ignored(Path("a/b/~$c.docx"))
    assert rules.path_ignored(Path("a/node_modules/x/y.txt"))
    assert not rules.path_ignored(Path("tests/fixtures/x.pdf"))
    assert not rules.path_ignored(Path("report.pdf"))


def test_skipped_summary():
    assert skipped_summary(0) is None
    assert skipped_summary(1) == "Skipped 1 file in ignored folders (virtualenvs, site-packages, hidden)"
    assert skipped_summary(23).startswith("Skipped 23 files in ignored folders")


def test_setting_defaults_and_env(monkeypatch):
    assert Settings().ingest_ignore_enabled is True
    monkeypatch.setenv("DOCKET_INGEST_IGNORE_ENABLED", "false")
    assert Settings().ingest_ignore_enabled is False


def _bare_pipeline(enabled: bool) -> IngestionPipeline:
    pipeline = object.__new__(IngestionPipeline)
    pipeline._settings = SimpleNamespace(ingest_ignore_enabled=enabled)
    return pipeline


def test_pipeline_discover_files_honours_rules_and_setting(tmp_path):
    _touch(tmp_path, "a.pdf")
    _touch(tmp_path, ".venv/b.pdf")
    _touch(tmp_path, "old.xls")  # unsupported-but-discoverable stays visible
    on = _bare_pipeline(True)._discover_files(tmp_path)
    assert [p.name for p in on] == ["a.pdf", "old.xls"]
    off = _bare_pipeline(False)._discover_files(tmp_path)
    assert sorted(p.name for p in off) == ["a.pdf", "b.pdf", "old.xls"]


def test_pipeline_run_reports_skipped_counts(env):
    _write_xlsx(env.folder / "book.xlsx", "S", ["n", "q"], [["a", 1]])
    _touch(env.folder, ".venv/lib/site-packages/junk.xlsx")
    _touch(env.folder, "node_modules/x/also.xlsx")
    result = env.pipeline.run_ingestion_for_source(env.source.id)
    assert result.files_processed == 1
    assert result.dirs_skipped == 2
    assert result.files_skipped == 2
    assert [r.path.name for r in result.file_results] == ["book.xlsx"]
