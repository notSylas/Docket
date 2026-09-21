"""Tests for attest.parsing.normalize.unescape_markdown."""

from __future__ import annotations

from attest.parsing.normalize import unescape_markdown


def test_unescapes_snake_case_identifier() -> None:
    # The exact finding from spike/RESULTS.md's Tier 2 section.
    assert unescape_markdown("evidence\\_version\\_id") == "evidence_version_id"


def test_unescapes_battery_of_commonmark_special_chars() -> None:
    cases = {
        "\\*bold\\*": "*bold*",
        "\\[link\\]\\(url\\)": "[link](url)",
        "\\_italic\\_": "_italic_",
        "\\# not a heading": "# not a heading",
        "\\> not a quote": "> not a quote",
        "1\\. not a list item": "1. not a list item",
        "\\!\\[alt\\]\\(img.png\\)": "![alt](img.png)",
        "\\{braces\\}": "{braces}",
        "a \\+ b \\- c": "a + b - c",
        "back\\`tick\\`": "back`tick`",
        "double\\\\backslash": "double\\backslash",
    }
    for escaped, expected in cases.items():
        assert unescape_markdown(escaped) == expected


def test_leaves_unescaped_text_unchanged() -> None:
    text = "authorized_sources and evidence_version_id with no backslashes"
    assert unescape_markdown(text) == text


def test_code_fences_are_not_escaped_by_docling_so_unescape_is_a_non_issue() -> None:
    """Empirical finding (Docling 2.129.0, verified by round-tripping small
    markdown docs through DocumentConverter + export_to_markdown): Docling
    escapes underscores/special chars in prose but NOT inside fenced code
    blocks or inline code spans. E.g. prose ``under_score`` exports as
    ``under\\_score``, but the same text inside ``` ```...``` ``` or
    `` `...` `` is left untouched.

    Consequence: unescape_markdown never needs fence-aware logic to avoid
    "un-escaping" something Docling didn't escape in the first place --
    there is nothing to reverse inside code regions. This test locks in
    that assumption: running unescape_markdown over text that already
    looks like a real Docling export (prose escaped, fence untouched)
    leaves the fence's underscores alone (they were never escaped, so
    there's no backslash for the regex to match) while still fixing prose.
    """
    markdown = (
        "Prose with under\\_score words.\n\n"
        "```\n"
        "plain fence with under_score and file_name.py\n"
        "```\n\n"
        "`inline_code_span` also here.\n"
    )
    result = unescape_markdown(markdown)
    assert "under_score words" in result  # prose fixed
    assert "under_score and file_name.py" in result  # fence already fine, untouched
    assert "`inline_code_span`" in result  # inline code span already fine, untouched
    assert "\\_" not in result


def test_idempotent() -> None:
    text = "evidence\\_version\\_id and \\*bold\\* and \\[a\\]\\(b\\)"
    once = unescape_markdown(text)
    twice = unescape_markdown(once)
    assert once == twice
