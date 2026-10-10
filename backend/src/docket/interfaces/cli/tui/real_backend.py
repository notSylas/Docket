"""``TuiBackend`` over the real services (``AppContext``).

Nothing here is imported by the demo. Everything is built from what the
existing plain interactive session already uses: ``SourceManager``,
``ReadinessService``, the ingestion pipeline's per-file progress callback, the
``QueryService`` factory and the ``EvidenceResolver``.

Rules this module follows (see Upgrade/16-terminal-ui-wiring.md):

* every method opens its own database sessions through ``session_factory``;
  no ORM object or session crosses a thread boundary;
* ``run_index``/``ask``/``readiness`` block and are meant for a worker thread;
  they report only through the callbacks they are handed, with immutable data;
* it never builds the document parser or opens the vector table just to draw
  a panel: ``load_world`` and ``job_history`` read SQLite only.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import func, select

from docket.core.db.models import (
    Chunk,
    EvidenceVersion,
    IngestionJob,
    IngestionJobStatus,
    Source,
    SourceStatus,
    VersionStatus,
)
from docket.infra.retrieval.resolver import ChunkNotFoundError, ResolvedEvidence
from docket.interfaces.cli.interactive.render import number_citations
from docket.interfaces.cli.quiet import condense_error, display_path, ignore_notices, quiet_ingest
from docket.interfaces.cli.tui import fake_data as fd
from docket.interfaces.cli.tui.backend import (
    AnswerView,
    AskOutcome,
    AskRequest,
    BackendError,
    CitationView,
    FileView,
    FolderCheck,
    IndexCallback,
    IndexOutcome,
    IndexProgress,
    JobRecord,
    ReadinessItem,
    ReadinessReport,
    SourceView,
    StageCallback,
    World,
    describe_error,
)
from docket.services.ingestion.pipeline import SUPPORTED_EXTENSIONS, SourceNotActiveError
from docket.services.query.classifier import QueryMode
from docket.services.query.conversation import ConversationTurn
from docket.services.sources.manager import InvalidSourceTransitionError, SourceNotFoundError

_STATUS_MAP = {
    SourceStatus.ACTIVE: fd.READY,
    SourceStatus.MISSING: fd.FAILED,
    SourceStatus.REVOKED: fd.DISCONNECTED,
}
_FORMATS = "PDF, Word, Excel, PowerPoint, text and Markdown"
_MAX_JOBS = 20


class _StopRequested(BaseException):
    """Raised from the progress callback to stop after the current file.

    It deliberately derives from ``BaseException``: the pipeline's ``emit``
    swallows every ``Exception`` raised by a progress callback (so a buggy
    callback cannot break ingestion), which would make a plain exception a
    silent no-op. ``BaseException`` is the same path a Ctrl-C takes: the
    pipeline finalizes the job row as failed ("interrupted") and re-raises.
    Files already completed are kept; the end-of-run reconcile pass is
    skipped (the next Refresh performs it).
    """


def _local_time(value: datetime | None) -> str:
    if value is None:
        return "unknown"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone().strftime("%Y-%m-%d %H:%M")


def _first_line(text: object) -> str:
    lines = (str(text) or "").strip().splitlines()
    return lines[0][:160] if lines else ""


class RealBackend:
    demo = False
    scope_enforced = False  # QueryScope does not exist yet; see Upgrade/16
    can_open_original = False  # needs original-file verification (deferred)
    supported_formats = _FORMATS
    thinking_levels = ("Fast", "Balanced", "Thorough")
    settings_note = (
        "Appearance and answering mode apply to this session only; nothing is saved. "
        "The answer model and thinking level are not connected yet."
    )
    jobs_hint = "per-file results are not stored yet"
    jobs_note = "Per-file results are not stored yet."
    stage_search = "Searching and writing answer"
    stage_citations = "Checking citations"
    intro_notice = "Type a question, or press F1 for commands."

    def __init__(
        self,
        context: Any,
        *,
        query_service_factory: Callable[[Any, Any], Any] | None = None,
        health_check: Callable[[], Any] | None = None,
    ) -> None:
        self._ctx = context
        settings = context.settings
        if query_service_factory is None:
            from docket.interfaces.cli.interactive.session import _default_factory

            query_service_factory = _default_factory
        self._factory = query_service_factory
        if health_check is None:
            from docket.infra.inference.health import check_ollama

            def health_check() -> Any:
                return check_ollama([settings.gen_model, settings.embed_model])

        self._health = health_check
        self._missing: frozenset[str] = frozenset()
        self.data_dir_label = str(settings.data_dir)
        self.embed_model = str(settings.embed_model)
        self.default_model = str(settings.gen_model)
        self.modes = (
            ("auto", "Auto", "Docket chooses the route. Recommended.", True, ""),
            ("fast", "Quick search", "Search and answer directly from retrieved passages.", True, ""),
            (
                "agent",
                "Agent investigation",
                "Investigate step by step with the agent. Slower, and less accurate than quick search today.",
                True,
                "",
            ),
            ("plan", "Plan", "Plan steps and ask you to approve them first.", False, "Not available yet: planning is not implemented in the backend."),
        )
        self.command_extras = (
            ("scope", "choose what to search", ""),
            ("jobs", "show indexing jobs and results", ""),
            ("settings", "answering, appearance, history, system", ""),
            ("details", "details of the latest answer", ""),
            ("rechunk", "update stored chunks", "Not available in this screen yet. Use: docket ingest --all --rechunk"),
            ("reindex", "rebuild the search index", "Not available in this screen yet. Use: docket reindex"),
        )

    # -- models ------------------------------------------------------------
    @property
    def answer_models(self) -> tuple[tuple[str, bool, str], ...]:
        name = self.default_model
        if name in self._missing:
            return ((name, False, f"not installed — run: ollama pull {name}"),)
        return ((name, True, ""),)

    # -- state -------------------------------------------------------------
    def _names(self, sources: list[Source]) -> dict[str, str]:
        """Display names: the folder name, with its parent added when two collide."""
        base = {s.id: (Path(s.path).name or s.path) for s in sources}
        counts: dict[str, int] = {}
        for n in base.values():
            counts[n] = counts.get(n, 0) + 1
        out: dict[str, str] = {}
        for s in sources:
            n = base[s.id]
            if counts[n] > 1:
                parent = Path(s.path).parent.name
                n = f"{n} ({parent})" if parent else n
            out[s.id] = n
        return out

    def load_world(self) -> World:
        sf = self._ctx.session_factory
        with sf() as session:
            sources = [s for s in session.execute(select(Source)).scalars().all() if s.status != SourceStatus.DELETED]
            versions = session.execute(
                select(
                    EvidenceVersion.source_id,
                    EvidenceVersion.file_path,
                    EvidenceVersion.status,
                    EvidenceVersion.observed_at,
                    func.count(Chunk.id),
                )
                .outerjoin(Chunk, Chunk.evidence_version_id == EvidenceVersion.id)
                .where(EvidenceVersion.status != VersionStatus.SUPERSEDED)
                .group_by(EvidenceVersion.id)
            ).all()
            last = dict(
                session.execute(
                    select(IngestionJob.source_id, func.max(IngestionJob.finished_at))
                    .where(IngestionJob.status.in_((IngestionJobStatus.SUCCEEDED, IngestionJobStatus.PARTIAL)))
                    .group_by(IngestionJob.source_id)
                ).all()
            )
        names = self._names(sources)
        latest: dict[tuple[str, str], tuple[datetime, VersionStatus, int]] = {}
        for source_id, file_path, status, observed, n_chunks in versions:
            key = (source_id, file_path or "")
            if key not in latest or observed >= latest[key][0]:
                latest[key] = (observed, status, int(n_chunks))
        by_source: dict[str, list[FileView]] = {}
        roots = {s.id: s.path for s in sources}
        for (source_id, file_path), (_obs, status, n_chunks) in sorted(latest.items(), key=lambda kv: kv[0]):
            root = roots.get(source_id)
            if root is None:
                continue
            rel = display_path(Path(file_path), Path(root)) if file_path else Path(root).name
            if status == VersionStatus.READY:
                fv = FileView(rel, Path(rel).suffix.lstrip(".").lower(), fd.FILE_READY if n_chunks > 0 else fd.FILE_EMPTY, n_chunks)
            elif status == VersionStatus.FAILED:
                fv = FileView(rel, Path(rel).suffix.lstrip(".").lower(), fd.FILE_FAILED, 0, "reason not stored; refresh to see it again")
            else:
                fv = FileView(rel, Path(rel).suffix.lstrip(".").lower(), fd.FILE_PENDING, 0)
            by_source.setdefault(source_id, []).append(fv)
        views: list[SourceView] = []
        for s in sources:
            files = by_source.get(s.id, [])
            status = _STATUS_MAP.get(s.status, fd.UNAVAILABLE)
            if status == fd.READY:
                note = "" if files else "Not indexed yet. Choose Refresh to index it."
            elif status == fd.FAILED:
                note = f"The folder cannot be reached at {s.path}. Restore it, then choose Retry."
            elif status == fd.DISCONNECTED:
                if s.status_reason in (None, "user_disconnected"):
                    note = "Disconnected by you. Stored originals are kept; it is not searched."
                else:
                    note = "Access to this folder was lost. It must be authorized again before it can be reconnected."
            else:
                note = "This source is not available. Its stored data may be removed by a cleanup."
            finished = last.get(s.id)
            views.append(
                SourceView(
                    s.id,
                    names[s.id],
                    s.path,
                    status,
                    files,
                    last_indexed=_local_time(finished) if finished else "Never indexed",
                    note=note,
                )
            )
        return World(sources=views)

    def readiness(self) -> ReadinessReport:
        settings = self._ctx.settings
        gen, emb = str(settings.gen_model), str(settings.embed_model)
        health = self._health()
        items: list[ReadinessItem] = []
        help_parts: list[str] = []
        if not health.reachable:
            self._missing = frozenset()
            host = getattr(health, "host", "the configured address")
            items.append(ReadinessItem("Ollama", f"Not reachable at {host}", True))
            items.append(ReadinessItem("Answer model", f"{gen} — unknown", True))
            items.append(ReadinessItem("Embedding model", f"{emb} — unknown", True))
            help_parts.append("Start Ollama (ollama serve), then check again.")
        else:
            missing = set(health.missing_models)
            self._missing = frozenset(missing)
            items.append(ReadinessItem("Ollama", "Available"))
            for label, model in (("Answer model", gen), ("Embedding model", emb)):
                if model in missing:
                    items.append(ReadinessItem(label, f"{model} — not installed", True))
                    help_parts.append(f"ollama pull {model}")
                else:
                    items.append(ReadinessItem(label, f"{model} — installed"))
        try:
            snap = self._ctx.readiness.snapshot()
            docs = f"{snap.files} files · {snap.chunks} chunks" if snap.chunks else "None yet"
            items.append(ReadinessItem("Searchable documents", docs))
        except Exception as exc:  # noqa: BLE001 -- shown, not raised
            items.append(ReadinessItem("Searchable documents", f"Unavailable: {_first_line(exc)}", True))
        blocked = (not health.reachable) or bool(health.missing_models)
        help_text = ""
        if blocked:
            pulls = [p for p in help_parts if p.startswith("ollama pull")]
            help_text = help_parts[0] if not health.reachable else "Run: " + "; ".join(pulls)
        stamp = time.strftime("%H:%M:%S")
        message = f"Checked at {stamp}: " + ("still blocked." if blocked else "Ollama and both models are available.")
        return ReadinessReport(tuple(items), blocked, help_text, message)

    def job_history(self, *, running: bool) -> list[JobRecord]:
        sf = self._ctx.session_factory
        with sf() as session:
            sources = list(session.execute(select(Source)).scalars().all())
            jobs = list(session.execute(select(IngestionJob).order_by(IngestionJob.started_at.desc()).limit(_MAX_JOBS)).scalars().all())
        names = self._names(sources)
        out: list[JobRecord] = []
        for job in jobs:
            if job.status in (IngestionJobStatus.RUNNING, IngestionJobStatus.PENDING):
                if running:
                    continue  # the live job is listed separately
                state = "Interrupted"
            elif job.status == IngestionJobStatus.SUCCEEDED:
                state = "Completed"
            elif job.status == IngestionJobStatus.PARTIAL:
                state = "Partial"
            else:
                state = "Interrupted" if (job.error or "").strip() == "interrupted" else "Failed"
            summary = ""
            try:
                import json

                stats = json.loads(job.stats_json or "{}")
                summary = (
                    f"{stats.get('files_processed', 0)} processed · {stats.get('files_failed', 0)} failed · "
                    f"{stats.get('chunks_written', 0)} chunks written"
                )
            except ValueError:
                pass
            if state == "Interrupted" and not job.error:
                summary = "Stopped when the process exited; refresh to finish."
            elif job.error and state != "Completed":
                summary = (summary + " — " if summary else "") + condense_error(job.error)
            out.append(JobRecord(names.get(job.source_id, job.source_id), state, _local_time(job.finished_at or job.started_at), summary))
        return out

    def system_rows(self, world: World, readiness: ReadinessReport) -> list[tuple[str, str]]:
        by_label = {i.label: i.value for i in readiness.items}
        return [
            ("Data folder", self.data_dir_label),
            ("Ollama", by_label.get("Ollama", "Not checked yet")),
            ("Answer model", self.default_model),
            ("Embedding", self.embed_model),
            ("Index", "Compatibility is checked when you ask or index."),
            ("Coverage", f"{world.ready_files()} files searchable"),
        ]

    # -- folders and sources -----------------------------------------------
    def folder_suggestions(self, typed: str) -> list[str]:
        t = typed.strip()
        if not t:
            return []
        try:
            p = Path(t).expanduser()
            if t.endswith(("/", os.sep)):
                parent, prefix = p, ""
            else:
                parent, prefix = p.parent, p.name.lower()
            names = []
            with os.scandir(parent) as it:
                for e in it:
                    if e.name.startswith(".") and not prefix.startswith("."):
                        continue
                    if e.name.lower().startswith(prefix) and e.is_dir():
                        names.append(e.name)
                    if len(names) >= 200:
                        break
            return [str(parent / n) for n in sorted(names, key=str.lower)[:6] if str(parent / n) != t]
        except OSError:
            return []

    def is_broad_location(self, path: str) -> bool:
        if path in (".", "~", "/"):
            return True
        try:
            r = Path(path).expanduser().resolve()
        except OSError:
            return False
        return r == Path("/") or r == Path.home().resolve()

    def check_folder(self, path: str) -> FolderCheck:
        path = path.strip()
        if not path:
            return FolderCheck(False, "", "Enter a folder path first.")
        try:
            resolved = Path(path).expanduser().resolve()
        except (OSError, RuntimeError) as exc:
            return FolderCheck(False, path, f"That path cannot be used: {_first_line(exc)}")
        if not resolved.exists():
            return FolderCheck(False, str(resolved), "That folder does not exist.")
        if not resolved.is_dir():
            return FolderCheck(False, str(resolved), "That path is a file. Choose the folder that contains your documents.")
        if not os.access(resolved, os.R_OK | os.X_OK):
            return FolderCheck(False, str(resolved), "Docket cannot read that folder (permission denied).")
        sources = self._ctx.source_manager.list_sources()
        names = self._names(sources)
        for s in sources:
            try:
                same = Path(s.path).expanduser().resolve() == resolved
            except OSError:
                same = False
            if not same:
                continue
            name = names.get(s.id, Path(s.path).name)
            if s.status == SourceStatus.REVOKED:
                return FolderCheck(False, str(resolved), f"{name} is disconnected. Use Reconnect in Sources instead of adding it again.")
            if s.status in (SourceStatus.ACTIVE, SourceStatus.MISSING):
                return FolderCheck(False, str(resolved), f"{name} is already registered at that path.")
            return FolderCheck(False, str(resolved), f"{name} is {s.status.value}; it cannot be registered again here.")
        return FolderCheck(True, str(resolved))

    def add_source(self, path: str) -> SourceView:
        try:
            source = self._ctx.source_manager.register_source(Path(path))
        except ValueError as exc:
            raise BackendError(f"Error: {exc}") from exc
        return SourceView(source.id, Path(source.path).name or source.path, source.path, fd.READY, [], last_indexed="Never indexed")

    def disconnect_source(self, source_id: str) -> None:
        try:
            self._ctx.source_manager.deactivate_source(source_id, reason="user_disconnected")
        except (SourceNotFoundError, InvalidSourceTransitionError) as exc:
            raise BackendError(f"Error: {exc}") from exc

    def reconnect_source(self, source_id: str) -> None:
        try:
            self._ctx.source_manager.reconnect_source(source_id)
        except (SourceNotFoundError, InvalidSourceTransitionError, ValueError) as exc:
            raise BackendError(f"Error: {exc}") from exc

    def job_finished(self, source_id: str, on_done: str) -> None:
        return None  # the pipeline already recorded the outcome

    def open_original_note(self, citation: CitationView) -> str:
        return "Opening the original file is not available yet."

    # -- indexing ------------------------------------------------------------
    def run_index(self, source_id: str, on_progress: IndexCallback, should_stop: Callable[[], bool]) -> IndexOutcome:
        ctx = self._ctx
        try:
            root: Path | None = Path(ctx.source_manager.get_source(source_id).path)
        except Exception:  # noqa: BLE001 -- display nicety only
            root = None
        indexed = unchanged = failed = 0
        failures: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        index = total = 0
        current = ""

        def snapshot() -> IndexProgress:
            return IndexProgress(index, total, current, indexed, unchanged, failed, tuple(failures))

        def on_event(event: Any) -> None:
            nonlocal indexed, unchanged, failed, index, total, current
            total = event.total
            current = display_path(event.path, root)
            if event.kind == "start":
                index = event.index - 1
                on_progress(snapshot())
                if should_stop():
                    raise _StopRequested()
                return
            index = event.index
            result = event.result
            if result is not None:
                if result.status == "failed":
                    failed += 1
                    pair = (current, condense_error(result.error))
                    if pair not in seen:
                        seen.add(pair)
                        failures.append(pair)
                elif result.status == "unchanged":
                    unchanged += 1
                else:
                    indexed += 1
            on_progress(snapshot())
            if should_stop() and event.index < event.total:
                raise _StopRequested()

        cold = "pipeline" not in vars(ctx) and "parser" not in vars(ctx)
        if cold:
            current = "Loading the document parser (first time only; this can take a moment)"
            on_progress(snapshot())
        try:
            with quiet_ingest():
                pipeline = ctx.pipeline
                current = ""
                on_progress(snapshot())
                result = pipeline.run_ingestion_for_source(source_id, progress=on_event)
        except _StopRequested:
            return IndexOutcome("stopped", snapshot())
        except (SourceNotFoundError, SourceNotActiveError) as exc:
            return IndexOutcome("failed", snapshot(), reason=f"Error: {_first_line(exc)}")

        results = list(result.file_results)
        indexed = sum(1 for r in results if r.status == "ingested")
        unchanged = sum(1 for r in results if r.status == "unchanged")
        failed = sum(1 for r in results if r.status == "failed")
        index = total = len(results)
        final = IndexProgress(index, total, "", indexed, unchanged, failed, tuple(failures))
        if result.files_processed == 0 and result.status == "failed":
            folder = str(root) if root else source_id
            try:
                status = ctx.source_manager.get_source(source_id).status
            except Exception:  # noqa: BLE001
                status = None
            if status == SourceStatus.MISSING:
                reason = f"The folder is unavailable: {folder}. Restore it, then retry."
            else:
                reason = f"No supported files found in {folder} (supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))})."
            return IndexOutcome("failed", final, reason=reason)
        notices: list[str] = []
        try:
            notices.extend(ignore_notices(result, getattr(ctx, "prune_service", None), source_id))
        except Exception:  # noqa: BLE001 -- a hint must never break the outcome
            pass
        try:
            stale = len(ctx.pipeline.find_stale_versions(source_id))
            if stale:
                notices.append(
                    f"{stale} already-ingested file(s) have chunks from an older recipe; "
                    "run `docket ingest --all --rechunk` to update."
                )
        except Exception:  # noqa: BLE001
            pass
        return IndexOutcome("done", final, notices=tuple(notices))

    # -- asking --------------------------------------------------------------
    def ask(self, request: AskRequest, on_stage: StageCallback, scope_label: str, mode_label: str) -> AskOutcome:
        ctx = self._ctx
        try:
            snap = ctx.readiness.snapshot()
        except Exception as exc:  # noqa: BLE001
            return AskOutcome(error=f"Could not check search readiness: {_first_line(exc)}")
        table = ctx.vector_writer.table if snap.chunks > 0 else None
        if table is None:
            return AskOutcome(error="Nothing is indexed yet. Add a folder and let indexing finish.")
        mode = {"auto": None, "fast": QueryMode.FAST, "agent": QueryMode.AGENT}.get(request.mode)
        history = [ConversationTurn(question=t.question, answer=t.answer) for t in request.history]
        on_stage(self.stage_search)
        started = time.perf_counter()
        try:
            service = self._factory(ctx, table)
            result = service.ask(request.question, mode=mode, history=history)
        except Exception as exc:  # noqa: BLE001
            return AskOutcome(error=describe_error(exc, self.default_model))
        elapsed = time.perf_counter() - started
        on_stage(self.stage_citations)
        text, numbered = number_citations(result.answer, list(result.citations))
        citations = self._citation_views(numbered)
        answer = AnswerView(
            text=text,
            citations=tuple(citations),
            mode_label="Investigated with agent" if str(result.mode) == "agent" else "Quick search",
            elapsed=elapsed,
            scope_label=self._scope_label(scope_label, result),
            rewrite=self._rewrite_text(request.question, result),
            ambiguity=self._ambiguity_text(result),
            warnings=tuple(str(w) for w in (result.validation_warnings or ())),
            compute=self._compute_text(result),
            abstained=bool(result.abstained),
            abstain_reason="The retrieved evidence did not support an answer." if result.abstained else "",
            status="No answer" if result.abstained else "Answered; citations are not verified",
            model=self.default_model,
            searched=-1,  # the query service does not report how many passages it searched
        )
        return AskOutcome(answer=answer, history_answer=result.answer)

    # -- answer details ------------------------------------------------------
    @staticmethod
    def _scope_label(scope_label: str, result: Any) -> str:
        named = getattr(result, "scoped_to", None)
        if named:
            return "Named in the question: " + ", ".join(named)
        return scope_label

    @staticmethod
    def _rewrite_text(question: str, result: Any) -> str:
        q = getattr(result, "standalone_query", None)
        if q and q.strip() and q.strip() != question.strip():
            return f'Follow-up rewritten as: "{q.strip()}"'
        return ""

    @staticmethod
    def _ambiguity_text(result: Any) -> str:
        amb = getattr(result, "ambiguity", None)
        if not amb:
            return ""
        options = amb.get("options") or []
        parts = [f"{o.get('fiscal_year', '?')} ({o.get('file', '?')})" for o in options if isinstance(o, dict)]
        reason = "Period not stated"
        if parts:
            return f"{reason}; the answer was asked to cover each period found: " + ", ".join(parts)
        return reason + "."

    @staticmethod
    def _compute_text(result: Any) -> str:
        rec = getattr(result, "compute", None)
        if not rec:
            return "Deterministic computation was not used."
        status = rec.get("status")
        if status == "used":
            steps = []
            for s in rec.get("derivation") or []:
                if isinstance(s, dict):
                    steps.append(f"{s.get('expression', s.get('operation', 'step'))} = {s.get('result_text', '?')}{(' ' + s['units']) if s.get('units') else ''}")
            return "Computed deterministically" + (": " + "; ".join(steps[:3]) if steps else ".")
        reason = rec.get("fallback_reason") or "no reason recorded"
        return f"Deterministic computation was attempted but not used: {_first_line(reason)}"

    def _citation_views(self, numbered: list[Any]) -> list[CitationView]:
        """One view per numbered citation: resolved text and location from the
        resolver, version facts from the evidence-version row. A citation whose
        chunk no longer resolves (source disconnected, file superseded) becomes
        an explained "unavailable" entry rather than an error."""
        ctx = self._ctx
        ids = [c.chunk_id for c in numbered]
        resolved: dict[str, ResolvedEvidence] = {}
        try:
            for ev in ctx.resolver.resolve_many(ids):
                resolved[ev.chunk_id] = ev
        except ChunkNotFoundError:
            for cid in ids:
                try:
                    resolved[cid] = ctx.resolver.resolve(cid)
                except ChunkNotFoundError:
                    pass
        meta: dict[str, tuple[str | None, datetime, str, str, str]] = {}
        history: dict[tuple[str, str | None], list[str]] = {}
        names: dict[str, str] = {}
        version_ids = sorted({e.evidence_version_id for e in resolved.values()})
        if version_ids:
            with ctx.session_factory() as session:
                rows = session.execute(
                    select(EvidenceVersion.id, EvidenceVersion.file_path, EvidenceVersion.observed_at, EvidenceVersion.content_hash, Source.path, Source.id)
                    .join(Source, Source.id == EvidenceVersion.source_id)
                    .where(EvidenceVersion.id.in_(version_ids))
                ).all()
                for vid, file_path, observed, chash, root, sid in rows:
                    meta[vid] = (file_path, observed, chash, root, sid)
                pairs = {(m[4], m[0]) for m in meta.values()}
                for sid, fpath in pairs:
                    ids_in_order = session.execute(
                        select(EvidenceVersion.id)
                        .where(EvidenceVersion.source_id == sid, EvidenceVersion.file_path == fpath)
                        .order_by(EvidenceVersion.observed_at, EvidenceVersion.id)
                    ).scalars().all()
                    history[(sid, fpath)] = list(ids_in_order)
                names = self._names(list(session.execute(select(Source)).scalars().all()))
        # the same relative path under two sources must stay distinguishable
        owners: dict[str, set[str]] = {}
        for ev in resolved.values():
            file_path, _o, _h, root, sid = meta.get(ev.evidence_version_id, (None, None, "", "", ev.source_id))
            rel = display_path(Path(file_path), Path(root)) if root and file_path else ev.source_display_name
            owners.setdefault(rel, set()).add(sid)
        out: list[CitationView] = []
        for c in numbered:
            ev = resolved.get(c.chunk_id)
            if ev is None:
                out.append(
                    CitationView(
                        c.source_display_name,
                        c.source_display_name,
                        None,
                        "",
                        available=False,
                        reason="This passage can no longer be shown: its source was disconnected, or the file changed after the answer was written.",
                        chunk_id=c.chunk_id,
                    )
                )
                continue
            file_path, observed, _chash, root, sid = meta.get(ev.evidence_version_id, (None, None, "", "", ev.source_id))
            rel = display_path(Path(file_path), Path(root)) if root and file_path else ev.source_display_name
            source_name = names.get(sid, Path(root).name if root else "")
            shown = f"{source_name}/{rel}" if len(owners.get(rel, ())) > 1 and source_name else rel
            order = history.get((sid, file_path), [])
            version = order.index(ev.evidence_version_id) + 1 if ev.evidence_version_id in order else 1
            out.append(
                CitationView(
                    Path(rel).name,
                    shown,
                    ev.location,
                    ev.text,
                    available=True,
                    version=version,
                    versions=max(len(order), version),
                    indexed=_local_time(observed) if observed else "unknown",
                    chunk_id=c.chunk_id,
                    source=source_name,
                )
            )
        return out
