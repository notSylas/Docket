"""Confirms `Settings.default_top_k` exists (value 8) and that the six
call sites that used to hardcode `top_k: int = 8` now read it, rather than
each retyping the literal independently."""

from __future__ import annotations

import inspect

import pytest

from docket.agent import graph as agent_graph
from docket.agent import tools as agent_tools
from docket.config import Settings, settings
from docket.eval import runner as eval_runner
from docket.query import service as query_service
from docket.retrieval import hybrid as retrieval_hybrid


def test_default_top_k_setting_defaults_to_eight():
    assert Settings().default_top_k == 8
    assert settings.default_top_k == 8


CALL_SITES = [
    (retrieval_hybrid.hybrid_search, "retrieval.hybrid.hybrid_search"),
    (query_service.QueryService.__init__, "query.service.QueryService.__init__"),
    (agent_graph.build_investigation_agent, "agent.graph.build_investigation_agent"),
    (agent_tools.make_search_knowledge_tool, "agent.tools.make_search_knowledge_tool"),
    (eval_runner.EvalRunner.__init__, "eval.runner.EvalRunner.__init__"),
    (eval_runner.run_eval, "eval.runner.run_eval"),
]


@pytest.mark.parametrize("target, name", CALL_SITES, ids=[name for _, name in CALL_SITES])
def test_top_k_default_is_settings_default_top_k_not_a_bare_literal(target, name):
    default = inspect.signature(target).parameters["top_k"].default
    assert default == settings.default_top_k
    # Guard against the source coincidentally retyping "= 8" instead of
    # actually reading the shared setting.
    source = inspect.getsource(target)
    assert "top_k: int = 8" not in source
    assert "default_top_k" in source
