"""`QueryService` -- routes a question to the fast path or the bounded
investigation agent, then answers it with citation validation either way.

This used to be single-path only (see git history / the previous version of
this docstring): always direct retrieve->generate, the spike's validated
"fast path" (`spike/query.py`, see `spike/RESULTS.md` "Retrieval Quality" --
12/12 correct on the eval set). That routing decision depended on the Agent
Runtime Manager / Policy Gateway pattern (CP9), which is now done
(`docket.services.agent.graph.build_investigation_agent`) -- so `ask()` now classifies
each question (`docket.services.query.classifier.QueryClassifier`) and routes it to
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
import logging
import re
from typing import Any, Callable

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from pydantic import BaseModel
from sqlalchemy import Engine

from docket.services.agent.graph import build_investigation_agent
from docket.core.config import Settings
from docket.core.config import settings as default_settings
from docket.infra.inference.gateway import InferenceGateway
from docket.services.query.classifier import HeuristicQueryClassifier, QueryClassifier, QueryMode
from docket.services.query.conversation import ConversationTurn, format_history_block, trim_history
from docket.services.query.citations import (
    CITATION_TAG_PATTERN,
    build_context_block,
    validate_citations,
)
from docket.services.query.latex import normalize_latex
from docket.prompts.query import rewrite_prompt
from docket.services.query.prompts import (
    ABSTENTION_PHRASE,
    AGENT_SYSTEM_PROMPT,
    AGENT_SYSTEM_PROMPT_WITH_HISTORY,
    REWRITE_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    SYSTEM_PROMPT_WITH_HISTORY,
)
from docket.infra.parsing.tokens import get_token_counter
from docket.infra.retrieval.hybrid import hybrid_search
from docket.infra.retrieval.resolver import ChunkNotFoundError, EvidenceResolver, ResolvedEvidence

logger = logging.getLogger(__name__)

# A rewrite longer than this is treated as a failure (a sane query is short).
_MAX_REWRITE_CHARS = 300
# A citation tag plus the blanks before it, so stripping leaves no stray gap.
_TAG_WITH_LEAD_RE = re.compile(r"[ \t]*" + CITATION_TAG_PATTERN)


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
    # Retrieved chunks left out of the prompt to fit the token budget (lowest
    # ranked first dropped); never citable.
    dropped_chunk_ids: list[str] = []
    # Follow-up rewrite used as a second retrieval query (None if no rewrite
    # ran or it was unusable). Never evidence, never cited.
    standalone_query: str | None = None


def _citations_from_agent_messages(
    messages: list, resolver: EvidenceResolver
) -> list[ResolvedEvidence]:
    """Reconstructs the citation universe an agent run actually earned, from
    its message trace, instead of from an upfront retrieval set (there isn't
    one on the agent path -- the agent decides what to look at).

    Walks `messages` for `ToolMessage`s produced by a *successful*
    `read_evidence` call: one whose corresponding `AIMessage` tool_call was
    named "read_evidence" (correlated via `tool_call_id`, not
    `ToolMessage.name` -- `docket.services.agent.policy_gateway.make_policy_gateway`
    never sets `name` on the `ToolMessage`s it builds, only `tool_call_id`,
    so that id is the only reliable link back to which tool was called) and
    whose JSON content does NOT contain an "error" key (an invented/mistyped
    chunk_id -- see `docket.services.agent.tools.make_read_evidence_tool`). Each
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

    try:
        return resolver.resolve_many(chunk_ids)
    except ChunkNotFoundError:
        # Evidence can be revoked between the tool call and finalization.
        return []


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
        top_k: int = default_settings.default_top_k,
        classifier: QueryClassifier | None = None,
        settings: Settings = default_settings,
        agent: Any | None = None,
        trace_callback: Callable[[dict[str, Any]], None] | None = None,
        page_table: Any | None = None,
        manifest_guard: Any | None = None,
    ):
        """
        `classifier` defaults to `HeuristicQueryClassifier()` -- zero-cost,
        deterministic routing with no dependencies of its own.

        `page_table` (visual retrieval checkpoint 3) is an optional LanceDB
        `pages` table (see `docket.infra.index.visual_index`), threaded straight
        through to `hybrid_search`'s own `page_table` parameter on the FAST
        path. Defaults to `None`, in which case `_ask_fast` calls
        `hybrid_search` exactly as it did before this parameter existed --
        callers that don't know about visual retrieval (every existing
        caller) get byte-for-byte unchanged behavior. Callers wire it in only
        when `settings.visual_index_enabled` is `True` (see `cli/main.py`'s
        `query` command and `cli/interactive/session.py`'s
        `_default_factory` for the two real wiring sites); the AGENT path's
        `search_knowledge` tool (`docket.services.agent.tools`) is unaffected and
        still runs 2-way fusion only -- out of scope for this checkpoint.

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
        state dict shaped like `docket.services.agent.policy_gateway.AgentState`)
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
        self._trace_callback = trace_callback
        self._page_table = page_table
        # Refuses queries when the index manifest names a different embedding
        # model (see `docket.infra.index.manifest`); `None` skips the check.
        self._manifest_guard = manifest_guard

    def _get_agent(self) -> Any:
        if self._agent is None:
            self._agent = build_investigation_agent(
                engine=self._engine,
                table=self._table,
                gateway=self._gateway,
                resolver=self._resolver,
                settings=self._settings,
                trace_callback=self._trace_callback,
                top_k=self._top_k,
                manifest_guard=self._manifest_guard,
            )
        return self._agent

    def ask(
        self,
        question: str,
        mode: QueryMode | None = None,
        history: list[ConversationTurn] | None = None,
    ) -> QueryResult:
        """Answer `question`, routing to FAST or AGENT.

        `mode`, when given, skips `self._classifier.classify(question)`
        entirely and routes directly -- for callers (e.g. the desktop
        sidecar's `query.ask` op) that want to let the caller force a mode
        explicitly rather than rely on `HeuristicQueryClassifier`'s
        phrasing-based guess. When omitted (the default), behavior is
        unchanged from before this parameter existed: the question is
        classified and routed based on that result.

        `history` holds prior conversation turns (oldest first), used only to
        resolve references in `question`. Retrieval still uses the literal
        latest question, and citations are validated against this turn's
        evidence only. None/empty behaves exactly as without history.
        """
        if mode is None:
            mode = self._classifier.classify(question)
        trimmed = trim_history(history)
        result = (
            self._ask_agent(question, trimmed)
            if mode == QueryMode.AGENT
            else self._ask_fast(question, trimmed)
        )
        # Deterministic belt-and-suspenders for the system prompt's "no
        # LaTeX" instruction (see `docket.services.query.latex`'s docstring for why):
        # applied here, once, so neither path can add a new way to return an
        # answer without it. Citation tags are never a `normalize_latex`
        # target (see that module), so this can't disturb `result.citations`
        # or the validation already done inside `_ask_fast`/`_ask_agent`.
        return result.model_copy(update={"answer": normalize_latex(result.answer)})

    def _ask_fast(
        self, question: str, history: list[ConversationTurn] | None = None
    ) -> QueryResult:
        # Follow-up rewrite: fast path only, never on the first turn. (The
        # agent path is unchanged -- the agent writes its own search queries.)
        standalone = self._rewrite_followup(question, history)
        ranked_chunks = hybrid_search(
            engine=self._engine,
            table=self._table,
            gateway=self._gateway,
            query=question,
            top_k=self._top_k,
            page_table=self._page_table,
            manifest_guard=self._manifest_guard,
            **({"extra_queries": [standalone]} if standalone else {}),
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
                standalone_query=standalone,
            )

        try:
            resolved = self._resolver.resolve_many([rc.chunk_id for rc in ranked_chunks])
        except ChunkNotFoundError:
            return QueryResult(
                question=question, answer=ABSTENTION_PHRASE, citations=[],
                abstained=True, validation_warnings=["retrieved evidence is no longer available"],
                mode=QueryMode.FAST.value, standalone_query=standalone,
            )
        system, prompt, resolved, dropped = self._fit_prompt(question, history, resolved)

        answer = self._gateway.generate(system=system, prompt=prompt, **self._answer_opts())

        return self._finalize_answer(
            question=question, answer=answer, resolved=resolved,
            mode=QueryMode.FAST, retry_system=system, retry_prompt=prompt,
            dropped_chunk_ids=dropped,
        ).model_copy(update={"standalone_query": standalone})

    def _rewrite_followup(
        self, question: str, history: list[ConversationTurn] | None
    ) -> str | None:
        """One local-model call turning `question` into a standalone search
        query from the preceding turns, or None when there is no history, the
        rewrite is disabled, or the result is unusable (error, empty, the
        abstention phrase, multi-line, over `_MAX_REWRITE_CHARS`, or identical
        to the question). Never raises: any failure means the original
        question alone is searched."""
        if not history or not self._settings.rewrite_enabled:
            return None
        # Citation tags would pollute the query; the prior answer text stays.
        clean = [
            ConversationTurn(
                question=_TAG_WITH_LEAD_RE.sub("", turn.question).strip(),
                answer=_TAG_WITH_LEAD_RE.sub("", turn.answer).strip(),
            )
            for turn in history
        ]
        opts: dict[str, Any] = {
            "options": {
                "temperature": self._settings.rewrite_temperature,
                "num_predict": self._settings.rewrite_num_predict,
            },
            "think": self._settings.rewrite_think,
        }
        if self._settings.rewrite_model:
            opts["model"] = self._settings.rewrite_model
        try:
            raw = self._gateway.generate(
                system=REWRITE_SYSTEM_PROMPT,
                prompt=rewrite_prompt(format_history_block(clean), question),
                **opts,
            )
        except Exception as exc:  # noqa: BLE001 -- a rewrite must never fail the query
            logger.warning("follow-up rewrite failed (%s); using the original question", exc)
            return None
        candidate = (raw or "").strip()
        if not candidate:
            logger.debug("follow-up rewrite was empty; using the original question")
            return None
        if "\n" in candidate or "\r" in candidate:
            logger.debug("follow-up rewrite was multi-line; using the original question")
            return None
        if len(candidate) > _MAX_REWRITE_CHARS:
            logger.debug("follow-up rewrite too long (%d chars); using the original", len(candidate))
            return None
        if ABSTENTION_PHRASE.lower() in candidate.lower():
            logger.debug("follow-up rewrite was the abstention phrase; using the original")
            return None
        if candidate == question.strip():
            return None
        return candidate

    def _answer_opts(self) -> dict[str, Any]:
        """Sampling for the answering path (fast path and citation repair)."""
        return {
            "options": {"temperature": self._settings.answer_temperature},
            "think": self._settings.answer_think,
        }

    def _fit_prompt(
        self,
        question: str,
        history: list[ConversationTurn] | None,
        resolved: list[ResolvedEvidence],
    ) -> tuple[str, str, list[ResolvedEvidence], list[str]]:
        """Build the fast-path prompt, dropping the lowest-ranked chunks (the
        tail of `resolved`, which is best-first) until system + history +
        context + question fit in `num_ctx - answer_token_reserve`. Never
        drops below one chunk. Returns `(system, prompt, included, dropped_ids)`.

        Tokens are counted with the embedding tokenizer, which only
        approximates the generator's, so the reserve doubles as the safety
        margin for that mismatch as well as room for the answer."""
        if history:
            head = f"Conversation so far:\n{format_history_block(history)}\n\n"
            system = SYSTEM_PROMPT_WITH_HISTORY
        else:
            head = ""
            system = SYSTEM_PROMPT

        def build(chunks: list[ResolvedEvidence]) -> str:
            return f"{head}Context:\n{build_context_block(chunks)}\n\nQuestion: {question}\n\nAnswer:"

        budget = self._settings.num_ctx - self._settings.answer_token_reserve
        counter = get_token_counter()
        # Count the fixed part once and each chunk block once; the joins add a
        # token or two per block, covered by the reserve.
        total = counter.count(system) + counter.count(build([]))
        costs = [counter.count(build_context_block([chunk])) + 1 for chunk in resolved]
        total += sum(costs)
        initial_total = total
        included = list(resolved)
        while total > budget and len(included) > 1:
            included.pop()
            total -= costs[len(included)]
        dropped = [chunk.chunk_id for chunk in resolved[len(included):]]
        if dropped:
            logger.warning(
                "prompt over token budget (%d > %d); dropped %d lowest-ranked chunk(s): %s",
                initial_total, budget, len(dropped), ", ".join(dropped),
            )
        return system, build(included), included, dropped

    def _finalize_answer(
        self, *, question: str, answer: str, resolved: list[ResolvedEvidence],
        mode: QueryMode, retry_system: str, retry_prompt: str,
        dropped_chunk_ids: list[str] | None = None,
    ) -> QueryResult:
        """Allow one citation repair, then fail closed on invalid attribution.

        Tag validation establishes that citations were actually available to
        this query. Semantic support still needs evaluation; a valid tag alone
        cannot prove that every claim is supported by its passage.
        """
        validation = validate_citations(answer, resolved)
        invalid = bool(answer.strip()) and not validation.is_abstention and (
            validation.uncited or bool(validation.unknown_citations)
        )
        if invalid and resolved:
            answer = self._gateway.generate(
                system=retry_system,
                prompt=(
                    retry_prompt
                    + "\n\nYour previous answer had missing or invalid citations. "
                    "Answer again using only the supplied citation tags after "
                    "each factual claim, or use the exact abstention sentence."
                ),
                **self._answer_opts(),
            )
            validation = validate_citations(answer, resolved)

        warnings: list[str] = []
        cited_chunks = [chunk for chunk in resolved if chunk.citation_label in validation.cited_labels]
        if cited_chunks:
            try:
                self._resolver.resolve_many([chunk.chunk_id for chunk in cited_chunks])
            except ChunkNotFoundError:
                warnings.append("cited evidence is no longer available")
        if validation.uncited:
            warnings.append("answer contains no citations")
        for unknown in validation.unknown_citations:
            warnings.append(f"answer references an unknown citation: {unknown}")
        if not answer.strip():
            warnings.append("answer is empty")
        if warnings:
            answer = ABSTENTION_PHRASE
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
        return QueryResult(
            question=question, answer=answer, citations=citations,
            abstained=validation.is_abstention, validation_warnings=warnings,
            mode=mode.value, dropped_chunk_ids=list(dropped_chunk_ids or []),
        )

    def _ask_agent(
        self, question: str, history: list[ConversationTurn] | None = None
    ) -> QueryResult:
        agent = self._get_agent()

        system_prompt = AGENT_SYSTEM_PROMPT_WITH_HISTORY if history else AGENT_SYSTEM_PROMPT
        messages_in: list = [SystemMessage(content=system_prompt)]
        for turn in history or []:
            messages_in.append(HumanMessage(content=turn.question))
            messages_in.append(AIMessage(content=turn.answer))
        messages_in.append(HumanMessage(content=question))

        initial_state = {
            "messages": messages_in,
            "iterations": 0,
            "tool_calls_made": 0,
            "blocked_calls": [],
        }
        result_state = agent.invoke(initial_state, config={"recursion_limit": 50})
        messages = result_state["messages"]

        final_ai_message = next(
            (m for m in reversed(messages) if isinstance(m, AIMessage)), None
        )
        answer = (
            str(final_ai_message.content)
            if final_ai_message is not None and not final_ai_message.tool_calls
            else ""
        )

        resolved = _citations_from_agent_messages(messages, self._resolver)
        if self._trace_callback is not None:
            self._trace_callback({
                "event": "agent_end",
                "messages": messages,
                "iterations": result_state.get("iterations", 0),
                "tool_calls_made": result_state.get("tool_calls_made", 0),
                "blocked_calls": result_state.get("blocked_calls", []),
            })

        return self._finalize_answer(
            question=question, answer=answer, resolved=resolved,
            mode=QueryMode.AGENT,
            retry_system=SYSTEM_PROMPT,
            retry_prompt=f"Context:\n{build_context_block(resolved)}\n\nQuestion: {question}\n\nAnswer:",
        )
