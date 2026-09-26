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

## Baseline (pre-M2, post-M1)

Reproduce with:

```
docket eval run --corpus ../Docs --gold eval-public/gold.yaml --split all --repeats 3 --out <out>.jsonl
docket eval judge --gold eval-public/gold.yaml --runs <out>.jsonl --cross-check --out <out>.judged.jsonl
docket eval report --gold eval-public/gold.yaml --runs <out>.jsonl --judged <out>.judged.jsonl
```

**Accuracy (judged): 87.9% (29/33), 95% interval 72.7%-95.2%.**

By type: enumeration 88.9% (8/9), single_fact 86.4% (19/22), table_lookup
100% (2/2). By split: dev 77.8% (14/18), test 100% (15/15) — small-N split,
not yet meaningful on its own.

Retrieval recall@8: 81.8%. Fact-in-context: 81.8%. Wrongful abstention
(model said "not found" on an answerable question): 5.1% (5/99).

**Failure classification: 11/11 classified failures were `retrieval_miss`.**
Zero parse, context_truncation, generation_error, or citation_error. This
points squarely at the FTS5 keyword-search bug described in the plan (M2),
not at context truncation (already fixed in M1) or the generation model.

Caveats: N=33 is small (wide interval); these are easy single-fact-style
questions, so real accuracy on harder types and on the user's own documents
is expected to be lower; this reviewer (Claude) is not independent of the
question drafter (also an LLM) — the plan's required ~30 user-labeled
calibration sample has not been done yet, so the judge is not yet certified
via kappa.
