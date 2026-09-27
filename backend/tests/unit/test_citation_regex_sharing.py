"""Confirms the citation-tag-shaped regex, now defined once in
`docket.query.citations`, still behaves identically at its three call
sites: `query.citations.validate_citations` (via `CITATION_TAG_RE`),
`eval.scoring.strip_citations` (imports `CITATION_TAG_RE`), and
`query.latex.normalize_latex` (builds its own capture-group-wrapped
version from `CITATION_TAG_PATTERN`, for `re.split`)."""

from __future__ import annotations

from docket.eval.scoring import _CITATION_TAG_RE, strip_citations
from docket.services.query.citations import CITATION_TAG_PATTERN, CITATION_TAG_RE
from docket.services.query.latex import _CITATION_RE, normalize_latex

REAL_TAGS = ["[report.pdf #a1b2c3d4e5f6]", "[other doc.docx #chunk_9]"]
PROSE_WITH_TAGS = f"The answer is 42 {REAL_TAGS[0]}, also see {REAL_TAGS[1]}."
NON_CITATION_BRACKETS = "roughly [1] percent, also [approximately]"


def test_pattern_string_is_shared_verbatim():
    assert CITATION_TAG_RE.pattern == CITATION_TAG_PATTERN
    assert _CITATION_TAG_RE.pattern == CITATION_TAG_PATTERN


def test_query_citations_findall_matches_real_tags_only():
    assert CITATION_TAG_RE.findall(PROSE_WITH_TAGS) == REAL_TAGS
    assert CITATION_TAG_RE.findall(NON_CITATION_BRACKETS) == []


def test_eval_scoring_strip_citations_removes_exactly_the_real_tags():
    stripped = strip_citations(PROSE_WITH_TAGS)
    for tag in REAL_TAGS:
        assert tag not in stripped
    assert "42" in stripped

    # Ordinary bracketed asides (no "#") must survive untouched.
    assert strip_citations(NON_CITATION_BRACKETS) == NON_CITATION_BRACKETS


def test_query_latex_split_preserves_citation_tags_as_odd_indexed_parts():
    parts = _CITATION_RE.split(PROSE_WITH_TAGS)
    # Every real tag shows up intact as one of the captured (odd-indexed) parts.
    for tag in REAL_TAGS:
        assert tag in parts

    # normalize_latex must never mangle a citation label even when LaTeX
    # markup sits right next to it.
    text = f"$V = IR$ {REAL_TAGS[0]}"
    result = normalize_latex(text)
    assert REAL_TAGS[0] in result
