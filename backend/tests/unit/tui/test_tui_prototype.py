"""Headless tests for the Screen A prototype (fake data only).

Every scenario runs on prompt_toolkit's pipe input + dummy output with a hard
timeout, so a regression fails instead of hanging. Nothing here opens a real
terminal, a database, the network, or the user's home directory.
"""

from __future__ import annotations

import asyncio

import pytest

from docket.interfaces.cli.tui import fake_data as fd
from docket.interfaces.cli.tui.headless import Harness, run
from docket.interfaces.cli.tui.textfmt import render_markdown, plain
from docket.interfaces.cli.tui.theme import asciify, detect_ascii


def scenario(fn, state="chat", cols=120, rows=40, **kw):
    async def go():
        async with Harness(state, cols, rows, **kw) as h:
            return await fn(h)

    return run(go, timeout=30)


# -- overlays: open, close, draft and focus ------------------------------------

OVERLAYS = [
    ("f1", "Commands"),
    ("f2", "Sources"),
    ("f3", "Search scope"),
    ("f4", "Answering mode"),
    ("f5", "Evidence [1] of 4"),
    ("f6", "Answer details"),
    ("f7", "Jobs"),
    ("f8", "Settings"),
]


@pytest.mark.parametrize("key,title", OVERLAYS)
def test_overlay_opens_and_restores_focus_and_draft(key, title):
    async def go(h):
        await h.type("half a que")
        assert h.focused == "composer"
        await h.press("pageup")  # move transcript position too
        scroll_before = h.ui.scroll
        await h.press(key)
        assert h.focused == "overlay"
        assert title in h.text()
        # typing goes to the overlay, never to the composer behind it
        await h.type("x")
        assert h.ui.composer.text == "half a que"
        await h.press("esc")
        assert h.focused == "composer"
        assert not h.ui.overlays
        assert h.ui.composer.text == "half a que"
        assert h.ui.scroll == scroll_before

    scenario(go)


def test_esc_closes_only_the_top_overlay():
    async def go(h):
        await h.press("f2")
        await h.press("f1")
        assert [o.name for o in h.ui.overlays] == ["sources", "palette"]
        await h.press("esc")
        assert [o.name for o in h.ui.overlays] == ["sources"]
        assert h.focused == "overlay"
        await h.press("esc")
        assert h.ui.overlays == []
        assert h.focused == "composer"

    scenario(go)


def test_slash_on_empty_composer_opens_palette_but_not_mid_text():
    async def go(h):
        await h.type("/")
        assert h.ui.overlays and h.ui.overlays[-1].name == "palette"
        await h.press("esc")
        await h.type("a/b")
        assert not h.ui.overlays
        assert h.ui.composer.text == "a/b"

    scenario(go)


def test_ctrl_c_semantics():
    async def go(h):
        # idle + empty -> exit hint, still running
        await h.press("ctrl-c")
        assert "Ctrl+D" in h.text()
        # idle + draft -> clears the draft
        await h.type("draft")
        await h.press("ctrl-c")
        assert h.ui.composer.text == ""
        # overlay open -> dismisses overlay, does not cancel indexing
        await h.press("f2")
        await h.press("ctrl-c")
        assert h.ui.overlays == []

    scenario(go)


def test_ctrl_c_does_not_cancel_indexing_when_overlay_open_but_does_when_closed():
    async def go(h):
        assert h.ui.job.state == "running"
        await h.press("ctrl-c")  # dismisses the overlay only
        assert h.ui.overlays == []
        assert h.ui.job.state == "running"
        await h.press("ctrl-c")  # operation active, no overlay -> cooperative stop
        assert h.ui.job.state == "stopping"
        await h.press("f7")
        await h.press("enter")  # open the current job
        assert "Stopping after the current operation" in h.text()

    scenario(go, state="indexing")


# -- palette -------------------------------------------------------------------


def test_palette_lists_existing_and_new_commands_and_filters():
    async def go(h):
        await h.press("f1")
        text = h.text()
        for name in ("/sources", "/add <folder>", "/status", "/scope", "/settings"):
            assert name in text
        await h.type("recon")
        text = h.text()
        assert "/reconnect" in text
        assert "/status" not in text
        assert "/clear" not in text
        await h.press("ctrl-u")
        await h.type("zzzz")
        assert "No command matches" in h.text()

    scenario(go, rows=60)


def test_palette_marks_unavailable_commands_with_reason_and_runs_available_ones():
    async def go(h):
        await h.press("f1")
        await h.type("reindex")
        assert "unavailable" in h.text()
        await h.press("enter")
        assert h.ui.overlays and h.ui.overlays[-1].name == "palette"  # stays open
        assert "not wired" in h.text()
        await h.press("ctrl-u")
        await h.type("settings")
        await h.press("enter")
        assert [o.name for o in h.ui.overlays] == ["settings"]
        assert h.focused == "overlay"

    scenario(go)


def test_typed_slash_commands_use_existing_names_aliases_and_typo_hints():
    async def go(h):
        await h.type("/ls")
        await h.press("enter")
        assert h.ui.overlays[-1].name == "sources"
        await h.press("esc")
        await h.type("/sorces")
        await h.press("enter")
        assert not h.ui.overlays
        assert "Did you mean /sources" in h.text()

    scenario(go)


# -- scope and mode ------------------------------------------------------------


def test_scope_selection_updates_header_and_keeps_transcript():
    async def go(h):
        before = len(h.ui.messages)
        await h.press("f3")
        await h.press("down")  # Finance
        await h.press("enter")
        assert h.ui.overlays == []
        assert "Finance" in h.text().splitlines()[0]
        assert h.ui.scope.kind == "source"
        assert len(h.ui.messages) == before + 1  # scope-change notice only
        assert "Scope is now Finance" in h.text()

    scenario(go)


def test_scope_picker_disables_unavailable_sources_and_searches_files():
    async def go(h):
        await h.press("f3")
        assert "Disconnected" in h.text()
        await h.type("old")
        await h.press("enter")  # disconnected: refused, stays open
        assert h.ui.overlays and h.ui.scope.kind == "all"
        await h.press("ctrl-u")
        await h.press("tab")  # to buttons -> Find a file
        await h.press("enter")
        await h.type("minutes-02")
        assert "board/minutes-02.pdf" in h.text()
        await h.press("enter")
        assert h.ui.scope.kind == "file"
        assert "board/minutes-02.pdf" in h.text().splitlines()[0]

    scenario(go)


def test_mode_selection_updates_header_and_plan_is_unavailable():
    async def go(h):
        await h.press("f4")
        await h.press("down", "down", "enter")  # Plan
        assert h.ui.mode == "auto"
        assert h.ui.overlays  # still open, explained
        assert "not implemented" in h.text()
        await h.press("up", "enter")  # Quick search
        assert h.ui.mode == "fast"
        assert "Quick search" in h.text().splitlines()[0]

    scenario(go)


# -- sources -------------------------------------------------------------------


def test_sources_panel_labels_actions_and_non_destructive_fake_actions():
    async def go(h):
        await h.press("f2")
        text = h.text()
        for label in ("Ready", "Failed", "Disconnected", "Add folder", "Refresh", "Retry", "Reconnect", "Disconnect"):
            assert label in text
        # Retry is disabled for a Ready source and explains why
        await h.press("tab", "right")  # Refresh -> Retry
        await h.press("right")
        await h.press("enter")
        assert "applies to a Failed source" in h.text()
        # Reconnect the disconnected source through the same job overlay
        await h.press("esc")  # close sources
        await h.press("f2")
        await h.press("down", "down", "down")
        await h.press("tab")
        for _ in range(3):
            await h.press("right")
        await h.press("enter")  # Reconnect
        assert h.ui.overlays[-1].name == "indexing"
        assert h.ui.job.verb == "Reconnecting"

    scenario(go)


def test_disconnect_needs_confirmation_and_esc_returns_to_parent():
    async def go(h):
        await h.press("f2")
        await h.press("tab")  # Add folder
        for _ in range(4):
            await h.press("right")  # Disconnect
        await h.press("enter")
        assert h.ui.overlays[-1].name == "confirm"
        await h.press("esc")
        assert h.ui.overlays[-1].name == "sources"
        assert h.ui.world.sources[0].status == fd.READY
        await h.press("enter")  # toggle... focus is on button row: re-activate Disconnect
        await h.press("enter")  # default is the safe choice: Keep connected
        assert h.ui.world.sources[0].status == fd.READY
        assert h.ui.overlays[-1].name == "sources"

    scenario(go)


def test_add_folder_validation_and_auto_index():
    async def go(h):
        assert h.ui.overlays[-1].name == "welcome"
        await h.press("enter")  # Add a folder
        assert h.ui.overlays[-1].name == "add"
        await h.press("enter")
        assert "Enter a folder path" in h.text()
        await h.type("'/demo/home/My Papers'")
        await h.press("enter")
        assert h.ui.overlays[-1].name == "indexing"
        assert h.ui.world.sources[-1].path == "/demo/home/My Papers"
        assert h.ui.job.active
        assert "Indexing My Papers" in h.text()

    scenario(go, state="welcome")


# -- indexing ------------------------------------------------------------------


def test_indexing_progress_stop_and_hide():
    async def go(h):
        assert "File 7 of 24" in h.text()
        await h.type("n")  # reduced motion: manual progress step
        assert h.ui.job.ticks == 22
        await h.press("tab")  # Stop indexing
        await h.press("enter")
        assert h.ui.job.state == "stopping"
        assert "Stopping after the current operation" in h.text()
        await h.type("n")
        assert h.ui.job.state == "stopped"
        assert "Stopped before finishing" in h.text()
        await h.press("esc")
        assert "Indexing Finance stopped" in h.text()

    scenario(go, state="indexing")


def test_hide_keeps_job_running_and_footer_indicator_and_blocks_sending():
    async def go(h):
        await h.press("enter")  # Hide progress
        assert h.ui.overlays == []
        assert h.ui.job.active
        assert "Indexing Finance" in h.text().splitlines()[-1]
        await h.type("what is revenue")
        await h.press("enter")
        assert h.ui.composer.text == "what is revenue"  # draft kept, nothing sent
        assert "Sending is unavailable while indexing" in h.text()

    scenario(go, state="indexing")


def test_job_runs_to_completion_with_failures_listed():
    async def go(h):
        for _ in range(100):
            if not h.ui.job.active:
                break
            h.ui.tick()
        assert h.ui.job.state == "done"
        await h.settle()
        assert h.ui.job.failed == 2
        assert "Partial" in h.text()
        assert "Failures" in h.text()

    scenario(go, state="indexing")


def test_blocked_prerequisites_keep_the_source_and_offer_retry():
    async def go(h):
        assert "not installed" in h.text()
        await h.press("enter")
        await h.type("/demo/work/reports")
        await h.press("enter")  # fills nothing: exact suggestion typed -> submit
        h.ui.tick()
        await h.settle()
        assert h.ui.job.state == "failed"
        assert "Retry indexing" in h.text()
        assert any(s.path == "/demo/work/reports" for s in h.ui.world.sources)

    scenario(go, state="welcome-blocked")


# -- chat ----------------------------------------------------------------------


def test_enter_sends_and_alt_enter_ctrl_j_insert_newlines():
    async def go(h):
        await h.type("first")
        await h.press("alt-enter")
        await h.type("second")
        await h.press("ctrl-j")
        await h.type("third")
        assert h.ui.composer.text == "first\nsecond\nthird"
        n = len(h.ui.messages)
        await h.press("enter")
        assert h.ui.composer.text == ""
        assert len(h.ui.messages) == n + 2  # user + assistant
        assert h.ui.messages[-2].text == "first\nsecond\nthird"

    scenario(go)


def test_answer_has_citations_table_sources_and_meta_line():
    async def go(h):
        await h.type("compare each region with its target")
        await h.press("enter")
        text = h.text()
        assert "Region" in text and "Revenue" in text
        assert "Sources:" in text
        assert "[1]" in text
        assert "Quick search" in text or "Auto" in text

    scenario(go, rows=60)


def test_abstention_is_not_called_a_failure():
    async def go(h):
        await h.type("what is the weather")
        await h.press("enter")
        text = h.text()
        assert "did not support an answer" in text
        assert "Failed" not in text

    scenario(go, rows=60)


def test_show_command_and_unavailable_citation_variant():
    async def go(h):
        await h.type("/show 4")
        await h.press("enter")
        text = h.text()
        assert "Evidence [4] of 4" in text
        assert "Unavailable" in text
        assert "superseded" in text
        await h.press("o")  # open original is disabled and explains
        assert "Unavailable" in h.text()
        await h.press("esc")
        await h.type("/show 9")
        await h.press("enter")
        assert "choose 1-4" in h.text()

    scenario(go, rows=60)


def test_evidence_previous_next_and_missing_location_label():
    async def go(h):
        await h.press("f5")
        assert "Sheet Summary" in h.text()
        await h.press("n", "n")
        assert "Evidence [3] of 4" in h.text()
        assert "Location not extracted" in h.text()
        await h.press("p")
        assert "Evidence [2] of 4" in h.text()
        assert "Page 4" in h.text()

    scenario(go)


def test_answer_details_never_show_a_confidence_percentage():
    async def go(h):
        await h.press("f6")
        text = h.text()
        assert "Not calibrated" in text
        assert "%" not in text.split("Answer details", 1)[1].split("Close", 1)[0]

    scenario(go)


def test_clear_resets_conversation_and_notes_history_is_kept():
    async def go(h):
        await h.type("/clear")
        await h.press("enter")
        assert [m.role for m in h.ui.messages] == ["system"]
        assert "typed-input history is kept" in h.text()

    scenario(go)


def test_scrolled_up_reader_gets_new_message_marker_not_auto_follow():
    async def go(h):
        await h.press("pageup")
        assert h.ui.scroll > 0
        h.ui.notice("a late system notice")
        await h.settle()
        assert h.ui.unread
        assert "New messages" in h.text()
        await h.press("ctrl-end")
        assert h.ui.scroll == 0
        assert "a late system notice" in h.text()

    scenario(go, rows=24, cols=80)


# -- settings ------------------------------------------------------------------


def test_settings_save_applies_theme_and_model_and_dirty_cancel_asks():
    async def go(h):
        await h.press("f8")
        assert "Embedding model" in h.text()
        await h.press("down", "down", "down")  # embedding row (read-only)
        await h.press("left")
        assert "Read-only" in h.text()
        await h.press("up", "up", "up")
        await h.press("right")  # change model
        assert "Unsaved changes" in h.text()
        await h.press("esc")
        assert h.ui.overlays[-1].name == "confirm"
        await h.press("esc")  # back to the form
        assert h.ui.overlays[-1].name == "settings"
        await h.press("tab", "enter")  # Save
        assert h.ui.model == "demo-model-7b"
        assert h.ui.overlays == []
        await h.press("f8")
        await h.press("up", "right")  # tabs -> Appearance
        await h.press("down", "right")
        await h.press("tab", "enter")
        assert h.ui.theme == "light"

    scenario(go)


def test_settings_discard_closes_without_applying():
    async def go(h):
        await h.press("f8")
        await h.press("right")
        await h.press("esc")
        await h.press("enter")  # default focus is the safe "Keep editing"? choose explicit move
        assert h.ui.overlays[-1].name == "settings"
        await h.press("esc")
        await h.press("left", "enter")  # Discard changes
        assert h.ui.overlays == []
        assert h.ui.model == fd.DEFAULT_MODEL

    scenario(go)


def test_exit_confirmation_when_work_is_active():
    async def go(h):
        await h.press("esc")  # hide indexing overlay
        await h.press("ctrl-d")
        assert h.ui.overlays[-1].name == "confirm"
        assert "Exit while work is running" in h.text()
        await h.press("enter")  # default: keep running
        assert h.ui.overlays == []
        assert h.ui.application.is_running

    scenario(go, state="indexing")


def test_ctrl_d_exits_when_idle():
    async def go(h):
        await h.send("\x04")
        return h.ui.application.is_running

    # a pure exit: the app stops, so the harness context closes cleanly
    assert scenario(go) is False


# -- layout --------------------------------------------------------------------


@pytest.mark.parametrize(
    "cols,rows,bp",
    [(120, 40, "wide"), (100, 30, "wide"), (80, 24, "medium"), (60, 20, "narrow")],
)
def test_layout_breakpoints_keep_header_composer_footer_and_fit(cols, rows, bp):
    async def go(h):
        m = h.ui.metrics()
        assert m.breakpoint == bp
        lines = h.screen_lines()
        assert len(lines) == rows
        assert "DOCKET" in lines[0]
        assert "Commands" in lines[-1] or "F1" in lines[-1]
        assert any("Ask" in ln for ln in lines)
        for ln in lines:
            assert len(ln) <= cols
        await h.press("f2")
        for ln in h.screen_lines():
            assert len(ln) <= cols
        assert "Sources" in h.text()

    scenario(go, cols=cols, rows=rows)


def test_header_drops_model_below_80_and_overlay_width_is_capped():
    async def go(h):
        assert fd.DEFAULT_MODEL in h.screen_lines()[0]
        await h.press("f2")
        widest = max(len(ln.rstrip()) for ln in h.screen_lines() if "┌" in ln)
        assert widest <= 120

    scenario(go, cols=120, rows=40)

    async def narrow(h):
        assert fd.DEFAULT_MODEL not in h.screen_lines()[0]
        await h.press("f2")
        # narrow layout puts details below the list rather than beside it
        assert not any("Handbook" in ln and "Path" in ln for ln in h.screen_lines())
        assert any("Path" in ln for ln in h.screen_lines())

    scenario(narrow, cols=70, rows=24)


def test_too_small_notice_and_reflow_keep_draft_and_selection():
    async def go(h):
        await h.type("keep this draft")
        await h.press("f2")
        await h.press("down", "down")
        sel = h.ui.overlays[-1].sel
        await h.resize(50, 12)
        assert "Terminal too small" in h.text()
        assert h.ui.composer.text == "keep this draft"
        await h.resize(100, 30)
        assert "Terminal too small" not in h.text()
        assert h.ui.overlays[-1].sel == sel
        assert h.ui.composer.text == "keep this draft"
        await h.press("esc")
        assert h.focused == "composer"

    scenario(go)


def test_rows_below_16_also_trigger_the_notice():
    async def go(h):
        await h.resize(100, 15)
        assert "Terminal too small" in h.text()

    scenario(go)


# -- ASCII fallback and appearance --------------------------------------------


def test_detect_ascii_from_env_and_encoding():
    assert detect_ascii({"DOCKET_ASCII": "1"}, "utf-8")
    assert detect_ascii({}, "ascii")
    assert detect_ascii({}, "latin-1")
    assert not detect_ascii({}, "UTF-8")
    assert not detect_ascii({"DOCKET_ASCII": "0"}, "utf8")


def test_ascii_mode_renders_only_ascii_borders_and_text():
    async def go(h):
        for key in (None, "f2", "esc", "f3", "esc", "f5", "esc", "f8", "esc"):
            if key:
                await h.press(key)
            for ln in h.screen_lines():
                assert all(ord(c) < 128 for c in ln), ln
        await h.press("f5")
        assert "+-" in h.text()

    scenario(go, ascii_mode=True)


def test_asciify_maps_known_glyphs_and_replaces_unknown():
    assert asciify("a · b — c…") == "a | b - c."
    assert asciify("中") == "?"


@pytest.mark.parametrize("theme", ["dark", "light", "terminal"])
def test_every_theme_renders_labels_not_only_colors(theme):
    async def go(h):
        await h.press("f2")
        text = h.text()
        assert "Ready" in text and "Failed" in text and "Disconnected" in text
        assert "▸" in text  # selection marker without colour

    scenario(go, theme=theme)


# -- pure helpers --------------------------------------------------------------


def test_markdown_table_falls_back_to_records_when_narrow():
    md = "| Region | Revenue | Target |\n|---|---|---|\n| North | 4.2 | 4.0 |"
    wide = [plain(r) for r in render_markdown(md, 60)]
    narrow = [plain(r) for r in render_markdown(md, 14)]
    assert any("│" in ln for ln in wide)
    assert any("Revenue: 4.2" in ln for ln in narrow)
    assert not any("│" in ln for ln in narrow)


def test_fake_content_lives_in_fake_data_only():
    # views must not embed demo records: spot-check identifying strings
    import pathlib

    root = pathlib.Path(fd.__file__).parent
    for name in ("app.py", "overlays.py", "textfmt.py", "model.py"):
        src = (root / name).read_text()
        for needle in ("q3-summary", "regional-review", "/demo/work", "demo-model-14b"):
            assert needle not in src, f"{needle} hard-coded in {name}"


# -- timed progress and CLI entry ----------------------------------------------


def test_timer_advances_indexing_and_answers_when_motion_is_enabled():
    async def go(h):
        assert h.ui.job.ticks > 21  # the timer, not a key press, moved it on
        for _ in range(200):
            if not h.ui.job.active:
                break
            await asyncio.sleep(0.05)
        assert h.ui.job.state in ("done", "stopped")
        await h.press("esc")
        n = len(h.ui.answers())
        await h.type("which policies need approval")
        await h.press("enter")
        for _ in range(200):
            await asyncio.sleep(0.05)
            if len(h.ui.answers()) > n:
                break
        assert len(h.ui.answers()) == n + 1
        await h.settle()
        assert "Hiring needs executive approval" in h.text()

    scenario(go, state="indexing", reduced_motion=False, tick_interval=0.05, rows=60)


def test_cli_tui_demo_is_hidden_and_refuses_without_a_tty():
    from typer.testing import CliRunner

    from docket.interfaces.cli.main import app

    runner = CliRunner()
    result = runner.invoke(app, ["tui-demo", "--state", "chat"])
    assert result.exit_code == 1
    assert "interactive terminal" in result.output
    bad = runner.invoke(app, ["tui-demo", "--state", "nope"])
    assert bad.exit_code == 2
    assert "tui-demo" not in runner.invoke(app, ["--help"]).output


def test_composer_and_footer_stay_visible_behind_overlays_when_there_is_room():
    async def go(h):
        for key in ("f1", "f2", "f3", "f8"):
            await h.press(key)
            lines = h.screen_lines()
            assert any(ln.startswith("┌─ Ask") for ln in lines), key
            assert "F1 / Commands" in lines[-1]
            await h.press("esc")

    scenario(go, cols=100, rows=30)
