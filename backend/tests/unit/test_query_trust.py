"""Trust types and the abstention-reason mapping (doc 07 sections 4, 9)."""

from __future__ import annotations

import pytest

from docket.services.query.trust import (
    AbstentionReason,
    AnswerStatus,
    ClaimDraft,
    ClaimKind,
    ClaimVerification,
    DerivationRecord,
    EvidenceRef,
    TrustSummary,
    VerificationStatus,
    abstention_reason_for,
)


def test_enum_values_are_stable_codes():
    assert {r.value for r in AbstentionReason} == {
        "NO_EVIDENCE", "INSUFFICIENT_SUPPORT", "SOURCE_UNAVAILABLE", "PERMISSION_CHANGED",
        "UNRESOLVED_CONFLICT", "INVALID_DERIVATION", "VERIFIER_UNAVAILABLE",
        "CONTEXT_BUDGET_EXCEEDED",
    }
    assert {s.value for s in AnswerStatus} == {"VERIFIED", "PARTIAL", "CONFLICT", "ABSTAINED"}
    assert {s.value for s in VerificationStatus} == {
        "SUPPORTED", "CONTRADICTED", "INSUFFICIENT", "INACCESSIBLE", "ERROR"}
    assert AnswerStatus.ABSTAINED == "ABSTAINED"


def test_claim_draft_defaults_and_roundtrip():
    ref = EvidenceRef(source_id="s", evidence_version_id="v1", chunk_id="c1",
                      locator={"sheet": "Q1", "cell": "B4"})
    claim = ClaimDraft(id="c1", text="August revenue was 8.66m", evidence_refs=[ref])
    assert claim.kind is ClaimKind.SOURCED and claim.material and claim.derivation_ref is None
    assert ClaimDraft.model_validate_json(claim.model_dump_json()) == claim
    with pytest.raises(ValueError):
        ClaimDraft(id="x", text="t", kind="MAYBE")
    d = DerivationRecord(id="d1", operation="sum", input_refs=[ref, "d0"], units="INR", result=12)
    assert d.rounding is None


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({}, None),
        ({"no_retrieval": True}, AbstentionReason.NO_EVIDENCE),
        ({"scoped_file_empty": True}, AbstentionReason.SOURCE_UNAVAILABLE),
        ({"scoped_file_empty": True, "no_retrieval": True}, AbstentionReason.SOURCE_UNAVAILABLE),
        ({"evidence_revoked": True}, AbstentionReason.PERMISSION_CHANGED),
        ({"evidence_revoked": True, "empty_generation": True}, AbstentionReason.PERMISSION_CHANGED),
        ({"empty_generation": True}, AbstentionReason.INSUFFICIENT_SUPPORT),
        ({"citation_repair_failed": True}, AbstentionReason.INSUFFICIENT_SUPPORT),
    ],
)
def test_abstention_reason_for(kwargs, expected):
    assert abstention_reason_for(**kwargs) is expected


def _v(cid, status):
    return ClaimVerification(claim_id=cid, status=status)


def test_trust_summary_roll_up():
    claims = [ClaimDraft(id="a", text="a"), ClaimDraft(id="b", text="b"),
              ClaimDraft(id="n", text="nav", material=False)]
    S, I, C = VerificationStatus.SUPPORTED, VerificationStatus.INSUFFICIENT, VerificationStatus.CONTRADICTED
    s = TrustSummary.from_verifications(claims, [_v("a", S), _v("b", S)])
    assert (s.status, s.claims_total, s.claims_supported) == (AnswerStatus.VERIFIED, 2, 2)
    s = TrustSummary.from_verifications(claims, [_v("a", S), _v("b", I)])
    assert s.status is AnswerStatus.PARTIAL
    s = TrustSummary.from_verifications(claims, [_v("a", S), _v("b", C)])
    assert s.status is AnswerStatus.CONFLICT and s.conflicts == ["b"]
    s = TrustSummary.from_verifications(claims, [_v("a", I)])
    assert s.status is AnswerStatus.ABSTAINED
    assert s.abstention_reason is AbstentionReason.INSUFFICIENT_SUPPORT
    s = TrustSummary.from_verifications(claims, [_v("a", C)])
    assert s.abstention_reason is AbstentionReason.UNRESOLVED_CONFLICT
