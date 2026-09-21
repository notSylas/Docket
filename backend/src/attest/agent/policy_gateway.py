"""The Agent Policy Gateway: fail-closed enforcement between the LLM's
tool-call requests and real tool execution.

Generalizes `spike/test_agent_policy_gateway.py`'s `policy_gateway()`
almost exactly as-is -- see `spike/RESULTS.md`'s "Tier 3 — Agent Policy
Gateway" section: that enforcement logic was already proven correct,
including the most important case (Case 3), which forges an out-of-policy
tool call and invokes the gateway directly, bypassing the LLM's own
tool-binding layer entirely, and confirms the disallowed call is rejected
while an allowed call in the same batch still executes.

The only real change here is that `ALLOWED_TOOLS`/`MAX_TOOL_CALLS` are no
longer module-level constants (the spike only ever needed one fixed pair of
fake tools) -- they're per-agent-instance configuration now, since a real
`allowed_tools` dict is built per query session (see
`attest.agent.tools`/`attest.agent.graph.build_investigation_agent`) and
`max_tool_calls` comes from `Settings.max_agent_tool_calls`. `make_policy_gateway`
binds them via closure and returns the gateway node function, which stays a
plain, directly-testable callable -- state in, state-update dict out, no
LLM involved -- so the spike's Case 3 style test (forge a tool_call, call
the gateway function directly) is still possible here.
"""

from __future__ import annotations

from typing import Annotated, Any, Callable, TypedDict

from langchain_core.messages import ToolMessage
from langgraph.graph.message import add_messages


class AgentState(TypedDict):
    messages: Annotated[list, add_messages]
    iterations: int
    tool_calls_made: int
    blocked_calls: list


def make_policy_gateway(
    allowed_tools: dict[str, Any], max_tool_calls: int
) -> Callable[[AgentState], dict]:
    """Returns a `policy_gateway(state) -> dict` node function closed over a
    specific allow-list and tool-call budget.

    Fail-closed enforcement, same behavior as the spike: only tool calls
    whose name is a key in `allowed_tools` may ever execute. Anything else
    (a name the model invents, e.g. `run_shell`) is rejected here -- never
    dispatched to real code -- and the model is told so via a `ToolMessage`.
    Once `tool_calls_made` reaches `max_tool_calls`, further allowed-name
    calls are also rejected (budget exhausted) rather than executed.
    """

    def policy_gateway(state: AgentState) -> dict:
        last_msg = state["messages"][-1]
        tool_messages = []
        blocked = list(state["blocked_calls"])
        calls_made = state["tool_calls_made"]

        for call in getattr(last_msg, "tool_calls", []):
            name = call["name"]
            if name not in allowed_tools:
                blocked.append(name)
                tool_messages.append(
                    ToolMessage(
                        content=f"POLICY DENIED: '{name}' is not an authorized tool.",
                        tool_call_id=call["id"],
                    )
                )
                continue
            if calls_made >= max_tool_calls:
                tool_messages.append(
                    ToolMessage(
                        content="POLICY DENIED: tool-call budget exhausted for this investigation.",
                        tool_call_id=call["id"],
                    )
                )
                continue
            result = allowed_tools[name].invoke(call["args"])
            tool_messages.append(ToolMessage(content=str(result), tool_call_id=call["id"]))
            calls_made += 1

        return {"messages": tool_messages, "tool_calls_made": calls_made, "blocked_calls": blocked}

    return policy_gateway
