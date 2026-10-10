"""Automatic freshness while Docket is open: what changed under a source, and
a debounced watcher that reports it.

Two parts, both only alive while the window is open (no daemon; the next
launch's `catch_up` covers the gap):

* `catch_up(source_id)` -- compare the files a fresh discovery finds with the
  READY/FAILED/PENDING versions stored for the source, by path, `byte_size`
  and `source_mtime`. Only candidates (size or mtime differ, or no mtime is
  stored) are ever hashed or parsed; they go to
  `IngestionPipeline.run_ingestion_for_paths`, whose content-hash idempotency
  makes a touched-but-identical file cost one hash. A file that is gone, with
  nothing re-created at that path, is reported as deleted (a rename is a
  delete plus a new file). The walk is bounded by an entry and a time budget;
  when it stops early `incomplete` is set with a `resume_from` cursor, and
  known files outside the scanned part are still checked by `stat`, so only
  brand-new files in the unscanned part wait for the next pass.

* `FreshnessMonitor` -- one `watchfiles` watcher thread per ACTIVE, not
  paused source. Events are filtered by the source's ignore rules (built-ins,
  `.docketignore`, stored `rules_json`) and the supported extensions, then
  debounced: a batch is released after `debounce_s` of quiet, or
  `max_batch_s` after the first event so a steady stream cannot starve it.
  Batches are handed to a callback; the monitor never decides *when* to run
  one (the TUI controller does, and queues them behind a question). When the
  watch cannot be established (inotify limit, root unreachable, watcher
  error) or the tree is large (more than `large_tree_entries` entries), the
  source falls back to a periodic catch-up every `periodic_s` seconds.

Safety: nothing is reported deleted while the root itself is unreachable (an
unmounted drive is not a deleted library; the source is marked MISSING via
`SourceManager.mark_unreachable` and back via `mark_reachable`), and a root
that turns up completely empty while files are indexed holds the deletions
back (`ChangeSet.held_back`) instead of wiping the index.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

import watchfiles
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from docket.core.db.models import EvidenceVersion, Source, SourceStatus, VersionStatus
from docket.services.ingestion.ignore import DOCKETIGNORE_NAME, IgnoreRules, discover
from docket.services.ingestion.pipeline import DISCOVERABLE_EXTENSIONS
from docket.services.sources.manager import SourceNotFoundError
from docket.services.sources.session import read_preferences, write_preferences

logger = logging.getLogger(__name__)

# Documented thresholds (all overridable per monitor).
DEBOUNCE_S = 5.0  # quiet period before a batch is released
MAX_BATCH_S = 60.0  # longest a batch may keep growing
PERIODIC_S = 300.0  # periodic catch-up interval when no watcher runs
LARGE_TREE_ENTRIES = 20_000  # more entries than this: periodic scans, no watcher
CATCH_UP_BUDGET_S = 20.0  # time bound of one catch-up walk
CATCH_UP_MAX_ENTRIES = 200_000  # entry bound of one catch-up walk
_MTIME_EPSILON = 1e-6
_WATCH_POLL_MS = 1000  # how often the watcher loop wakes (stop / root checks)

_TRACKED = (VersionStatus.READY, VersionStatus.FAILED, VersionStatus.PENDING)


@dataclass
class ChangeSet:
    """What differs between a source's folder and its stored versions."""

    source_id: str
    new: list[Path] = field(default_factory=list)
    changed: list[Path] = field(default_factory=list)
    deleted: list[Path] = field(default_factory=list)
    checked: int = 0  # documents compared (found on disk plus known)
    incomplete: bool = False  # the bounded walk stopped early
    resume_from: tuple[str, ...] | None = None
    unreachable: bool = False  # the root itself is not there; nothing is reported
    held_back: int = 0  # deletions not reported (the root looked empty)
    entries_scanned: int = 0

    @property
    def paths(self) -> list[Path]:
        return [*self.new, *self.changed, *self.deleted]

    @property
    def count(self) -> int:
        return len(self.new) + len(self.changed) + len(self.deleted)

    @property
    def empty(self) -> bool:
        return self.count == 0


def _stat(path: Path) -> tuple[int, float] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return st.st_size, st.st_mtime


def _differs(known: tuple[int, float | None, VersionStatus], size: int, mtime: float) -> bool:
    stored_size, stored_mtime, status = known
    if status == VersionStatus.PENDING:  # never finished: needs processing
        return True
    if stored_mtime is None or stored_size != size:
        return True
    return abs(stored_mtime - mtime) > _MTIME_EPSILON


def catch_up(
    source_id: str,
    *,
    session_factory: sessionmaker,
    settings: object | None = None,
    max_entries: int | None = CATCH_UP_MAX_ENTRIES,
    time_budget_s: float | None = CATCH_UP_BUDGET_S,
    start_after: tuple[str, ...] | None = None,
) -> ChangeSet:
    """Compare the folder with the stored versions (see module docstring).
    Cheap: one discovery walk, one `stat` per document, no hashing here --
    the caller ingests exactly `ChangeSet.paths`."""
    with session_factory() as session:
        source = session.get(Source, source_id)
        if source is None:
            raise SourceNotFoundError(source_id)
        root = Path(source.path)
        rules_json = source.rules_json
        rows = session.execute(
            select(
                EvidenceVersion.file_path,
                EvidenceVersion.byte_size,
                EvidenceVersion.source_mtime,
                EvidenceVersion.status,
            ).where(
                EvidenceVersion.source_id == source_id,
                EvidenceVersion.status.in_(_TRACKED),
                EvidenceVersion.file_path.is_not(None),
            )
        ).all()
    known: dict[str, tuple[int, float | None, VersionStatus]] = {
        path: (size, mtime, status) for path, size, mtime, status in rows
    }
    out = ChangeSet(source_id)
    if not root.is_dir():
        out.unreachable = True
        return out

    enabled = bool(getattr(settings, "ingest_ignore_enabled", True))
    report = discover(
        root,
        extensions=DISCOVERABLE_EXTENSIONS,
        enabled=enabled,
        extra_rules=rules_json,
        max_entries=max_entries,
        time_budget_s=time_budget_s,
        count_ignored=False,
        start_after=start_after,
    )
    out.incomplete = report.incomplete
    out.resume_from = report.resume_from if report.incomplete else None
    out.entries_scanned = report.entries_scanned

    seen: set[str] = set()
    for path in report.files:
        key = str(path)
        seen.add(key)
        st = _stat(path)
        if st is None:
            continue  # vanished between the walk and now; the next pass sees it
        out.checked += 1
        entry = known.get(key)
        if entry is None:
            out.new.append(path)
        elif _differs(entry, *st):
            out.changed.append(path)

    full_pass = start_after is None and not report.incomplete
    rules = IgnoreRules.for_root(root, rules_json) if enabled else IgnoreRules()
    for key, entry in known.items():
        if key in seen:
            continue
        path = Path(key)
        st = _stat(path)
        if st is None:
            if not path.exists():
                out.deleted.append(path)
            continue
        if full_pass:
            continue  # exists but discovery skipped it: ignored or not a real document
        # Partial walk: this file lay outside the scanned part. Check it by stat.
        try:
            rel = path.relative_to(root)
        except ValueError:
            continue
        if path.suffix.lower() not in DISCOVERABLE_EXTENSIONS or (enabled and rules.path_ignored(rel)):
            continue
        out.checked += 1
        if _differs(entry, *st):
            out.changed.append(path)

    if out.deleted and full_pass and not report.files and len(out.deleted) == len(known):
        # The root exists but shows nothing at all while everything indexed is
        # "gone": most likely an unmounted or emptied mount point.
        out.held_back = len(out.deleted)
        out.deleted = []
    out.new.sort()
    out.changed.sort()
    out.deleted.sort()
    return out


# -- the global switch ----------------------------------------------------------

AUTO_REFRESH_KEY = "auto_refresh_v1"


def auto_refresh_enabled(data_dir: Path) -> bool:
    """"Keep my library up to date automatically": on unless the user turned
    it off (`<data_dir>/preferences.json`, key `auto_refresh_v1`)."""
    entry = read_preferences(Path(data_dir)).get(AUTO_REFRESH_KEY)
    if isinstance(entry, dict):
        entry = entry.get("enabled")
    return entry if isinstance(entry, bool) else True


def set_auto_refresh(data_dir: Path, enabled: bool) -> None:
    """Persist the switch (atomic write, other keys kept)."""
    prefs = read_preferences(Path(data_dir))
    prefs[AUTO_REFRESH_KEY] = {"enabled": bool(enabled)}
    write_preferences(data_dir, prefs)


# -- debouncing -----------------------------------------------------------------


class Debouncer:
    """Pure batching logic (no threads, no clock of its own): paths are added
    with the time they were seen; `due` says whether the batch should be
    released -- quiet for `quiet_s`, or `max_s` since its first event."""

    def __init__(self, quiet_s: float = DEBOUNCE_S, max_s: float = MAX_BATCH_S) -> None:
        self.quiet_s = quiet_s
        self.max_s = max_s
        self._paths: set[Path] = set()
        self._rescan = False
        self._first: float | None = None
        self._last: float | None = None

    @property
    def pending(self) -> bool:
        return bool(self._paths) or self._rescan

    def add(self, paths: Iterable[Path], now: float, *, rescan: bool = False) -> None:
        added = False
        for p in paths:
            self._paths.add(Path(p))
            added = True
        if rescan:
            self._rescan = True
            added = True
        if added:
            if self._first is None:
                self._first = now
            self._last = now

    def due(self, now: float) -> bool:
        if not self.pending or self._first is None or self._last is None:
            return False
        return now - self._last >= self.quiet_s or now - self._first >= self.max_s

    def take(self) -> tuple[list[Path], bool]:
        paths, rescan = sorted(self._paths), self._rescan
        self._paths, self._rescan, self._first, self._last = set(), False, None, None
        return paths, rescan


# -- the monitor ---------------------------------------------------------------


@dataclass(frozen=True)
class Batch:
    """Paths whose state should be re-checked, handed to the controller.
    Deleted paths are included (the pipeline retires their versions)."""

    source_id: str
    paths: tuple[Path, ...]
    reason: str  # "catch_up" | "watch" | "periodic"
    incomplete: bool = False


def _count_entries(root: Path, rules: IgnoreRules, cap: int, budget_s: float) -> int:
    """Entries under `root` (ignored folders pruned), stopping above `cap`
    or after `budget_s`; a value above `cap` means "large"."""
    deadline = time.monotonic() + budget_s
    n = 0
    for dirpath, dirnames, filenames in os.walk(root):
        rel = Path(dirpath).relative_to(root)
        prefix = "" if str(rel) == "." else rel.as_posix() + "/"
        dirnames[:] = [d for d in dirnames if not rules.entry_ignored(prefix + d, d, True)]
        n += len(dirnames) + len(filenames)
        if n > cap or time.monotonic() > deadline:
            return cap + 1
    return n


def classify_event(
    root: Path, rules: IgnoreRules, change: object, raw: str, *, enabled: bool = True
) -> tuple[bool, bool]:
    """(keep, rescan) for one raw filesystem event under `root`: keep the
    path if it is a supported document the rules would not ignore; ask for a
    rescan when a folder appeared or a suffix-less path vanished (a removed
    or moved folder says nothing about the files inside it)."""
    path = Path(raw)
    try:
        rel = path.relative_to(root)
    except ValueError:
        return False, False
    if not rel.parts or rel.name == DOCKETIGNORE_NAME:
        return False, False
    if enabled and rules.path_ignored(rel):
        return False, False
    if path.suffix.lower() in DISCOVERABLE_EXTENSIONS:
        return True, False
    name = getattr(change, "name", str(change))
    if name == "added" and path.is_dir():
        return False, True
    if name == "deleted" and not path.suffix:
        return False, True
    return False, False


class _SourceWatch(threading.Thread):
    """Watcher thread of one source: a watch while it works, a periodic
    catch-up when it cannot (or must not)."""

    def __init__(self, monitor: "FreshnessMonitor", source_id: str, *, catch_up_first: bool, tree_hint: int | None):
        super().__init__(name=f"docket-fresh-{source_id[:8]}", daemon=True)
        self.mon = monitor
        self.source_id = source_id
        self.stop = threading.Event()
        self.mode = "starting"  # watching | periodic
        self.reason = ""  # why it is periodic
        self._catch_up_first = catch_up_first
        self._tree_hint = tree_hint
        self._cursor: tuple[str, ...] | None = None
        self._budget = monitor.catch_up_budget_s

    # -- helpers --------------------------------------------------------------
    def _source(self) -> Source | None:
        try:
            return self.mon.source_manager.get_source(self.source_id)
        except SourceNotFoundError:
            return None

    def _root_ok(self, source: Source) -> bool:
        """Reachability bookkeeping through the existing hooks."""
        reachable = Path(source.path).is_dir()
        try:
            if not reachable and source.status == SourceStatus.ACTIVE:
                self.mon.source_manager.mark_unreachable(self.source_id)
            elif reachable and source.status == SourceStatus.MISSING:
                self.mon.source_manager.mark_reachable(self.source_id)
        except Exception:  # noqa: BLE001 -- bookkeeping never stops the watcher
            logger.debug("reachability update failed", exc_info=True)
        return reachable

    def _deliver(self, paths: list[Path], reason: str, incomplete: bool = False) -> None:
        if not paths or self.stop.is_set():
            return
        try:
            self.mon.on_batch(Batch(self.source_id, tuple(paths), reason, incomplete))
        except Exception:  # noqa: BLE001
            logger.exception("freshness batch callback failed")

    def _scan(self, reason: str) -> list[Path]:
        cs = catch_up(
            self.source_id,
            session_factory=self.mon.session_factory,
            settings=self.mon.settings,
            max_entries=self.mon.catch_up_max_entries,
            time_budget_s=self._budget,
            start_after=self._cursor,
        )
        if cs.incomplete and cs.resume_from == self._cursor and self._cursor is not None:
            self._budget = min(self._budget * 2, self.mon.catch_up_budget_s * 8)  # no progress: widen
        self._cursor = cs.resume_from if cs.incomplete else None
        if cs.incomplete is False:
            self._budget = self.mon.catch_up_budget_s
        return cs.paths if not cs.unreachable else []

    def _tick(self, reason: str) -> None:
        source = self._source()
        if source is None or not self._root_ok(source):
            return
        paths = self._scan(reason)
        self._deliver(paths, reason, incomplete=self._cursor is not None)

    # -- thread body ---------------------------------------------------------
    def run(self) -> None:  # noqa: D401
        try:
            if self._catch_up_first:
                self._tick("catch_up")
            mode = self._choose_mode()
            while not self.stop.is_set():
                mode = self._watch_loop() if mode == "watch" else self._periodic_loop()
        except Exception:  # noqa: BLE001
            logger.exception("freshness watcher for %s stopped", self.source_id)
        finally:
            self.mode = "stopped"

    def _choose_mode(self) -> str:
        source = self._source()
        if source is None:
            return "periodic"
        root = Path(source.path)
        if not root.is_dir():
            self.reason = "unreachable"
            return "periodic"
        count = self._tree_hint
        if count is None:
            rules = IgnoreRules.for_root(root, source.rules_json)
            count = _count_entries(root, rules, self.mon.large_tree_entries, 10.0)
        if count > self.mon.large_tree_entries:
            self.reason = "large"
            return "periodic"
        return "watch"

    def _relevant(self, root: Path, rules: IgnoreRules, change: object, raw: str) -> tuple[bool, bool]:
        return classify_event(root, rules, change, raw, enabled=self.mon.ignore_enabled)

    def _watch_loop(self) -> str:
        source = self._source()
        if source is None:
            return "periodic"
        root = Path(source.path)
        rules = IgnoreRules.for_root(root, source.rules_json)
        deb = Debouncer(self.mon.debounce_s, self.mon.max_batch_s)
        self.mode, self.reason = "watching", ""
        try:
            events = self.mon.watch_factory(
                root,
                stop_event=self.stop,
                rust_timeout=_WATCH_POLL_MS,
                yield_on_timeout=True,
                debounce=400,
                raise_interrupt=False,
                watch_filter=lambda change, raw: any(self._relevant(root, rules, change, raw)),
            )
            for batch in events:
                if self.stop.is_set():
                    return "stop"
                now = self.mon.clock()
                paths: list[Path] = []
                rescan = False
                for change, raw in batch:
                    keep, scan = self._relevant(root, rules, change, raw)
                    if keep:
                        paths.append(Path(raw))
                    rescan = rescan or scan
                if paths or rescan:
                    deb.add(paths, now, rescan=rescan)
                if not self._root_ok(source):
                    self.reason = "unreachable"
                    return "periodic"
                if deb.due(now):
                    out, scan_needed = deb.take()
                    if scan_needed:
                        out = sorted({*out, *self._scan("watch")})
                    self._deliver(out, "watch")
        except Exception as exc:  # noqa: BLE001 -- inotify limits, vanished root, ...
            logger.info("watch for %s unavailable (%s); using periodic checks", self.source_id, exc)
            self.reason = "error"
            return "periodic"
        if self.stop.is_set():
            return "stop"
        self.reason = "error"  # the watcher ended on its own
        return "periodic"

    def _periodic_loop(self) -> str:
        self.mode = "periodic"
        retry_watch = self.reason in ("unreachable", "error")
        while not self.stop.is_set():
            if self.stop.wait(self.mon.periodic_s):
                return "stop"
            self._tick("periodic")
            if retry_watch:
                source = self._source()
                if source is not None and Path(source.path).is_dir():
                    return "watch"
        return "stop"


class FreshnessMonitor:
    """Owns one watcher thread per source (see module docstring)."""

    def __init__(
        self,
        source_manager,
        pipeline_provider: Callable[[], object],
        *,
        session_factory: sessionmaker | None = None,
        on_batch: Callable[[Batch], None] | None = None,
        settings: object | None = None,
        debounce_s: float = DEBOUNCE_S,
        max_batch_s: float = MAX_BATCH_S,
        periodic_s: float = PERIODIC_S,
        large_tree_entries: int = LARGE_TREE_ENTRIES,
        catch_up_budget_s: float = CATCH_UP_BUDGET_S,
        catch_up_max_entries: int | None = CATCH_UP_MAX_ENTRIES,
        watch_factory: Callable[..., Iterable] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.source_manager = source_manager
        self.pipeline_provider = pipeline_provider
        self.session_factory = session_factory or source_manager._session_factory
        self.on_batch: Callable[[Batch], None] = on_batch or (lambda _b: None)
        self.settings = settings
        self.ignore_enabled = bool(getattr(settings, "ingest_ignore_enabled", True))
        self.debounce_s = debounce_s
        self.max_batch_s = max_batch_s
        self.periodic_s = periodic_s
        self.large_tree_entries = large_tree_entries
        self.catch_up_budget_s = catch_up_budget_s
        self.catch_up_max_entries = catch_up_max_entries
        self.watch_factory = watch_factory or watchfiles.watch
        self.clock = clock
        self._lock = threading.Lock()
        self._workers: dict[str, _SourceWatch] = {}
        self._closed = False

    # -- lifecycle -----------------------------------------------------------
    def start(self, source_ids: Iterable[str] | None = None) -> list[str]:
        """Start watching every ACTIVE (or temporarily MISSING), not paused
        source (or just `source_ids`). Returns the ids actually started."""
        started: list[str] = []
        wanted = None if source_ids is None else set(source_ids)
        for source in self.source_manager.list_sources():
            if wanted is not None and source.id not in wanted:
                continue
            if self.start_source(source.id):
                started.append(source.id)
        return started

    def start_source(
        self, source_id: str, *, catch_up_first: bool = False, tree_hint: int | None = None
    ) -> bool:
        if self._closed:
            return False
        try:
            source = self.source_manager.get_source(source_id)
        except SourceNotFoundError:
            return False
        if source.status not in (SourceStatus.ACTIVE, SourceStatus.MISSING):
            return False
        if source.sync_paused_at is not None:
            return False
        with self._lock:
            current = self._workers.get(source_id)
            if current is not None and current.is_alive():
                return True
            worker = _SourceWatch(self, source_id, catch_up_first=catch_up_first, tree_hint=tree_hint)
            self._workers[source_id] = worker
        worker.start()
        return True

    def stop_source(self, source_id: str, *, timeout: float = 5.0) -> None:
        with self._lock:
            worker = self._workers.pop(source_id, None)
        if worker is not None:
            worker.stop.set()
            if worker.is_alive() and worker is not threading.current_thread():
                worker.join(timeout)

    def pause(self, source_id: str) -> None:
        """Persist the pause (`sync_paused_at`) and stop watching."""
        self.source_manager.pause_sync(source_id)
        self.stop_source(source_id)

    def resume(self, source_id: str) -> bool:
        """Clear the pause and watch again; the first thing the new watcher
        does is a catch-up for what changed meanwhile."""
        self.source_manager.resume_sync(source_id)
        return self.start_source(source_id, catch_up_first=True)

    def is_paused(self, source_id: str) -> bool:
        try:
            return self.source_manager.get_source(source_id).sync_paused_at is not None
        except SourceNotFoundError:
            return False

    def mode(self, source_id: str) -> str:
        """"watching", "periodic", "paused" or "stopped"."""
        if self.is_paused(source_id):
            return "paused"
        with self._lock:
            worker = self._workers.get(source_id)
        if worker is None or not worker.is_alive():
            return "stopped"
        return worker.mode

    def reason(self, source_id: str) -> str:
        with self._lock:
            worker = self._workers.get(source_id)
        return worker.reason if worker else ""

    def close(self, timeout: float = 5.0) -> None:
        """Stop every watcher and wait (bounded) for the threads."""
        self._closed = True
        with self._lock:
            workers = list(self._workers.values())
            self._workers.clear()
        for w in workers:
            w.stop.set()
        deadline = time.monotonic() + timeout
        for w in workers:
            if w.is_alive() and w is not threading.current_thread():
                w.join(max(deadline - time.monotonic(), 0.05))

    # -- running a batch -----------------------------------------------------
    def run_batch(self, batch: Batch, progress: Callable | None = None):
        """Ingest one batch through the shared path-level entry. Called by the
        controller when it decides the batch may run."""
        pipeline = self.pipeline_provider()
        return pipeline.run_ingestion_for_paths(
            batch.source_id, list(batch.paths), progress, missing="skip"
        )
