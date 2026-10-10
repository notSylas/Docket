"""Tests for `docket.services.sources.watcher.SourceWatcher`.

The primary tests stub `watchfiles.watch` so they run instantly and
deterministically: they prove the *dispatch logic* (each yielded batch
re-ingests only the affected, relevant paths through
`run_ingestion_for_paths`; ignored paths and unsupported files never
trigger anything; the whole source is never re-run). One real-filesystem
smoke test, bounded to a few seconds, proves the real `watchfiles.watch`
wiring end to end.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from docket.services.sources.watcher import SourceWatcher


class _RecordingPipeline:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[Path]]] = []
        self.whole_source_runs = 0

    def run_ingestion_for_paths(self, source_id, paths, progress=None, *, missing="error"):
        self.calls.append((source_id, list(paths)))
        assert missing == "skip"

    def run_ingestion_for_source(self, source_id, progress=None):  # must never be used
        self.whole_source_runs += 1


def _patch_watch(monkeypatch, batches, captured=None):
    def fake_watch(path, **kw):
        if captured is not None:
            captured["path"] = path
            captured.update(kw)
        yield from batches(Path(path))

    monkeypatch.setattr("docket.services.sources.watcher.watchfiles.watch", fake_watch)


def test_watch_ingests_only_the_changed_path(monkeypatch, tmp_path: Path) -> None:
    captured: dict = {}
    _patch_watch(monkeypatch, lambda p: [{("modified", str(p / "a.pdf"))}], captured)
    pipeline = _RecordingPipeline()
    stop_event = threading.Event()

    SourceWatcher(pipeline).watch("src_1", tmp_path, stop_event=stop_event)

    assert pipeline.calls == [("src_1", [tmp_path / "a.pdf"])]
    assert pipeline.whole_source_runs == 0
    assert captured["path"] == tmp_path
    assert captured["stop_event"] is stop_event


def test_watch_one_call_per_batch_with_deleted_paths_included(monkeypatch, tmp_path: Path) -> None:
    _patch_watch(
        monkeypatch,
        lambda p: [
            {("added", str(p / "a.pdf"))},
            {("modified", str(p / "a.pdf")), ("modified", str(p / "b.docx"))},
            {("deleted", str(p / "a.pdf"))},
        ],
    )
    pipeline = _RecordingPipeline()
    SourceWatcher(pipeline).watch("src_1", tmp_path)
    assert [c[1] for c in pipeline.calls] == [
        [tmp_path / "a.pdf"],
        [tmp_path / "a.pdf", tmp_path / "b.docx"],
        [tmp_path / "a.pdf"],
    ]


def test_watch_ignores_unsupported_hidden_and_ignored_paths(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / ".docketignore").write_text("drafts/\n")
    _patch_watch(
        monkeypatch,
        lambda p: [
            {
                ("added", str(p / "notes.txt")),  # not a supported document
                ("added", str(p / ".hidden.pdf")),
                ("added", str(p / "node_modules" / "x.pdf")),
                ("added", str(p / "drafts" / "y.pdf")),
                ("added", str(p / "~$lock.docx")),
            }
        ],
    )
    pipeline = _RecordingPipeline()
    SourceWatcher(pipeline).watch("src_1", tmp_path)
    assert pipeline.calls == []  # nothing relevant: no job at all


def test_watch_no_events_never_dispatches(monkeypatch, tmp_path: Path) -> None:
    _patch_watch(monkeypatch, lambda p: [])
    pipeline = _RecordingPipeline()
    SourceWatcher(pipeline).watch("src_1", tmp_path)
    assert pipeline.calls == []


@pytest.mark.filterwarnings("ignore")
def test_watch_real_filesystem_smoke_test(tmp_path: Path) -> None:
    """Real `watchfiles.watch`, bounded to a few seconds."""
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
        (watch_dir / "new_file.pdf").write_bytes(b"%PDF-1.4 hello")

        deadline = time.time() + 4.0
        while time.time() < deadline and not pipeline.calls:
            time.sleep(0.1)

        assert pipeline.calls and pipeline.calls[0][0] == "src_1"
        assert pipeline.calls[0][1] == [watch_dir / "new_file.pdf"]
    finally:
        stop_event.set()
        thread.join(timeout=3.0)
