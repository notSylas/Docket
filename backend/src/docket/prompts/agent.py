"""Model-facing text for the bounded LangGraph investigation agent.

Covers three previously-inline sources:
- `agent/graph.py`'s `force_tool_use` node built two corrective
  `HumanMessage` strings inline; promoted here to
  `missing_required_tool_message`/`missing_any_tool_message`.
- `agent/tools.py`'s `search_knowledge`/`read_evidence` tool descriptions
  lived as the tool functions' own docstrings; promoted here to
  `SEARCH_KNOWLEDGE_DESCRIPTION`/`READ_EVIDENCE_DESCRIPTION`, passed via
  `@tool(description=...)` (confirmed: `langchain_core.tools.tool`'s
  `description` kwarg takes precedence over the function's docstring).
- `agent/policy_gateway.py`'s `ToolMessage` denial strings; promoted here
  to `policy_denied_tool`/`policy_denied_budget`.
"""

from __future__ import annotations

SEARCH_KNOWLEDGE_DESCRIPTION = (
    "Search the local evidence index for chunks relevant to a query. "
    "Returns a JSON list of {chunk_id, score} results, best match first. "
    "Call read_evidence with a chunk_id to see its full text."
)

READ_EVIDENCE_DESCRIPTION = (
    "Read the full text of a specific evidence chunk by its id (as "
    "returned by search_knowledge). Returns JSON with the chunk's id, "
    "text, citation_label, source_display_name, and heading."
)


def missing_required_tool_message(require_tool_call: str) -> str:
    """Corrective `HumanMessage` text when the model tries to stop before a
    successful call to `require_tool_call` (see
    `agent.graph.build_agent`'s `force_tool_use` node)."""
    return (
        f"You answered without a successful {require_tool_call} call. "
        "Every claim in your final answer must be grounded in "
        f"evidence you actually retrieved -- call search_knowledge, "
        f"then call {require_tool_call} on one of its chunk_ids, and "
        "copy its citation_label into your answer, before answering."
    )


def missing_any_tool_message() -> str:
    """Corrective `HumanMessage` text when the model tries to stop without
    ever calling any tool (see `agent.graph.build_agent`'s
    `force_tool_use` node)."""
    return (
        "You answered without calling any tool. Every claim in your "
        "final answer must be grounded in evidence you actually "
        "retrieved -- investigate using the available tools before "
        "answering."
    )


def policy_denied_tool(name: str) -> str:
    """`ToolMessage` content for a tool call whose name isn't in the
    allow-list (see `agent.policy_gateway.make_policy_gateway`)."""
    return f"POLICY DENIED: '{name}' is not an authorized tool."


def policy_denied_budget() -> str:
    """`ToolMessage` content once `max_tool_calls` has been reached for
    this investigation (see `agent.policy_gateway.make_policy_gateway`)."""
    return "POLICY DENIED: tool-call budget exhausted for this investigation."
