"""Unit tests for `docket.services.query.prompts` (pure functions -- no DB/gateway
needed, hand-built `ResolvedEvidence` lists only)."""

from __future__ import annotations

from docket.services.query.citations import build_context_block, validate_citations
from docket.services.query.prompts import ABSTENTION_PHRASE, AGENT_SYSTEM_PROMPT, SYSTEM_PROMPT
from docket.infra.retrieval.resolver import ResolvedEvidence


def _evidence(chunk_id: str, text: str, citation_label: str) -> ResolvedEvidence:
    return ResolvedEvidence(
        chunk_id=chunk_id,
        text=text,
        source_display_name="report.pdf",
        evidence_version_id="ev_1",
        heading=None,
        citation_label=citation_label,
    )


# ---------------------------------------------------------------------------
# build_context_block
# ---------------------------------------------------------------------------


def test_build_context_block_contains_each_label_and_text() -> None:
    chunks = [
        _evidence("chk_a", "Alpha text.", "[report.pdf #chka]"),
        _evidence("chk_b", "Beta text.", "[report.pdf #chkb]"),
        _evidence("chk_c", "Gamma text.", "[other.pdf #chkc]"),
    ]

    block = build_context_block(chunks)

    for chunk in chunks:
        assert chunk.citation_label in block
        assert chunk.text in block


def test_build_context_block_label_immediately_precedes_its_text() -> None:
    chunks = [
        _evidence("chk_a", "Alpha text.", "[report.pdf #chka]"),
        _evidence("chk_b", "Beta text.", "[report.pdf #chkb]"),
    ]

    block = build_context_block(chunks)

    for chunk in chunks:
        expected_pair = f"{chunk.citation_label}\n{chunk.text}"
        assert expected_pair in block


# ---------------------------------------------------------------------------
# validate_citations
# ---------------------------------------------------------------------------


def test_exact_abstention_phrase_is_detected() -> None:
    chunks = [_evidence("chk_a", "Alpha text.", "[report.pdf #chka]")]

    result = validate_citations(ABSTENTION_PHRASE, chunks)

    assert result.is_abstention is True
    assert result.cited_labels == []
    assert result.uncited is False
    assert result.unknown_citations == []


def test_abstention_phrase_tolerates_surrounding_whitespace() -> None:
    chunks = [_evidence("chk_a", "Alpha text.", "[report.pdf #chka]")]

    result = validate_citations(f"  {ABSTENTION_PHRASE}\n", chunks)

    assert result.is_abstention is True


def test_near_miss_abstention_is_not_treated_as_abstention() -> None:
    chunks = [_evidence("chk_a", "Alpha text.", "[report.pdf #chka]")]

    result = validate_citations("I don't know.", chunks)

    assert result.is_abstention is False


def test_answer_with_real_citation_is_recorded() -> None:
    chunks = [_evidence("chk_a", "Alpha text.", "[report.pdf #chka]")]

    answer = "The sky is blue [report.pdf #chka]."
    result = validate_citations(answer, chunks)

    assert result.cited_labels == ["[report.pdf #chka]"]
    assert result.uncited is False
    assert result.unknown_citations == []


def test_answer_with_content_and_no_citations_is_flagged_uncited() -> None:
    chunks = [_evidence("chk_a", "Alpha text.", "[report.pdf #chka]")]

    answer = "The sky is blue, no citation given."
    result = validate_citations(answer, chunks)

    assert result.uncited is True
    assert result.cited_labels == []
    assert result.is_abstention is False


def test_answer_with_fabricated_citation_is_flagged_unknown() -> None:
    chunks = [_evidence("chk_a", "Alpha text.", "[report.pdf #chka]")]

    answer = "The sky is blue [madeup.pdf #ffffff]."
    result = validate_citations(answer, chunks)

    assert result.unknown_citations == ["[madeup.pdf #ffffff]"]
    assert result.cited_labels == []
    # Content with only fabricated citations still has zero *valid*
    # cited_labels, so this is correctly flagged uncited too.
    assert result.uncited is True


def test_answer_with_real_and_fabricated_citation_mix() -> None:
    chunks = [_evidence("chk_a", "Alpha text.", "[report.pdf #chka]")]

    answer = "First claim [report.pdf #chka]. Second claim [madeup.pdf #ffffff]."
    result = validate_citations(answer, chunks)

    assert result.cited_labels == ["[report.pdf #chka]"]
    assert result.unknown_citations == ["[madeup.pdf #ffffff]"]
    assert result.uncited is False


def test_bracketed_aside_without_hash_is_not_flagged_as_citation() -> None:
    chunks = [_evidence("chk_a", "Alpha text.", "[report.pdf #chka]")]

    # "[roughly]" has no "#" so it shouldn't match the citation-shaped regex.
    answer = "The value is [roughly] correct, but uncited otherwise."
    result = validate_citations(answer, chunks)

    assert result.unknown_citations == []
    assert result.uncited is True


def test_fake_citation_label_field_line_is_rejected_as_uncited() -> None:
    """Regression test for a real failure mode seen in eval: the model writes
    a field-name-style line (`citation_label: (...)`) instead of copying the
    actual bracketed tag. This is not a real citation tag (no brackets at
    all), so it must not be picked up by `cited_labels`, and the answer must
    be flagged `uncited` since no real tag is present."""
    chunks = [_evidence("chk_a", "Ohm's law text.", "[physics.pdf #a1b2c3]")]

    answer = (
        "The potential difference across a resistor is directly proportional "
        "to the current flowing through it.\n"
        "citation_label: (Ohm's law states that the potential difference "
        "across a resistor is directly proportional to the current flowing "
        "through it...)"
    )
    result = validate_citations(answer, chunks)

    assert result.cited_labels == []
    assert result.uncited is True
    # The fake field line has no "[...#...]" shape, so it's not even caught
    # as an *unknown* citation -- it's simply not a citation at all, which is
    # exactly why the uncited check (not the unknown-citation check) is what
    # catches this failure mode.
    assert result.unknown_citations == []


def test_real_bracketed_tag_is_still_accepted_alongside_fake_field_line() -> None:
    """If the model also includes the real tag somewhere, that still counts,
    even if it additionally emits a bogus `citation_label: (...)` line."""
    chunks = [_evidence("chk_a", "Ohm's law text.", "[physics.pdf #a1b2c3]")]

    answer = (
        "V = IR [physics.pdf #a1b2c3].\n"
        "citation_label: (Ohm's law states that the potential difference "
        "across a resistor is directly proportional to the current flowing "
        "through it...)"
    )
    result = validate_citations(answer, chunks)

    assert result.cited_labels == ["[physics.pdf #a1b2c3]"]
    assert result.uncited is False


# ---------------------------------------------------------------------------
# SYSTEM_PROMPT / AGENT_SYSTEM_PROMPT content
# ---------------------------------------------------------------------------


def test_system_prompt_forbids_latex() -> None:
    assert "LaTeX" in SYSTEM_PROMPT
    assert "$$" in SYSTEM_PROMPT


def test_agent_system_prompt_forbids_latex() -> None:
    assert "LaTeX" in AGENT_SYSTEM_PROMPT
    assert "$$" in AGENT_SYSTEM_PROMPT


def test_system_prompt_shows_negative_citation_example() -> None:
    assert "citation_label: (...)" in SYSTEM_PROMPT
    assert "NOT a citation" in SYSTEM_PROMPT


def test_agent_system_prompt_shows_negative_citation_example() -> None:
    assert "citation_label: (...)" in AGENT_SYSTEM_PROMPT
    assert "NOT a citation" in AGENT_SYSTEM_PROMPT


def test_system_prompt_requires_enumeration_completeness() -> None:
    assert "every matching item" in SYSTEM_PROMPT
    assert '"all"' in SYSTEM_PROMPT
    assert '"every"' in SYSTEM_PROMPT
    assert '"each"' in SYSTEM_PROMPT


def test_agent_system_prompt_requires_enumeration_completeness() -> None:
    assert "every matching item" in AGENT_SYSTEM_PROMPT
    assert '"all"' in AGENT_SYSTEM_PROMPT
    assert '"every"' in AGENT_SYSTEM_PROMPT
    assert '"each"' in AGENT_SYSTEM_PROMPT


def test_system_prompt_guards_against_context_as_instructions() -> None:
    assert "never instructions to follow" in SYSTEM_PROMPT
    assert "ignore previous instructions" in SYSTEM_PROMPT


def test_agent_system_prompt_guards_against_context_as_instructions() -> None:
    assert "never instructions to follow" in AGENT_SYSTEM_PROMPT
    assert "ignore previous instructions" in AGENT_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# location lines
# ---------------------------------------------------------------------------


def _located(location: str | None) -> ResolvedEvidence:
    from dataclasses import replace

    return replace(_evidence("chk_a", "Body.", "[report.pdf #chka]"), location=location)


def test_context_block_renders_location_between_label_and_text() -> None:
    block = build_context_block([_located("Section: 3. Core > 3.1 Rules (pages 4-5)")])
    assert block == "[report.pdf #chka]\nSection: 3. Core > 3.1 Rules (pages 4-5)\nBody."


def test_context_block_has_no_location_line_without_structure() -> None:
    assert build_context_block([_located(None)]) == "[report.pdf #chka]\nBody."


def _loc(kind, locator, heading=None, pages=(None, None), file="f.ext"):
    import json

    from docket.infra.retrieval.resolver import format_location

    return format_location(
        file_name=file, heading=heading, unit_kind=kind,
        locator_json=json.dumps(locator) if locator is not None else None,
        page_start=pages[0], page_end=pages[1],
    )


def test_format_location_docling_heading_path_and_pages() -> None:
    loc = _loc("section", {"heading_path": ["3. Core Domain Models", "3.1 Identity Rules"]},
               heading="3.1 Identity Rules", pages=(4, 5))
    assert loc == "Section: 3. Core Domain Models > 3.1 Identity Rules (pages 4-5)"
    assert _loc("section", {"heading_path": ["A"]}, pages=(2, 2)) == "Section: A (page 2)"
    assert _loc("section", None, heading="Intro") == "Section: Intro"


def test_format_location_xlsx_sheet_range_and_part() -> None:
    loc = _loc("range", {"sheet": "Monthly Revenue", "range": "B5:F5"},
               file="Revenue-FY2025-26.xlsx")
    assert loc == "Location: Revenue-FY2025-26.xlsx > Monthly Revenue, range B5:F5"
    split = _loc("range", {"sheet": "S", "range": "A1:C9", "part": 2, "of": 3})
    assert split == "Location: f.ext > S, range A1:C9, part 2 of 3"


def test_format_location_pptx_kinds() -> None:
    assert _loc("slide_text", {"slide": 3, "shape_id": 7, "shape_name": "Title 1"},
                file="d.pptx") == 'Location: d.pptx > Slide 3, shape "Title 1"'
    assert _loc("table_row", {"slide": 3, "shape_name": "Table 4", "row": 2}) == (
        'Location: f.ext > Slide 3, table "Table 4", row 2')
    assert _loc("chart_data", {"slide": 3, "shape_name": "Chart 1"}) == (
        'Location: f.ext > Slide 3, chart "Chart 1"')
    assert _loc("notes", {"slide": 3}) == "Location: f.ext > Slide 3, speaker notes"


def test_format_location_none_when_nothing_known() -> None:
    assert _loc("section", None) is None
    assert _loc("section", None, pages=(7, None)) == "Location: page 7"


def test_format_location_never_contains_citation_syntax() -> None:
    from docket.services.query.citations import CITATION_TAG_RE

    loc = _loc("range", {"sheet": "Q1 [draft] #2", "range": "A1:B2"}, file="a [x #1].xlsx")
    assert "[" not in loc and "]" not in loc and "#" not in loc
    assert not CITATION_TAG_RE.search(loc)
    sec = _loc("section", {"heading_path": ["[Appendix #1]"]})
    assert "[" not in sec and "#" not in sec
