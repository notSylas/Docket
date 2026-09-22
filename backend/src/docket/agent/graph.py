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
  calls and iterations remain, `end` once `max_iterations` is hit or the
  model stops calling tools

`build_investigation_agent()` is the real-use convenience wrapper: it builds
the real `search_knowledge`/`read_evidence` tools (via `docket.agent.tools`)
bound to a caller-supplied engine/table/gateway/resolver, and assembles them
into a graph via `build_agent()`, pulling its model/iteration/call-budget
config from `Settings` (`gen_model`, `max_agent_iterations`,
`max_agent_tool_calls`) instead of the spike's module-level constants. This
is the function a later milestone's Agent Runtime Manager / QueryService
routing would call to hand off a question to the bounded-investigation path
-- not wired into the CLI yet (routing fast-path-vs-agent-path is out of
scope here, per CP7's design note in `docket.query.service`), but it must
work end-to-end when called directly.
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage
from langchain_ollama import ChatOllama
from langgraph.graph import END, StateGraph

from docket.agent.policy_gateway import AgentState, make_policy_gateway
from docket.agent.tools import make_read_evidence_tool, make_search_knowledge_tool
from docket.config import Settings, settings as default_settings
from docket.inference.gateway import InferenceGateway
from docket.retrieval.resolver import EvidenceResolver


def build_agent(
    *,
    allowed_tools: dict[str, Any],
    gateway_llm_model: str,
    max_iterations: int,
    max_tool_calls: int,
):
    """Compile the bounded agent graph for a given tool allow-list and model/
    budget configuration. `allowed_tools` maps tool name -> LangChain tool
    object; the model is bound only to `allowed_tools.values()`."""
    llm = ChatOllama(model=gateway_llm_model, temperature=0)
    llm_with_tools = llm.bind_tools(list(allowed_tools.values()))
    gateway_node = make_policy_gateway(allowed_tools, max_tool_calls)

    def call_model(state: AgentState) -> dict:
        response = llm_with_tools.invoke(state["messages"])
        return {"messages": [response], "iterations": state["iterations"] + 1}

    def should_continue(state: AgentState) -> str:
        last_msg = state["messages"][-1]
        if state["iterations"] >= max_iterations:
            return "end"
        if isinstance(last_msg, AIMessage) and getattr(last_msg, "tool_calls", None):
            return "gateway"
        return "end"

    graph = StateGraph(AgentState)
    graph.add_node("agent", call_model)
    graph.add_node("gateway", gateway_node)
    graph.set_entry_point("agent")
    graph.add_conditional_edges("agent", should_continue, {"gateway": "gateway", "end": END})
    graph.add_edge("gateway", "agent")
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
    )
