"""Polish-pass tests for the Screen A prototype (fake data only, headless, hard timeouts)."""

from __future__ import annotations

import asyncio
import re

import pytest

from docket.interfaces.cli.tui import fake_data as fd
from docket.interfaces.cli.tui import overlays as ov
from docket.interfaces.cli.tui.headless import Harness, run
from docket.interfaces.cli.tui.textfmt import (
    middle_ellipsis,
    plain,
    render_grid,
    render_markdown,
    unique_path_labels,
    with_result_glyph,
)

SIZES = [(120, 40), (100, 30), (80, 24), (60, 20)]
TABLE = "| Region | Revenue | Target | Result |\n|---|---|---|---|\n| North | 4.2 | 4.0 | Met |\n| South | 13.1 | 3.4 | Missed |"


def scenario(fn, state="chat", cols=120, rows=40, **kw):
    async def go():
        async with Harness(state, cols, rows, **kw) as h:
            return await fn(h)

    return run(go, timeout=30)


def box_geometry(lines: list[str], title: str, cols: int) -> tuple[int, int, int]:
    """(left margin, width, right margin) of the overlay whose top border holds `title`."""
    for ln in lines:
        m = re.search(r"[┌+][─-] " + re.escape(title), ln)
        if m and not ln.lstrip().startswith("┌─ Ask"):
            left = m.start()
            end = max(ln.rfind("┐"), ln.rfind("+"))
            return left, end - left + 1, cols - end - 1
    raise AssertionError(f"no overlay titled {title!r} in\n" + "\n".join(lines))


# -- tables, glyphs -----------------------------------------------------------


def test_table_numbers_are_right_aligned_with_header():
    rows = [plain(r) for r in render_markdown(TABLE, 60)]
    # every cell of the Revenue column ends at the same cell
    ends = {ln.index("│") for ln in rows if "│" in ln}
    assert len(ends) == 1
    body = [ln for ln in rows if ln.startswith(("North", "South"))]
    rev = [ln.split("│")[1] for ln in body]
    assert rev[0].endswith(" 4.2 ") and rev[1].endswith("13.1 ")
    assert len({len(c) for c in rev}) == 1
    header = [ln for ln in rows if ln.startswith("Region")][0].split("│")[1]
    assert header.strip() == "Revenue" and header.startswith(" ") and len(header) == len(rev[0])
    # text columns stay left-aligned
    assert body[0].startswith("North ")


def test_result_words_get_glyph_and_keep_text_with_ascii_fallback():
    uni = [plain(r) for r in render_markdown(TABLE, 60)]
    assert any("✓ Met" in ln for ln in uni) and any("✗ Missed" in ln for ln in uni)
    asc = [plain(r) for r in render_markdown(TABLE, 60, ascii_mode=True)]
    assert any("OK Met" in ln for ln in asc) and any("X Missed" in ln for ln in asc)
    assert all(ord(c) < 128 for ln in asc for c in ln.replace("│", "|").replace("─", "-").replace("┼", "+"))
    assert with_result_glyph("Met")[0] == "✓ Met"
    assert with_result_glyph("Other") == ("Other", "")


def test_chat_table_shows_glyphs_and_ascii_chat_is_pure_ascii():
    async def go(h):
        text = h.text()
        assert "✓ Met" in text and "✗ Missed" in text

    scenario(go, rows=50)

    async def ascii_go(h):
        text = h.text()
        assert "OK Met" in text and "X Missed" in text
        for ln in text.split("\n"):
            assert all(ord(c) < 128 for c in ln), ln

    scenario(ascii_go, rows=50, ascii_mode=True)


# -- greeting, warnings, shortcuts, duplicates ---------------------------------


@pytest.mark.parametrize("q", ["Hey", "hello!", "Hi there", "thanks"])
def test_greeting_reply_has_no_citations_or_sourced_claims(q):
    a = fd.answer_for(q, "All ready sources", "Quick search")
    assert a.citations == ()
    assert not re.search(r"\[\d+\]", a.text)
    assert "/help" in a.text

    async def go(h):
        await h.type(q)
        await h.press("enter")
        last = h.ui.answers()[-1].answer
        assert last.citations == ()
        assert "Sources:" not in h.text().split("You")[-1]
        assert "/help" in h.text()

    scenario(go, rows=60)


def test_real_question_is_not_mistaken_for_a_greeting():
    assert not fd.is_greeting("hello what is the revenue")
    assert fd.answer_for("which policies need approval", "s", "m").citations


def test_blank_line_separates_the_warning_from_the_sources_list():
    async def go(h):
        lines = h.screen_lines()
        i = next(n for n, ln in enumerate(lines) if "Warning:" in ln)
        assert lines[i - 1].strip() == ""
        assert lines[i - 2].strip() != ""  # the last source line

    scenario(go, rows=60)


def test_number_shortcuts_are_shown_and_work():
    async def go(h):
        text = h.text()
        assert "[Ctrl+E]" in text and "/show 1" in text
        assert "Ctrl+E evidence" in h.screen_lines()[-1]
        await h.press("\x05")  # Ctrl+E
        assert h.ui.overlays and h.ui.overlays[-1].name == "evidence"

    scenario(go, rows=50)


def test_duplicate_file_names_show_the_shortest_unique_parent_path():
    assert unique_path_labels(["policies/handbook.pdf", "archive/2023/handbook.pdf", "a/b.pdf"]) == [
        "policies/handbook.pdf",
        "2023/handbook.pdf",
        "b.pdf",
    ]
    assert unique_path_labels(["x/a/h.pdf", "y/a/h.pdf"]) == ["x/a/h.pdf", "y/a/h.pdf"]
    assert unique_path_labels(["only.pdf"]) == ["only.pdf"]

    async def go(h):
        text = h.text()
        assert "[5] policies/handbook.pdf" in text
        assert "[6] 2023/handbook.pdf" in text
        assert "[2] regional-review.pdf" in text  # unique names stay bare

    scenario(go, rows=60)


def test_fake_data_really_contains_two_files_with_the_same_name():
    cits = fd.initial_answer().citations
    names = [c.source_name for c in cits]
    assert any(names.count(n) == 2 for n in names)
    for c in cits:
        assert c.rel_path.rsplit("/", 1)[-1] == c.source_name


def test_selected_answer_marker_is_explained_and_enter_esc_work():
    async def go(h):
        await h.type("compare each region with its target")
        await h.press("enter")
        assert h.ui.marker_visible()
        assert "› Docket" in h.text()
        assert "Enter opens evidence, Esc clears" in h.text()
        await h.press("enter")  # empty composer + marker -> evidence
        assert h.ui.overlays and h.ui.overlays[-1].name == "evidence"
        await h.press("esc")
        assert not h.ui.overlays
        await h.press("esc")
        await asyncio.sleep(0.6)  # bare Esc waits out the key timeout
        await h.settle()
        assert not h.ui.marker_visible()
        assert "› Docket" not in h.text()

    scenario(go, rows=70)


# -- evidence ------------------------------------------------------------------


def test_spreadsheet_location_matches_the_table_and_cited_cells_are_inside():
    for c in fd.initial_answer().citations:
        if not c.table:
            continue
        rng = fd.table_range(c.table_origin, c.table)
        assert c.location and c.location.endswith(rng)
        assert rng == "B7:D10"
        assert len(c.table) == 4  # header + three rows
        inside = {fd.cell_address(c.table_origin, r, k) for r in range(len(c.table)) for k in range(len(c.table[0]))}
        assert set(c.cited) <= inside


def test_evidence_renders_an_aligned_table_with_addresses_and_cited_cells():
    async def go(h):
        await h.press("f5")
        lines = h.screen_lines()
        text = h.text()
        assert "Sheet Summary · B7:D10" in text
        assert "Supports" in text and "revenue growing in most regions" in text
        assert "Indexed   10 Oct 2026 10:42 · version 3 of 3 (current)" in text
        for n in ("8", "9", "10"):
            assert any(re.match(rf"^\s*│\s+{n}\s+(North|South|West)", ln) for ln in lines), n
        row = next(ln for ln in lines if "North" in ln and "│" in ln[3:])
        assert "[4.2]" in row
        cell_ends = [ln.index("]") for ln in lines if re.search(r"\[\d\.\d\]", ln)]
        assert len(set(cell_ends)) == 1  # cited numbers share a right edge
        assert "stored version" not in text
        # the old inconsistent range is gone
        assert "B8:F8" not in text

    scenario(go, rows=40)


def test_prose_passage_keeps_breaks_verbatim():
    async def go(h):
        await h.press("f5", "n")
        lines = h.screen_lines()
        assert any("Page 4 · Section 2.1" in ln for ln in lines)
        quote = [ln for ln in lines if "│ " in ln and ln.count("│") >= 3]
        assert quote, "quoted passage rows"

    scenario(go, rows=50)


def test_previous_next_position_text_is_consistent():
    async def go(h):
        await h.press("f5")
        assert "1 of 6" in h.text()
        await h.press("n")
        assert "2 of 6" in h.text() and "Evidence [2] of 6" in h.text()
        await h.press("n", "n", "n", "n")
        assert "6 of 6" in h.text()

    scenario(go, rows=50)


def test_evidence_has_a_single_key_legend_line():
    async def go(h):
        await h.press("f5")
        text = h.text()
        assert text.count("Esc close") == 1
        assert "P previous" not in text and "N next" not in text

    scenario(go, rows=40)


# -- buttons ------------------------------------------------------------------


@pytest.mark.parametrize("key", ["f1", "f2", "f3", "f4", "f5", "f6", "f7", "f8"])
def test_buttons_share_one_shape_and_focus_marker(key):
    async def go(h):
        await h.press(key)
        for _ in range(3):
            text = h.text()
            assert ">(" not in text and "▸(" not in text and not re.search(r"\( [A-Z][a-z]+ \)", text)
            await h.press("tab")
        lines = [ln for ln in h.screen_lines() if "[ " in ln and " ]" in ln]
        assert lines, "button row"
        for ln in lines:
            for m in re.finditer(r"(.)\[ ([^\]]+) \]", ln):
                assert m.group(1) in " ▸│", ln

    scenario(go, cols=100, rows=34)


def test_focused_button_gets_arrow_marker_and_disabled_keeps_shape():
    async def go(h):
        await h.press("f5")
        ev = h.ui.overlays[-1]
        # Previous is disabled on citation 1 but still drawn as [ Previous ]
        assert "[ Previous ]" in h.text()
        assert "▸[ Previous ]" in h.text()  # first stop, even though disabled
        await h.press("right")
        assert "▸[ Next ]" in h.text()
        assert "▸[ Previous ]" not in h.text()
        assert sum(ln.count("▸[") for ln in h.screen_lines()) == 1
        assert ev.zone == "buttons"

    scenario(go, rows=40)


# -- answer details ------------------------------------------------------------


def test_answer_details_have_the_requested_rows_in_two_columns():
    async def go(h):
        await h.press("f6")
        lines = h.screen_lines()
        text = h.text()
        for label in ("Status", "Mode", "Model", "Scope", "Elapsed", "Passages used", "Computation", "Citations", "Confidence"):
            assert re.search(rf"│\s+{label}\s{{2,}}\S", text), label
        assert "Not verified (prototype)" in text
        assert "5 passages from 5 files, 14 searched" in text
        assert "Computation    Not used" in text
        assert "syntax valid; meaning not machine-verified" in text
        assert "Confidence     Not calibrated" in text
        starts = {re.search(r"│\s+(Status|Model|Scope|Citations)\s+(\S)", ln).start(2) for ln in lines if re.search(r"│\s+(Status|Model|Scope|Citations)\s+\S", ln)}
        assert len(starts) == 1  # one value column

    scenario(go)


def test_greeting_details_say_nothing_was_searched():
    async def go(h):
        await h.type("Hey")
        await h.press("enter")
        await h.press("f6")
        text = h.text()
        assert "None (no search was made)" in text
        assert "Citations      None" in text

    scenario(go, rows=50)


# -- all overlays: width rule, centring, backdrop ------------------------------


@pytest.mark.parametrize("cols,rows", SIZES)
def test_overlay_width_rule_and_centering(cols, rows):
    expected = min(100, cols - 2) if cols >= 80 else cols
    assert ov.overlay_width(cols) == expected

    async def go(h):
        for key, title in (("f1", "Commands"), ("f2", "Sources"), ("f3", "Search scope"), ("f5", "Evidence"), ("f6", "Answer details"), ("f7", "Jobs"), ("f8", "Settings")):
            await h.press(key)
            left, width, right = box_geometry(h.screen_lines(), title, cols)
            assert width == expected, (key, width, expected)
            assert abs(left - right) <= 1, (key, left, right)
            if cols < 80:
                assert left == 0 and right == 0
            await h.press("esc")

    scenario(go, cols=cols, rows=rows)


@pytest.mark.parametrize("cols,rows", [(120, 40), (100, 30)])
def test_backdrop_beside_and_around_the_modal_is_clean(cols, rows):
    async def go(h):
        m = h.ui.metrics()
        for key, title in (("f2", "Sources"), ("f5", "Evidence"), ("f6", "Answer details"), ("f8", "Settings")):
            await h.press(key)
            lines = h.screen_lines()
            left, width, _right = box_geometry(lines, title, cols)
            area = lines[1 + m.notice_rows : 1 + m.notice_rows + m.transcript_h]
            for ln in area:
                ln = ln.ljust(cols)
                assert ln[:left].strip() == "", (key, ln)
                assert ln[left + width :].strip() == "", (key, ln)
            await h.press("esc")

    scenario(go, cols=cols, rows=rows)


def test_transcript_returns_after_the_overlay_closes():
    async def go(h):
        await h.press("f2")
        assert "Docket" not in "\n".join(h.screen_lines()[1:8]).replace("DOCKET", "")
        await h.press("esc")
        assert "Key points" in h.text() or "Sources:" in h.text()

    scenario(go, rows=60)


# -- paths, dates, tables in other overlays --------------------------------------


def test_middle_ellipsis_keeps_both_ends_within_width():
    p = "/demo/home/Documents/Tax papers/2025/return-summary-final.pdf"
    out = middle_ellipsis(p, 30)
    assert len(out) == 30 and out.startswith("/demo/") and out.endswith(".pdf") and "…" in out
    assert middle_ellipsis("short.pdf", 30) == "short.pdf"


def test_sources_panel_uses_labelled_rows_consistent_dates_and_full_path_in_details():
    async def go(h):
        await h.press("f2")
        text = h.text()
        assert re.search(r"Path\s+/demo/work/finance", text)
        assert re.search(r"Last indexed\s+10 Oct 2026 10:42", text)
        assert "demo time" not in text

    scenario(go, cols=100, rows=34)


def test_jobs_is_a_table_with_header_and_consistent_dates():
    async def go(h):
        await h.press("f7")
        text = h.text()
        assert re.search(r"Source\s+Result\s+When\s+Summary", text)
        assert "10 Oct 2026 10:42" in text and "3 Oct 2026 14:05" in text
        assert "demo time" not in text
        cols = {ln.index("Partial") for ln in h.screen_lines() if "Partial" in ln}
        assert len(cols) == 1

    scenario(go, cols=100, rows=34)


def test_indexing_uses_labelled_rows():
    async def go(h):
        text = h.text()
        assert re.search(r"File\s+7 of 24", text)
        assert re.search(r"Stage\s+\S", text) and re.search(r"Elapsed\s+\d\d:\d\d", text)

    scenario(go, state="indexing", cols=100, rows=34)


def test_render_grid_labels_follow_origin():
    rows = [["A", "B"], ["1", "22"]]
    out = [plain(r) for r in render_grid(rows, 40, first_row=7, first_col="C", cited=frozenset({(1, 1)}))]
    assert out[0].split() == ["C", "D"]
    assert out[1].lstrip().startswith("7")
    assert out[3].lstrip().startswith("8") and "[22]" in out[3]
