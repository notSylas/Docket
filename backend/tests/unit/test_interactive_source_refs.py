"""Classic prompt: /ingest /remove /reconnect accept names and list numbers;
/add reports a folder already covered by a source."""

from __future__ import annotations

from test_interactive import ctx, drive  # noqa: F401  (fixture + helper)


def test_remove_by_name_and_number_and_reconnect_by_name(ctx, tmp_path):
    folder = tmp_path / "Reports"
    folder.mkdir()
    ctx.source_manager.register_source(folder)

    out, _ = drive(ctx, ["/remove reports", "y", "/sources"])
    assert "Removed src_" in out
    assert "disconnected" in out or "revoked" in out

    out, _ = drive(ctx, ["/reconnect 1"])
    assert "Reconnected src_" in out


def test_ingest_by_name_and_ambiguous_name_lists_candidates(ctx, tmp_path):
    for parent in ("x", "y"):
        (tmp_path / parent / "docs").mkdir(parents=True)
        ctx.source_manager.register_source(tmp_path / parent / "docs")
    out, _ = drive(ctx, ["/ingest docs"])
    assert "matches 2 sources" in out
    assert "docs (x)" in out and "docs (y)" in out

    out, _ = drive(ctx, ["/ingest 2"])
    assert "1 ingested" in out


def test_sources_table_shows_number_and_name(ctx, tmp_path):
    folder = tmp_path / "Reports"
    folder.mkdir()
    ctx.source_manager.register_source(folder)
    out, _ = drive(ctx, ["/sources"])
    header = next(ln for ln in out.splitlines() if "Name" in ln)
    assert "#" in header and "Status" in header
    assert "Reports" in out


def test_add_inside_existing_source_says_already_covered(ctx, tmp_path):
    parent = tmp_path / "work"
    (parent / "inner").mkdir(parents=True)
    ctx.source_manager.register_source(parent, display_name="Work")
    out, _ = drive(ctx, [f'/add "{parent / "inner"}"'])
    assert "Already covered by Work" in out
    assert len(ctx.source_manager.list_sources()) == 1
