# Public eval baseline

A gold question set and its first measured baseline, built on the design docs
in `/Docs` (public, already tracked in this repo) — not the user's own
documents, which live outside git per the accuracy-milestone plan.

## What's here

- `gold.yaml` — 33 reviewed questions (single_fact, enumeration, table_lookup)
  drafted by `docket eval draft` and reviewed for correctness against the
  source docs. This is a starting slice, not the full ~105-question mix the
  plan calls for: it has no numeric, multi_doc, follow_up, out_of_corpus or
  revoked questions yet, and no hand-written hard cases.
- `baseline.json` — `docket eval report` output (machine-readable) at the
  commit noted below.
- `baseline-runs.jsonl` / `baseline-judged.jsonl` — the raw run records and
  judge verdicts that produced it, for `docket eval compare` against later
  checkpoints.

## Baseline (post-M2)

Reproduce with:

```
docket eval run --corpus ../Docs --gold eval-public/gold.yaml --split all --repeats 3 --out <out>.jsonl
docket eval judge --gold eval-public/gold.yaml --runs <out>.jsonl --cross-check --out <out>.judged.jsonl
docket eval report --gold eval-public/gold.yaml --runs <out>.jsonl --judged <out>.judged.jsonl
```

**Accuracy (judged): 100.0% (33/33), 95% interval 89.6%-100.0%.** Pass-all
repeats (all 3 of 3, not just majority): 97.0% (32/33).

By type: enumeration 100% (9/9), single_fact 100% (22/22), table_lookup 100%
(2/2). By split: dev 100% (18/18), test 100% (15/15).

Retrieval recall@8: 100.0%. Fact-in-context: 100.0%. Wrongful abstention:
0.0% (0/99). The one remaining run-level failure is `generation_error`, not
retrieval — retrieval_miss is now 0/0 (was 11/11 pre-M2).

Caveats, unchanged from before: N=33 is small (wide interval); these are
easy single-fact/enumeration/table_lookup questions with no numeric,
multi_doc, follow_up, out_of_corpus or revoked cases yet, so 100% here is an
**upper bound for this easy slice**, not a claim about the milestone target
— real accuracy on harder question types and on the user's own documents is
expected to be lower. This reviewer (Claude) is not independent of the
question drafter (also an LLM) — the plan's required ~30 user-labeled
calibration sample has not been done yet, so the judge is not yet certified
via kappa.

### Pre-M2 baseline (for reference)

Accuracy (judged) 87.9% (29/33) [72.7%-95.2%]; recall@8 81.8%;
fact-in-context 81.8%; wrongful abstention 5.1% (5/99); 11/11 classifiable
failures were `retrieval_miss` — which is exactly the FTS5 keyword-search
bug M2 fixed (queries ANDed every word of a natural question together, and
bare and/or/not in a question could be parsed as FTS5 operators, so
"What are the six user journeys defined in the PRD" returned zero FTS
rows). Not context truncation (fixed separately in M1) or the generation
model.
