"""CP6 in the full-screen UI: the launch catch-up notice, background update
batches on the indexing slot (behind a question, never over one), the footer,
Pause/Resume updates per folder, and the persisted global switch.

A stub backend with real worker-thread semantics, plus a RealBackend check
over a scratch data dir. No Ollama, no parser, no user data; every wait is
bounded.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from test_tui_real import (
    StubBackend,
    backend_for,
    ctx,  # noqa: F401 -- fixtures
    scenario,
)

from docket.interfaces.cli.tui import fake_data as fd
from docket.interfaces.cli.tui import overlays as ov
from docket.interfaces.cli.tui.backend import IndexOutcome, IndexProgress
from docket.services.sources.freshness import Batch, ChangeSet


class FreshBackend(StubBackend):
    freshness_supported = True

    def __init__(self, changes: dict[str, ChangeSet] | None = None) -> None:
        super().__init__(sources=True)
        self.changes = changes or {}
        self.auto = True
        self.saved: list[bool] = []
        self.catch_up_calls: list[str] = []
        self.on_batch = None
        self.watched: list[list[str] | None] = []
        self.stopped = 0
        self.refreshed: list[tuple[str, list[Path]]] = []
        self.refresh_started = threading.Event()
        self.refresh_gate = threading.Event()
        self.refresh_gate.set()
        self.paused_calls: list[tuple[str, str]] = []

    def auto_refresh_enabled(self) -> bool:
        return self.auto

    def set_auto_refresh(self, enabled: bool) -> None:
        self.auto = enabled
        self.saved.append(enabled)

    def catch_up(self, source_id: str):
        self.catch_up_calls.append(source_id)
        if source_id in self.changes:
            return self.changes[source_id]
        src = self.world.find(source_id)
        return ChangeSet(source_id, checked=len(src.files) if src else 0)

    def watch_sources(self, on_batch, source_ids=None, hints=None) -> None:
        self.on_batch = on_batch
        self.watched.append(source_ids)

    def stop_watching(self) -> None:
        self.stopped += 1

    def pause_updates(self, source_id: str) -> None:
        self.paused_calls.append(("pause", source_id))
        super().pause_updates(source_id)

    def resume_updates(self, source_id: str) -> None:
        self.paused_calls.append(("resume", source_id))
        super().resume_updates(source_id)

    def refresh_paths(self, source_id, paths, on_progress, should_stop) -> IndexOutcome:
        self.refreshed.append((source_id, list(paths)))
        self.refresh_started.set()
        total = len(paths)
        for i, p in enumerate(paths):
            on_progress(IndexProgress(i, total, Path(p).name, indexed=i))
            deadline = time.monotonic() + 5
            while not self.refresh_gate.wait(0.01):
                if should_stop() or time.monotonic() > deadline:
                    return IndexOutcome("stopped", IndexProgress(i, total, "", indexed=i))
        return IndexOutcome(
            "done",
            IndexProgress(total, total, "", indexed=total),
            notices=(f"{total} documents updated.",),
        )


def _first_source(b: FreshBackend):
    return b.world.sources[0]


def _notices(h) -> list[str]:
    return [m.text for m in h.ui.messages if m.role == "system"]


def _scenario(fn, backend, **kw):
    kw.setdefault("tick_interval", 0.05)
    return scenario(fn, backend, **kw)


# -- launch catch-up ---------------------------------------------------------------------


def test_catch_up_says_up_to_date_quietly_and_starts_watching():
    b = FreshBackend()
    active = [s for s in b.world.sources if s.status in (fd.READY, fd.FAILED) and s.files]
    total = sum(len(s.files) for s in active)
    assert len(active) < len(b.world.sources)  # a disconnected source is left alone

    async def go(h, b):
        await h.wait_for(lambda: b.watched)
        await h.wait_for(lambda: any("up to date" in n and n.startswith("Checked") for n in _notices(h)))
        notice = next(n for n in _notices(h) if n.startswith("Checked"))
        assert notice == f"Checked {total} documents - up to date"
        assert sorted(b.catch_up_calls) == sorted(s.id for s in active)
        assert b.refreshed == []  # nothing to read
        assert h.ui.job is None
        assert set(b.watched[0]) == set(b.catch_up_calls)

    _scenario(go, b)


def test_catch_up_with_changes_queues_a_refresh_job():
    b = FreshBackend()
    src = _first_source(b)
    paths = [Path(f"/demo/{src.name}/{n}") for n in ("a.pdf", "b.pdf", "c.pdf")]
    b.changes[src.id] = ChangeSet(src.id, new=paths[:1], changed=paths[1:2], deleted=paths[2:], checked=12)
    b.refresh_gate.clear()

    async def go(h, b):
        await h.wait_for(lambda: b.refresh_started.is_set())
        assert "Updating 3 changed documents" in _notices(h)
        j = h.ui.job
        assert j is not None and j.kind == "refresh" and j.active
        assert "Updating 3 files" in h.text()  # the footer
        assert [p for _s, ps in b.refreshed for p in ps] == paths
        b.refresh_gate.set()
        await h.wait_for(lambda: not h.ui.job.active)
        assert j.state == "done"
        assert "3 documents updated." in _notices(h)
        assert "Updating 3 files" not in h.text()

    _scenario(go, b)


def test_a_refresh_does_not_trigger_the_indexing_coverage_note():
    b = FreshBackend()
    src = _first_source(b)
    b.changes[src.id] = ChangeSet(src.id, changed=[Path("/x/a.pdf")])
    b.refresh_gate.clear()

    async def go(h, b):
        await h.wait_for(lambda: b.refresh_started.is_set())
        assert h.ui.index_coverage() is None  # only a first index counts as "still indexing"
        b.refresh_gate.set()

    _scenario(go, b)


def test_nothing_runs_when_automatic_updates_are_off():
    b = FreshBackend()
    b.auto = False

    async def go(h, b):
        await h.settle()
        await h.wait_for(lambda: h.ui.readiness is not None and not h.ui.readiness.checking)
        assert b.catch_up_calls == [] and b.watched == []
        assert "Updates paused" in h.text()  # the footer says why nothing moves

    _scenario(go, b)


# -- queued behind a question ----------------------------------------------------------------


def test_a_batch_waits_for_the_question_then_runs():
    b = FreshBackend()
    src = _first_source(b)
    b.ask_gate.clear()
    path = Path("/demo/new.pdf")

    async def go(h, b):
        await h.wait_for(lambda: b.watched)
        await h.type("what is revenue?")
        await h.press("enter")
        await h.wait_for(lambda: b.ask_started.is_set())
        assert h.ui.question_active

        # a watcher thread reports a change while the question is being answered
        b.on_batch(Batch(src.id, (path,), "watch"))
        await h.wait_for(lambda: h.ui.queued_changes() == 1)
        await h.settle()
        assert b.refreshed == [] and h.ui.job is None  # queued, not started
        assert "1 change waiting" in h.text()

        b.ask_gate.set()  # the answer arrives
        await h.wait_for(lambda: b.refreshed)
        assert b.refreshed == [(src.id, [path])]
        await h.wait_for(lambda: h.ui.job is not None and not h.ui.job.active)
        assert h.ui.queued_changes() == 0

    _scenario(go, b)


def test_a_running_refresh_pauses_for_a_question_and_stop_ends_it():
    b = FreshBackend()
    src = _first_source(b)
    b.refresh_gate.clear()
    b.changes[src.id] = ChangeSet(src.id, changed=[Path(f"/x/{i}.pdf") for i in range(3)])

    async def go(h, b):
        await h.wait_for(lambda: b.refresh_started.is_set())
        j = h.ui.job
        h.ui.stop_job()
        await h.wait_for(lambda: not j.active)
        assert j.state == "stopped"
        assert any("Update stopped" in n for n in _notices(h))

    _scenario(go, b)


def test_batches_for_one_source_merge_while_waiting():
    b = FreshBackend()
    src = _first_source(b)
    b.refresh_gate.clear()
    b.changes[src.id] = ChangeSet(src.id, changed=[Path("/x/first.pdf")])

    async def go(h, b):
        await h.wait_for(lambda: b.refresh_started.is_set())  # slot taken by the catch-up batch
        for name in ("a.pdf", "b.pdf", "a.pdf"):
            b.on_batch(Batch(src.id, (Path(f"/x/{name}"),), "watch"))
        await h.wait_for(lambda: h.ui.queued_changes() == 2)
        assert "2 changes waiting" in h.text()
        b.refresh_gate.set()
        await h.wait_for(lambda: len(b.refreshed) == 2)
        assert sorted(p.name for p in b.refreshed[1][1]) == ["a.pdf", "b.pdf"]

    _scenario(go, b)


# -- pause / resume and the global switch -------------------------------------------------------


def test_pause_and_resume_updates_from_sources():
    b = FreshBackend()
    src = _first_source(b)

    async def go(h, b):
        await h.wait_for(lambda: b.watched)
        await h.press("f2")
        sources = h.ui.overlays[-1]
        btn = next(x for x in sources.buttons() if x.key == "pause")
        assert btn.label == "Pause updates" and btn.enabled
        sources.press("pause")
        assert b.paused_calls == [("pause", src.id)]
        assert "Updates paused" in h.ui.freshness_footer_text()
        btn = next(x for x in sources.buttons() if x.key == "pause")
        assert btn.label == "Resume updates"
        # a change reported for a paused folder is dropped, not queued
        b.on_batch(Batch(src.id, (Path("/x/a.pdf"),), "watch"))
        await h.settle()
        assert h.ui.queued_changes() == 0 and b.refreshed == []
        sources.press("pause")
        assert b.paused_calls[-1] == ("resume", src.id)
        assert h.ui.freshness_footer_text() == ""

    _scenario(go, b)


def test_the_global_switch_is_in_settings_and_turns_watching_off_and_on():
    b = FreshBackend()

    async def go(h, b):
        await h.wait_for(lambda: b.watched)
        h.ui.push(ov.SettingsOverlay(h.ui, "library"))
        await h.settle()
        text = h.text()
        assert "Keep my library up to date automatically" in text and "On" in text
        panel = h.ui.overlays[-1]
        panel.left_right(1)
        assert panel.pending["auto_refresh"] is False
        panel.press("save")
        await h.settle()
        assert b.saved == [False] and h.ui.auto_refresh is False
        assert b.stopped >= 1
        assert "Updates paused" in h.text()

        h.ui.push(ov.SettingsOverlay(h.ui, "library"))
        await h.settle()
        h.ui.overlays[-1].left_right(1)
        h.ui.overlays[-1].press("save")
        await h.settle()
        assert b.saved == [False, True] and h.ui.auto_refresh is True
        await h.wait_for(lambda: len(b.watched) >= 2)  # watching started again

    _scenario(go, b)


def test_everything_stops_when_the_window_closes():
    b = FreshBackend()

    async def go(h, b):
        await h.wait_for(lambda: b.watched)
        return b

    _scenario(go, b)
    assert b.stopped >= 1  # the harness exit ran the same shutdown as a real close


def test_the_demo_has_no_automatic_updates():
    from docket.interfaces.cli.tui.headless import Harness, run

    async def go():
        async with Harness("chat", 120, 40) as h:
            assert h.ui.freshness_supported is False
            assert h.ui.freshness_footer_text() == ""
            h.ui.push(ov.SettingsOverlay(h.ui))
            await h.settle()
            assert "Library" not in h.text().split("Settings")[1][:400]

    run(go, timeout=30)


# -- the real backend ------------------------------------------------------------------------


def test_real_backend_persists_the_switch_in_preferences_json(ctx, tmp_path):  # noqa: F811
    b = backend_for(ctx)
    prefs = Path(ctx.settings.data_dir) / "preferences.json"
    assert b.freshness_supported and b.auto_refresh_enabled() is True
    b.set_auto_refresh(False)
    assert json.loads(prefs.read_text())["auto_refresh_v1"] == {"enabled": False}
    assert backend_for(ctx).auto_refresh_enabled() is False  # a new session reads it back
    b.set_auto_refresh(True)
    assert backend_for(ctx).auto_refresh_enabled() is True


def test_toggle_in_the_window_writes_the_preference(ctx):  # noqa: F811
    b = backend_for(ctx)
    prefs = Path(ctx.settings.data_dir) / "preferences.json"

    async def go(h, backend):
        h.ui.push(ov.SettingsOverlay(h.ui, "library"))
        await h.settle()
        panel = h.ui.overlays[-1]
        panel.left_right(1)
        panel.press("save")
        await h.settle()
        assert json.loads(prefs.read_text())["auto_refresh_v1"] == {"enabled": False}
        assert h.ui.auto_refresh is False

    scenario(go, b)
    assert b.auto_refresh_enabled() is False


def test_real_backend_pause_updates_persists_sync_paused_at(ctx, tmp_path):  # noqa: F811
    folder = tmp_path / "watched"
    folder.mkdir()
    src = ctx.source_manager.register_source(folder)
    b = backend_for(ctx)
    b.pause_updates(src.id)
    assert ctx.source_manager.get_source(src.id).sync_paused_at is not None
    view = next(s for s in b.load_world().sources if s.id == src.id)
    assert view.updates_paused is True
    b.resume_updates(src.id)
    assert ctx.source_manager.get_source(src.id).sync_paused_at is None
