"""Bounded LangGraph agent loop: the Agent Policy Gateway wired into an
actual `agent` <-> `gateway` graph.

`build_agent()` reproduces `spike/test_agent_policy_gateway.py`'s
`build_agent()` structure exactly (see that file, and `spike/RESULTS.md`'s
"Tier 3 — Agent Policy Gateway" section, which validated this shape end to
end: bounded iteration, fail-closed tool enforcement, correct cited
answers), just parameterized instead of hardcoded:

- a `ChatOllama` bound ONLY to `allowed_tools.values()` (never anything else
  -- this is the first enforcement layer, proven in the spike's Case 2: the
  model can't even attempt an unbound tool name because it was never told
  the tool exists)
- an `agent` node that calls the model
- a `gateway` node (`docket.agent.policy_gateway.make_policy_gateway`) that
  is the second, independent enforcement layer -- it doesn't trust the
  first layer's cooperation (spike Case 3)
- a conditional edge routing to `gateway` while there are pending tool
  calls and iterations remain, `end` once `max_iterations` is hit; and,
  when the model stops calling tools before satisfying `require_tool_call`
  (see below), a `force_tool_use` node instead of `end` (see
  `should_continue`'s docstring -- this is the real-data-driven fix for the
  agent answering from parametric knowledge with zero citations, which the
  original two-way `gateway`/`end` split allowed on the very first turn)

`require_tool_call` (optional, `None` by default -- fully backward
compatible, e.g. for `test_agent_graph_construction.py`'s bare-`{}`-tools
construction tests) names one specific tool that must have been
successfully called at least once before a final answer is accepted. This
was tightened from an earlier, simpler version of this fix that only
required ANY successful tool call: re-running the real eval that motivated
this fix showed the model satisfying that weaker check by calling
`search_knowledge` (which returns bare chunk_ids/scores, no citation
material) and then still answering without ever calling `read_evidence` --
i.e. it retrieved something but never actually read/cited it, which is
exactly the citation-or-abstain contract this whole mechanism exists to
protect. `build_investigation_agent` passes `require_tool_call=
"read_evidence"` for that reason, since a `citation_label` (the only thing
`query.service._citations_from_agent_messages` can turn into a real
citation) only ever appears in a `read_evidence` result, never in
`search_knowledge`'s.

`build_investigation_agent()` is the real-use convenience wrapper: it builds
the real `search_knowledge`/`read_evidence` tools (via `docket.agent.tools`)
bound to a caller-supplied engine/table/gateway/resolver, and assembles them
into a graph via `build_agent()`, pulling its model/iteration/call-budget/
context config from `Settings` (`gen_model`, `max_agent_iterations`,
`max_agent_tool_calls`, `num_ctx`, `num_predict`) instead of the spike's
module-level constants. This
is the function a later milestone's Agent Runtime Manager / QueryService
routing would call to hand off a question to the bounded-investigation path
-- not wired into the CLI yet (routing fast-path-vs-agent-path is out of
scope here, per CP7's design note in `docket.query.service`), but it must
work end-to-end when called directly.
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_ollama import ChatOllama
from langgraph.graph import END, StateGraph

from docket.agent.policy_gateway import AgentState, make_policy_gateway
from docket.agent.tools import make_read_evidence_tool, make_search_knowledge_tool
from docket.config import Settings, settings as default_settings
from docket.inference.gateway import InferenceGateway
from docket.retrieval.resolver import EvidenceResolver


def _has_successful_call(messages: list, tool_name: str) -> bool:
    """True iff `messages` contains a `ToolMessage` produced by a
    SUCCESSFUL call to `tool_name` -- i.e. one whose corresponding
    `AIMessage` tool_call was actually named `tool_name` (matched via
    `tool_call_id`, not `ToolMessage.name` -- `make_policy_gateway` never
    sets `name` on the `ToolMessage`s it builds, only `tool_call_id`, so
    that id is the only reliable link back to which tool was called),
    excluding one the gateway itself rejected (policy-denied or
    budget-exhausted -- `content` starts with "POLICY DENIED", see
    `make_policy_gateway`) or one the tool itself reported failing (a JSON
    body with an "error" key, e.g. `read_evidence` on an invented chunk_id --
    see `docket.agent.tools.make_read_evidence_tool`).

    Same matching technique as `query.service._citations_from_agent_
    messages`, generalized from "collect read_evidence's chunk_ids" to "did
    any call to this tool actually succeed" -- used by `should_continue`
    below to gate on a SPECIFIC tool (e.g. `read_evidence`) rather than just
    "any tool call happened at all"."""
    call_name_by_id: dict[str, str] = {}
    for message in messages:
        if isinstance(message, AIMessage):
            for call in getattr(message, "tool_calls", None) or []:
                call_name_by_id[call["id"]] = call["name"]

    for message in messages:
        if not isinstance(message, ToolMessage):
            continue
        if call_name_by_id.get(message.tool_call_id) != tool_name:
            continue
        content = str(message.content)
        if content.startswith("POLICY DENIED"):
            continue
        try:
            payload = json.loads(content)
        except (TypeError, ValueError):
            return True  # non-JSON tool output still counts as a real call
        if isinstance(payload, dict) and "error" in payload:
            continue
        return True
    return False


def build_agent(
    *,
    allowed_tools: dict[str, Any],
    gateway_llm_model: str,
    max_iterations: int,
    max_tool_calls: int,
    num_ctx: int,
    num_predict: int,
    require_tool_call: str | None = None,
):
    """Compile the bounded agent graph for a given tool allow-list and model/
    budget configuration. `allowed_tools` maps tool name -> LangChain tool
    object; the model is bound only to `allowed_tools.values()`.

    `num_ctx`/`num_predict` are passed through explicitly (rather than read
    from `Settings` here) so this stays testable without a settings object --
    `build_investigation_agent` is the one that sources them from
    `Settings.num_ctx`/`Settings.num_predict`.

    `require_tool_call`: see the module docstring. `None` (the default)
    keeps the weaker, tool-agnostic check (`tool_calls_made == 0`, i.e. "was
    ANY allowed tool ever successfully called") -- fine for `build_agent`'s
    own generic construction tests, which use a bare/empty tool set with no
    particular tool name to require."""
    llm = ChatOllama(
        model=gateway_llm_model, temperature=0, num_ctx=num_ctx, num_predict=num_predict
    )
    llm_with_tools = llm.bind_tools(list(allowed_tools.values()))
    gateway_node = make_policy_gateway(allowed_tools, max_tool_calls)

    def call_model(state: AgentState) -> dict:
        response = llm_with_tools.invoke(state["messages"])
        return {"messages": [response], "iterations": state["iterations"] + 1}

    def force_tool_use(state: AgentState) -> dict:
        """Real-data-driven fix (see the module docstring): the eval that
        found this showed the agent sometimes answers -- from the model's
        own parametric knowledge, or from `search_knowledge` snippets alone,
        never actually reading/citing real evidence -- which silently
        defeats the citation-or-abstain contract downstream in
        `query.service._ask_agent` (an uncited answer just gets flagged as a
        warning, not converted to an abstention). `should_continue` routes
        here instead of `end` whenever the model tries to stop before
        satisfying `require_tool_call`, so this node's only job is to push
        back with a corrective `HumanMessage` and loop back to `agent` for
        another attempt -- bounded by `max_iterations` same as every other
        loop through this graph, so a model that keeps refusing still
        terminates (at `end`, with whatever it last said, still subject to
        the existing citation validation) rather than looping forever."""
        if require_tool_call:
            content = (
                f"You answered without a successful {require_tool_call} call. "
                "Every claim in your final answer must be grounded in "
                f"evidence you actually retrieved -- call search_knowledge, "
                f"then call {require_tool_call} on one of its chunk_ids, and "
                "copy its citation_label into your answer, before answering."
            )
        else:
            content = (
                "You answered without calling any tool. Every claim in your "
                "final answer must be grounded in evidence you actually "
                "retrieved -- investigate using the available tools before "
                "answering."
            )
        return {"messages": [HumanMessage(content=content)]}

    def should_continue(state: AgentState) -> str:
        last_msg = state["messages"][-1]
        if state["iterations"] >= max_iterations:
            return "end"
        if isinstance(last_msg, AIMessage) and getattr(last_msg, "tool_calls", None):
            return "gateway"
        if require_tool_call:
            satisfied = _has_successful_call(state["messages"], require_tool_call)
        else:
            satisfied = state["tool_calls_made"] > 0
        if not satisfied:
            return "force_tool_use"
        return "end"

    graph = StateGraph(AgentState)
    graph.add_node("agent", call_model)
    graph.add_node("gateway", gateway_node)
    graph.add_node("force_tool_use", force_tool_use)
    graph.set_entry_point("agent")
    graph.add_conditional_edges(
        "agent",
        should_continue,
        {"gateway": "gateway", "force_tool_use": "force_tool_use", "end": END},
    )
    graph.add_edge("gateway", "agent")
    graph.add_edge("force_tool_use", "agent")
    return graph.compile()


def build_investigation_agent(
    *,
    engine: Any,
    table: Any,
    gateway: InferenceGateway,
    resolver: EvidenceResolver,
    settings: Settings = default_settings,
):
    """Real-use convenience wrapper: builds the real `search_knowledge`/
    `read_evidence` tools bound to `engine`/`table`/`gateway`/`resolver`,
    then compiles a bounded agent graph over them via `build_agent()`,
    using `settings.gen_model`/`max_agent_iterations`/`max_agent_tool_calls`.
    """
    search_knowledge = make_search_knowledge_tool(engine=engine, table=table, gateway=gateway)
    read_evidence = make_read_evidence_tool(resolver=resolver)
    allowed_tools = {"search_knowledge": search_knowledge, "read_evidence": read_evidence}

    return build_agent(
        allowed_tools=allowed_tools,
        gateway_llm_model=settings.gen_model,
        max_iterations=settings.max_agent_iterations,
        max_tool_calls=settings.max_agent_tool_calls,
        num_ctx=settings.num_ctx,
        num_predict=settings.num_predict,
        require_tool_call="read_evidence",
    )
