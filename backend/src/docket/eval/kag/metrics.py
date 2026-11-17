"""Deterministic KAG metrics. Every metric reports n, a point value and a 95% bootstrap CI.

Metrics take plain data (gold items + a GraphSnapshot, or retrieval results), so they are unit
tested on hand-built fixtures with exact expected numbers.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from docket.eval.kag.graph import GraphSnapshot
from docket.eval.kag.schema import (
    ClauseItem,
    EntityDoc,
    GoldEntity,
    RelationItem,
    ResolutionPair,
    RoleItem,
    select_items,
)
from docket.eval.scoring import matches, normalize_text
from docket.eval.stats import (
    bootstrap_ci,
    classify_difference,
    paired_bootstrap_diff,
    paired_flips,
)
from docket.services.documents import kag as kag_graph

N_BOOT = 2000


@dataclass
class Metric:
    name: str
    n: int  # units the value is over (0 = not computable)
    value: float | None
    ci: tuple[float, float] | None
    note: str = ""

    def fmt(self) -> str:
        if self.value is None or self.n == 0:
            return f"n/a (n={self.n})"
        lo, hi = self.ci if self.ci else (self.value, self.value)
        return f"{self.value:.1%} [{lo:.1%}-{hi:.1%}] n={self.n}"


def rate(name: str, outcomes: Sequence[bool | int], *, note: str = "", n_boot: int = N_BOOT, seed: int = 0) -> Metric:
    vals = [1.0 if o else 0.0 for o in outcomes]
    if not vals:
        return Metric(name, 0, None, None, note)
    return Metric(name, len(vals), sum(vals) / len(vals), bootstrap_ci(vals, n_boot=n_boot, seed=seed), note)


# -- (a) entity extraction -------------------------------------------------------------------


def entity_keys(name: str, aliases: Sequence[str] = ()) -> set[str]:
    keys: set[str] = set()
    for n in (name, *aliases):
        keys |= kag_graph.name_variants(n)
        c = kag_graph.core_name(n)
        if kag_graph.usable_variant(c):
            keys.add(c)
    return keys


def _extracted_keys(snap: GraphSnapshot, doc_id: int) -> set[str]:
    keys: set[str] = set()
    for eid in snap.entity_ids_of_doc(doc_id):
        keys |= snap.entity_variants.get(eid, set())
    return keys


@dataclass
class EntityResult:
    recall: Metric
    precision: Metric
    per_doc: dict[str, tuple[int, int, int]] = field(default_factory=dict)  # file -> (gold, found, extracted)
    missing_docs: list[str] = field(default_factory=list)


def entity_extraction(docs: Sequence[EntityDoc], snap: GraphSnapshot, *, n_boot: int = N_BOOT) -> EntityResult:
    """Recall over every labelled entity; precision only over documents whose list is `complete`
    (a silver list names some entities, so extra extracted entities are not errors)."""
    hits: list[bool] = []
    prec: list[bool] = []
    per_doc: dict[str, tuple[int, int, int]] = {}
    missing: list[str] = []
    for d in select_items(list(docs)):
        info = snap.doc_for(d.file)
        if info is None:
            missing.append(d.file)
            continue
        extracted = _extracted_keys(snap, info.doc_id)
        found = 0
        for ge in d.entities:
            ok = bool(entity_keys(ge.name, ge.aliases) & extracted)
            hits.append(ok)
            found += ok
        n_extracted = len(snap.entity_ids_of_doc(info.doc_id))
        per_doc[d.file] = (len(d.entities), found, n_extracted)
        if d.complete:
            gold_keys = [entity_keys(g.name, g.aliases) for g in d.entities]
            for eid in snap.entity_ids_of_doc(info.doc_id):
                prec.append(any(snap.entity_variants.get(eid, set()) & gk for gk in gold_keys))
    note = "silver lists are incomplete: precision is only computed on documents marked complete"
    return EntityResult(
        recall=rate("entity_recall", hits, n_boot=n_boot),
        precision=rate("entity_precision", prec, note=note, n_boot=n_boot),
        per_doc=per_doc, missing_docs=missing,
    )


# -- (b) entity resolution -------------------------------------------------------------------


def resolve_name(snap: GraphSnapshot, name: str) -> set[int]:
    ids: set[int] = set()
    for key in entity_keys(name):
        ids |= snap.variants.get(key, set())
    return ids


@dataclass
class ResolutionResult:
    precision: Metric
    recall: Metric
    false_merge_rate: Metric
    confusion: dict[str, int]


def entity_resolution(pairs: Sequence[ResolutionPair], snap: GraphSnapshot, *, n_boot: int = N_BOOT) -> ResolutionResult:
    """Pairwise: predicted 'same' iff both names resolve to a common graph entity. False-merge
    rate = gold-different pairs predicted same, over gold-different pairs where both resolve."""
    tp = fp = fn = tn = 0
    merge_outcomes: list[bool] = []
    pred_pos: list[bool] = []
    gold_pos: list[bool] = []
    unresolved = 0
    for p in select_items(list(pairs)):
        a, b = resolve_name(snap, p.a), resolve_name(snap, p.b)
        both = bool(a) and bool(b)
        unresolved += not both
        predicted = bool(a & b)
        if p.same:
            gold_pos.append(predicted)
            tp += predicted
            fn += not predicted
        else:
            fp += predicted
            tn += not predicted
            if both:
                merge_outcomes.append(predicted)
        if predicted:
            pred_pos.append(p.same)
    return ResolutionResult(
        precision=rate("resolution_precision", pred_pos, n_boot=n_boot),
        recall=rate("resolution_recall", gold_pos, n_boot=n_boot),
        false_merge_rate=rate("false_merge_rate", merge_outcomes, n_boot=n_boot,
                              note="gold-different pairs predicted same / gold-different pairs with both names in the graph"),
        confusion={"tp": tp, "fp": fp, "fn": fn, "tn": tn, "pairs_with_unresolved_name": unresolved},
    )


# -- (c) relationships -----------------------------------------------------------------------


def role_correctness(items: Sequence[RoleItem], snap: GraphSnapshot, *, n_boot: int = N_BOOT) -> tuple[Metric, Metric]:
    """(edge present, role correct). `party` counts the filename-derived party edge as a party."""
    present: list[bool] = []
    correct: list[bool] = []
    for it in select_items(list(items)):
        info = snap.doc_for(it.file)
        if info is None:
            continue
        roles_by_entity = snap.roles_of_doc(info.doc_id)
        eids = resolve_name(snap, it.entity) & set(roles_by_entity)
        present.append(bool(eids))
        if not eids:
            correct.append(False)
            continue
        roles = {r for e in eids for r in roles_by_entity[e]}
        graph_role = "party" if roles & {"party", "filename"} else "mention"
        correct.append(graph_role == it.role)
    return rate("role_edge_present", present, n_boot=n_boot), rate("role_correct", correct, n_boot=n_boot)


def relation_prf(
    items: Sequence[RelationItem], predicted: Callable[[str, str], str], *, n_boot: int = N_BOOT
) -> dict[str, Metric]:
    """amends/supersedes: `predicted(from_file, to_file)` returns amends | supersedes | none.
    Per relation precision and recall over the labelled pairs."""
    kept = select_items(list(items))
    out: dict[str, Metric] = {}
    for rel in ("amends", "supersedes"):
        pred = [(predicted(i.from_file, i.to_file) == rel, i.relation == rel) for i in kept]
        out[f"{rel}_precision"] = rate(f"{rel}_precision", [g for p, g in pred if p], n_boot=n_boot)
        out[f"{rel}_recall"] = rate(f"{rel}_recall", [p for p, g in pred if g], n_boot=n_boot)
    return out


def graph_relation_predictor(conn: sqlite3.Connection, snap: GraphSnapshot) -> Callable[[str, str], str]:
    """The only document-to-document relation the library records is supersedes_document_id."""
    sup = dict(conn.execute("SELECT id, supersedes_document_id FROM documents WHERE supersedes_document_id IS NOT NULL"))

    def predict(a: str, b: str) -> str:
        da, db = snap.doc_for(a), snap.doc_for(b)
        return "supersedes" if da and db and sup.get(da.doc_id) == db.doc_id else "none"

    return predict


# -- (d) clause retrieval and absence --------------------------------------------------------


def span_hit(spans: Sequence[str], texts: Sequence[str]) -> bool:
    norm = [normalize_text(t) for t in texts]
    return any(matches(s, n) for s in spans for n in norm)


@dataclass
class ClauseRecallResult:
    ks: tuple[int, ...]
    off: dict[int, Metric]
    on: dict[int, Metric]
    paired: dict[int, dict] = field(default_factory=dict)  # k -> diff, ci, flips, label


def clause_recall_at_k(
    items: Sequence[ClauseItem],
    results_off: Mapping[str, Sequence[str]],
    results_on: Mapping[str, Sequence[str]],
    ks: Sequence[int] = (3, 6, 12),
    *,
    n_boot: int = N_BOOT,
) -> ClauseRecallResult:
    """Recall@k over present clauses that have gold spans. `results_*` map item id -> ranked
    chunk texts (graph scoping off / on). Includes a paired comparison per k."""
    used = [i for i in select_items(list(items)) if i.present and i.gold_spans and i.id in results_off and i.id in results_on]
    off: dict[int, Metric] = {}
    on: dict[int, Metric] = {}
    paired: dict[int, dict] = {}
    for k in ks:
        h_off = {i.id: span_hit(i.gold_spans, results_off[i.id][:k]) for i in used}
        h_on = {i.id: span_hit(i.gold_spans, results_on[i.id][:k]) for i in used}
        off[k] = rate(f"recall@{k} scoping off", list(h_off.values()), n_boot=n_boot)
        on[k] = rate(f"recall@{k} scoping on", list(h_on.values()), n_boot=n_boot)
        if used:
            diff, ci = paired_bootstrap_diff({q: [v] for q, v in h_off.items()}, {q: [v] for q, v in h_on.items()}, n_boot=n_boot)
            flips = paired_flips(h_off, h_on)
            paired[k] = {"diff": diff, "ci": ci, "flips": flips,
                         "label": classify_difference(diff, ci, flips["a_only"], flips["b_only"])}
    return ClauseRecallResult(tuple(ks), off, on, paired)


def clause_absence(items: Sequence[ClauseItem], snap: GraphSnapshot, *, n_boot: int = N_BOOT) -> tuple[Metric, Metric]:
    """Graph says 'absent' when the document has no edge for the clause type (only for clause types
    that can be absent). Returns (precision, recall) of the absent prediction."""
    pred_abs_correct: list[bool] = []
    gold_abs_found: list[bool] = []
    have = {(d, t) for d, t, _c, _f in snap.clauses}
    for it in select_items(list(items)):
        ct = kag_graph.clause_type_by_name(it.clause_type)
        info = snap.doc_for(it.file)
        if ct is None or info is None or not ct.can_be_absent:
            continue
        predicted_absent = (info.doc_id, it.clause_type) not in have
        if predicted_absent:
            pred_abs_correct.append(not it.present)
        if not it.present:
            gold_abs_found.append(predicted_absent)
    return rate("absent_precision", pred_abs_correct, n_boot=n_boot), rate("absent_recall", gold_abs_found, n_boot=n_boot)


# -- (e) provenance --------------------------------------------------------------------------


@dataclass
class ProvenanceResult:
    entity_edges_resolve: Metric
    entity_edges_contain: Metric
    entity_edges_filename_only: int
    clause_edges_resolve: Metric
    clause_edges_contain: Metric


def provenance_coverage(conn: sqlite3.Connection, snap: GraphSnapshot, *, n_boot: int = N_BOOT) -> ProvenanceResult:
    """Share of edges whose chunk resolves to existing READY evidence of the same document and
    whose text contains the entity (any variant) or matches the clause's keyword pattern.
    `filename` role edges come from the file name, not text, so they are counted separately and
    excluded from the 'contains' denominator."""
    doc_version = {d.doc_id: d.version_id for d in snap.docs_by_file.values()}
    chunk_rows = {
        cid: (vid, txt)
        for cid, vid, txt in conn.execute(
            "SELECT c.id, c.evidence_version_id, c.text FROM chunks c JOIN evidence_versions ev "
            "ON ev.id = c.evidence_version_id WHERE upper(ev.status) = 'READY'"
        )
    }

    def resolves(doc_id: int, chunk_id: str | None) -> tuple[bool, str]:
        row = chunk_rows.get(chunk_id or "")
        if row is None or row[0] != doc_version.get(doc_id):
            return False, ""
        return True, row[1]

    e_res: list[bool] = []
    e_con: list[bool] = []
    filename_only = 0
    for doc_id, eid, role, chunk_id in snap.doc_entities:
        ok, text_ = resolves(doc_id, chunk_id)
        e_res.append(ok)
        if role == "filename":
            filename_only += 1
            continue
        folded = " " + " ".join(kag_graph._clean(text_)) + " "  # same folding the extractor uses for names
        e_con.append(ok and any(f" {v} " in folded for v in snap.entity_variants.get(eid, set())))
    c_res: list[bool] = []
    c_con: list[bool] = []
    for doc_id, ctype, chunk_id, _conf in snap.clauses:
        ok, text_ = resolves(doc_id, chunk_id)
        c_res.append(ok)
        ct = kag_graph.clause_type_by_name(ctype)
        c_con.append(ok and ct is not None and bool(ct.chunk_re.search(text_)))
    return ProvenanceResult(
        rate("entity_edge_chunk_resolves", e_res, n_boot=n_boot),
        rate("entity_edge_text_contains_entity", e_con, n_boot=n_boot,
             note="text match is on punctuation-folded lowercase variants"),
        filename_only,
        rate("clause_edge_chunk_resolves", c_res, n_boot=n_boot),
        rate("clause_edge_text_matches_clause", c_con, n_boot=n_boot),
    )


# -- (f) lifecycle consistency ---------------------------------------------------------------


@dataclass
class LifecycleResult:
    consistent_edges: Metric
    stale_edges: int
    orphan_entities: int
    clause_edges_missing_chunk: int


def lifecycle_consistency(conn: sqlite3.Connection, snap: GraphSnapshot, *, n_boot: int = N_BOOT) -> LifecycleResult:
    """Graph rows that survive their evidence: an edge is consistent when its document is a
    servable (READY, ACTIVE) document and its chunk exists. Query planning filters stale edges
    at read time (tests/unit/test_kag_lifecycle.py); this measures leftover rows. Orphan
    entities (no document edge) are the documented purge finding."""
    servable = {d.doc_id for d in snap.docs_by_file.values() if d.servable}
    doc_ids = {d.doc_id for d in snap.docs_by_file.values()}
    chunk_ids = {r[0] for r in conn.execute("SELECT id FROM chunks")}
    outcomes: list[bool] = []
    stale = missing = 0
    for doc_id, _e, _r, chunk_id in snap.doc_entities:
        ok = doc_id in servable and (chunk_id in chunk_ids)
        outcomes.append(ok)
        stale += doc_id not in servable or doc_id not in doc_ids
    for doc_id, _t, chunk_id, _c in snap.clauses:
        has_chunk = chunk_id in chunk_ids
        outcomes.append(doc_id in servable and has_chunk)
        stale += doc_id not in servable
        missing += not has_chunk
    linked = {e for _d, e, _r, _c in snap.doc_entities}
    orphans = sum(1 for e in snap.entities if e not in linked)
    return LifecycleResult(rate("lifecycle_consistent_edges", outcomes, n_boot=n_boot), stale, orphans, missing)
