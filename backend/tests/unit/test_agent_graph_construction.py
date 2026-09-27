"""Unit tests for `docket.agent.graph` GRAPH CONSTRUCTION only -- specifically
that `num_ctx`/`num_predict` (M1, context-window fix) reach `ChatOllama`.

`ChatOllama` is mocked out (`mocker.patch.object`) so these run with no real
Ollama/GPU dependency, unlike `tests/integration/test_agent_graph.py`, which
covers the agent LOOP actually running against a real model.
`engine`/`table`/`gateway`/`resolver` in `build_investigation_agent` are only
closed over by the tool factories (see `docket.agent.tools`), never called at
construction time, so plain placeholders are fine here.
"""

from __future__ import annotations

from docket.agent import graph as graph_mod
from docket.agent.graph import build_agent, build_investigation_agent
from docket.core.config import Settings


def test_build_agent_passes_num_ctx_and_num_predict_to_chat_ollama(mocker) -> None:
    mock_chat_ollama = mocker.patch.object(graph_mod, "ChatOllama")

    build_agent(
        allowed_tools={},
        gateway_llm_model="qwen3:14b",
        max_iterations=3,
        max_tool_calls=8,
        num_ctx=8192,
        num_predict=4096,
    )

    _, kwargs = mock_chat_ollama.call_args
    assert kwargs["model"] == "qwen3:14b"
    assert kwargs["num_ctx"] == 8192
    assert kwargs["num_predict"] == 4096


def test_build_investigation_agent_sources_num_ctx_and_num_predict_from_settings(
    mocker,
) -> None:
    mock_chat_ollama = mocker.patch.object(graph_mod, "ChatOllama")
    custom_settings = Settings(num_ctx=16384, num_predict=2048)

    build_investigation_agent(
        engine=object(),
        table=object(),
        gateway=object(),
        resolver=object(),
        settings=custom_settings,
    )

    _, kwargs = mock_chat_ollama.call_args
    assert kwargs["model"] == custom_settings.gen_model
    assert kwargs["num_ctx"] == 16384
    assert kwargs["num_predict"] == 2048
