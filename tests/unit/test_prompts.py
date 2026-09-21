"""Unit tests for `attest.query.prompts` (pure functions -- no DB/gateway
needed, hand-built `ResolvedEvidence` lists only)."""

from __future__ import annotations

from attest.query.prompts import (
    ABSTENTION_PHRASE,
    build_context_block,
    validate_citations,
)
from attest.retrieval.resolver import ResolvedEvidence


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
