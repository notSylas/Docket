from types import SimpleNamespace

from sqlalchemy import select
from typer.testing import CliRunner

from docket.core.config import Settings
from docket.core.db.models import Chunk
from docket.eval.schema import load_gold_set
from docket.infra.inference.gateway import FakeInferenceGateway
from docket.infra.inference.health import HealthReport
from docket.interfaces.cli.context import AppContext
from docket.interfaces.cli.tui.backend import AnswerView
from docket.interfaces.cli.tui.headless import Harness, run
from docket.interfaces.cli.tui.model import Message
from docket.interfaces.cli.tui.real_backend import RealBackend
from docket.services.feedback import FeedbackStore
from docket.services.query.service import Citation


def library(tmp_path):
    ctx = AppContext.for_testing(data_dir=tmp_path / "data", gateway=FakeInferenceGateway(),
                                 parser=SimpleNamespace(parser_name="unused", parser_version="1"))
    ctx.settings.feedback_enabled = True
    folder = tmp_path / "docs"; folder.mkdir()
    (folder / "minutes.txt").write_text("The committee approved the revised budget.")
    src = ctx.source_manager.register_source(folder)
    ctx.pipeline.run_ingestion_for_source(src.id)
    with ctx.session_factory() as db:
        chunk = db.execute(select(Chunk)).scalar_one()
        cid, vid = chunk.id, chunk.evidence_version_id
    return ctx, cid, vid


def test_keyboard_capture_targets_focused_answer_and_keeps_stable_versions(tmp_path):
    ctx, cid, vid = library(tmp_path)
    be = RealBackend(ctx, health_check=lambda: HealthReport(reachable=True))
    citations = be._citation_views([Citation(chunk_id=cid, citation_label="[1]", source_display_name="minutes.txt")])
    assert citations[0].evidence_version_id == vid
    store = FeedbackStore(ctx.settings.data_dir)
    async def scenario():
        async with Harness(backend=be) as h:
            h.ui.add_message(Message("assistant", "first answer", answer=AnswerView("first answer", tuple(citations)),
                                     question="first question", history_answer="original tagged answer [1]"))
            h.ui.add_message(Message("assistant", "second answer", answer=AnswerView("second answer", ()), question="second question"))
            h.ui.focus_answer = 0
            await h.press("f9")
            assert h.ui.overlays[-1].name == "feedback"
            await h.type("Please include the date")
            await h.press("tab", "tab", "enter")
            await h.wait_for(lambda: len(store.items()) == 1)
            item = store.items()[0]
            assert item["question"] == "first question" and item["answer"] == "original tagged answer [1]"
            assert item["label"] == "not_good" and item["correction"] == "Please include the date"
            assert item["citations"] == [{"chunk_id": cid, "evidence_version_id": vid}]
    try:
        run(scenario)
    finally:
        ctx.engine.dispose()


def test_off_setting_keyboard_does_not_write(tmp_path, monkeypatch):
    monkeypatch.delenv("DOCKET_FEEDBACK_ENABLED", raising=False)
    assert Settings(data_dir=tmp_path).feedback_enabled is False
    ctx, _, _ = library(tmp_path)
    ctx.settings.feedback_enabled = False
    be = RealBackend(ctx, health_check=lambda: HealthReport(reachable=True))
    async def scenario():
        async with Harness(backend=be) as h:
            await h.press("f9")
            assert any("Feedback capture is off" in m.text for m in h.ui.messages)
            assert not (ctx.settings.data_dir / "feedback").exists()
    try:
        run(scenario)
    finally:
        ctx.engine.dispose()


def test_review_and_draft_cli_validate_existing_gold_schema(tmp_path, monkeypatch):
    from docket.interfaces.cli import main
    ctx, cid, vid = library(tmp_path)
    monkeypatch.setattr(main, "build_context", lambda: ctx)
    store = FeedbackStore(ctx.settings.data_dir, True)
    fid = store.capture("What was approved?", "Budget", [{"chunk_id": cid, "evidence_version_id": vid}], "good")
    runner = CliRunner()
    try:
        no_raw = runner.invoke(main.app, ["feedback", "export"])
        assert no_raw.exit_code == 1 and "What was approved" not in no_raw.output
        review = runner.invoke(main.app, ["feedback", "review", fid, "--expected", "budget", "--span", "The committee approved the revised budget."])
        assert review.exit_code == 0, review.output
        path = tmp_path / "DRAFT.yml"
        exported = runner.invoke(main.app, ["feedback", "export", "--gold-draft", "--out", str(path)])
        assert exported.exit_code == 0, exported.output
        assert path.stat().st_mode & 0o777 == 0o600
        assert load_gold_set(path).questions[0].reviewed is False
        old = path.read_bytes()
        refused = runner.invoke(main.app, ["feedback", "export", "--gold-draft", "--out", str(path)])
        assert refused.exit_code == 1 and path.read_bytes() == old
    finally:
        ctx.engine.dispose()
