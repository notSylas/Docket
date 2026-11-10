"""Query-time orchestration of the document layer (all three flags off by default)."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Engine
from sqlalchemy.orm import sessionmaker

from docket.services.documents import family, lookup, routing

logger = logging.getLogger(__name__)
_ON = ("on", "1", "true", "yes")


def flag(value: Any) -> bool:
    return str(value).lower() in _ON


@dataclass
class LookupAnswer:
    answer: str
    chunk_id: str
    field: str
    document_file: str | None


class DocumentLayer:
    def __init__(self, engine: Engine, session_factory: sessionmaker, settings: Any) -> None:
        self._engine = engine
        self._sf = session_factory
        self._s = settings

    @property
    def routing_on(self) -> bool:
        return flag(getattr(self._s, 'doc_routing', 'off'))

    @property
    def lookup_on(self) -> bool:
        return flag(getattr(self._s, 'field_lookup', 'off'))

    @property
    def family_on(self) -> bool:
        return flag(getattr(self._s, 'family_latest', 'off'))

    def route(self, question: str, scope_ids: list[str] | None) -> routing.RouteDecision | None:
        """Never raises: any failure means 'no routing'."""
        if not (self.routing_on or self.lookup_on):
            return None
        try:
            return routing.route(self._engine, question, getattr(self._s, 'doc_routing_top_n', 8), scope_version_ids=scope_ids)
        except Exception:  # noqa: BLE001
            logger.warning("document routing failed; searching globally", exc_info=True)
            return None

    def restrict_scope(self, decision: routing.RouteDecision | None, scope_ids: list[str] | None) -> list[str] | None:
        if not self.routing_on or decision is None or not decision.routed or not decision.version_ids:
            return scope_ids
        if scope_ids is None:
            return list(decision.version_ids)
        inter = [v for v in decision.version_ids if v in set(scope_ids)]
        return inter or scope_ids

    def lookup_answer(self, question: str, decision: routing.RouteDecision | None, resolver: Any) -> tuple[LookupAnswer, Any] | None:
        """(answer, resolved_evidence) or None. `resolver.resolve_many` supplies the real
        citation label; if the supporting chunk is gone the lookup is dropped."""
        if not self.lookup_on or decision is None or not decision.routed or not decision.top_unique:
            return None
        try:
            intent = lookup.classify_intent(question)
            if intent is None:
                return None
            field, kind = intent
            sf = lookup.stored_field(self._engine, decision.top_version_id, field)
            if not lookup.usable(sf, kind):
                return None
            resolved = resolver.resolve_many([sf.chunk_id])
            if not resolved:
                return None
            ev = resolved[0]
            if ev.evidence_version_id != decision.top_version_id:
                return None
            text_ = lookup.compose(sf, kind, ev.text, ev.citation_label)
            if not text_:
                return None
            return LookupAnswer(text_, sf.chunk_id, field, sf.file_name), ev
        except Exception:  # noqa: BLE001
            logger.warning("field lookup failed; using normal retrieval", exc_info=True)
            return None

    def family_filter(self, resolved: list) -> tuple[list, str | None]:
        if not self.family_on or len(resolved) < 2:
            return resolved, None
        try:
            dec = family.apply_latest(self._sf, resolved, lambda c: c.text)
        except Exception:  # noqa: BLE001
            logger.warning("family filter failed", exc_info=True)
            return resolved, None
        if not dec.drop_version_ids:
            return resolved, dec.note
        kept = [c for c in resolved if c.evidence_version_id not in dec.drop_version_ids]
        return (kept or resolved), dec.note
