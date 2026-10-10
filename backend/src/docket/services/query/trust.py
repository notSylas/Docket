"""Trust types for claim-level verification and reason-coded abstention.

Types and pure helpers only (doc 07 sections 4, 9, 15 steps 1-2): nothing here
changes answer generation or rendering. Later steps build the structured-draft
path, verifiers and renderers on top of these records.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class ClaimKind(StrEnum):
    SOURCED = "SOURCED"
    DERIVED = "DERIVED"


class VerificationStatus(StrEnum):
    SUPPORTED = "SUPPORTED"
    CONTRADICTED = "CONTRADICTED"
    INSUFFICIENT = "INSUFFICIENT"
    INACCESSIBLE = "INACCESSIBLE"
    ERROR = "ERROR"


class AnswerStatus(StrEnum):
    VERIFIED = "VERIFIED"  # all requested material claims and completeness passed
    PARTIAL = "PARTIAL"  # displayed claims passed, requested coverage incomplete
    CONFLICT = "CONFLICT"  # displayed evidence conflicts, authority does not resolve
    ABSTAINED = "ABSTAINED"  # no useful supported answer may be shown


class AbstentionReason(StrEnum):
    NO_EVIDENCE = "NO_EVIDENCE"
    INSUFFICIENT_SUPPORT = "INSUFFICIENT_SUPPORT"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    PERMISSION_CHANGED = "PERMISSION_CHANGED"
    UNRESOLVED_CONFLICT = "UNRESOLVED_CONFLICT"
    INVALID_DERIVATION = "INVALID_DERIVATION"
    VERIFIER_UNAVAILABLE = "VERIFIER_UNAVAILABLE"
    CONTEXT_BUDGET_EXCEEDED = "CONTEXT_BUDGET_EXCEEDED"


class EvidenceRef(BaseModel):
    source_id: str
    evidence_version_id: str
    chunk_id: str
    evidence_unit_id: str | None = None
    locator: dict[str, Any] = Field(default_factory=dict)


class ClaimDraft(BaseModel):
    id: str
    text: str
    kind: ClaimKind = ClaimKind.SOURCED
    material: bool = True
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    derivation_ref: str | None = None
    display_group: str | None = None


class DerivationRecord(BaseModel):
    id: str
    operation: str
    input_refs: list[EvidenceRef | str] = Field(default_factory=list)
    units: str = ""
    rounding: dict[str, Any] | None = None
    result: Any = None


class ClaimVerification(BaseModel):
    claim_id: str
    status: VerificationStatus
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    support_spans: list[dict[str, Any]] = Field(default_factory=list)
    deterministic_checks: list[dict[str, Any]] = Field(default_factory=list)
    verifier_models: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    high_impact: bool = False


class TrustSummary(BaseModel):
    """Answer-level roll-up: separate support from coverage (doc 07 section 10)."""

    status: AnswerStatus
    abstention_reason: AbstentionReason | None = None
    claims_total: int = 0
    claims_supported: int = 0
    coverage_note: str | None = None
    conflicts: list[str] = Field(default_factory=list)

    @classmethod
    def from_verifications(
        cls, claims: list[ClaimDraft], verifications: list[ClaimVerification]
    ) -> TrustSummary:
        """Roll verified claims up to an answer status (no conflict/authority
        logic yet: any CONTRADICTED material claim is a CONFLICT)."""
        by_id = {v.claim_id: v for v in verifications}
        material = [c for c in claims if c.material]
        supported = [c for c in material if c.id in by_id
                     and by_id[c.id].status is VerificationStatus.SUPPORTED]
        contradicted = [c.id for c in material if c.id in by_id
                        and by_id[c.id].status is VerificationStatus.CONTRADICTED]
        if not supported:
            return cls(
                status=AnswerStatus.ABSTAINED,
                abstention_reason=(
                    AbstentionReason.UNRESOLVED_CONFLICT if contradicted
                    else AbstentionReason.INSUFFICIENT_SUPPORT
                ),
                claims_total=len(material),
                conflicts=contradicted,
            )
        if contradicted:
            status = AnswerStatus.CONFLICT
        elif len(supported) < len(material):
            status = AnswerStatus.PARTIAL
        else:
            status = AnswerStatus.VERIFIED
        return cls(
            status=status,
            claims_total=len(material),
            claims_supported=len(supported),
            conflicts=contradicted,
        )


def abstention_reason_for(
    *,
    no_retrieval: bool = False,
    scoped_file_empty: bool = False,
    evidence_revoked: bool = False,
    empty_generation: bool = False,
    citation_repair_failed: bool = False,
) -> AbstentionReason | None:
    """Map the conditions the fast path already knows to a reason code.

    Pure. Precedence (first match wins): a permission/revocation change beats
    everything because nothing about the answer may be revealed; then a named
    file with no usable content; then nothing retrieved; then generation-side
    failures. Returns None when no abstention condition applies.
    """
    if evidence_revoked:
        return AbstentionReason.PERMISSION_CHANGED
    if scoped_file_empty:
        return AbstentionReason.SOURCE_UNAVAILABLE
    if no_retrieval:
        return AbstentionReason.NO_EVIDENCE
    if empty_generation or citation_repair_failed:
        return AbstentionReason.INSUFFICIENT_SUPPORT
    return None
