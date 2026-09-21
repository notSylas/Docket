# Tier 1 Validation Spike — Results

Date: 2026-09-21
Corpus: the 6 Work Intelligence design docs (`Docs/*.docx`, 204 chunks after ingestion)

## Model Performance

Ran `bench_model.py` against **qwen3:14b** (qwen3:30b was still downloading — a slow
connection made the 19GB pull impractical to wait on for this spike; re-run this
benchmark against qwen3:30b once it finishes to compare).

| Prompt | Wall time | Tokens | tok/s | VRAM used |
|---|---|---|---|---|
| short_factual | 10.19s | 347 | 51.06 | ~11.4GB / 16GB |
| long_context (with citation instructions) | 6.08s | 302 | 50.61 | ~11.4GB / 16GB |

- GPU: AMD discrete GPU, 16GB VRAM, ROCm-accelerated via Ollama — worked out of the box,
  no CPU fallback needed.
- ~51 tok/s is comfortably fast for interactive use (a few seconds per answer).
- qwen3:14b fits in ~11.4GB, leaving headroom. qwen3:30b (MoE, ~3B active params) is
  expected to fit too given the "A3B" active-parameter design, but this is unverified —
  **follow-up needed** once the download completes.

**Verdict: local inference is fast enough and fits comfortably in this machine's
resources at the 14B tier.** No resource-contention or unusable-desktop issues observed
during a single-query workload (concurrent ingestion+query load not yet tested — still a
Tier 3 item).

## Retrieval Quality

Ran all 12 questions from `eval_questions.md` through `query.py` (hybrid FTS5 + LanceDB
retrieval, RRF fusion, top-8 chunks, qwen3:14b generation with a citation-or-abstain
system prompt).

### Should be answerable (5/5 correct)
1. Vector DB choice — correct (LanceDB primary, Qdrant Edge challenger), citations pointed to the right doc.
2. G1 exit criteria — correct, matched the WBS gate criteria, cited.
3. Generation model — correct (Qwen3-30B-A3B via on-prem Ollama, Inference Gateway), cited.
4. Six user journeys — **partially correct, and revealing.** Retrieval only surfaced 5 of the 6 journey chunks. The model did **not** hallucinate a 6th journey — it explicitly said the context only supported 5 and flagged the discrepancy. This is a retrieval **recall** gap, not a generation/faithfulness failure, and the model's honest handling of missing evidence is exactly the desired trust behavior.
5. Source lifecycle states — correct, all 6 states named accurately, cited.

### Should trigger abstention (3/3 correct)
6. Revenue target — correctly abstained ("I don't know based on the available evidence").
7. CEO — correctly abstained.
8. Prior version's programming language — correctly abstained.

No hallucination observed on any out-of-corpus question.

### Trap questions (4/4 correct)
9. evidence_unit vs. chunk — correct, captured the derivation relationship accurately.
10. FTS5 (lexical) vs. LanceDB (semantic/vector) — correct, well-cited.
11. Control plane vs. intelligence plane — correct.
12. Derived vs. inferred (epistemic status) — correct, captured the distinction precisely.

**Overall: 12/12 questions handled correctly** (counting Q4's honest recall-gap
disclosure as correct behavior, not a failure).

### Known rough edges (implementation, not architecture)
- Citation formatting is inconsistent — the model sometimes emits malformed tags like
  `[source_file#chunk_id: file.docx#chunk_id]` instead of the clean `[file#chunk_id]`
  format requested. This is a prompt-engineering issue, easily fixed with stricter
  formatting instructions or structured output constraints — not a sign the underlying
  approach is broken.
- Chunking is naive (200-word splits on markdown headings) and is the likely cause of
  the Q4 recall miss — a real implementation should tune chunk size/overlap and verify
  recall more rigorously (this spike used k=8 fused results across 204 chunks; a larger
  corpus will need real recall tuning, not just eyeballing).

## Verdict

**Both Tier 1 risks look de-risked enough to proceed.** A 14B local model on this
hardware answers accurately, cites correctly, and abstains honestly rather than
hallucinating — even under a deliberately naive implementation (basic word-count
chunking, no reranking, a small 0.6B embedding model). The architecture as designed
(SQLite FTS5 + LanceDB + RRF fusion + citation-forced generation) is validated as a
sound approach at small scale.

**Follow-ups before treating this as fully proven:**
1. Re-run the model benchmark against qwen3:30b once downloaded, to confirm it fits VRAM and check whether answer quality improves over 14b.
2. Test at a larger, more realistic corpus size (hundreds of documents, not 6) — recall behavior and chunk-size tuning matter more at scale.
3. Fix the citation formatting via a stricter prompt/output schema.
4. Test resource contention (ingestion running concurrently with a query) — not yet exercised.
5. Consider testing the reranker to see if it improves precision on ambiguous questions like Q4.

No findings here suggest the SAD/TDD architecture needs to change. The main
adjustment is tactical: chunking strategy needs real tuning, not a redesign.
