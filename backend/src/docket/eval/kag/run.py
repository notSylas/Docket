"""Lane A KAG evaluation runner: load gold, measure the scratch library, print the table."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from docket.eval.kag import metrics as M
from docket.eval.kag.graph import GraphSnapshot, connect_ro, engine_ro, load_snapshot
from docket.eval.kag.retrieval import LexicalIndex, retrieve
from docket.eval.kag.schema import KagGold, provenance_counts, select_items, validate_against_library

DEFAULT_KS = (3, 6, 12)
SILVER_CAVEAT = ("SILVER, UNREVIEWED labels (mechanically derived; not human validated). Treat every number as a "
                 "candidate measurement, not a result.")


@dataclass
class KagReport:
    gold_path: str
    db_path: str
    human_only: bool
    provenance: dict[str, dict[str, int]]
    library_docs: int
    problems: list[str]
    metrics: dict[str, M.Metric] = field(default_factory=dict)
    paired: dict[int, dict] = field(default_factory=dict)
    details: dict = field(default_factory=dict)

    @property
    def all_silver(self) -> bool:
        return not any(p["human_reviewed"] for p in self.provenance.values())


def run_kag_eval(gold: KagGold, db_path: str | Path, *, gold_path: str = "", ks=DEFAULT_KS, human_only: bool = False,
                 n_boot: int = M.N_BOOT) -> KagReport:
    conn = connect_ro(db_path)
    try:
        snap: GraphSnapshot = load_snapshot(conn)
        problems = validate_against_library(gold, set(snap.docs_by_file))
        prov = {k: provenance_counts(getattr(gold, k)) for k in ("entities", "resolution_pairs", "roles", "relations", "clauses")}
        ent = [i for i in select_items(gold.entities, human_only=human_only)]
        rep = KagReport(gold_path, str(db_path), human_only, prov, len(snap.docs_by_file), problems)
        m = rep.metrics
        er = M.entity_extraction(ent, snap, n_boot=n_boot)
        m["entity_recall"], m["entity_precision"] = er.recall, er.precision
        rep.details["entity_per_doc"] = er.per_doc
        rep.details["entity_docs_missing_from_library"] = er.missing_docs
        rr = M.entity_resolution(select_items(gold.resolution_pairs, human_only=human_only), snap, n_boot=n_boot)
        m["resolution_precision"], m["resolution_recall"], m["false_merge_rate"] = rr.precision, rr.recall, rr.false_merge_rate
        rep.details["resolution_confusion"] = rr.confusion
        m["role_edge_present"], m["role_correct"] = M.role_correctness(select_items(gold.roles, human_only=human_only), snap, n_boot=n_boot)
        m.update(M.relation_prf(select_items(gold.relations, human_only=human_only), M.graph_relation_predictor(conn, snap), n_boot=n_boot))
        clauses = select_items(gold.clauses, human_only=human_only)
        m["absent_precision"], m["absent_recall"] = M.clause_absence(clauses, snap, n_boot=n_boot)
        # clause retrieval: lexical BM25, production KAG scoping on vs off
        index = LexicalIndex.from_db(conn)
        engine = engine_ro(db_path)
        try:
            usable = [c for c in clauses if c.present and c.gold_spans and c.question]
            off = {c.id: [index.text(i) for i in retrieve(index, engine, c.question, scoping=False)] for c in usable}
            on = {c.id: [index.text(i) for i in retrieve(index, engine, c.question, scoping=True)] for c in usable}
        finally:
            engine.dispose()
        cr = M.clause_recall_at_k(usable, off, on, ks, n_boot=n_boot)
        for k in ks:
            m[f"clause_recall@{k}_off"], m[f"clause_recall@{k}_on"] = cr.off[k], cr.on[k]
        rep.paired = cr.paired
        pv = M.provenance_coverage(conn, snap, n_boot=n_boot)
        m["prov_entity_edge_resolves"], m["prov_entity_edge_contains"] = pv.entity_edges_resolve, pv.entity_edges_contain
        m["prov_clause_edge_resolves"], m["prov_clause_edge_matches"] = pv.clause_edges_resolve, pv.clause_edges_contain
        rep.details["entity_edges_filename_only"] = pv.entity_edges_filename_only
        lc = M.lifecycle_consistency(conn, snap, n_boot=n_boot)
        m["lifecycle_consistent_edges"] = lc.consistent_edges
        rep.details["lifecycle"] = {"stale_edges": lc.stale_edges, "orphan_entities": lc.orphan_entities,
                                    "clause_edges_missing_chunk": lc.clause_edges_missing_chunk}
        rep.details["graph"] = {"documents": len(snap.docs_by_file), "entities": len(snap.entities),
                                "entity_edges": len(snap.doc_entities), "clause_edges": len(snap.clauses)}
        return rep
    finally:
        conn.close()


def format_kag_report(rep: KagReport) -> str:
    g = rep.details.get("graph", {})
    lines = [
        f"KAG Lane A evaluation (CPU-only, lexical retrieval, no model)   gold={rep.gold_path}   library={rep.db_path}",
        f"graph: {g.get('documents', 0)} documents, {g.get('entities', 0)} entities, {g.get('entity_edges', 0)} entity edges, "
        f"{g.get('clause_edges', 0)} clause edges",
        "label provenance: " + "; ".join(f"{k} {v['human_reviewed']} human / {v['silver_unreviewed']} silver" for k, v in rep.provenance.items()),
    ]
    if rep.all_silver:
        lines.append("WARNING: " + SILVER_CAVEAT)
    if rep.human_only:
        lines.append("(human_only: silver and pending items excluded)")
    lines += [f"PROBLEM: {p}" for p in rep.problems]
    lines.append("")
    rows = [
        ("Entity extraction recall", "entity_recall"), ("Entity extraction precision (complete docs only)", "entity_precision"),
        ("Resolution precision (pairwise)", "resolution_precision"), ("Resolution recall (pairwise)", "resolution_recall"),
        ("False-merge rate", "false_merge_rate"), ("Role edge present", "role_edge_present"), ("Role correct (party vs mention)", "role_correct"),
        ("amends precision", "amends_precision"), ("amends recall", "amends_recall"),
        ("supersedes precision", "supersedes_precision"), ("supersedes recall", "supersedes_recall"),
        ("Clause-absent precision", "absent_precision"), ("Clause-absent recall", "absent_recall"),
    ]
    ks = sorted({int(k.split("@")[1].split("_")[0]) for k in rep.metrics if k.startswith("clause_recall@")})
    for k in ks:
        rows.append((f"Clause recall@{k}, scoping off", f"clause_recall@{k}_off"))
        rows.append((f"Clause recall@{k}, scoping ON ", f"clause_recall@{k}_on"))
    rows += [
        ("Provenance: entity edge chunk resolves to READY evidence", "prov_entity_edge_resolves"),
        ("Provenance: entity edge text contains the entity", "prov_entity_edge_contains"),
        ("Provenance: clause edge chunk resolves", "prov_clause_edge_resolves"),
        ("Provenance: clause edge text matches clause", "prov_clause_edge_matches"),
        ("Lifecycle: edges consistent with servable evidence", "lifecycle_consistent_edges"),
    ]
    width = max(len(r[0]) for r in rows)
    for label, key in rows:
        lines.append(f"{label:<{width}}  {rep.metrics[key].fmt()}")
    for k, p in sorted(rep.paired.items()):
        lines.append(f"paired scoping on-off @k={k}: diff {p['diff']:+.1%} [{p['ci'][0]:+.1%}, {p['ci'][1]:+.1%}] "
                     f"flips on-only={p['flips']['b_only']} off-only={p['flips']['a_only']}: {p['label']}")
    lines.append("")
    lines.append(f"resolution confusion: {rep.details.get('resolution_confusion')}")
    lines.append(f"entity edges derived from file name only (not text-provable): {rep.details.get('entity_edges_filename_only')}")
    lines.append(f"lifecycle: {rep.details.get('lifecycle')}")
    lines.append("CI: 95% percentile bootstrap over items (not clustered by document).")
    return "\n".join(lines)


def report_to_json(rep: KagReport, *, redact: bool = False) -> str:
    data = asdict(rep)
    data["metrics"] = {k: asdict(v) for k, v in rep.metrics.items()}
    if redact:
        data["details"].pop("entity_per_doc", None)  # file names of the user's documents
        data["details"].pop("entity_docs_missing_from_library", None)
        data["db_path"] = data["gold_path"] = "[redacted]"
        data["problems"] = ["[redacted]"] * len(rep.problems)
    return json.dumps(data, indent=2, default=str)
