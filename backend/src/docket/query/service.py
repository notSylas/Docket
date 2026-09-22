"""`QueryService` -- routes a question to the fast path or the bounded
investigation agent, then answers it with citation validation either way.

This used to be single-path only (see git history / the previous version of
this docstring): always direct retrieve->generate, the spike's validated
"fast path" (`spike/query.py`, see `spike/RESULTS.md` "Retrieval Quality" --
12/12 correct on the eval set). That routing decision depended on the Agent
Runtime Manager / Policy Gateway pattern (CP9), which is now done
(`docket.agent.graph.build_investigation_agent`) -- so `ask()` now classifies
each question (`docket.query.classifier.QueryClassifier`) and routes it to
either:

- FAST: the original hybrid retrieval -> evidence resolution ->
  citation-grounded generation -> citation validation flow, unchanged.
- AGENT: the bounded LangGraph investigation agent, given the question
  directly and left to call `search_knowledge`/`read_evidence` itself; its
  citations are reconstructed from the actual tool-call trace afterwards
  (see `_citations_from_agent_messages`) and validated through the exact
  same `validate_citations` logic as the fast path, so citation-quality
  behavior is consistent between both paths.
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from pydantic import BaseModel
from sqlalchemy import Engine

from docket.agent.graph import build_investigation_agent
from docket.config import Settings
from docket.config import settings as default_settings
from docket.inference.gateway import InferenceGateway
from docket.query.classifier import HeuristicQueryClassifier, QueryClassifier, QueryMode
from docket.query.prompts import (
    ABSTENTION_PHRASE,
    AGENT_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    build_context_block,
    validate_citations,
)
from docket.retrieval.hybrid import hybrid_search
from docket.retrieval.resolver import EvidenceResolver, ResolvedEvidence


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
    mode: str


def _citations_from_agent_messages(
    messages: list, resolver: EvidenceResolver
) -> list[ResolvedEvidence]:
    """Reconstructs the citation universe an agent run actually earned, from
    its message trace, instead of from an upfront retrieval set (there isn't
    one on the agent path -- the agent decides what to look at).

    Walks `messages` for `ToolMessage`s produced by a *successful*
    `read_evidence` call: one whose corresponding `AIMessage` tool_call was
    named "read_evidence" (correlated via `tool_call_id`, not
    `ToolMessage.name` -- `docket.agent.policy_gateway.make_policy_gateway`
    never sets `name` on the `ToolMessage`s it builds, only `tool_call_id`,
    so that id is the only reliable link back to which tool was called) and
    whose JSON content does NOT contain an "error" key (an invented/mistyped
    chunk_id -- see `docket.agent.tools.make_read_evidence_tool`). Each
    surviving call's `chunk_id` (present in that JSON since the fix in this
    same change) is collected in first-seen order, deduped, then resolved in
    one batch via `resolver.resolve_many`.

    If the agent made zero successful `read_evidence` calls (answered from
    `search_knowledge` snippets alone, got fully policy-blocked, or hit the
    iteration cap before reading anything), `chunk_ids` is empty and
    `resolve_many` returns `[]` -- not an error. The caller then validates
    the answer against an empty citation universe, same as any other case
    with no real evidence backing it: any claim would be flagged uncited,
    any bracketed tag the model still emitted would be flagged unknown. That
    mirrors reality (nothing was actually looked at) rather than inventing a
    separate agent-only rule for "no citations available".

    Pure aside from the `resolve_many` call -- no LangGraph/LLM involved --
    so it's directly unit-testable with a hand-built message list.
    """
    call_name_by_id: dict[str, str] = {}
    for message in messages:
        if isinstance(message, AIMessage):
            for call in getattr(message, "tool_calls", None) or []:
                call_name_by_id[call["id"]] = call["name"]

    seen: set[str] = set()
    chunk_ids: list[str] = []
    for message in messages:
        if not isinstance(message, ToolMessage):
            continue
        if call_name_by_id.get(message.tool_call_id) != "read_evidence":
            continue
        try:
            payload = json.loads(message.content)
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict) or "error" in payload:
            continue
        chunk_id = payload.get("chunk_id")
        if not chunk_id or chunk_id in seen:
            continue
        seen.add(chunk_id)
        chunk_ids.append(chunk_id)

    return resolver.resolve_many(chunk_ids)


class QueryService:
    """Ties classification, hybrid retrieval, the investigation agent,
    generation, and citation validation together behind one
    `ask(question) -> QueryResult` call."""

    def __init__(
        self,
        *,
        engine: Engine,
        table: Any,
        gateway: InferenceGateway,
        resolver: EvidenceResolver,
        top_k: int = 8,
        classifier: QueryClassifier | None = None,
        settings: Settings = default_settings,
        agent: Any | None = None,
    ):
        """
        `classifier` defaults to `HeuristicQueryClassifier()` -- zero-cost,
        deterministic routing with no dependencies of its own.

        The investigation agent is deliberately NOT built here. Building one
        means constructing real `search_knowledge`/`read_evidence` LangChain
        tools and a compiled LangGraph graph (`build_investigation_agent`)
        -- work that should not be paid on every `QueryService` construction
        or every FAST-mode query, since FAST is still the common case (see
        `HeuristicQueryClassifier`'s docstring). Instead, `QueryService`
        keeps exactly what `build_investigation_agent` needs to build one
        itself (`engine`/`table`/`gateway`/`resolver`, already required
        here for the fast path anyway, plus `settings` for the
        iteration/tool-call budget) and builds it lazily, once, on first
        AGENT-mode use (see `_get_agent`).

        `agent` is an escape hatch accepting an already-compiled agent graph
        (anything with an `.invoke(state, config=...)` method returning a
        state dict shaped like `docket.agent.policy_gateway.AgentState`)
        directly, instead of having `QueryService` build one. Two reasons
        this exists as its own parameter rather than folding into the lazy
        path: (1) tests -- unit-testing the AGENT branch's citation
        reconstruction needs a fake/stub agent that returns a canned
        message trace without ever touching LangGraph or Ollama; injecting
        one here is the cleanest way to do that (2) callers that already
        have a pre-built agent (e.g. one shared across several
        `QueryService` instances, or configured differently than
        `build_investigation_agent`'s defaults) can hand it over directly.
        When given, it's used as-is and `settings` is ignored for
        agent-building purposes.
        """
        self._engine = engine
        self._table = table
        self._gateway = gateway
        self._resolver = resolver
        self._top_k = top_k
        self._classifier = classifier or HeuristicQueryClassifier()
        self._settings = settings
        self._agent = agent

    def _get_agent(self) -> Any:
        if self._agent is None:
            self._agent = build_investigation_agent(
                engine=self._engine,
                table=self._table,
                gateway=self._gateway,
                resolver=self._resolver,
                settings=self._settings,
            )
        return self._agent

    def ask(self, question: str, mode: QueryMode | None = None) -> QueryResult:
        """Answer `question`, routing to FAST or AGENT.

        `mode`, when given, skips `self._classifier.classify(question)`
        entirely and routes directly -- for callers (e.g. the desktop
        sidecar's `query.ask` op) that want to let the caller force a mode
        explicitly rather than rely on `HeuristicQueryClassifier`'s
        phrasing-based guess. When omitted (the default), behavior is
        unchanged from before this parameter existed: the question is
        classified and routed based on that result.
        """
        if mode is None:
            mode = self._classifier.classify(question)
        if mode == QueryMode.AGENT:
            return self._ask_agent(question)
        return self._ask_fast(question)

    def _ask_fast(self, question: str) -> QueryResult:
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
                mode=QueryMode.FAST.value,
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
            mode=QueryMode.FAST.value,
        )

    def _ask_agent(self, question: str) -> QueryResult:
        agent = self._get_agent()

        initial_state = {
            "messages": [
                SystemMessage(content=AGENT_SYSTEM_PROMPT),
                HumanMessage(content=question),
            ],
            "iterations": 0,
            "tool_calls_made": 0,
            "blocked_calls": [],
        }
        result_state = agent.invoke(initial_state, config={"recursion_limit": 50})
        messages = result_state["messages"]

        final_ai_message = next(
            (m for m in reversed(messages) if isinstance(m, AIMessage)), None
        )
        answer = str(final_ai_message.content) if final_ai_message is not None else ABSTENTION_PHRASE

        resolved = _citations_from_agent_messages(messages, self._resolver)
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
            mode=QueryMode.AGENT.value,
        )
