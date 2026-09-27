# Docket accuracy and evidence workflow

Docket parses local PDF/DOCX files with Docling, creates heading-aware chunks,
embeds them through Ollama, and fuses SQLite FTS5 and LanceDB rankings with RRF.
Ordinary queries generate from retrieved chunks; investigative queries use a
bounded search/read agent. Both paths now share final citation enforcement.

## What is enforced

- Search and direct evidence reads require an active source and current version.
  Historical bytes and rows remain stored, but are unavailable to query tools.
- Missing or fabricated citation tags trigger at most one regeneration using
  the evidence already resolved. If tags remain invalid, or the agent ends
  with an unfinished tool request, Docket abstains. Final cited evidence is
  checked again for revocation before release.
- Citation-tag validity does not prove semantic support. The eval judge checks
  every candidate passing answer against its cited passages, including extra
  claims and whether each claim's own citation supports it. Deterministic
  failures remain failures. Judge output is bound to the gold set and exact run.
- Agent traces retain search/read calls, model input messages, tool schemas,
  responses, and available token counts. Generation and repair calls are also
  retained. Agent evidence metrics describe the chunks actually read; they are
  not a measurement of all search candidates. Recorded input is not proof that
  the model's internal context window consumed every token.

## Prepare representative documents and questions

Use a dedicated corpus folder. Each immediate subfolder is a revocable source;
place all documents inside those subfolders, or use a flat folder as one source.
Keep private documents and eval artifacts outside the repository.

```sh
docket eval draft --corpus /path/to/corpus --out /path/to/gold.yaml --count 100
docket eval review /path/to/gold.yaml
```

Drafting supplies corpus-relative `source_documents`. Check quotes and expected
answers against the original PDFs/DOCX files, including the page image for
math and tables. Reviewing only parsed text can bless a parser's mistake.
Add handwritten numeric, multi-document, follow-up, out-of-corpus and revoked
cases; drafts remain a starting point for simple fact/list/table questions.

The gold file's population must be explicit for acceptance:

```yaml
version: 1
population: real_user_documents
questions:
  - id: vacation-days
    type: single_fact
    question: How many annual vacation days are allowed?
    answerable: true
    must_contain: ['re:\b25\b']
    gold_spans: ['25 days of paid vacation per year']
    source_documents: ['handbook/policies.pdf']
    formula_dependent: false
    split: test
    reviewed: true
```

This is a schema example, not a claim about a real document. Use your own facts.
`formula_dependent: true` marks questions whose answer requires an equation.
Missing labels appear as `unlabeled` in reports and block milestone acceptance.
A formula question can be either numeric, single-fact, multi-document, etc.

Single-document drafts receive a stable split derived from the document path.
Assign connected multi-document questions and all of their documents to one
split manually. Loading a gold file rejects documents shared across dev/test.
Review and merge operations preserve explicit splits and population metadata.

## Calibrate on development questions

```sh
docket eval run --corpus /path/to/corpus --gold /path/to/gold.yaml --split dev --mode auto --repeats 3 --out /path/to/dev.jsonl
docket eval judge --gold /path/to/gold.yaml --runs /path/to/dev.jsonl --cross-check --out /path/to/dev-judged.jsonl
docket eval calibrate export --gold /path/to/gold.yaml --runs /path/to/dev.jsonl --judged /path/to/dev-judged.jsonl --n 60 --out /path/to/dev-labels.yaml
```

Fill `correct: true/false` yourself without looking at automatic verdicts.
Exports include full cited passages and citation labels. Acceptance requires
30 distinct judge-scored questions, both pass and fail human labels, matching
run fingerprints, and judge Cohen's kappa >= 0.7. Repeated labels for the same
question do not increase that independent sample count.

```sh
docket eval calibrate score --labels /path/to/dev-labels.yaml --judged /path/to/dev-judged.jsonl
```

For routing experiments, run the same dev questions with `--mode fast` and
`--mode agent`, judge each file, then use `docket eval compare FAST_JUDGED
AGENT_JUDGED`. Keep files separate: duplicate `(question_id, repeat)` records
are rejected. Select routing changes from dev results; retain the test split
for acceptance. No classifier rule is promoted simply because it fits the
existing physics questions.

## Freeze and test

```sh
docket eval freeze --corpus /path/to/corpus --gold /path/to/gold.yaml --out /path/to/benchmark.json
docket eval run --corpus /path/to/corpus --gold /path/to/gold.yaml --split test --mode auto --repeats 3 --manifest /path/to/benchmark.json --out /path/to/test.jsonl
docket eval judge --gold /path/to/gold.yaml --runs /path/to/test.jsonl --cross-check --out /path/to/test-judged.jsonl
docket eval report --gold /path/to/gold.yaml --runs /path/to/test.jsonl --judged /path/to/test-judged.jsonl --manifest /path/to/benchmark.json --labels /path/to/dev-labels.yaml --calibration-judged /path/to/dev-judged.jsonl --require-milestone
```

Freezing hashes the gold set and original document bytes. Frozen runs reject
changed inputs and require three repeats. Runtime model names/settings are
recorded separately; archive the installed model/dependency versions alongside
results because a model tag alone is not an immutable model digest.

The milestone requires a reviewed, fully recorded real-document test split,
matching frozen inputs and semantic judgments, calibrated judge models, revoked
and out-of-corpus cases, and no observed revoked-source leak. Its question-level
majority-pass rate must have a **95% Wilson lower bound >= 90%**. Missing runs,
legacy unbound judgments and absent calibration cannot pass the gate.
`--require-milestone` exits nonzero when these conditions are not met.

Reports retain pass-all, type, document and formula slices. Multi-document
questions appear in each relevant document slice, so these slices overlap.
Wilson intervals use questions as the sampling unit; near-duplicate questions
from one document do not justify broad claims about new documents.

## Formula evidence and experiments

Formula enrichment is explicitly disabled. Detected formula page numbers,
bounding boxes, coordinate origins, page sizes and item references are stored
on the evidence version and returned with resolved evidence. These regions
cover the source document, not an asserted one-to-one mapping to a chunk.
Original bytes remain the authority. Versions created before migration 0004
receive a one-time coordinate backfill on re-ingestion without changing their
existing chunk IDs. Plain-text rendering preserves currency, citation labels,
and explicit vector/hat notation; raw model replies remain in eval traces.

Before enabling formula answers from a new extraction method, assemble a
separate manually checked equation set. For each source version and region,
compare a page-image reading or verified transcription to the original crop,
including signs, exponents, subscripts, vectors and units. Agreement between
models alone is not verification. Keep that experiment outside the searchable
index until reviewed; no automatic visual-model promotion is implemented.

The existing physics artifacts can be found under
`~/.local/share/docket/eval/gold_physics.yaml`, `physics-runs.jsonl`, and
`physics-judged.jsonl`. They remain a public-textbook stress test. A historical
high score with legacy citation checks cannot establish the real-document
milestone. New runs with the stricter judge are a new measurement baseline.
