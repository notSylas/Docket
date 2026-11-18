#!/usr/bin/env python
"""TEST-ONLY worker for the R2b-1c end-to-end evidence tests.

Runs the REAL ``docket.interfaces.worker`` protocol layer (``serve`` /
``build_methods``: ping, status, health, sources.list, query.ask, evidence.get,
the real FEATURES list) against a REAL scratch library (alembic-migrated SQLite
through the app's own ``AppContext``, real ``EvidenceResolver``, real
``EvidenceReader``, real ``QueryService.ask`` including scope resolution,
citation validation and ``_citation_grants``).

Stubbed (and only these): vector/semantic retrieval (``QueryService._hybrid`` is
replaced by a deterministic word-overlap ranking over SQLite chunks, restricted to
the real scope's version ids) and LLM generation (a fake gateway that composes an
answer citing the labels of the retrieved chunks). No Ollama, GPU or model.

Environment (all optional):
  E2E_OLD=1             emulate an OLD worker: no ``evidence.v1`` feature, no
                        ``evidence.get`` method, citations without grant fields
  E2E_COUNTER_FILE      one line is appended per ``evidence.get`` call reaching the
                        worker (counts only, never content)
  E2E_PIDFILE           written with this process's pid at start
  E2E_SLOW_IDS_FILE     file of chunk ids (one per line; ``fail:<id>`` makes the real
                        EvidenceReader raise instead); the reader sleeps E2E_SLOW_S
                        seconds for the listed chunks only
  E2E_SLOW_S            default 1.0
  E2E_TOP_N             chunks retrieved per question (default 2)
Magic questions: ``stale ...`` cites the old id of an aliased chunk (agent-path emulation),
``slow <seconds> ...`` sleeps before answering, ``fail ...``
raises, ``nothing ...`` retrieves nothing (real abstention path).
"""

from __future__ import annotations

import os
import re
import sys
import time
from types import SimpleNamespace

os.environ.setdefault("DOCKET_EMBED_TOKENIZER", "heuristic")  # never download a tokenizer

TOP_N = int(os.environ.get("E2E_TOP_N", "2"))
OLD = os.environ.get("E2E_OLD") == "1"


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"\w+", text.lower()) if len(w) >= 3}


def make_service(context):
    from sqlalchemy import select

    from docket.core.db.models import (
        KEYWORD_SERVABLE,
        Chunk,
        ChunkAlias,
        EvidenceVersion,
        Source,
        SourceStatus,
    )
    from docket.services.query.classifier import QueryMode
    from docket.services.query.service import QueryService

    class FastOnly:
        def classify(self, question):
            return QueryMode.FAST

    class FakeGateway:
        """Composes an answer that cites the labels of the chunks it was given."""

        def __init__(self):
            self.svc = None

        def generate(self, *, system, prompt, **opts):
            labels = [lb for lb in self.svc._labels() if lb in prompt]
            if not labels:
                return "I do not know."
            return "Per the library: " + " ".join(f"fact {lb}." for lb in labels)

        def embed(self, text):  # never used
            raise RuntimeError("no embeddings in the e2e harness")

        embed_batch = embed

    class HarnessService(QueryService):
        _ranked: list[str] = []

        def _labels(self):
            try:
                return [r.citation_label for r in self._resolver.resolve_many(list(self._ranked))]
            except Exception:  # noqa: BLE001
                return []

        def _hybrid(self, question, standalone, scope_ids, top_k=None):
            q = _words(question)
            allowed = set(scope_ids) if scope_ids is not None else None
            scored = []
            with context.session_factory() as s:
                rows = s.execute(
                    select(Chunk.id, Chunk.text, Chunk.evidence_version_id, Chunk.ordinal)
                    .join(EvidenceVersion, Chunk.evidence_version_id == EvidenceVersion.id)
                    .join(Source, Chunk.source_id == Source.id)
                    .where(EvidenceVersion.status.in_(KEYWORD_SERVABLE), Source.status == SourceStatus.ACTIVE)
                ).all()
                for cid, text, vid, ordinal in rows:
                    if allowed is not None and vid not in allowed:
                        continue
                    score = len(q & _words(text or ""))
                    if score:
                        scored.append((-score, vid, ordinal, cid))
                scored.sort()
                ids = [c for *_x, c in scored[:TOP_N]]
                out = list(ids)
            if question.lower().startswith("nothing"):
                out = []
            self._ranked = out
            return [SimpleNamespace(chunk_id=c) for c in out]

        def ask(self, question, *args, **kwargs):
            q = question.strip().lower()
            m = re.match(r"slow (\d+(?:\.\d+)?)", q)
            if m:
                end = time.monotonic() + float(m.group(1))
                while time.monotonic() < end:
                    time.sleep(0.05)
            if q.startswith("fail"):
                raise RuntimeError("simulated generation failure")
            result = super().ask(question, *args, **kwargs)
            if q.startswith("stale") and result.citations:
                # The FAST path cites the already-resolved id; the agent path can cite the id it
                # was handed (an old provisional id). Emulate that: cite the alias' old id and
                # let the REAL _citation_grants resolve it.
                with context.session_factory() as s:
                    cites = []
                    for c in result.citations:
                        old = s.execute(select(ChunkAlias.old_chunk_id).where(
                            ChunkAlias.new_chunk_id == c.chunk_id).limit(1)).scalar_one_or_none()
                        cites.append(c.model_copy(update={"chunk_id": old or c.chunk_id}))
                result = result.model_copy(update={
                    "citations": cites, "evidence_grants": self._citation_grants(cites, None, None)})
            if OLD:  # an old worker never attached grant metadata
                result = result.model_copy(update={"evidence_grants": []})
            return result

    gw = FakeGateway()
    svc = HarnessService(
        engine=context.engine, table="STUB", gateway=gw, resolver=context.resolver,
        classifier=FastOnly(), settings=context.settings,
    )
    gw.svc = svc
    return svc


def main() -> None:
    from docket.interfaces import worker

    proto = os.fdopen(os.dup(1), "w", encoding="utf-8", buffering=1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    from docket.core.privacy import RedactedLogStream

    sys.stderr = RedactedLogStream(sys.stderr)
    if OLD:
        worker.FEATURES[:] = [f for f in worker.FEATURES if f != "evidence.v1"]

    from docket.interfaces.cli.context import AppContext

    context = AppContext()  # real settings from DOCKET_DATA_DIR, real alembic migration
    context.__dict__["vector_writer"] = SimpleNamespace(table="STUB")  # no LanceDB
    pidfile = os.environ.get("E2E_PIDFILE")
    if pidfile:
        with open(pidfile, "w") as f:
            f.write(str(os.getpid()))

    slow_file = os.environ.get("E2E_SLOW_IDS_FILE")
    slow_s = float(os.environ.get("E2E_SLOW_S", "1.0"))
    if slow_file:
        from docket.infra.retrieval import evidence_read

        orig_read = evidence_read.EvidenceReader.read

        def slow_read(self, chunk_id, allowed, **kw):
            try:
                with open(slow_file) as f:
                    listed = {ln.strip() for ln in f}
            except OSError:
                listed = set()
            if f"fail:{chunk_id}" in listed:
                # the message deliberately carries content: the worker must never log it
                raise RuntimeError(f"reader exploded on {chunk_id} CONFIDENTIAL-IN-EXCEPTION")
            if chunk_id in listed:
                time.sleep(slow_s)
            return orig_read(self, chunk_id, allowed, **kw)

        evidence_read.EvidenceReader.read = slow_read

    methods = worker.build_methods(context, lambda ctx, table: make_service(ctx))
    counter = os.environ.get("E2E_COUNTER_FILE")
    if OLD:
        methods.pop("evidence.get", None)
    elif counter:
        inner = methods["evidence.get"]

        def counted(params, notify):
            with open(counter, "a") as f:
                f.write("x\n")
            return inner(params, notify)

        methods["evidence.get"] = counted
    print("e2e-worker: ready", file=sys.stderr, flush=True)
    try:
        worker.serve(methods, sys.stdin, proto)
    finally:
        context.engine.dispose()


if __name__ == "__main__":
    main()
