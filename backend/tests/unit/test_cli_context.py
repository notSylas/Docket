"""Unit tests for `docket.cli.context.AppContext` -- specifically
`page_table_for_query`, the read-side wiring visual retrieval checkpoint 3
adds for query-time access to the `pages` LanceDB table.

Mirrors the off-by-default rigor `test_ingestion_pipeline.py` already
established for the ingestion side (`test_page_images_disabled_by_default_
no_vlm_or_index_calls`): with `settings.visual_index_enabled` at its default
(`False`), a real query call must never open/query the `pages` table.
"""

from __future__ import annotations

from docket.interfaces.cli.context import AppContext


def test_page_table_for_query_disabled_by_default_never_touches_visual_index_writer(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("DOCKET_DATA_DIR", str(tmp_path / "data"))
    context = AppContext()

    assert context.settings.visual_index_enabled is False
    assert context.page_table_for_query is None

    # `visual_index_writer` is a `functools.cached_property` -- it only
    # appears in the instance `__dict__` once actually accessed (which
    # constructs a `LancePageIndexWriter`, i.e. connects to the LanceDB dir
    # and, on any `.table` access, opens/queries the `pages` table). Its
    # absence here is direct proof that `page_table_for_query` never
    # touched it while the flag is off -- not just an assertion about the
    # return value.
    assert "visual_index_writer" not in context.__dict__


def test_page_table_for_query_enabled_does_access_visual_index_writer(
    tmp_path, monkeypatch
) -> None:
    """Sanity check for the other branch: once the flag is on,
    `page_table_for_query` does go through `visual_index_writer` (and
    returns `None` gracefully here only because nothing has been ingested
    with visual indexing yet -- the `pages` table doesn't exist)."""
    monkeypatch.setenv("DOCKET_DATA_DIR", str(tmp_path / "data"))
    context = AppContext()
    context.settings.visual_index_enabled = True

    assert context.page_table_for_query is None
    assert "visual_index_writer" in context.__dict__


# -- for_testing -------------------------------------------------------------
#
# The public seam `eval/runner.py`'s `EvalRunner` uses instead of the old
# `_EvalContext(AppContext)` subclass, which reached into `AppContext`'s
# private `_ensure_schema` (see Phase 5 of the restructuring plan).


class _FakeGateway:
    def __init__(self, gen_model: str) -> None:
        self.gen_model = gen_model


class _FakeWrappingGateway:
    """Mirrors `eval/runner.py`'s `RecordingGateway`: no `.gen_model` of its
    own, but exposes the real one via `.inner`."""

    def __init__(self, inner: _FakeGateway) -> None:
        self.inner = inner


def test_for_testing_ignores_docket_data_dir_env_var(tmp_path, monkeypatch) -> None:
    """Unlike the real constructor, `for_testing` must not construct its own
    `Settings()` from the environment -- `data_dir` is the only source of
    truth, so a stray `DOCKET_DATA_DIR` (e.g. left over from another test,
    or a developer's shell) can't make it silently touch the wrong dir."""
    monkeypatch.setenv("DOCKET_DATA_DIR", str(tmp_path / "not-this-one"))
    pinned_dir = tmp_path / "pinned"

    context = AppContext.for_testing(data_dir=pinned_dir)

    assert context.settings.data_dir == pinned_dir
    assert pinned_dir.exists()  # ensure_data_dirs() ran
    assert context.engine is not None  # ensure_schema() ran without error


def test_for_testing_seeds_gateway_and_derives_gen_model(tmp_path) -> None:
    gateway = _FakeGateway(gen_model="fake-model:1b")

    context = AppContext.for_testing(data_dir=tmp_path / "data", gateway=gateway)

    # cached_property stores into the instance dict -- its presence here
    # (without ever calling `context.gateway`) is direct proof the real
    # OllamaGateway construction was replaced, not just that the getter
    # would return the right thing.
    assert context.__dict__["gateway"] is gateway
    assert context.settings.gen_model == "fake-model:1b"


def test_for_testing_derives_gen_model_through_inner_wrapper(tmp_path) -> None:
    """`RecordingGateway`-style wrappers (no `.gen_model` of their own, a
    `.inner` that has one) must still steer `Settings.gen_model`."""
    wrapped = _FakeWrappingGateway(_FakeGateway(gen_model="wrapped-model:7b"))

    context = AppContext.for_testing(data_dir=tmp_path / "data", gateway=wrapped)

    assert context.__dict__["gateway"] is wrapped
    assert context.settings.gen_model == "wrapped-model:7b"


def test_for_testing_without_gateway_or_parser_leaves_them_lazy(tmp_path) -> None:
    """No gateway/parser passed -- both stay unset in `__dict__` so the
    normal `cached_property` (real `OllamaGateway`/`DoclingParser`) would
    still build on first access, exactly like the real constructor."""
    context = AppContext.for_testing(data_dir=tmp_path / "data")

    assert "gateway" not in context.__dict__
    assert "parser" not in context.__dict__


def test_for_testing_seeds_parser_when_given(tmp_path) -> None:
    sentinel_parser = object()

    context = AppContext.for_testing(data_dir=tmp_path / "data", parser=sentinel_parser)

    assert context.__dict__["parser"] is sentinel_parser
