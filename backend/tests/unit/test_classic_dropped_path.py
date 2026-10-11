"""CP7 in the classic prompt: a pasted or dragged folder is offered for adding
(y/N), never asked as a question."""

from __future__ import annotations

from test_interactive import ctx, drive  # noqa: F401 -- fixtures and driver


def test_folder_is_offered_and_added_on_yes(ctx, tmp_path):
    d = tmp_path / "My Docs"
    d.mkdir()
    out, service = drive(ctx, [str(d).replace(" ", "\\ "), "y"])
    assert "Registered source src" in out
    assert service.calls == []
    assert len(ctx.source_manager.list_sources()) == 1


def test_folder_declined_registers_nothing(ctx, tmp_path):
    d = tmp_path / "docs"
    d.mkdir()
    out, service = drive(ctx, [f"'{d}'", "n"])
    assert "Cancelled." in out
    assert service.calls == [] and ctx.source_manager.list_sources() == []


def test_file_offers_its_folder(ctx, tmp_path):
    f = tmp_path / "plan.pdf"
    f.write_bytes(b"%PDF-1.4")
    out, service = drive(ctx, [str(f), "y"])
    assert service.calls == []
    sources = ctx.source_manager.list_sources()
    assert len(sources) == 1 and str(sources[0].path) == str(tmp_path.resolve())


def test_sentence_with_a_path_is_a_question(ctx, tmp_path):
    out, service = drive(ctx, [f"what is in {tmp_path}?"])
    assert [c["question"] for c in service.calls] == [f"what is in {tmp_path}?"]
    assert ctx.source_manager.list_sources() == []


def test_slash_command_wins_over_a_path_lookalike(ctx, tmp_path):
    out, service = drive(ctx, ["/sources"])
    assert "No sources registered" in out and service.calls == []
