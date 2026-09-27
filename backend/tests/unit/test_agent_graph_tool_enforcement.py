"""Unit tests for the tool-use-enforcement fix in `docket.agent.graph.build_agent`.

Root cause this covers (found via a real 93-run Physics eval, not
speculative): once a question got misrouted to the agent path, the original
graph let the model answer on its very first turn with zero tool calls --
i.e. from its own parametric/training knowledge instead of the corpus --
which silently defeated the citation-or-abstain contract (an uncited answer
was only ever flagged as a warning downstream in `query.service._ask_agent`,
never converted to an abstention).

The fix has two layers, both exercised here:

1. `require_tool_call=None` (generic): `should_continue` forces a retry
   (`force_tool_use`) unless AT LEAST ONE allowed tool has ever been
   successfully called (`tool_calls_made > 0`).
2. `require_tool_call="read_evidence"` (what `build_investigation_agent`
   actually uses): re-running the real eval after (1) alone showed the model
   satisfying it by calling `search_knowledge` -- which returns bare
   chunk_ids, no citation material -- and then STILL answering without ever
   calling `read_evidence`, so `require_tool_call` gates on one SPECIFIC
   tool having succeeded, not just "any tool", via `_has_successful_call`
   scanning the message trace for a non-error `read_evidence` result.

`ChatOllama` is mocked out (same pattern as
`test_agent_graph_construction.py`) with a scripted sequence of responses, so
these run with no real Ollama/GPU dependency, unlike
`tests/integration/test_agent_graph.py`.
"""

from __future__ import annotations

import json

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool

from docket.agent import graph as graph_mod
from docket.agent.graph import build_agent


@tool
def search_knowledge(query: str) -> str:
    """Fake allowed tool for graph tests."""
    return json.dumps({"results": [{"chunk_id": "chk_1", "score": 0.9}]})


@tool
def read_evidence(chunk_id: str) -> str:
    """Fake allowed tool for graph tests."""
    if chunk_id == "bad_id":
        return json.dumps({"error": f"chunk not found: {chunk_id}"})
    return json.dumps(
        {
            "chunk_id": chunk_id,
            "text": "RRF sums 1/(k+rank) scores.",
            "citation_label": "[doc.pdf #chk_1]",
            "source_display_name": "doc.pdf",
            "heading": None,
        }
    )


ALLOWED_TOOLS = {"search_knowledge": search_knowledge, "read_evidence": read_evidence}


def _build(mocker, *, max_iterations: int = 3, require_tool_call: str | None = None):
    mock_chat_ollama = mocker.patch.object(graph_mod, "ChatOllama")
    agent = build_agent(
        allowed_tools=ALLOWED_TOOLS,
        gateway_llm_model="qwen3:14b",
        max_iterations=max_iterations,
        max_tool_calls=8,
        num_ctx=8192,
        num_predict=4096,
        require_tool_call=require_tool_call,
    )
    mock_bound_llm = mock_chat_ollama.return_value.bind_tools.return_value
    return agent, mock_bound_llm


def _initial_state() -> dict:
    return {
        "messages": [HumanMessage(content="What does Reciprocal Rank Fusion do?")],
        "iterations": 0,
        "tool_calls_made": 0,
        "blocked_calls": [],
    }


def _tool_call(name: str, args: dict, call_id: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


def _nudges(messages: list) -> list:
    return [m for m in messages if isinstance(m, HumanMessage) and "without" in str(m.content)]


# ---------------------------------------------------------------------------
# Generic mode (require_tool_call=None): any successful tool call satisfies.
# ---------------------------------------------------------------------------


def test_answering_with_zero_tool_calls_on_first_turn_is_forced_to_retry(mocker) -> None:
    """The exact bug shape from the real eval: the model's first response is
    a confident, uncited final answer with no tool_calls at all. The old
    graph would `end` right there. The fixed graph must instead loop back
    through `force_tool_use` and give the model another chance, and only
    accept a final answer once it has actually called a tool."""
    agent, mock_bound_llm = _build(mocker)

    hallucinated_answer = AIMessage(content="It's the moving-rod emf formula.", tool_calls=[])
    grounded_answer = AIMessage(content="RRF sums 1/(k+rank) scores. [chunk_1]", tool_calls=[])

    mock_bound_llm.invoke.side_effect = [
        hallucinated_answer,
        _tool_call("search_knowledge", {"query": "RRF"}, "call-1"),
        grounded_answer,
    ]

    result = agent.invoke(_initial_state(), config={"recursion_limit": 50})

    assert result["tool_calls_made"] == 1
    assert result["blocked_calls"] == []
    assert result["messages"][-1].content == "RRF sums 1/(k+rank) scores. [chunk_1]"
    assert len(_nudges(result["messages"])) == 1
    # ChatOllama was invoked a third time only because the first attempt was
    # rejected -- confirms the retry actually happened.
    assert mock_bound_llm.invoke.call_count == 3


def test_model_that_never_calls_a_tool_still_terminates_at_max_iterations(mocker) -> None:
    """Adversarial/uncooperative case: the model keeps answering without
    tools even after being nudged. This must NOT loop forever -- it must
    still stop at `max_iterations`, same bound as every other path through
    this graph, just later than turn 1 instead of never stopping."""
    agent, mock_bound_llm = _build(mocker, max_iterations=2)

    # Two distinct AIMessage instances, not the same object reused twice --
    # LangGraph's `add_messages` reducer dedups/merges by `.id`, and reusing
    # one object (same auto-assigned id) for both turns would make the
    # second "response" collapse onto the first instead of appending.
    mock_bound_llm.invoke.side_effect = [
        AIMessage(content="I already know the answer.", tool_calls=[]),
        AIMessage(content="I still already know the answer.", tool_calls=[]),
    ]

    result = agent.invoke(_initial_state(), config={"recursion_limit": 50})

    assert result["iterations"] == 2
    assert result["tool_calls_made"] == 0
    assert mock_bound_llm.invoke.call_count == 2
    assert result["messages"][-1].content == "I still already know the answer."


def test_model_that_calls_a_tool_on_the_first_turn_is_unaffected(mocker) -> None:
    """Regression guard: the common/already-working case (model calls a tool
    right away) must not be touched by the new enforcement path -- no nudge,
    no extra LLM calls."""
    agent, mock_bound_llm = _build(mocker)

    grounded_answer = AIMessage(content="RRF sums 1/(k+rank) scores. [chunk_1]", tool_calls=[])
    mock_bound_llm.invoke.side_effect = [
        _tool_call("search_knowledge", {"query": "RRF"}, "call-1"),
        grounded_answer,
    ]

    result = agent.invoke(_initial_state(), config={"recursion_limit": 50})

    assert result["tool_calls_made"] == 1
    assert mock_bound_llm.invoke.call_count == 2
    assert _nudges(result["messages"]) == []


# ---------------------------------------------------------------------------
# require_tool_call="read_evidence" (what build_investigation_agent uses):
# a specific tool must succeed, not just any tool.
# ---------------------------------------------------------------------------


def test_search_knowledge_alone_does_not_satisfy_a_read_evidence_requirement(mocker) -> None:
    """The real-eval regression this tightening fixes: the model calls
    search_knowledge (a real, successful tool call -- satisfies the generic
    check) and then answers directly from the snippets, never calling
    read_evidence, so it never has a real citation_label to cite. With
    require_tool_call="read_evidence", this must be forced to retry, not
    accepted."""
    # max_iterations=5: the scripted sequence needs 4 full agent turns
    # (search_knowledge call, premature answer, read_evidence call, final
    # answer) -- the default of 3 (this test's `_build` default) would clip
    # the run before the forced read_evidence call ever executes.
    agent, mock_bound_llm = _build(mocker, max_iterations=5, require_tool_call="read_evidence")

    premature_answer = AIMessage(
        content="It's roughly ε = Blv, supported by multiple sources [1]", tool_calls=[]
    )
    grounded_answer = AIMessage(
        content="ε = Blv [doc.pdf #chk_1]", tool_calls=[]
    )

    mock_bound_llm.invoke.side_effect = [
        _tool_call("search_knowledge", {"query": "induced emf"}, "call-1"),
        premature_answer,
        _tool_call("read_evidence", {"chunk_id": "chk_1"}, "call-2"),
        grounded_answer,
    ]

    result = agent.invoke(_initial_state(), config={"recursion_limit": 50})

    assert result["tool_calls_made"] == 2  # search_knowledge + read_evidence
    assert result["messages"][-1].content == "ε = Blv [doc.pdf #chk_1]"
    # Exactly one nudge -- the premature answer after search_knowledge alone
    # was rejected, forcing the read_evidence call that then satisfied it.
    assert len(_nudges(result["messages"])) == 1
    assert mock_bound_llm.invoke.call_count == 4


def test_a_failed_read_evidence_call_does_not_satisfy_the_requirement(mocker) -> None:
    """A read_evidence call that executes but returns a tool-reported error
    (invented/mistyped chunk_id -- see make_read_evidence_tool) must NOT
    count as satisfying the requirement, even though `tool_calls_made` was
    incremented (the gateway counts any dispatched call, success or not) --
    `_has_successful_call` must look at the actual payload, not just the
    call count."""
    # max_iterations=5 for the same reason as the test above -- 4 full
    # agent turns are scripted here too.
    agent, mock_bound_llm = _build(mocker, max_iterations=5, require_tool_call="read_evidence")

    grounded_answer = AIMessage(content="ε = Blv [doc.pdf #chk_1]", tool_calls=[])
    mock_bound_llm.invoke.side_effect = [
        _tool_call("read_evidence", {"chunk_id": "bad_id"}, "call-1"),
        AIMessage(content="I'll answer anyway.", tool_calls=[]),
        _tool_call("read_evidence", {"chunk_id": "chk_1"}, "call-2"),
        grounded_answer,
    ]

    result = agent.invoke(_initial_state(), config={"recursion_limit": 50})

    assert result["tool_calls_made"] == 2  # both dispatched: one errored, one real
    assert result["messages"][-1].content == "ε = Blv [doc.pdf #chk_1]"
    assert len(_nudges(result["messages"])) == 1


def test_read_evidence_on_the_first_attempt_needs_no_nudge(mocker) -> None:
    """Regression guard: when the model does the right thing immediately
    (calls read_evidence, then answers with a real citation), the stricter
    requirement adds no extra turns."""
    agent, mock_bound_llm = _build(mocker, require_tool_call="read_evidence")

    grounded_answer = AIMessage(content="ε = Blv [doc.pdf #chk_1]", tool_calls=[])
    mock_bound_llm.invoke.side_effect = [
        _tool_call("read_evidence", {"chunk_id": "chk_1"}, "call-1"),
        grounded_answer,
    ]

    result = agent.invoke(_initial_state(), config={"recursion_limit": 50})

    assert result["tool_calls_made"] == 1
    assert mock_bound_llm.invoke.call_count == 2
    assert _nudges(result["messages"]) == []


def test_model_trace_captures_input_tools_output_and_usage(mocker):
    mock_chat = mocker.patch.object(graph_mod, "ChatOllama")
    mock_chat.return_value.bind_tools.return_value.invoke.return_value = AIMessage(
        content="Uncited answer", response_metadata={"prompt_eval_count": 120, "eval_count": 8})
    traces = []
    agent = build_agent(allowed_tools=ALLOWED_TOOLS, gateway_llm_model="fixture",
                        max_iterations=1, max_tool_calls=2, num_ctx=8192, num_predict=4096,
                        require_tool_call="read_evidence", trace_callback=traces.append)
    agent.invoke(_initial_state())
    (call,) = traces
    assert call["messages"][0].content == _initial_state()["messages"][0].content
    assert call["response"].response_metadata["eval_count"] == 8
    assert {tool["name"] for tool in call["tools"]} == set(ALLOWED_TOOLS)
