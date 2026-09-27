"""Unit tests for `docket.services.agent.policy_gateway.make_policy_gateway`.

Ported almost directly from `spike/test_agent_policy_gateway.py`'s Case 3 --
see `spike/RESULTS.md`'s "Tier 3 — Agent Policy Gateway" section. These are
the most important tests in this checkpoint: they forge tool_calls and
invoke the gateway function directly, bypassing the LLM/tool-binding layer
entirely, proving the gateway's own fail-closed enforcement logic is sound
independent of whether the model cooperates. No Ollama/LLM involved -- pure
function tests, fast, not marked `integration`.
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.tools import tool

from docket.services.agent.policy_gateway import make_policy_gateway


@tool
def search_knowledge(query: str) -> str:
    """Fake allowed tool for gateway tests."""
    return f"results for: {query}"


@tool
def read_evidence(chunk_id: str) -> str:
    """Fake allowed tool for gateway tests."""
    return f"text of {chunk_id}"


ALLOWED_TOOLS = {"search_knowledge": search_knowledge, "read_evidence": read_evidence}


def _state(tool_calls: list[dict], *, tool_calls_made: int = 0) -> dict:
    return {
        "messages": [
            SystemMessage(content="test"),
            HumanMessage(content="test"),
            AIMessage(content="", tool_calls=tool_calls),
        ],
        "iterations": 0,
        "tool_calls_made": tool_calls_made,
        "blocked_calls": [],
    }


# ---------------------------------------------------------------------------
# Case 3 (spike): forged out-of-policy tool_call, bypassing the LLM entirely.
# ---------------------------------------------------------------------------


def test_disallowed_tool_rejected_and_allowed_tool_in_same_batch_still_executes() -> None:
    gateway = make_policy_gateway(ALLOWED_TOOLS, max_tool_calls=8)
    state = _state(
        [
            {"name": "run_shell", "args": {"cmd": "rm -rf /"}, "id": "forged-1"},
            {"name": "search_knowledge", "args": {"query": "test"}, "id": "forged-2"},
        ]
    )

    result = gateway(state)

    assert result["blocked_calls"] == ["run_shell"]
    assert result["tool_calls_made"] == 1

    denied_msg, executed_msg = result["messages"]
    assert denied_msg.tool_call_id == "forged-1"
    assert "not an authorized tool" in denied_msg.content
    assert executed_msg.tool_call_id == "forged-2"
    assert "results for: test" in executed_msg.content


def test_unknown_tool_name_never_dispatched() -> None:
    gateway = make_policy_gateway(ALLOWED_TOOLS, max_tool_calls=8)
    state = _state([{"name": "delete_source", "args": {}, "id": "forged-1"}])

    result = gateway(state)

    assert result["blocked_calls"] == ["delete_source"]
    assert result["tool_calls_made"] == 0
    assert "not an authorized tool" in result["messages"][0].content


# ---------------------------------------------------------------------------
# Tool-call budget exhaustion.
# ---------------------------------------------------------------------------


def test_budget_exhaustion_rejects_calls_beyond_the_cap() -> None:
    gateway = make_policy_gateway(ALLOWED_TOOLS, max_tool_calls=2)
    state = _state(
        [
            {"name": "search_knowledge", "args": {"query": "a"}, "id": "c1"},
            {"name": "search_knowledge", "args": {"query": "b"}, "id": "c2"},
            {"name": "search_knowledge", "args": {"query": "c"}, "id": "c3"},
        ]
    )

    result = gateway(state)

    assert result["tool_calls_made"] == 2
    c1, c2, c3 = result["messages"]
    assert "results for: a" in c1.content
    assert "results for: b" in c2.content
    assert "budget exhausted" in c3.content
    # A budget-exhausted rejection is not a policy-violation block -- the
    # tool itself was allowed, it just ran out of budget.
    assert result["blocked_calls"] == []


def test_budget_already_exhausted_from_prior_iterations_rejects_everything() -> None:
    gateway = make_policy_gateway(ALLOWED_TOOLS, max_tool_calls=2)
    state = _state(
        [{"name": "search_knowledge", "args": {"query": "a"}, "id": "c1"}],
        tool_calls_made=2,
    )

    result = gateway(state)

    assert result["tool_calls_made"] == 2
    assert "budget exhausted" in result["messages"][0].content


def test_no_tool_calls_on_last_message_returns_empty_update() -> None:
    gateway = make_policy_gateway(ALLOWED_TOOLS, max_tool_calls=8)
    state = {
        "messages": [SystemMessage(content="test"), AIMessage(content="just text, no tools")],
        "iterations": 0,
        "tool_calls_made": 0,
        "blocked_calls": [],
    }

    result = gateway(state)

    assert result["messages"] == []
    assert result["tool_calls_made"] == 0
    assert result["blocked_calls"] == []
