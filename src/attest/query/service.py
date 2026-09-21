"""`QueryService` -- the single-path retrieve-then-generate query flow.

This is the spike's validated "fast path" (`spike/query.py`, see
`spike/RESULTS.md` "Retrieval Quality" -- 12/12 correct on the eval set) turned
into a library-shaped service: hybrid retrieval -> evidence resolution ->
citation-grounded generation -> citation validation.

Deliberately single-path. There's no "classify the question and maybe route to
a bounded agent investigation loop" step here -- that routing decision depends
on the Agent Runtime Manager / Policy Gateway pattern, which is a separate,
already-scoped checkpoint (CP9). `QueryService.ask()` always does direct
retrieve->generate.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel
from sqlalchemy import Engine

from attest.inference.gateway import InferenceGateway
from attest.query.prompts import ABSTENTION_PHRASE, SYSTEM_PROMPT, build_context_block, validate_citations
from attest.retrieval.hybrid import hybrid_search
from attest.retrieval.resolver import EvidenceResolver


class Citation(BaseModel):
    citation_label: str
    chunk_id: str
    source_display_name: str


class QueryResult(BaseModel):
    question: str
    answer: str
    citations: list[Citation]
    abstained: bool
    validation_warnings: list[str]


class QueryService:
    """Ties hybrid retrieval, evidence resolution, generation, and citation
    validation together behind one `ask(question) -> QueryResult` call."""

    def __init__(
        self,
        *,
        engine: Engine,
        table: Any,
        gateway: InferenceGateway,
        resolver: EvidenceResolver,
        top_k: int = 8,
    ):
        self._engine = engine
        self._table = table
        self._gateway = gateway
        self._resolver = resolver
        self._top_k = top_k

    def ask(self, question: str) -> QueryResult:
        ranked_chunks = hybrid_search(
            engine=self._engine,
            table=self._table,
            gateway=self._gateway,
            query=question,
            top_k=self._top_k,
        )

        if not ranked_chunks:
            # Nothing retrieved at all -- skip generation entirely. There is
            # no context to ground an answer in, so there's nothing useful
            # for the model to do; calling the gateway here would just risk
            # an ungrounded (potentially hallucinated) answer for no benefit.
            return QueryResult(
                question=question,
                answer=ABSTENTION_PHRASE,
                citations=[],
                abstained=True,
                validation_warnings=[],
            )

        resolved = self._resolver.resolve_many([rc.chunk_id for rc in ranked_chunks])
        context = build_context_block(resolved)
        prompt = f"Context:\n{context}\n\nQuestion: {question}\n\nAnswer:"

        answer = self._gateway.generate(system=SYSTEM_PROMPT, prompt=prompt)

        validation = validate_citations(answer, resolved)

        citations = [
            Citation(
                citation_label=chunk.citation_label,
                chunk_id=chunk.chunk_id,
                source_display_name=chunk.source_display_name,
            )
            for chunk in resolved
            if chunk.citation_label in validation.cited_labels
        ]

        validation_warnings: list[str] = []
        if validation.uncited:
            validation_warnings.append("answer contains no citations")
        for unknown in validation.unknown_citations:
            validation_warnings.append(f"answer references an unknown citation: {unknown}")

        return QueryResult(
            question=question,
            answer=answer,
            citations=citations,
            abstained=validation.is_abstention,
            validation_warnings=validation_warnings,
        )
