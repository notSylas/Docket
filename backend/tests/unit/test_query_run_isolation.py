"""Concurrent `QueryService.ask()` calls must not share per-run state
(follow-up rewrite / file scope) -- doc 06 section 11 step 3."""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool

from docket.core.config import settings as default_settings
from docket.infra.inference.gateway import FakeInferenceGateway
from docket.services.agent import graph as graph_mod
from docket.services.agent.graph import build_agent
from docket.services.agent.run_context import (
    RunContext,
    bind_run_context,
    current_extra_queries,
    current_scope_ids,
)
from docket.services.query.classifier import QueryMode
from docket.services.query.prompts import ABSTENTION_PHRASE
from docket.services.query.service import QueryService

N = 4


class _BarrierAgent:
    """Fake agent: every concurrent run waits at a barrier (so all runs are
    in flight together), then records what the tool providers see."""

    def __init__(self, barrier: threading.Barrier) -> None:
        self._barrier = barrier
        self.seen: dict[str, tuple[list[str], list[str]]] = {}

    def invoke(self, state: dict, config: dict | None = None) -> dict:
        question = state["messages"][-1].content
        self._barrier.wait(timeout=10)
        seen = (list(current_extra_queries()), list(current_scope_ids()))
        self._barrier.wait(timeout=10)  # all have read; none has finished
        self.seen[question] = seen
        return {
            "messages": [AIMessage(content=ABSTENTION_PHRASE)],
            "iterations": 1, "tool_calls_made": 1, "blocked_calls": [],
        }


def test_concurrent_asks_do_not_see_each_others_run_state() -> None:
    agent = _BarrierAgent(threading.Barrier(N))
    settings = default_settings.model_copy(update={"query_scope_enabled": True})
    service = QueryService(
        engine=None, table=None, gateway=FakeInferenceGateway(), resolver=SimpleNamespace(resolve_many=lambda ids: []),
        settings=settings, agent=agent,
    )
    service._rewrite_followup = lambda q, h: f"standalone-{q}"  # type: ignore[method-assign]
    service._query_signals = lambda q, s, h: SimpleNamespace(  # type: ignore[method-assign]
        scope_version_ids=[f"ev-{q}"], scope_files=[f"file-{q}"]
    )
    errors: list[BaseException] = []

    def run(i: int) -> None:
        try:
            service.ask(f"q{i}", mode=QueryMode.AGENT)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(N)]
    [t.start() for t in threads]
    [t.join(timeout=30) for t in threads]

    assert not errors, errors
    assert len(agent.seen) == N
    for i in range(N):
        assert agent.seen[f"q{i}"] == ([f"standalone-q{i}"], [f"ev-q{i}"])
    # Nothing leaks onto the shared instance or into the calling context.
    assert not [a for a in vars(service) if a.startswith("_run_")]
    assert current_extra_queries() == [] and current_scope_ids() == []


def test_providers_reach_graph_tool_nodes_per_thread(mocker) -> None:
    """The real compiled graph executes tools under the caller's context, so a
    tool built once still sees only its own run's context."""
    barrier = threading.Barrier(2)

    @tool
    def search_knowledge(query: str) -> str:
        """Fake search that reports the active run's state."""
        barrier.wait(timeout=10)
        return json.dumps({"extra": current_extra_queries(), "scope": current_scope_ids()})

    mock_chat = mocker.patch.object(graph_mod, "ChatOllama")
    agent = build_agent(
        allowed_tools={"search_knowledge": search_knowledge}, gateway_llm_model="m",
        max_iterations=3, max_tool_calls=4, num_ctx=1024, num_predict=256,
    )
    mock_chat.return_value.bind_tools.return_value.invoke.side_effect = lambda msgs: (
        AIMessage(content="done") if any(m.type == "tool" for m in msgs)
        else AIMessage(content="", tool_calls=[
            {"name": "search_knowledge", "args": {"query": "x"}, "id": "c1"}])
    )
    out: dict[int, str] = {}

    def run(i: int) -> None:
        with bind_run_context(RunContext(extra_queries=[f"e{i}"], scope_ids=[f"s{i}"])):
            state = agent.invoke({
                "messages": [HumanMessage(content="q")], "iterations": 0,
                "tool_calls_made": 0, "blocked_calls": [],
            })
        out[i] = next(m.content for m in state["messages"] if m.type == "tool")

    threads = [threading.Thread(target=run, args=(i,)) for i in range(2)]
    [t.start() for t in threads]
    [t.join(timeout=30) for t in threads]

    for i in range(2):
        assert json.loads(out[i]) == {"extra": [f"e{i}"], "scope": [f"s{i}"]}
