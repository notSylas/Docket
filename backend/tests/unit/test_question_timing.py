"""Exclusive nested and overlapping spans, including copied agent contexts."""
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context

import pytest

from docket.services.query import timing


def test_overlapping_agent_spans_count_wall_time_once():
    recorder = timing.QuestionTiming()
    recorder.started = 0.0
    recorder.spans = [
        (1.0, 9.0, "citation_validation", 1),
        (2.0, 5.0, "generation", 2),
        (3.0, 6.0, "generation", 2),
    ]
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(timing.time, "perf_counter", lambda: 10.0)
        phases, total = recorder.finish()
    assert total == 10
    assert phases["generation"] == 4
    assert phases["citation_validation"] == 4
    assert phases["overhead"] == 2
    assert sum(phases.values()) == total


def test_agent_worker_context_records_phases_in_its_own_question():
    recorder = timing.QuestionTiming()
    token = timing._timing.set(recorder)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            @timing.timed_phase("search")
            def search():
                return "fake search"
            assert pool.submit(copy_context().run, search).result(timeout=2) == "fake search"
        phases, total = recorder.finish()
        assert phases["search"] > 0
        assert sum(phases.values()) == pytest.approx(total)
    finally:
        timing._timing.reset(token)
    with timing.phase("generation"):
        pass
    assert len(recorder.spans) == 1
