"""G5 routing: measure `classify_route` (deterministic route decision) on routing-g5.yaml.

    cd backend && .venv/bin/python eval-public/experiments/g5_route_decision.py [--json out.json]

Offline, no model call, no corpus ingestion. DOCKET_DATA_DIR defaults to a fresh
temp dir before docket is imported (never the real data directory). Reports
overall accuracy, confusion per expected path, per reason code and
false positives, next to the old HeuristicQueryClassifier for comparison.
Pre-retrieval signals (`extract_signals` over the corpus file names) are passed
to `classify_route`, as the service would.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

if "DOCKET_DATA_DIR" not in os.environ:
    os.environ["DOCKET_DATA_DIR"] = tempfile.mkdtemp(prefix="docket-g5-")

import yaml  # noqa: E402

from docket.services.query.classifier import HeuristicQueryClassifier, QueryMode  # noqa: E402
from docket.services.query.routing import classify_route  # noqa: E402
from docket.services.query.signals import EligibleFile, extract_signals  # noqa: E402

PUBLIC = Path(__file__).resolve().parents[1]
PATHS = ("FAST", "INVESTIGATION", "CLARIFY")
OLD = {QueryMode.FAST: "FAST", QueryMode.AGENT: "INVESTIGATION"}


def corpus_files(corpus: str) -> list[EligibleFile]:
    root = PUBLIC / corpus
    return [EligibleFile(name=p.name, version_id=f"{corpus}:{p.name}") for p in sorted(root.rglob("*")) if p.is_file()]


def pct(n: int, d: int) -> str:
    return f"{n}/{d} = {100 * n / d:.1f}%" if d else "0/0"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", type=Path)
    args = ap.parse_args()
    items = yaml.safe_load((PUBLIC / "routing-g5.yaml").read_text(encoding="utf-8"))["items"]
    files = {c: corpus_files(c) for c in {i["corpus"] for i in items}}
    old = HeuristicQueryClassifier()

    rows = []
    for it in items:
        sig = extract_signals(it["question"], files[it["corpus"]])
        d = classify_route(it["question"], it.get("history"), sig)
        rows.append({**it, "pred": d.path.value, "pred_codes": d.reason_codes,
                     "clarification_question": d.clarification_question,
                     "old": OLD[old.classify(it["question"])]})

    n = len(rows)
    gold = Counter(r["expected_path"] for r in rows)
    conf: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        conf[r["expected_path"]][r["pred"]] += 1
    ok = sum(r["pred"] == r["expected_path"] for r in rows)
    ok_old = sum(r["old"] == r["expected_path"] for r in rows)
    print(f"items: {n} (FAST {gold['FAST']}, INVESTIGATION {gold['INVESTIGATION']}, CLARIFY {gold['CLARIFY']})")
    print(f"\nclassify_route overall: {pct(ok, n)}   (HeuristicQueryClassifier: {pct(ok_old, n)})")
    print("\nConfusion (expected -> predicted):")
    for p in PATHS:
        row = conf[p]
        print(f"  {p:<14} n={gold[p]:<3} -> " + "  ".join(f"{q} {row[q]}" for q in PATHS))
    print("\nPer path, recall / precision (old heuristic recall in brackets):")
    for p in PATHS:
        tp = conf[p][p]
        predicted = sum(conf[e][p] for e in PATHS)
        old_tp = sum(1 for r in rows if r["expected_path"] == p and r["old"] == p)
        print(f"  {p:<14} recall {pct(tp, gold[p]):<18} precision {pct(tp, predicted):<18} [old recall {old_tp}/{gold[p]}]")

    print("\nFalse positives (predicted path != expected):")
    for r in rows:
        if r["pred"] != r["expected_path"]:
            print(f"  {r['id']} expected {r['expected_path']}, got {r['pred']} {r['pred_codes']}: {r['question']}")

    by_code: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for r in rows:
        for c in r["reason_codes"]:
            by_code[c][1] += 1
            by_code[c][0] += r["pred"] == r["expected_path"]
    print("\nAccuracy by gold reason code (all items):")
    for c, (a, b) in sorted(by_code.items(), key=lambda kv: (kv[1][0] / kv[1][1], kv[0])):
        print(f"  {c:<30} {a}/{b}")

    emitted: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        for c in r["pred_codes"]:
            emitted[c][r["expected_path"]] += 1
    print("\nEmitted reason codes (count by expected path):")
    for c, cnt in sorted(emitted.items()):
        print(f"  {c:<30} " + "  ".join(f"{p} {cnt[p]}" for p in PATHS if cnt[p]))

    if args.json:
        args.json.write_text(json.dumps({"items": rows}, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
