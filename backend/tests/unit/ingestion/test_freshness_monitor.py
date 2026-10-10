"""CP6 `FreshnessMonitor` and `Debouncer`: batching with a fake clock and
scripted events, the fall back to periodic catch-ups, pause/resume through
`sync_paused_at`, reachability bookkeeping, and a clean `close()`.

No Docling, no Ollama, no real inotify (a scripted event source stands in
for `watchfiles.watch`); every wait is bounded.
"""

from __future__ import annotations

import queue
import threading
import time
from datetime import datetime
from pathlib import Path

import pytest

from docket.core.db.engine import get_session_factory
from docket.core.db.models import EvidenceVersion, SourceStatus, VersionStatus
from docket.services.sources.freshness import (
    Batch,
    Debouncer,
    FreshnessMonitor,
    auto_refresh_enabled,
    set_auto_refresh,
)
from docket.services.sources.manager import SourceManager


def _wait(predicate, timeout: float = 4.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class ScriptedWatch:
    """Stands in for `watchfiles.watch`: yields whatever the test pushed
    (or an empty set on each poll timeout) until `stop_event` is set."""

    def __init__(self) -> None:
        self.q: queue.Queue = queue.Queue()
        self.calls: list[dict] = []

    def push(self, *events) -> None:
        self.q.put(set(events))

    def __call__(self, root, **kw):
        self.calls.append({"root": root, **kw})
        stop = kw["stop_event"]
        while not stop.is_set():
            try:
                yield self.q.get(timeout=0.02)
            except queue.Empty:
                yield set()


class Fixture:
    def __init__(self, tmp_path: Path, engine) -> None:
        self.session_factory = get_session_factory(engine)
        self.manager = SourceManager(self.session_factory)
        self.root = tmp_path / "docs"
        self.root.mkdir()
        self.source = self.manager.register_source(self.root)
        self.batches: list[Batch] = []
        self.monitors: list[FreshnessMonitor] = []

    def monitor(self, **kw) -> FreshnessMonitor:
        kw.setdefault("on_batch", self.batches.append)
        m = FreshnessMonitor(
            self.manager, lambda: None, session_factory=self.session_factory, **kw
        )
        self.monitors.append(m)
        return m

    def index_version(self, name: str, *, size: int = 5, mtime: float | None = None) -> None:
        """A READY stored version for `name` (so catch-up has something to compare)."""
        path = self.root / name
        with self.session_factory() as session:
            session.add(
                EvidenceVersion(
                    source_id=self.source.id,
                    file_path=str(path),
                    content_hash=(name.encode().hex() * 8)[:64].ljust(64, "a"),
                    byte_size=size,
                    observed_at=datetime(2026, 1, 1),
                    source_mtime=mtime,
                    parser_name="p",
                    parser_version="1",
                    status=VersionStatus.READY,
                )
            )
            session.commit()


@pytest.fixture()
def fx(tmp_path, migrated_sqlite_engine):
    f = Fixture(tmp_path, migrated_sqlite_engine)
    yield f
    for m in f.monitors:
        m.close(timeout=3)


def _pdf(root: Path, name: str, payload: bytes = b"%PDF-1.4 x") -> Path:
    p = root / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(payload)
    return p


# -- debounce logic (pure) -----------------------------------------------------------


def test_debouncer_releases_after_a_quiet_period():
    d = Debouncer(quiet_s=5, max_s=60)
    d.add([Path("a.pdf")], 0)
    d.add([Path("b.pdf")], 3)
    assert not d.due(4)  # 1 s after the last event
    assert not d.due(7.9)
    assert d.due(8)
    paths, rescan = d.take()
    assert paths == [Path("a.pdf"), Path("b.pdf")] and not rescan
    assert not d.pending and not d.due(100)


def test_debouncer_caps_batching_at_the_max_window():
    d = Debouncer(quiet_s=5, max_s=60)
    t = 0.0
    while t < 59:  # an event every 2 s: never quiet
        d.add([Path(f"{t}.pdf")], t)
        assert not d.due(t)
        t += 2
    d.add([Path("late.pdf")], 60)
    assert d.due(60)  # 60 s since the first event, despite no quiet moment


def test_debouncer_carries_a_rescan_request_and_dedupes():
    d = Debouncer(quiet_s=1, max_s=10)
    d.add([Path("a.pdf"), Path("a.pdf")], 0)
    d.add([], 0.5, rescan=True)
    paths, rescan = d.take()
    assert paths == [Path("a.pdf")] and rescan


# -- the watcher thread -----------------------------------------------------------------


def test_events_are_debounced_filtered_and_batched(fx):
    (fx.root / ".docketignore").write_text("drafts/\n")
    clock, watch = FakeClock(), ScriptedWatch()
    m = fx.monitor(watch_factory=watch, clock=clock, debounce_s=5, max_batch_s=60, periodic_s=999)
    assert m.start() == [fx.source.id]
    assert _wait(lambda: m.mode(fx.source.id) == "watching")

    a, b = fx.root / "a.pdf", fx.root / "sub" / "b.docx"
    watch.push(("added", str(a)), ("added", str(fx.root / "drafts" / "x.pdf")), ("added", str(fx.root / "n.txt")))
    time.sleep(0.15)
    clock.now += 2
    watch.push(("modified", str(b)), ("modified", str(a)), ("added", str(fx.root / ".git" / "y.pdf")))
    time.sleep(0.15)
    assert fx.batches == []  # inside the quiet period: nothing released yet
    clock.now += 5
    watch.push()  # a poll tick after the quiet period
    assert _wait(lambda: len(fx.batches) == 1)
    batch = fx.batches[0]
    assert batch.reason == "watch" and batch.source_id == fx.source.id
    assert sorted(batch.paths) == sorted([a, b])  # ignored and unsupported paths never made it
    time.sleep(0.15)
    assert len(fx.batches) == 1  # one batch, not one per event
    assert watch.calls[0]["root"] == fx.root


def test_a_steady_stream_is_released_at_the_max_window(fx):
    clock, watch = FakeClock(), ScriptedWatch()
    m = fx.monitor(watch_factory=watch, clock=clock, debounce_s=5, max_batch_s=60, periodic_s=999)
    m.start()
    assert _wait(lambda: m.mode(fx.source.id) == "watching")
    for i in range(31):  # a new event every 2 fake seconds, never quiet for 5
        watch.push(("modified", str(fx.root / f"f{i}.pdf")))
        time.sleep(0.03)
        clock.now += 2
    assert _wait(lambda: len(fx.batches) >= 1)
    assert len(fx.batches[0].paths) >= 30


def test_a_new_folder_triggers_a_catch_up_scan(fx):
    clock, watch = FakeClock(), ScriptedWatch()
    m = fx.monitor(watch_factory=watch, clock=clock, debounce_s=1, periodic_s=999)
    m.start()
    assert _wait(lambda: m.mode(fx.source.id) == "watching")
    inside = _pdf(fx.root / "moved_in", "doc.pdf")  # a whole folder arrives at once
    watch.push(("added", str(fx.root / "moved_in")))
    time.sleep(0.15)
    clock.now += 2
    watch.push()
    assert _wait(lambda: len(fx.batches) == 1)
    assert fx.batches[0].paths == (inside,)


def test_watch_failure_falls_back_to_periodic_catch_up(fx):
    def broken(root, **kw):
        raise OSError("inotify watch limit reached")
        yield  # pragma: no cover

    m = fx.monitor(watch_factory=broken, periodic_s=0.05)
    m.start()
    assert _wait(lambda: m.mode(fx.source.id) == "periodic")
    assert m.reason(fx.source.id) == "error"
    new = _pdf(fx.root, "arrived.pdf")
    assert _wait(lambda: any(b.reason == "periodic" for b in fx.batches))
    assert new in fx.batches[0].paths


def test_a_large_tree_uses_periodic_scans_not_a_watcher(fx):
    for i in range(6):
        _pdf(fx.root, f"f{i}.pdf")
    watch = ScriptedWatch()
    m = fx.monitor(watch_factory=watch, large_tree_entries=3, periodic_s=0.05)
    m.start()
    assert _wait(lambda: m.mode(fx.source.id) == "periodic")
    assert m.reason(fx.source.id) == "large"
    assert _wait(lambda: len(fx.batches) >= 1)
    assert watch.calls == []  # no watcher was ever established
    assert len(fx.batches[0].paths) == 6


def test_a_known_large_tree_hint_skips_counting(fx):
    watch = ScriptedWatch()
    m = fx.monitor(watch_factory=watch, large_tree_entries=100, periodic_s=999)
    assert m.start_source(fx.source.id, tree_hint=10_000)
    assert _wait(lambda: m.mode(fx.source.id) == "periodic")
    assert m.reason(fx.source.id) == "large" and watch.calls == []


# -- pause / resume ------------------------------------------------------------------------


def test_pause_persists_stops_the_watcher_and_resume_catches_up(fx):
    watch = ScriptedWatch()
    m = fx.monitor(watch_factory=watch, periodic_s=999)
    m.start()
    assert _wait(lambda: m.mode(fx.source.id) == "watching")

    m.pause(fx.source.id)
    assert m.mode(fx.source.id) == "paused"
    assert fx.manager.get_source(fx.source.id).sync_paused_at is not None
    assert m.start_source(fx.source.id) is False  # a paused source is not watched
    assert m.start() == []
    changed = _pdf(fx.root, "while_paused.pdf")

    assert m.resume(fx.source.id)
    assert fx.manager.get_source(fx.source.id).sync_paused_at is None
    assert _wait(lambda: any(changed in b.paths for b in fx.batches))
    assert _wait(lambda: m.mode(fx.source.id) == "watching")


def test_a_source_paused_in_the_database_is_not_started(fx):
    fx.manager.pause_sync(fx.source.id)
    m = fx.monitor(watch_factory=ScriptedWatch())
    assert m.start() == []
    assert m.mode(fx.source.id) == "paused"


def test_only_active_sources_are_watched(fx, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    gone = fx.manager.register_source(other)
    fx.manager.deactivate_source(gone.id, reason="user_disconnected")
    m = fx.monitor(watch_factory=ScriptedWatch(), periodic_s=999)
    assert m.start() == [fx.source.id]


# -- reachability ----------------------------------------------------------------------------


def test_a_vanished_root_marks_the_source_missing_and_back(fx, tmp_path):
    fx.index_version("a.pdf")
    m = fx.monitor(watch_factory=ScriptedWatch(), periodic_s=0.05)
    m.start()
    assert _wait(lambda: m.mode(fx.source.id) == "watching")
    away = tmp_path / "away"
    fx.root.rename(away)
    assert _wait(lambda: fx.manager.get_source(fx.source.id).status == SourceStatus.MISSING)
    assert fx.batches == []  # an unreachable root is not a batch of deletions
    away.rename(fx.root)
    assert _wait(lambda: fx.manager.get_source(fx.source.id).status == SourceStatus.ACTIVE)


# -- shutdown ----------------------------------------------------------------------------------


def test_close_stops_every_thread(fx, tmp_path):
    other = tmp_path / "second"
    other.mkdir()
    fx.manager.register_source(other)
    m = fx.monitor(watch_factory=ScriptedWatch(), periodic_s=999)
    assert len(m.start()) == 2
    threads = [t for t in threading.enumerate() if t.name.startswith("docket-fresh-")]
    assert len(threads) == 2
    started = time.monotonic()
    m.close(timeout=3)
    assert time.monotonic() - started < 3
    assert not [t for t in threading.enumerate() if t.name.startswith("docket-fresh-") and t.is_alive()]
    assert m.start() == []  # a closed monitor stays closed


def test_a_real_watchfiles_event_is_reported(fx):
    """The real watcher, bounded: a file created under the root becomes a batch."""
    m = fx.monitor(debounce_s=0.2, max_batch_s=2, periodic_s=999)
    m.start()
    assert _wait(lambda: m.mode(fx.source.id) == "watching")
    time.sleep(0.4)  # let the OS watch settle
    created = _pdf(fx.root, "real.pdf")
    assert _wait(lambda: any(created in b.paths for b in fx.batches), timeout=8)
    m.close(timeout=3)


# -- the global switch ---------------------------------------------------------------------------


def test_auto_refresh_switch_defaults_on_and_persists_atomically(tmp_path):
    assert auto_refresh_enabled(tmp_path) is True
    set_auto_refresh(tmp_path, False)
    assert auto_refresh_enabled(tmp_path) is False
    assert (tmp_path / "preferences.json").exists()
    assert not list(tmp_path.glob(".preferences.*"))  # no temp file left behind
    set_auto_refresh(tmp_path, True)
    assert auto_refresh_enabled(tmp_path) is True


def test_auto_refresh_switch_keeps_other_preferences(tmp_path):
    (tmp_path / "preferences.json").write_text('{"folder_consent_v1": {"at": "x"}}')
    set_auto_refresh(tmp_path, False)
    import json

    data = json.loads((tmp_path / "preferences.json").read_text())
    assert data["folder_consent_v1"] == {"at": "x"}
    assert data["auto_refresh_v1"] == {"enabled": False}


def test_run_batch_uses_the_shared_path_level_entry(fx):
    class Pipe:
        calls: list = []

        def run_ingestion_for_paths(self, source_id, paths, progress=None, *, missing="error"):
            self.calls.append((source_id, paths, missing))
            return "result"

    pipe = Pipe()
    m = FreshnessMonitor(fx.manager, lambda: pipe, session_factory=fx.session_factory)
    out = m.run_batch(Batch(fx.source.id, (fx.root / "a.pdf",), "watch"))
    assert out == "result"
    assert pipe.calls == [(fx.source.id, [fx.root / "a.pdf"], "skip")]
