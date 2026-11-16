"""Plain-language caveats for citations whose original file no longer matches.

In reference mode the original stays in the user's folder, so after an answer
was indexed the file may have been edited, moved or deleted. The cited passage
is still the indexed text; this says so instead of letting it pass as current.
"""

from __future__ import annotations

from typing import Any


def original_notes(context: Any, chunk_ids: list[str]) -> dict[str, str]:
    """chunk_id -> caveat, only for chunks whose original does not match the
    indexed bytes. Advisory: any failure yields no note, never an error."""
    notes: dict[str, str] = {}
    try:
        from sqlalchemy import select

        from docket.core.db.models import EvidenceVersion

        versions: dict[str, str] = {}
        for cid in chunk_ids:
            try:
                versions[cid] = context.resolver.resolve(cid).evidence_version_id
            except Exception:  # noqa: BLE001
                continue
        if not versions:
            return notes
        with context.session_factory() as session:
            rows = {
                v.id: v
                for v in session.execute(
                    select(EvidenceVersion).where(EvidenceVersion.id.in_(set(versions.values())))
                ).scalars()
            }
            status: dict[str, str] = {}
            for vid, version in rows.items():
                note = context.evidence_manager.original_status(version).note
                if note:
                    status[vid] = note
        for cid, vid in versions.items():
            if vid in status:
                notes[cid] = status[vid]
    except Exception:  # noqa: BLE001
        return {}
    return notes
