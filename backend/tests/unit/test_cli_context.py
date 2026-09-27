"""Unit tests for `docket.cli.context.AppContext` -- specifically
`page_table_for_query`, the read-side wiring visual retrieval checkpoint 3
adds for query-time access to the `pages` LanceDB table.

Mirrors the off-by-default rigor `test_ingestion_pipeline.py` already
established for the ingestion side (`test_page_images_disabled_by_default_
no_vlm_or_index_calls`): with `settings.visual_index_enabled` at its default
(`False`), a real query call must never open/query the `pages` table.
"""

from __future__ import annotations

from docket.cli.context import AppContext


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
