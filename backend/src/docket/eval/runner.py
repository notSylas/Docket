"""Runs a gold set through `QueryService` against a throwaway data dir.

The corpus folder is ingested into a temp data dir (never the user's real one).
Each immediate sub-folder of the corpus is one *source*, named after the folder,
so a question's `setup.revoke: [name]` can revoke it; a corpus with no
sub-folders is a single source named after the folder itself.

Recording is done by decorating the services `QueryService` already accepts
(inference gateway, evidence resolver) -- no production code is modified:

- `RecordingGateway` captures the exact system/prompt sent, the reply, latency,
  and (when the inner gateway exposes it) Ollama's `prompt_eval_count`.
- `RecordingResolver` captures the chunks that were resolved for the prompt,
  which is exactly the retrieved set on the fast path.
"""

from __future__ import annotations

import shutil
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import ollama as _ollama
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from docket.cli.context import AppContext, _ensure_schema
from docket.config import Settings, settings
from docket.db.engine import get_engine, get_session_factory
from docket.db.models import Chunk, Source, SourceStatus
from docket.eval.schema import (
    GoldSet,
    Question,
    RecordedChunk,
    RecordedCitation,
    RunRecord,
    Split,
)
from docket.eval.scoring import normalize_text
from docket.inference.gateway import (
    InferenceError,
    InferenceGateway,
    OllamaGateway,
    _translate_error,
)
from docket.query.classifier import QueryMode
from docket.query.conversation import ConversationTurn
from docket.query.service import QueryService
from docket.retrieval.resolver import EvidenceResolver, ResolvedEvidence

ProgressFn = Callable[[int, int, RunRecord], None]


class EvalSetupError(Exception):
    """The corpus or a question's setup cannot be run (message is user-facing)."""


class OllamaMetaGateway(OllamaGateway):
    """`OllamaGateway` that also keeps the last response's token counts in
    `last_generate_meta`, which `RecordingGateway` picks up. Lives here so the
    production gateway stays untouched."""

    last_generate_meta: dict[str, Any] | None = None

    def generate(self, *, system: str, prompt: str, **opts) -> str:
        # Reuses the base class's num_ctx/num_predict-default merging (see
        # OllamaGateway.generate) rather than calling _ollama.generate
        # directly, so eval runs measure the same options production calls
        # get -- otherwise M1's context-window fix would silently not apply
        # to eval harness runs.
        opts = dict(opts)
        options = dict(opts.get("options") or {})
        options.setdefault("num_ctx", settings.num_ctx)
        options.setdefault("num_predict", settings.num_predict)
        opts["options"] = options
        try:
            response = _ollama.generate(
                model=self.gen_model, system=system, prompt=prompt, **opts
            )
        except Exception as exc:
            raise _translate_error(exc, self.gen_model) from exc
        self.last_generate_meta = {
            "prompt_eval_count": response.get("prompt_eval_count"),
            "eval_count": response.get("eval_count"),
        }
        return response["response"]


@dataclass
class GenerateCall:
    system: str
    prompt: str
    response: str
    latency_s: float
    prompt_eval_count: int | None


class RecordingGateway:
    """Delegates to `inner` and remembers every `generate` call since `reset()`."""

    def __init__(self, inner: InferenceGateway):
        self.inner = inner
        self.calls: list[GenerateCall] = []

    def reset(self) -> None:
        self.calls = []

    def generate(self, *, system: str, prompt: str, **opts) -> str:
        start = time.perf_counter()
        response = self.inner.generate(system=system, prompt=prompt, **opts)
        latency = time.perf_counter() - start
        meta = getattr(self.inner, "last_generate_meta", None) or {}
        self.calls.append(
            GenerateCall(system, prompt, response, latency, meta.get("prompt_eval_count"))
        )
        return response

    def embed(self, text: str) -> list[float]:
        return self.inner.embed(text)


class RecordingResolver:
    """Delegates to a real `EvidenceResolver`, recording resolved chunks."""

    def __init__(self, inner: EvidenceResolver):
        self.inner = inner
        self.resolved: list[ResolvedEvidence] = []

    def reset(self) -> None:
        self.resolved = []

    def resolve(self, chunk_id: str) -> ResolvedEvidence:
        chunk = self.inner.resolve(chunk_id)
        self.resolved.append(chunk)
        return chunk

    def resolve_many(self, chunk_ids: list[str]) -> list[ResolvedEvidence]:
        chunks = self.inner.resolve_many(chunk_ids)
        self.resolved.extend(chunks)
        return chunks


class _EvalContext(AppContext):
    """`AppContext` rooted at an explicit data dir with injected gateway/parser,
    so no environment variable or real user data is involved."""

    def __init__(self, data_dir: Path, gateway: InferenceGateway, parser: Any | None):
        self.settings = Settings(data_dir=data_dir)
        self.settings.ensure_data_dirs()
        _ensure_schema(self.settings.sqlite_path)
        self.engine = get_engine(self.settings.sqlite_path)
        self.session_factory: sessionmaker = get_session_factory(self.engine)
        # cached_property stores into the instance dict, so pre-seeding it
        # replaces the default OllamaGateway / DoclingParser construction.
        self.__dict__["gateway"] = gateway
        if parser is not None:
            self.__dict__["parser"] = parser


@dataclass(frozen=True)
class CorpusChunk:
    chunk_id: str
    source_name: str
    heading: str | None
    text: str


def discover_sources(corpus_dir: Path) -> dict[str, Path]:
    """Map source name -> folder (see module docstring for the rule)."""
    corpus_dir = Path(corpus_dir)
    if not corpus_dir.is_dir():
        raise EvalSetupError(f"corpus folder does not exist: {corpus_dir}")
    subdirs = sorted(p for p in corpus_dir.iterdir() if p.is_dir() and not p.name.startswith("."))
    if not subdirs:
        return {corpus_dir.name: corpus_dir}
    loose = [p.name for p in corpus_dir.iterdir() if p.is_file() and not p.name.startswith(".")]
    if loose:
        raise EvalSetupError(
            f"corpus {corpus_dir} mixes sub-folders (sources) and loose files {loose}; "
            "put every file inside a source sub-folder"
        )
    return {p.name: p for p in subdirs}


def _to_record_chunks(chunks: list[ResolvedEvidence]) -> list[RecordedChunk]:
    return [
        RecordedChunk(
            chunk_id=c.chunk_id,
            text=c.text,
            citation_label=c.citation_label,
            source_display_name=c.source_display_name,
        )
        for c in chunks
    ]


class EvalRunner:
    """Owns one ingested temp workspace and runs questions against it."""

    def __init__(
        self,
        corpus_dir: Path,
        *,
        gateway: InferenceGateway,
        parser: Any | None = None,
        data_dir: Path | None = None,
        top_k: int = 8,
        mode: QueryMode | None = None,
    ):
        self._sources = discover_sources(corpus_dir)
        self._owns_data_dir = data_dir is None
        # mkdtemp (not TemporaryDirectory) so `close()` decides when to delete.
        self._data_dir = Path(data_dir) if data_dir else Path(tempfile.mkdtemp(prefix="docket-eval-"))
        self._gateway = RecordingGateway(gateway)
        self._context = _EvalContext(self._data_dir, self._gateway, parser)
        self._top_k = top_k
        self._mode = mode
        self._source_ids: dict[str, str] = {}
        self._indexed_text: list[str] = []
        self.ingest_failures: list[str] = []
        self._resolver: RecordingResolver | None = None
        self._service: QueryService | None = None

    # -- lifecycle --------------------------------------------------------

    def __enter__(self) -> EvalRunner:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._context.engine.dispose()
        if self._owns_data_dir:
            shutil.rmtree(self._data_dir, ignore_errors=True)

    # -- ingestion --------------------------------------------------------

    def ingest(self, progress: Callable[[str, Any], None] | None = None) -> None:
        ctx = self._context
        for name, path in self._sources.items():
            source = ctx.source_manager.register_source(path)
            self._source_ids[name] = source.id
            result = ctx.pipeline.run_ingestion_for_source(source.id, progress)
            for file_result in result.file_results:
                if file_result.status == "failed":
                    self.ingest_failures.append(f"{name}/{file_result.path.name}: {file_result.error}")
        table = ctx.vector_writer.table
        if table is None:
            raise EvalSetupError(f"nothing was indexed from {sorted(self._sources)}: {self.ingest_failures}")

        with ctx.session_factory() as session:
            texts = session.execute(select(Chunk.text)).scalars().all()
        self._indexed_text = [normalize_text(t) for t in texts]

        self._resolver = RecordingResolver(ctx.resolver)
        self._service = QueryService(
            engine=ctx.engine,
            table=table,
            gateway=self._gateway,  # type: ignore[arg-type]
            resolver=self._resolver,  # type: ignore[arg-type]
            top_k=self._top_k,
            settings=ctx.settings,
        )

    def corpus_chunks(self) -> list[CorpusChunk]:
        """Every indexed chunk (parsed text, as stored) with its source name."""
        names = {sid: name for name, sid in self._source_ids.items()}
        with self._context.session_factory() as session:
            rows = session.execute(
                select(Chunk.id, Chunk.source_id, Chunk.heading, Chunk.text).order_by(
                    Chunk.source_id, Chunk.ordinal
                )
            ).all()
        return [CorpusChunk(cid, names.get(sid, sid), heading, text) for cid, sid, heading, text in rows]

    # -- running ----------------------------------------------------------

    def check_setup(self, questions: list[Question]) -> None:
        for question in questions:
            for name in question.setup.revoke:
                if name not in self._sources:
                    raise EvalSetupError(
                        f"question {question.id!r} revokes unknown source {name!r}; "
                        f"corpus sources are {sorted(self._sources)}"
                    )

    def _set_status(self, names: list[str], status: SourceStatus) -> None:
        with self._context.session_factory() as session:
            for name in names:
                source = session.get(Source, self._source_ids[name])
                source.status = status
            session.commit()

    def _spans_indexed(self, question: Question) -> list[bool]:
        return [
            any(normalize_text(span) in text for text in self._indexed_text)
            for span in question.gold_spans
        ]

    def run_once(self, question: Question, repeat: int) -> RunRecord:
        assert self._service is not None and self._resolver is not None, "call ingest() first"
        self._gateway.reset()
        self._resolver.reset()
        history = [ConversationTurn(question=t.question, answer=t.answer) for t in question.history]
        record = RunRecord(
            question_id=question.id,
            repeat=repeat,
            spans_indexed=self._spans_indexed(question),
        )
        start = time.perf_counter()
        try:
            result = self._service.ask(question.question, mode=self._mode, history=history or None)
        except InferenceError as exc:
            record.error = f"{type(exc).__name__}: {exc}"
            record.latency_s = time.perf_counter() - start
            return record
        record.latency_s = time.perf_counter() - start

        record.answer = result.answer
        record.abstained = result.abstained
        record.mode = result.mode
        record.validation_warnings = list(result.validation_warnings)
        record.citations = [RecordedCitation(**c.model_dump()) for c in result.citations]
        unique = {c.chunk_id: c for c in self._resolver.resolved}  # first-seen order, deduped
        record.retrieved = _to_record_chunks(list(unique.values()))
        if self._gateway.calls:
            last = self._gateway.calls[-1]  # the answer-generating call
            record.system, record.prompt = last.system, last.prompt
            record.prompt_eval_count = last.prompt_eval_count
        return record

    def run(
        self,
        questions: list[Question],
        *,
        repeats: int = 3,
        out_path: Path | None = None,
        progress: ProgressFn | None = None,
    ) -> list[RunRecord]:
        """Run every question `repeats` times; append each record to
        `out_path` (JSONL) as it finishes so an interrupted run keeps its data.

        Questions with `setup.revoke` run last, each with its sources revoked
        only for the duration of that question (then restored), so ordering
        and other questions are unaffected.
        """
        self.check_setup(questions)
        ordered = [q for q in questions if not q.setup.revoke] + [
            q for q in questions if q.setup.revoke
        ]
        total = len(ordered) * repeats
        records: list[RunRecord] = []
        out = out_path.open("w", encoding="utf-8") if out_path else None
        try:
            done = 0
            for question in ordered:
                self._set_status(question.setup.revoke, SourceStatus.REVOKED)
                try:
                    for repeat in range(repeats):
                        record = self.run_once(question, repeat)
                        records.append(record)
                        done += 1
                        if out:
                            out.write(record.model_dump_json() + "\n")
                            out.flush()
                        if progress:
                            progress(done, total, record)
                finally:
                    self._set_status(question.setup.revoke, SourceStatus.ACTIVE)
        finally:
            if out:
                out.close()
        return records


def select_questions(
    gold: GoldSet, *, split: Split | None = None, ids: list[str] | None = None
) -> list[Question]:
    questions = gold.questions
    if split is not None:
        questions = [q for q in questions if q.split is split]
    if ids:
        wanted = set(ids)
        unknown = wanted - {q.id for q in gold.questions}
        if unknown:
            raise EvalSetupError(f"unknown question ids: {sorted(unknown)}")
        questions = [q for q in questions if q.id in wanted]
    return list(questions)


def run_eval(
    gold: GoldSet,
    corpus_dir: Path,
    out_path: Path,
    *,
    gateway: InferenceGateway,
    parser: Any | None = None,
    repeats: int = 3,
    split: Split | None = None,
    ids: list[str] | None = None,
    top_k: int = 8,
    mode: QueryMode | None = None,
    progress: ProgressFn | None = None,
) -> list[RunRecord]:
    """Ingest `corpus_dir` into a temp data dir, run the selected questions
    `repeats` times each, write JSONL to `out_path`, and clean up the temp dir."""
    questions = select_questions(gold, split=split, ids=ids)
    with EvalRunner(corpus_dir, gateway=gateway, parser=parser, top_k=top_k, mode=mode) as runner:
        runner.ingest()
        return runner.run(questions, repeats=repeats, out_path=out_path, progress=progress)
