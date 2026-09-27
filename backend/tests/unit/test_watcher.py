"""Tests for `docket.services.sources.watcher.SourceWatcher`.

Primary tests stub `watchfiles.watch` so they run instantly and
deterministically -- they prove the *dispatch logic* (each yielded change
batch triggers exactly one `run_ingestion_for_source` call, `path`/
`stop_event` are forwarded correctly) without needing a real, timing-
sensitive filesystem wait. One additional real-filesystem smoke test is
included, bounded to a few seconds, to prove the real `watchfiles.watch`
wiring itself works end to end.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from docket.services.sources.watcher import SourceWatcher


class _RecordingPipeline:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def run_ingestion_for_source(self, source_id: str) -> None:
        self.calls.append(source_id)


def test_watch_dispatches_ingestion_on_synthetic_event(monkeypatch, tmp_path: Path) -> None:
    captured: dict = {}

    def fake_watch(path, *, stop_event=None):
        captured["path"] = path
        captured["stop_event"] = stop_event
        yield {("modified", str(Path(path) / "f.txt"))}

    monkeypatch.setattr("docket.services.sources.watcher.watchfiles.watch", fake_watch)

    pipeline = _RecordingPipeline()
    watcher = SourceWatcher(pipeline)
    watch_dir = tmp_path / "watched"
    watch_dir.mkdir()
    stop_event = threading.Event()

    watcher.watch("src_1", watch_dir, stop_event=stop_event)

    assert pipeline.calls == ["src_1"]
    assert captured["path"] == watch_dir
    assert captured["stop_event"] is stop_event


def test_watch_dispatches_once_per_change_batch(monkeypatch, tmp_path: Path) -> None:
    def fake_watch(path, *, stop_event=None):
        yield {("added", "a.txt")}
        yield {("modified", "a.txt")}
        yield {("deleted", "a.txt")}

    monkeypatch.setattr("docket.services.sources.watcher.watchfiles.watch", fake_watch)

    pipeline = _RecordingPipeline()
    watcher = SourceWatcher(pipeline)

    watcher.watch("src_1", tmp_path)

    assert pipeline.calls == ["src_1", "src_1", "src_1"]


def test_watch_no_events_never_dispatches(monkeypatch, tmp_path: Path) -> None:
    def fake_watch(path, *, stop_event=None):
        return
        yield  # pragma: no cover -- makes this a generator function

    monkeypatch.setattr("docket.services.sources.watcher.watchfiles.watch", fake_watch)

    pipeline = _RecordingPipeline()
    watcher = SourceWatcher(pipeline)

    watcher.watch("src_1", tmp_path)

    assert pipeline.calls == []


@pytest.mark.filterwarnings("ignore")
def test_watch_real_filesystem_smoke_test(tmp_path: Path) -> None:
    """Real `watchfiles.watch`, bounded to a few seconds. Not the primary
    correctness test (see the stubbed tests above) -- just proof the real
    wiring (path, stop_event) actually works against a real filesystem."""
    pipeline = _RecordingPipeline()
    watcher = SourceWatcher(pipeline)
    watch_dir = tmp_path / "watched"
    watch_dir.mkdir()
    stop_event = threading.Event()

    thread = threading.Thread(
        target=watcher.watch, args=("src_1", watch_dir), kwargs={"stop_event": stop_event}
    )
    thread.start()
    try:
        time.sleep(0.3)  # let the watcher start observing before we change anything
        (watch_dir / "new_file.txt").write_text("hello")

        deadline = time.time() + 4.0
        while time.time() < deadline and not pipeline.calls:
            time.sleep(0.1)

        assert pipeline.calls == ["src_1"]
    finally:
        stop_event.set()
        thread.join(timeout=3.0)
