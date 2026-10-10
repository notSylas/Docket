"""Folder-first launch (CP3): bounded scan, session classification, consent,
preview rules, launcher argv and CLI argument parsing.

Everything runs on temp trees and a temp data directory: no Ollama, no parser,
no user data, no real window.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from docket.core.db.models import IngestionJob, IngestionJobStatus
from docket.interfaces.cli import launcher
from docket.interfaces.cli import main as cli_main
from docket.interfaces.cli.context import AppContext
from docket.services.ingestion.ignore import discover
from docket.services.query.scope import ScopeKind
from docket.services.sources.manager import SourceNotFoundError
from docket.services.sources.scan import (
    BROAD_DOCUMENT_LIMIT,
    SECONDS_PER_TYPE,
    ScanResult,
    estimate_seconds,
    format_duration,
    recent_seconds_per_file,
    scan_folder,
)
from docket.services.sources.session import (
    KIND_COVERED,
    KIND_EMPTY,
    KIND_LIBRARY,
    KIND_NEW,
    broad_location_reason,
    folder_label,
    has_consent,
    record_consent,
    resolve_session,
    start_folder,
)

EXT = {".pdf", ".docx", ".xlsx", ".pptx"}


def doc(path: Path, kind: str = "pdf", size: int = 64) -> Path:
    """A tiny file whose first bytes match its extension (a 'real' document)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    head = b"%PDF-1.4\n" if kind == "pdf" else b"PK\x03\x04"
    path.write_bytes(head + b"x" * size)
    return path


def fake_doc(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("this is not a pdf")
    return path


@pytest.fixture
def ctx(tmp_path: Path) -> AppContext:
    return AppContext.for_testing(data_dir=tmp_path / "data")


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "home"
    (h / "Desktop").mkdir(parents=True)
    (h / "Downloads").mkdir()
    (h / "Documents").mkdir()
    return h


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "ForgeOPs" / "docs"
    doc(root / "a.pdf")
    doc(root / "guides" / "b.docx", "zip")
    doc(root / "guides" / "c.xlsx", "zip")
    doc(root / "reports" / "d.pptx", "zip")
    doc(root / "node_modules" / "x" / "e.pdf")
    doc(root / ".hidden" / "f.pdf")
    fake_doc(root / "fake.pdf")
    (root / "notes.txt").write_text("not a supported type")
    return root


# -- scan ------------------------------------------------------------------------


def test_scan_counts_types_subfolders_skips_and_bytes(tree):
    r = scan_folder(tree)
    assert r.documents == 4
    assert r.by_type == {".pdf": 1, ".docx": 1, ".xlsx": 1, ".pptx": 1}
    assert r.by_subfolder == {"guides": 2, "reports": 1}
    assert r.root_documents == 1
    assert r.skipped == {"ignored_folders": 2, "invalid_files": 1}
    assert r.total_bytes > 0
    assert not r.incomplete and not r.broad
    assert r.suggested_subfolders == (("guides", 2), ("reports", 1))
    assert [n for n, _c in r.subfolders] == ["guides", "reports"]


def test_scan_estimate_uses_type_constants_then_measured_rate(tree):
    r = scan_folder(tree)
    assert r.estimated_seconds == pytest.approx(
        SECONDS_PER_TYPE[".pdf"] + SECONDS_PER_TYPE[".docx"] + SECONDS_PER_TYPE[".xlsx"] + SECONDS_PER_TYPE[".pptx"]
    )
    measured = scan_folder(tree, seconds_per_file=1.5)
    assert measured.estimated_seconds == pytest.approx(6.0)
    assert estimate_seconds({".pdf": 10}) == 40.0
    assert estimate_seconds({".pdf": 10}, 0.5) == 5.0


def test_scan_estimate_comes_from_recent_jobs(ctx, tmp_path):
    src = ctx.source_manager.register_source(tmp_path)
    now = datetime.now(timezone.utc)
    with ctx.session_factory() as session:
        # 10 files in 20 s -> 2 s/file
        session.add(
            IngestionJob(
                source_id=src.id,
                status=IngestionJobStatus.SUCCEEDED,
                started_at=now - timedelta(seconds=40),
                finished_at=now - timedelta(seconds=20),
                stats_json=json.dumps({"files_processed": 10, "files_failed": 0, "chunks_written": 50}),
            )
        )
        # a catch-up that wrote nothing says nothing about parsing cost
        session.add(
            IngestionJob(
                source_id=src.id,
                status=IngestionJobStatus.SUCCEEDED,
                started_at=now - timedelta(seconds=10),
                finished_at=now,
                stats_json=json.dumps({"files_processed": 50, "files_failed": 0, "chunks_written": 0}),
            )
        )
        session.commit()
    assert recent_seconds_per_file(ctx.session_factory) == pytest.approx(2.0)


def test_scan_rate_is_none_without_history(ctx):
    assert recent_seconds_per_file(ctx.session_factory) is None


def test_scan_entry_budget_marks_incomplete_and_broad(tmp_path):
    root = tmp_path / "big"
    for i in range(10):
        doc(root / f"d{i}" / "a.pdf")
    r = scan_folder(root, max_entries=3)
    assert r.incomplete and r.broad
    assert r.documents < 10


def test_scan_time_budget_marks_incomplete(tree):
    r = scan_folder(tree, time_budget_s=-1.0)
    assert r.incomplete and r.broad


def test_scan_broad_by_document_count(tmp_path):
    root = tmp_path / "many"
    root.mkdir()
    for i in range(BROAD_DOCUMENT_LIMIT + 1):
        doc(root / f"f{i}.pdf", size=1)
    r = scan_folder(root)
    assert r.documents == BROAD_DOCUMENT_LIMIT + 1
    assert r.broad and "400" in r.broad_reason
    ok = scan_folder(tmp_path / "many", extra_rules={"exclude": ["f1*.pdf"]})
    assert ok.documents < r.documents


def test_scan_broad_by_first_level_entries(tmp_path):
    root = tmp_path / "dump"
    root.mkdir()
    for i in range(2001):
        (root / f"n{i}.txt").write_text("")
    doc(root / "one.pdf")
    r = scan_folder(root)
    assert r.documents == 1 and r.broad and "top level" in r.broad_reason


def test_scan_broad_by_well_known_location(tmp_path):
    root = tmp_path / "Desktop"
    doc(root / "a.pdf")
    r = scan_folder(root, well_known_broad=[root])
    assert r.broad and r.documents == 1


def test_scan_honours_stored_rules_and_anchors_top_level(tmp_path):
    root = tmp_path / "p"
    doc(root / "docs" / "a.pdf")
    doc(root / "src" / "docs" / "b.pdf")
    r = scan_folder(root, extra_rules={"exclude": ["/docs/"]})
    assert r.documents == 1  # only the top-level docs is excluded
    assert r.by_subfolder == {"src": 1}


def test_scan_never_walks_into_ignored_folders(tmp_path, monkeypatch):
    root = tmp_path / "p"
    doc(root / "a.pdf")
    doc(root / "node_modules" / "deep" / "x.pdf")
    import docket.services.ingestion.ignore as ignore

    def boom(*_a, **_k):
        raise AssertionError("ignored folders must not be walked by a scan")

    monkeypatch.setattr(ignore, "_count_supported", boom)
    assert scan_folder(root).skipped == {"ignored_folders": 1}


def test_format_duration():
    assert format_duration(10) == "under a minute"
    assert format_duration(120) == "about 2 minutes"
    assert format_duration(7200) == "about 2.0 hours"


# -- session classification --------------------------------------------------------


def plan_for(ctx, cwd, home, **kw):
    return resolve_session(
        cwd, ctx.source_manager, session_factory=ctx.session_factory, home=home, **kw
    )


def test_library_for_home_root_and_well_known_roots(ctx, home):
    assert plan_for(ctx, None, home).kind == KIND_LIBRARY
    assert plan_for(ctx, home, home).kind == KIND_LIBRARY
    assert plan_for(ctx, "/", home).kind == KIND_LIBRARY
    for name in ("Desktop", "Downloads", "Documents"):
        p = plan_for(ctx, home / name, home)
        assert p.kind == KIND_LIBRARY and name in p.reason
    # a folder inside Desktop is a project, not a broad place
    proj = home / "Desktop" / "proj"
    doc(proj / "a.pdf")
    assert plan_for(ctx, proj, home).kind == KIND_NEW


def test_library_for_a_non_folder(ctx, home, tmp_path):
    f = tmp_path / "file.txt"
    f.write_text("x")
    assert plan_for(ctx, f, home).kind == KIND_LIBRARY
    assert plan_for(ctx, tmp_path / "missing", home).kind == KIND_LIBRARY


def test_library_suggestions_are_ranked_by_document_count(ctx, home):
    doc(home / "Downloads" / "a.pdf")
    for i in range(3):
        doc(home / "Documents" / f"{i}.pdf")
    p = plan_for(ctx, home, home)
    assert p.kind == KIND_LIBRARY
    assert [Path(path).name for path, _n in p.suggestions] == ["Documents", "Downloads"]
    assert [n for _p, n in p.suggestions] == [3, 1]


def test_library_has_no_suggestions_once_a_source_exists(ctx, home, tmp_path):
    doc(home / "Downloads" / "a.pdf")
    other = tmp_path / "other"
    other.mkdir()
    ctx.source_manager.register_source(other)
    assert plan_for(ctx, home, home).suggestions == ()


def test_new_small_folder(ctx, home, tree):
    p = plan_for(ctx, tree, home)
    assert p.kind == KIND_NEW
    assert p.documents == 4 and not p.needs_preview
    assert p.name == "ForgeOPs/docs" == folder_label(tree)
    assert p.scan.by_subfolder == {"guides": 2, "reports": 1}


def test_new_large_folder_needs_a_preview(ctx, home, tmp_path):
    root = tmp_path / "huge"
    for i in range(BROAD_DOCUMENT_LIMIT + 1):
        doc(root / f"{i}.pdf", size=1)
    p = plan_for(ctx, root, home)
    assert p.kind == KIND_NEW and p.needs_preview


def test_empty_folder_and_only_invalid_files(ctx, home, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / "notes.txt").write_text("hi")
    p = plan_for(ctx, empty, home)
    assert p.kind == KIND_EMPTY and "no supported documents" in p.reason
    bad = tmp_path / "bad"
    fake_doc(bad / "x.pdf")
    q = plan_for(ctx, bad, home)
    assert q.kind == KIND_EMPTY and "not real documents" in q.reason


def test_covered_folder_reuses_the_source_with_a_scope(ctx, home, tmp_path):
    root = tmp_path / "proj"
    doc(root / "a.pdf")
    doc(root / "sub" / "deep" / "b.pdf")
    src = ctx.source_manager.register_source(root, display_name="proj")

    at_root = plan_for(ctx, root, home)
    assert at_root.kind == KIND_COVERED and at_root.source.id == src.id
    assert at_root.scope.kind == ScopeKind.SOURCE and at_root.scope.source_id == src.id
    assert at_root.relative_path == "" and at_root.scan is None  # nothing is scanned or re-indexed

    inside = plan_for(ctx, root / "sub" / "deep", home)
    assert inside.kind == KIND_COVERED and inside.source.id == src.id
    assert inside.scope.kind == ScopeKind.FOLDER
    assert Path(inside.scope.folder) == (root / "sub" / "deep").resolve()
    assert inside.relative_path == "sub/deep" and inside.source_name == "proj"


def test_covered_wins_over_a_scan_even_for_a_folder_without_documents(ctx, home, tmp_path):
    root = tmp_path / "proj"
    doc(root / "a.pdf")
    (root / "empty").mkdir()
    ctx.source_manager.register_source(root)
    assert plan_for(ctx, root / "empty", home).kind == KIND_COVERED


def test_symlinked_folders_resolve_before_matching(ctx, home, tmp_path):
    root = tmp_path / "proj"
    doc(root / "sub" / "a.pdf")
    ctx.source_manager.register_source(root)
    link = tmp_path / "shortcut"
    link.symlink_to(root / "sub")
    assert plan_for(ctx, link, home).kind == KIND_COVERED
    outside = tmp_path / "elsewhere"
    doc(outside / "z.pdf")
    away = tmp_path / "away-link"
    away.symlink_to(outside)
    assert plan_for(ctx, away, home).kind == KIND_NEW


def test_parent_of_an_existing_source_excludes_the_child(ctx, home, tmp_path):
    parent = tmp_path / "work"
    doc(parent / "top.pdf")
    child = parent / "client"
    doc(child / "c1.pdf")
    doc(child / "c2.pdf")
    ctx.source_manager.register_source(child)
    p = plan_for(ctx, parent, home)
    assert p.kind == KIND_NEW
    assert p.excludes == ("client/",)
    assert p.documents == 1  # the child's documents are not counted twice
    # registration stores the same excludes
    src = start_folder(ctx.source_manager, p)
    assert json.loads(src.rules_json)["exclude"] == ["client/"]


def test_parent_whose_documents_are_all_indexed_is_empty(ctx, home, tmp_path):
    parent = tmp_path / "work"
    child = parent / "client"
    doc(child / "c1.pdf")
    ctx.source_manager.register_source(child)
    p = plan_for(ctx, parent, home)
    assert p.kind == KIND_EMPTY and "already indexed" in p.reason


def test_disconnected_source_is_carried_for_reconnecting(ctx, home, tmp_path):
    root = tmp_path / "proj"
    doc(root / "a.pdf")
    src = ctx.source_manager.register_source(root)
    ctx.source_manager.deactivate_source(src.id, reason="user_disconnected")
    p = plan_for(ctx, root, home)
    assert p.kind == KIND_NEW and p.disconnected.id == src.id
    again = start_folder(ctx.source_manager, p)
    assert again.id == src.id and again.status.value == "active"
    assert len(ctx.source_manager.list_sources()) == 1


def test_broad_location_reason_is_pure_path_logic(home, tmp_path):
    assert "home" in broad_location_reason(home, home=home)
    assert "Downloads" in broad_location_reason(home / "Downloads", home=home)
    assert "top of the file system" in broad_location_reason(Path("/"), home=home)
    assert broad_location_reason(tmp_path, home=home) == ""
    assert broad_location_reason(home / "Desktop" / "proj", home=home) == ""


# -- consent -----------------------------------------------------------------------


def test_consent_roundtrip_keeps_other_keys_and_is_atomic(tmp_path):
    data = tmp_path / "data"
    assert not has_consent(data)
    (data).mkdir()
    (data / "preferences.json").write_text(json.dumps({"theme": "dark"}))
    assert not has_consent(data)
    record_consent(data)
    assert has_consent(data)
    prefs = json.loads((data / "preferences.json").read_text())
    assert prefs["theme"] == "dark" and "folder_consent_v1" in prefs
    assert [p.name for p in data.iterdir()] == ["preferences.json"]  # no temp file left


def test_consent_survives_a_corrupt_file_and_creates_the_dir(tmp_path):
    data = tmp_path / "fresh" / "data"
    record_consent(data)
    assert has_consent(data)
    (data / "preferences.json").write_text("{not json")
    assert not has_consent(data)
    record_consent(data)
    assert has_consent(data)


# -- registering with preview choices -------------------------------------------------


def test_preview_unticked_folders_are_saved_as_rules_and_honoured(ctx, home, tmp_path):
    root = tmp_path / "proj"
    doc(root / "keep" / "a.pdf")
    doc(root / "drop" / "b.pdf")
    doc(root / "src" / "drop" / "c.pdf")  # same name deeper down: stays
    doc(root / "top.pdf")
    p = plan_for(ctx, root, home)
    src = start_folder(ctx.source_manager, p, excluded_subfolders=("drop",))
    assert src.display_name == "proj"
    rules = json.loads(src.rules_json)
    assert rules["exclude"] == ["/drop/"]
    stored = ctx.source_manager.get_source(src.id)
    found = discover(root, extensions=EXT, extra_rules=stored.rules_json).files
    assert [f.relative_to(root).as_posix() for f in found] == [
        "keep/a.pdf",
        "src/drop/c.pdf",
        "top.pdf",
    ]


def test_set_rules_validates_and_clears(ctx, tmp_path):
    src = ctx.source_manager.register_source(tmp_path)
    saved = ctx.source_manager.set_rules(src.id, {"exclude": ["a/"], "ignored_files": ["x.pdf"]})
    assert json.loads(saved.rules_json) == {
        "version": 1,
        "exclude": ["a/"],
        "include": [],
        "ignored_files": ["x.pdf"],
    }
    with pytest.raises(ValueError):
        ctx.source_manager.set_rules(src.id, {"exclude": "a/"})
    with pytest.raises(ValueError):
        ctx.source_manager.set_rules(src.id, {"nonsense": []})
    with pytest.raises(ValueError):
        ctx.source_manager.set_rules(src.id, {"exclude": [1]})
    assert ctx.source_manager.get_source(src.id).rules_json == saved.rules_json  # untouched
    cleared = ctx.source_manager.set_rules(src.id, {})
    assert cleared.rules_json is None
    with pytest.raises(SourceNotFoundError):
        ctx.source_manager.set_rules("src_unknown", {})


def test_anchored_exclude_pattern_semantics():
    from docket.services.ingestion.ignore import IgnoreRules

    rules = IgnoreRules.for_root(Path("/nonexistent"), {"exclude": ["/docs/", "build"]})
    assert rules.entry_ignored("docs", "docs", True)
    assert not rules.entry_ignored("src/docs", "docs", True)
    assert rules.entry_ignored("src/build", "build", True)  # unanchored: any depth


# -- launcher and CLI ----------------------------------------------------------------------


def test_spawn_argv_carries_folder_title_and_all():
    term = ["/usr/bin/gnome-terminal"]
    docket = ["/opt/bin/docket"]
    folder = "/work/ForgeOPs"
    argv = launcher.build_spawn_argv(term, folder, env={}, docket_command=docket, folder=folder)
    assert argv[-4:] == ["ui", "--in-window", "--folder", folder]
    assert "--title=Docket - ForgeOPs" in argv
    assert f"--working-directory={folder}" in argv
    every = launcher.build_spawn_argv(term, "/tmp", env={}, docket_command=docket, all_folders=True)
    assert every[-3:] == ["ui", "--in-window", "--all"]
    assert "--title=Docket" in every
    classic = launcher.build_spawn_argv(
        term, folder, env={"DOCKET_CLASSIC": "1"}, docket_command=docket, folder=folder
    )
    assert classic[-4:] == ["chat", "--in-window", "--folder", folder]


def test_window_title_is_plain_for_broad_places(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert launcher.window_title(None) == "Docket"
    assert launcher.window_title(str(tmp_path)) == "Docket"
    assert launcher.window_title(str(tmp_path / "Desktop")) == "Docket"
    assert launcher.window_title(str(tmp_path / "proj")) == "Docket - proj"


def test_spawn_window_passes_the_folder_through(monkeypatch, tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    seen = {}
    monkeypatch.setattr(launcher, "find_terminal", lambda env: ["/usr/bin/kitty"])
    monkeypatch.setattr(launcher, "resolve_docket_command", lambda: ["/opt/bin/docket"])

    def fake_popen(argv, **kw):
        seen["argv"], seen["cwd"] = argv, kw.get("cwd")
        return SimpleNamespace()

    monkeypatch.setattr(launcher.subprocess, "Popen", fake_popen)
    env = {"DISPLAY": ":0", "TERM": "xterm"}
    assert launcher.spawn_window(env, "linux", folder=str(proj)) is True
    assert seen["argv"][-2:] == ["--folder", str(proj)]
    assert seen["cwd"] == str(proj)
    assert launcher.spawn_window(env, "linux", all_folders=True) is True
    assert seen["argv"][-1] == "--all"


def _fake_tty(monkeypatch):
    tty = SimpleNamespace(isatty=lambda: True)
    monkeypatch.setattr(cli_main, "sys", SimpleNamespace(stdin=tty, stdout=tty))


@pytest.fixture
def spawned(monkeypatch):
    calls: list[dict] = []
    _fake_tty(monkeypatch)
    monkeypatch.setattr(
        cli_main.launcher, "spawn_window", lambda **kw: calls.append(kw) or True
    )
    monkeypatch.setattr(cli_main, "_run_interactive", lambda *a: calls.append({"inplace": a}))
    return calls


def test_bare_docket_opens_the_current_folder(spawned, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert CliRunner().invoke(cli_main.app, []).exit_code == 0
    assert spawned == [{"folder": str(tmp_path.resolve()), "all_folders": False}]


def test_docket_path_argument(spawned, tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    assert CliRunner().invoke(cli_main.app, [str(proj)]).exit_code == 0
    assert spawned == [{"folder": str(proj.resolve()), "all_folders": False}]


def test_docket_all_forces_the_library(spawned, tmp_path):
    assert CliRunner().invoke(cli_main.app, ["--all"]).exit_code == 0
    assert spawned == [{"folder": None, "all_folders": True}]


def test_docket_bad_path_is_a_plain_error(spawned, tmp_path):
    result = CliRunner().invoke(cli_main.app, [str(tmp_path / "nope")])
    assert result.exit_code == 2
    assert "not a folder" in result.output
    assert spawned == []


def test_subcommands_are_not_mistaken_for_paths(spawned, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    r = CliRunner().invoke(cli_main.app, ["chat"])
    assert r.exit_code == 0
    assert spawned == [{"inplace": (tmp_path.resolve(),)}]
    r = CliRunner().invoke(cli_main.app, ["--version"])
    assert r.exit_code == 0 and "docket" in r.output


def test_chat_takes_a_folder_and_all(spawned, tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    CliRunner().invoke(cli_main.app, ["chat", str(proj)])
    CliRunner().invoke(cli_main.app, ["chat", "--all"])
    assert spawned == [{"inplace": (proj.resolve(),)}, {"inplace": (None,)}]


def test_launch_command_opens_the_library(spawned):
    CliRunner().invoke(cli_main.app, ["launch"])
    assert spawned == [{"all_folders": True}]


def test_no_window_env_runs_in_place_with_the_folder(monkeypatch, tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    _fake_tty(monkeypatch)
    monkeypatch.setenv("DOCKET_NO_WINDOW", "1")
    ran = []
    monkeypatch.setattr(cli_main, "_run_interactive", lambda *a: ran.append(a))
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda *a, **k: pytest.fail("spawned"))
    CliRunner().invoke(cli_main.app, [str(proj)])
    assert ran == [(proj.resolve(),)]


def test_ui_command_accepts_folder_and_all(monkeypatch, tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    _fake_tty(monkeypatch)
    seen = []
    import docket.interfaces.cli.tui as tui

    monkeypatch.setattr(tui, "run_ui", lambda ctx, **kw: seen.append(kw["folder"]))
    monkeypatch.setattr(cli_main, "build_context", lambda: object())
    CliRunner().invoke(cli_main.app, ["ui", "--folder", str(proj)])
    CliRunner().invoke(cli_main.app, ["ui", "--all"])
    assert seen == [proj.resolve(), None]
    assert os.environ.get("DOCKET_IN_WINDOW") != "1"
