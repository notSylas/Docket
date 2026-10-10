"""G5 routing: measure the current HeuristicQueryClassifier against routing-g5.yaml.

    cd backend && .venv/bin/python eval-public/experiments/g5_route_accuracy.py [--json out.json]

Deterministic and offline: no model call, no corpus ingestion, no GPU. It
never touches the user's real data directory (DOCKET_DATA_DIR defaults to a
fresh temp dir before docket is imported).

What is measured
  * Route accuracy of `HeuristicQueryClassifier.classify(question)`, with
    FAST -> FAST and AGENT -> INVESTIGATION. The classifier is a function of
    the question alone (it never sees history), exactly as `QueryService`
    calls it. CLARIFY is not a value the classifier can produce, so every
    CLARIFY item counts as wrong for it; they are reported separately.
  * CLARIFY, as far as the existing deterministic signals go. Real ambiguity
    detection (`signals.detect_period_ambiguity`) runs AFTER retrieval on the
    included chunks, so it cannot be evaluated offline without ingesting the
    corpora (embedding model on the GPU). What can be evaluated offline is the
    pre-retrieval half of doc 05 section 6: `signals.has_period` and
    `signals.extract_signals(...).scoped` over the corpus file names. The
    proxy flags a question when it states no period, names no file, and asks
    for a period-dependent value. It is a bound on what the pre-retrieval
    signals alone could do, not a model of the product.

Reason-code vocabulary used in routing-g5.yaml (labels, not a closed enum)
  FAST:          SINGLE_LOOKUP, PERIOD_STATED, SINGLE_SOURCE, NAMED_FILE,
                 FOLLOWUP_RESOLVED, PERIOD_RESOLVED_FROM_HISTORY,
                 OUT_OF_SCOPE_ABSTAIN, LEXICAL_TRAP_ACROSS (a lookup that
                 contains a word the heuristic treats as a breadth signal)
  INVESTIGATION: COMPARISON, CROSS_DOCUMENT, MULTI_SHEET, DERIVED_CALCULATION,
                 UNIT_CONVERSION, ACTUAL_VS_BUDGET, EXHAUSTIVE,
                 THRESHOLD_FILTER, TIMELINE, CAUSAL, IMPACT, CONFLICT_CHECK,
                 VERSION_DIFF
  CLARIFY:       AMBIGUOUS_PERIOD, MULTIPLE_FISCAL_YEARS, MULTIPLE_YEARS,
                 RELATIVE_PERIOD_UNANCHORED, MISSING_SCOPE, AMBIGUOUS_METRIC,
                 MISSING_REFERENT, NO_HISTORY
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

if "DOCKET_DATA_DIR" not in os.environ:
    os.environ["DOCKET_DATA_DIR"] = tempfile.mkdtemp(prefix="docket-g5-")

import yaml  # noqa: E402

from docket.services.query.classifier import HeuristicQueryClassifier, QueryMode  # noqa: E402
from docket.services.query.signals import EligibleFile, extract_signals, has_period  # noqa: E402

PUBLIC = Path(__file__).resolve().parents[1]
ROUTING = PUBLIC / "routing-g5.yaml"
PATHS = ("FAST", "INVESTIGATION", "CLARIFY")
MAP = {QueryMode.FAST: "FAST", QueryMode.AGENT: "INVESTIGATION"}

_MONTH_RE = re.compile(
    r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|"
    r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b", re.IGNORECASE)
_PERIOD_WORD_RE = re.compile(r"\b(?:first|second|last|previous|prior)\s+(?:half|quarter|year|month)\b|\bquarter\b|\bQ[1-4]\b", re.IGNORECASE)
_METRIC_RE = re.compile(r"\b(?:revenue|sales|total|budget|variance|cost|costs|spend|growth|grow)\b", re.IGNORECASE)
_FOLLOWUP_RE = re.compile(r"^\s*(?:and|what about|how about)\b|\b(?:it|that|those|they)\b", re.IGNORECASE)


def corpus_files(corpus: str) -> list[EligibleFile]:
    root = PUBLIC / corpus
    return [EligibleFile(name=p.name, version_id=f"{corpus}:{p.name}") for p in sorted(root.rglob("*")) if p.is_file()]


def clarify_proxy(question: str, has_history: bool, files: list[EligibleFile]) -> bool:
    """Pre-retrieval CLARIFY proxy built only from existing deterministic signals."""
    sig = extract_signals(question, files)
    if sig.scoped:
        return False
    if _FOLLOWUP_RE.search(question) and not has_history and len(question.split()) <= 8:
        return True  # an unresolved referent: nothing to resolve it against
    asks_period_value = bool(_MONTH_RE.search(question) or _PERIOD_WORD_RE.search(question) or _METRIC_RE.search(question))
    return asks_period_value and not has_period(question)


def pct(n: int, d: int) -> str:
    return f"{n}/{d} = {100 * n / d:.1f}%" if d else "0/0"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", type=Path, help="write the full result as JSON")
    args = ap.parse_args()

    data = yaml.safe_load(ROUTING.read_text(encoding="utf-8"))
    items = data["items"]
    clf = HeuristicQueryClassifier()
    files = {c: corpus_files(c) for c in {i["corpus"] for i in items}}

    rows = []
    for it in items:
        pred = MAP[clf.classify(it["question"])]
        proxy = clarify_proxy(it["question"], bool(it.get("history")), files[it["corpus"]])
        rows.append({**it, "heuristic": pred, "clarify_proxy": proxy})

    # --- classifier route accuracy
    gold_counts = Counter(r["expected_path"] for r in rows)
    confusion: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        confusion[r["expected_path"]][r["heuristic"]] += 1
    correct = sum(1 for r in rows if r["heuristic"] == r["expected_path"])
    print(f"items: {len(rows)}  (FAST {gold_counts['FAST']}, INVESTIGATION {gold_counts['INVESTIGATION']}, "
          f"CLARIFY {gold_counts['CLARIFY']})")
    print(f"\nHeuristicQueryClassifier, overall (CLARIFY is unreachable for it): {pct(correct, len(rows))}")
    two = [r for r in rows if r["expected_path"] != "CLARIFY"]
    c2 = sum(1 for r in two if r["heuristic"] == r["expected_path"])
    print(f"Heuristic on FAST/INVESTIGATION items only: {pct(c2, len(two))}")
    print("\nPer expected path (heuristic output):")
    for path in PATHS:
        row = confusion[path]
        print(f"  {path:<14} n={gold_counts[path]:<3} -> FAST {row['FAST']:<3} INVESTIGATION {row['INVESTIGATION']:<3}")
    tp = confusion["INVESTIGATION"]["INVESTIGATION"]
    fp = confusion["FAST"]["INVESTIGATION"] + confusion["CLARIFY"]["INVESTIGATION"]
    fn = confusion["INVESTIGATION"]["FAST"]
    print(f"\nINVESTIGATION recall {pct(tp, gold_counts['INVESTIGATION'])}; "
          f"precision {pct(tp, tp + fp)}; false INVESTIGATION on FAST items {confusion['FAST']['INVESTIGATION']}; "
          f"missed {fn}")

    # --- by reason code (FAST / INVESTIGATION items)
    by_code: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for r in two:
        for code in r["reason_codes"]:
            by_code[code][1] += 1
            by_code[code][0] += r["heuristic"] == r["expected_path"]
    print("\nHeuristic accuracy by reason code (FAST/INVESTIGATION items):")
    for code, (ok, n) in sorted(by_code.items(), key=lambda kv: (kv[1][0] / kv[1][1], kv[0])):
        print(f"  {code:<30} {ok}/{n}")

    # --- by corpus
    print("\nBy corpus (FAST/INVESTIGATION items):")
    for corpus in sorted(files):
        sub = [r for r in two if r["corpus"] == corpus]
        print(f"  {corpus:<22} {pct(sum(r['heuristic'] == r['expected_path'] for r in sub), len(sub))}")

    # --- CLARIFY via the pre-retrieval signals
    cl = [r for r in rows if r["expected_path"] == "CLARIFY"]
    non = [r for r in rows if r["expected_path"] != "CLARIFY"]
    hit = sum(r["clarify_proxy"] for r in cl)
    fa = sum(r["clarify_proxy"] for r in non)
    print("\nCLARIFY (existing pre-retrieval signals only: has_period + explicit file scope):")
    print(f"  CLARIFY items flagged (recall)        {pct(hit, len(cl))}")
    print(f"  non-CLARIFY items flagged (false +)   {pct(fa, len(non))}")
    print(f"  precision                             {pct(hit, hit + fa)}")
    print("  note: the product flags ambiguity only after retrieval (detect_period_ambiguity); the router "
          "contract needs it before planning, so this is a ceiling for the pre-retrieval signals.")
    print("  heuristic classifier on CLARIFY items: "
          + ", ".join(f"{p} {n}" for p, n in sorted(confusion["CLARIFY"].items())) + " (always wrong by construction)")

    print("\nHeuristic misroutes (FAST/INVESTIGATION items):")
    for r in two:
        if r["heuristic"] != r["expected_path"]:
            print(f"  {r['id']} expected {r['expected_path']}, got {r['heuristic']}: {r['question']}")
    print("\nClarify proxy misses / false positives:")
    for r in rows:
        if r["expected_path"] == "CLARIFY" and not r["clarify_proxy"]:
            print(f"  miss {r['id']}: {r['question']}")
        if r["expected_path"] != "CLARIFY" and r["clarify_proxy"]:
            print(f"  false+ {r['id']} ({r['expected_path']}): {r['question']}")

    if args.json:
        args.json.write_text(json.dumps({"items": rows}, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
