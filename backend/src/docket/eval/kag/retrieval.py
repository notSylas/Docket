"""GPU-free retrieval for clause recall: lexical BM25 over a scratch library, with the production
KAG scoping logic (plan, scoped query, clause-chunk priority, fuse, dedupe) applied on top.

This mirrors `QueryService._kag_scope` without calling it (it needs embeddings); only the lexical
leg is substituted, so the numbers measure the graph's contribution to a lexical retriever.
"""

from __future__ import annotations

import math
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass

from sqlalchemy import Engine

from docket.services.documents import kag as kag_graph
from docket.services.documents import routing

_TOKEN = re.compile(r"[a-z0-9]+")
_STOP = frozenset("the a an of and or to in on by for with is are was were be this that it as at under what which who how".split())
POOL = 30
SCOPED_KEEP = 6


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in _STOP and len(t) > 1]


@dataclass
class LexicalIndex:
    chunk_ids: list[str]
    versions: list[str]
    texts: list[str]
    tf: list[Counter]
    df: Counter
    avg_len: float

    @classmethod
    def from_db(cls, conn: sqlite3.Connection) -> LexicalIndex:
        rows = conn.execute(
            "SELECT c.id, c.evidence_version_id, c.text FROM chunks c "
            "JOIN evidence_versions ev ON ev.id = c.evidence_version_id JOIN sources s ON s.id = ev.source_id "
            "WHERE upper(ev.status) = 'READY' AND upper(s.status) = 'ACTIVE' ORDER BY c.evidence_version_id, c.ordinal"
        ).fetchall()
        tf = [Counter(tokenize(t)) for _i, _v, t in rows]
        df: Counter = Counter()
        for c in tf:
            df.update(c.keys())
        total = sum(sum(c.values()) for c in tf)
        return cls([r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows], tf, df, total / max(1, len(rows)))

    def search(self, query: str, k: int = POOL, versions: set[str] | None = None) -> list[str]:
        q = tokenize(query)
        n = len(self.chunk_ids)
        scored: list[tuple[float, int]] = []
        for i, counts in enumerate(self.tf):
            if versions is not None and self.versions[i] not in versions:
                continue
            length = sum(counts.values()) or 1
            s = 0.0
            for t in q:
                f = counts.get(t, 0)
                if f:
                    idf = math.log(1 + (n - self.df[t] + 0.5) / (self.df[t] + 0.5))
                    s += idf * f * 2.2 / (f + 1.2 * (0.25 + 0.75 * length / self.avg_len))
            if s > 0:
                scored.append((s, i))
        scored.sort(key=lambda x: (-x[0], x[1]))
        return [self.chunk_ids[i] for _s, i in scored[:k]]

    def text(self, chunk_id: str) -> str:
        return self.texts[self._pos()[chunk_id]]

    def _pos(self) -> dict[str, int]:
        if not hasattr(self, "_p"):
            self._p = {c: i for i, c in enumerate(self.chunk_ids)}
        return self._p


def retrieve(index: LexicalIndex, engine: Engine, question: str, *, scoping: bool, limit: int = POOL) -> list[str]:
    """Ranked chunk ids for `question`; with scoping=True the KAG plan is fused in exactly like the
    service does (soft scoping, list questions excluded)."""
    glob = index.search(question, POOL)
    if not scoping or routing.is_list_question(question):
        return glob[:limit]
    plan = kag_graph.plan(engine, question)
    if not plan.active:
        return glob[:limit]
    scoped = index.search(kag_graph.scoped_query(question, plan), POOL, set(plan.version_ids))
    ids = kag_graph.dedupe_ids(engine, scoped)
    if plan.clause_chunk_ids:
        ids = sorted(ids, key=lambda c: (c not in plan.clause_chunk_ids, ids.index(c)))
    ids = ids[:SCOPED_KEEP]
    if not ids:
        return glob[:limit]
    fused_all = kag_graph.fuse(glob, ids, limit * 3)
    keep = set(kag_graph.dedupe_ids(engine, [c for c, _ in fused_all]))
    return [c for c, _ in fused_all if c in keep][:limit]
