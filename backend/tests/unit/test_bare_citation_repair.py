"""repair_bare_citations: re-bracket exact labels the model copied without brackets."""

from docket.infra.retrieval.resolver import ResolvedEvidence
from docket.services.query.citations import repair_bare_citations, validate_citations
from tests.unit.test_prompts import _evidence

LABEL = "[memo.docx #chk_7060b551]"


def _chunks() -> list[ResolvedEvidence]:
    return [_evidence("c1", "rate is now 2,650", LABEL), _evidence("c2", "x", "[contract.pdf #chk_d8ea7d32]")]


def test_bare_label_is_rebracketed_and_validates():
    ans = "The rate is USD 2,650 per day, per Approval AM-01 in the memo.docx #chk_7060b551, which amended it."
    fixed = repair_bare_citations(ans, _chunks())
    assert LABEL in fixed
    v = validate_citations(fixed, _chunks())
    assert not v.uncited and not v.unknown_citations


def test_already_bracketed_untouched():
    ans = f"2,650 per day. {LABEL}"
    assert repair_bare_citations(ans, _chunks()) == ans


def test_unknown_bare_label_not_invented():
    ans = "see memo.docx #chk_deadbeef"
    assert repair_bare_citations(ans, _chunks()) == ans


def test_abstention_untouched():
    ans = "I don't know based on the available evidence."
    assert repair_bare_citations(ans, _chunks()) == ans
