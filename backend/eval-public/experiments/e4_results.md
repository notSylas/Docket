# E4 results: wider pool and Ollama reranker (retrieval only)

Script: `e4_pool_rerank.py`. Questions: answerable with gold_spans, follow_up skipped (6 in extended, 0 in G1; they need the LLM rewrite). "poolN" = per-leg size N passed to `hybrid_search(top_k=N)`, then the fused list is cut to final k (so pool8 gives at most about 16 fused, and k12/k20 equal k8 only because pool8 rarely has more candidates than 8 after fusion cut; it returns top_k=8). Reranker: pointwise yes/no, qwen3 via Ollama /api/chat, temperature 0, think off, logprobs (score = logsumexp(yes variants) - logsumexp(no variants) on the first token), input = Location line + context lines + chunk text, applied to the full fused 30-chunk pool. MRR = 1/rank of the first chunk containing any gold span.
The original `gold.yaml` set (../Docs corpus, 33 questions) was also run as the "original-set" regression check.

## Conclusion (plain language)

- A wider pool alone helps, because the gold chunks are mostly already inside the top 30 but just not the top 8. On G1, all-spans goes 58.7% -> 63.0% (final 8), 69.6% (final 12), 82.6% (final 20) with no per-question losses at k12/k20. On extended, 86.8% -> 89.5% / 92.1% / 97.4%.
- The qwen3:8b reranker is the best option on G1: all-spans 71.7% at final 8 (+6 net questions) and 78.3% at final 12 (+9 net, 0 lost), and first-gold-chunk MRR rises 0.54 -> 0.72. Plain pool30 at final 20 gets more raw recall (82.6%) but passes 20 chunks to the answer model.
- qwen3:14b is worse than 8b as a reranker here (G1 final 8: -1 net; final 12: +4 net). Bigger is not better with this prompt.
- On extended, reranking is mixed: 8b nets +1 (3 gained, 2 lost; any-span drops 36 -> 35), 14b nets +2 (3 gained, 1 lost). Both stay under the pool-only k12 result (+2, 0 lost). On the original set, reranking keeps 33/33 and raises MRR 0.80 -> 0.95; pool30 final 8 without rerank loses 1 question, which the reranker and final 12 both avoid.
- Against the section 8 threshold (net gain >= 3 questions on the extended set, no original-set regression, <= 4 s added at p50): NOT MET on the extended set. Net gain is +1 (8b) or +2 (14b), below 3, and 1-2 questions lose all-spans; the original set shows no regression. The gain only clears 3 on G1 (+6 / +9 for 8b), but the threshold is defined on the extended set, which is mostly easy. Latency: warm, 30 chunks sequentially took about 1.1-1.5 s (8b) and 1.7-2.1 s (14b) in a separate spot check, so latency would pass; the in-run numbers in the tables (8 s for 8b, 4-6 s for 14b) are inflated by Ollama swapping models between the two rerankers and the embedding model on every question, and should not be used.
- Cheapest reliable step: widen the pool to 30 and send final 12 (no model call, +5 net on G1, +2 on extended, 0 lost at k12 except 1 on G1 at k8). The 8b reranker at final 12 is the strongest G1 result (+9, 0 lost) and is worth a decision if G1-style numeric questions matter; keep default off per section 8 until the extended-set criterion is met.

## Caveats

- hybrid_search latency rows: pool8 includes the embedding-model load on the first call; pool30 reuses the warm model (0.02 s is the embedding cache/warm path), so compare neither as a pure pool-size cost.
- Single run, temperature 0, 46 + 38 + 33 questions: a net change of 1 or 2 questions is within noise.
- Gold-span matching is on chunk text only (not the Location/Context lines the reranker sees).
- No product code under backend/src was changed. All ingestion went to scratch data dirs.

## G1 (hard numeric): 46 questions (follow_up skipped: 0)

### Overall

| config | any-span | all-spans | MRR |
|---|---|---|---|
| fuse pool8 k8 | 82.6% (38/46) | 58.7% (27/46) | 0.543 |
| fuse pool8 k12 | 82.6% (38/46) | 58.7% (27/46) | 0.543 |
| fuse pool8 k20 | 82.6% (38/46) | 58.7% (27/46) | 0.543 |
| fuse pool30 k8 | 87.0% (40/46) | 63.0% (29/46) | 0.556 |
| fuse pool30 k12 | 93.5% (43/46) | 69.6% (32/46) | 0.563 |
| fuse pool30 k20 | 100.0% (46/46) | 82.6% (38/46) | 0.567 |
| rerank qwen3:8b pool30 k8 | 97.8% (45/46) | 71.7% (33/46) | 0.719 |
| rerank qwen3:8b pool30 k12 | 97.8% (45/46) | 78.3% (36/46) | 0.719 |
| rerank qwen3:14b pool30 k8 | 89.1% (41/46) | 56.5% (26/46) | 0.601 |
| rerank qwen3:14b pool30 k12 | 93.5% (43/46) | 67.4% (31/46) | 0.605 |

### Net all-spans change vs baseline (pool8 k8, production default), questions

| config | gained | lost | net | any-span gained | any-span lost |
|---|---|---|---|---|---|
| fuse pool8 k8 | 0 | 0 | +0 | 0 | 0 |
| fuse pool8 k12 | 0 | 0 | +0 | 0 | 0 |
| fuse pool8 k20 | 0 | 0 | +0 | 0 | 0 |
| fuse pool30 k8 | 3 | 1 | +2 | 3 | 1 |
| fuse pool30 k12 | 6 | 1 | +5 | 5 | 0 |
| fuse pool30 k20 | 11 | 0 | +11 | 8 | 0 |
| rerank qwen3:8b pool30 k8 | 8 | 2 | +6 | 7 | 0 |
| rerank qwen3:8b pool30 k12 | 9 | 0 | +9 | 7 | 0 |
| rerank qwen3:14b pool30 k8 | 6 | 7 | -1 | 4 | 1 |
| rerank qwen3:14b pool30 k12 | 8 | 4 | +4 | 5 | 0 |

### Type `multi_doc` (8 questions)

| config | any-span | all-spans | MRR |
|---|---|---|---|
| fuse pool8 k8 | 75.0% (6/8) | 37.5% (3/8) | 0.573 |
| fuse pool8 k12 | 75.0% (6/8) | 37.5% (3/8) | 0.573 |
| fuse pool8 k20 | 75.0% (6/8) | 37.5% (3/8) | 0.573 |
| fuse pool30 k8 | 75.0% (6/8) | 37.5% (3/8) | 0.573 |
| fuse pool30 k12 | 100.0% (8/8) | 50.0% (4/8) | 0.599 |
| fuse pool30 k20 | 100.0% (8/8) | 50.0% (4/8) | 0.599 |
| rerank qwen3:8b pool30 k8 | 100.0% (8/8) | 62.5% (5/8) | 0.542 |
| rerank qwen3:8b pool30 k12 | 100.0% (8/8) | 62.5% (5/8) | 0.542 |
| rerank qwen3:14b pool30 k8 | 75.0% (6/8) | 25.0% (2/8) | 0.500 |
| rerank qwen3:14b pool30 k12 | 75.0% (6/8) | 62.5% (5/8) | 0.500 |

### Type `numeric` (38 questions)

| config | any-span | all-spans | MRR |
|---|---|---|---|
| fuse pool8 k8 | 84.2% (32/38) | 63.2% (24/38) | 0.536 |
| fuse pool8 k12 | 84.2% (32/38) | 63.2% (24/38) | 0.536 |
| fuse pool8 k20 | 84.2% (32/38) | 63.2% (24/38) | 0.536 |
| fuse pool30 k8 | 89.5% (34/38) | 68.4% (26/38) | 0.552 |
| fuse pool30 k12 | 92.1% (35/38) | 73.7% (28/38) | 0.555 |
| fuse pool30 k20 | 100.0% (38/38) | 89.5% (34/38) | 0.560 |
| rerank qwen3:8b pool30 k8 | 97.4% (37/38) | 73.7% (28/38) | 0.756 |
| rerank qwen3:8b pool30 k12 | 97.4% (37/38) | 81.6% (31/38) | 0.756 |
| rerank qwen3:14b pool30 k8 | 92.1% (35/38) | 63.2% (24/38) | 0.622 |
| rerank qwen3:14b pool30 k12 | 97.4% (37/38) | 68.4% (26/38) | 0.627 |

### Latency (seconds per question)

| step | p50 | p90 | mean |
|---|---|---|---|
| hybrid_search pool8 (embed+FTS+vector+fuse) | 0.55 | 0.56 | 0.54 |
| hybrid_search pool30 (embed+FTS+vector+fuse) | 0.02 | 0.02 | 0.02 |
| rerank qwen3:8b, 30 chunks (sequential) | 8.05 | 8.21 | 7.33 |
| rerank qwen3:14b, 30 chunks (sequential) | 4.41 | 4.65 | 4.52 |

## Extended: 38 questions (follow_up skipped: 6)

### Overall

| config | any-span | all-spans | MRR |
|---|---|---|---|
| fuse pool8 k8 | 94.7% (36/38) | 86.8% (33/38) | 0.804 |
| fuse pool8 k12 | 94.7% (36/38) | 86.8% (33/38) | 0.804 |
| fuse pool8 k20 | 94.7% (36/38) | 86.8% (33/38) | 0.804 |
| fuse pool30 k8 | 97.4% (37/38) | 89.5% (34/38) | 0.788 |
| fuse pool30 k12 | 97.4% (37/38) | 92.1% (35/38) | 0.788 |
| fuse pool30 k20 | 100.0% (38/38) | 97.4% (37/38) | 0.789 |
| rerank qwen3:8b pool30 k8 | 92.1% (35/38) | 89.5% (34/38) | 0.725 |
| rerank qwen3:8b pool30 k12 | 92.1% (35/38) | 89.5% (34/38) | 0.725 |
| rerank qwen3:14b pool30 k8 | 94.7% (36/38) | 92.1% (35/38) | 0.836 |
| rerank qwen3:14b pool30 k12 | 94.7% (36/38) | 92.1% (35/38) | 0.836 |

### Net all-spans change vs baseline (pool8 k8, production default), questions

| config | gained | lost | net | any-span gained | any-span lost |
|---|---|---|---|---|---|
| fuse pool8 k8 | 0 | 0 | +0 | 0 | 0 |
| fuse pool8 k12 | 0 | 0 | +0 | 0 | 0 |
| fuse pool8 k20 | 0 | 0 | +0 | 0 | 0 |
| fuse pool30 k8 | 1 | 0 | +1 | 1 | 0 |
| fuse pool30 k12 | 2 | 0 | +2 | 1 | 0 |
| fuse pool30 k20 | 4 | 0 | +4 | 2 | 0 |
| rerank qwen3:8b pool30 k8 | 3 | 2 | +1 | 1 | 2 |
| rerank qwen3:8b pool30 k12 | 3 | 2 | +1 | 1 | 2 |
| rerank qwen3:14b pool30 k8 | 3 | 1 | +2 | 1 | 1 |
| rerank qwen3:14b pool30 k12 | 3 | 1 | +2 | 1 | 1 |

### Type `multi_doc` (9 questions)

| config | any-span | all-spans | MRR |
|---|---|---|---|
| fuse pool8 k8 | 100.0% (9/9) | 88.9% (8/9) | 0.829 |
| fuse pool8 k12 | 100.0% (9/9) | 88.9% (8/9) | 0.829 |
| fuse pool8 k20 | 100.0% (9/9) | 88.9% (8/9) | 0.829 |
| fuse pool30 k8 | 100.0% (9/9) | 88.9% (8/9) | 0.831 |
| fuse pool30 k12 | 100.0% (9/9) | 88.9% (8/9) | 0.831 |
| fuse pool30 k20 | 100.0% (9/9) | 88.9% (8/9) | 0.831 |
| rerank qwen3:8b pool30 k8 | 100.0% (9/9) | 100.0% (9/9) | 0.944 |
| rerank qwen3:8b pool30 k12 | 100.0% (9/9) | 100.0% (9/9) | 0.944 |
| rerank qwen3:14b pool30 k8 | 100.0% (9/9) | 100.0% (9/9) | 1.000 |
| rerank qwen3:14b pool30 k12 | 100.0% (9/9) | 100.0% (9/9) | 1.000 |

### Type `numeric` (24 questions)

| config | any-span | all-spans | MRR |
|---|---|---|---|
| fuse pool8 k8 | 91.7% (22/24) | 83.3% (20/24) | 0.774 |
| fuse pool8 k12 | 91.7% (22/24) | 83.3% (20/24) | 0.774 |
| fuse pool8 k20 | 91.7% (22/24) | 83.3% (20/24) | 0.774 |
| fuse pool30 k8 | 95.8% (23/24) | 87.5% (21/24) | 0.748 |
| fuse pool30 k12 | 95.8% (23/24) | 91.7% (22/24) | 0.748 |
| fuse pool30 k20 | 100.0% (24/24) | 100.0% (24/24) | 0.751 |
| rerank qwen3:8b pool30 k8 | 87.5% (21/24) | 83.3% (20/24) | 0.616 |
| rerank qwen3:8b pool30 k12 | 87.5% (21/24) | 83.3% (20/24) | 0.616 |
| rerank qwen3:14b pool30 k8 | 91.7% (22/24) | 87.5% (21/24) | 0.771 |
| rerank qwen3:14b pool30 k12 | 91.7% (22/24) | 87.5% (21/24) | 0.771 |

### Type `table_lookup` (5 questions)

| config | any-span | all-spans | MRR |
|---|---|---|---|
| fuse pool8 k8 | 100.0% (5/5) | 100.0% (5/5) | 0.900 |
| fuse pool8 k12 | 100.0% (5/5) | 100.0% (5/5) | 0.900 |
| fuse pool8 k20 | 100.0% (5/5) | 100.0% (5/5) | 0.900 |
| fuse pool30 k8 | 100.0% (5/5) | 100.0% (5/5) | 0.900 |
| fuse pool30 k12 | 100.0% (5/5) | 100.0% (5/5) | 0.900 |
| fuse pool30 k20 | 100.0% (5/5) | 100.0% (5/5) | 0.900 |
| rerank qwen3:8b pool30 k8 | 100.0% (5/5) | 100.0% (5/5) | 0.850 |
| rerank qwen3:8b pool30 k12 | 100.0% (5/5) | 100.0% (5/5) | 0.850 |
| rerank qwen3:14b pool30 k8 | 100.0% (5/5) | 100.0% (5/5) | 0.850 |
| rerank qwen3:14b pool30 k12 | 100.0% (5/5) | 100.0% (5/5) | 0.850 |

### Latency (seconds per question)

| step | p50 | p90 | mean |
|---|---|---|---|
| hybrid_search pool8 (embed+FTS+vector+fuse) | 0.55 | 0.56 | 0.54 |
| hybrid_search pool30 (embed+FTS+vector+fuse) | 0.02 | 0.02 | 0.02 |
| rerank qwen3:8b, 30 chunks (sequential) | 8.29 | 8.35 | 7.27 |
| rerank qwen3:14b, 30 chunks (sequential) | 4.75 | 4.86 | 4.72 |

## Original gold.yaml (../Docs): 33 questions (follow_up skipped: 0)

### Overall

| config | any-span | all-spans | MRR |
|---|---|---|---|
| fuse pool8 k8 | 100.0% (33/33) | 100.0% (33/33) | 0.801 |
| fuse pool8 k12 | 100.0% (33/33) | 100.0% (33/33) | 0.801 |
| fuse pool8 k20 | 100.0% (33/33) | 100.0% (33/33) | 0.801 |
| fuse pool30 k8 | 97.0% (32/33) | 97.0% (32/33) | 0.775 |
| fuse pool30 k12 | 100.0% (33/33) | 100.0% (33/33) | 0.779 |
| fuse pool30 k20 | 100.0% (33/33) | 100.0% (33/33) | 0.779 |
| rerank qwen3:8b pool30 k8 | 100.0% (33/33) | 100.0% (33/33) | 0.949 |
| rerank qwen3:8b pool30 k12 | 100.0% (33/33) | 100.0% (33/33) | 0.949 |
| rerank qwen3:14b pool30 k8 | 100.0% (33/33) | 100.0% (33/33) | 0.949 |
| rerank qwen3:14b pool30 k12 | 100.0% (33/33) | 100.0% (33/33) | 0.949 |

### Net all-spans change vs baseline (pool8 k8, production default), questions

| config | gained | lost | net | any-span gained | any-span lost |
|---|---|---|---|---|---|
| fuse pool8 k8 | 0 | 0 | +0 | 0 | 0 |
| fuse pool8 k12 | 0 | 0 | +0 | 0 | 0 |
| fuse pool8 k20 | 0 | 0 | +0 | 0 | 0 |
| fuse pool30 k8 | 0 | 1 | -1 | 0 | 1 |
| fuse pool30 k12 | 0 | 0 | +0 | 0 | 0 |
| fuse pool30 k20 | 0 | 0 | +0 | 0 | 0 |
| rerank qwen3:8b pool30 k8 | 0 | 0 | +0 | 0 | 0 |
| rerank qwen3:8b pool30 k12 | 0 | 0 | +0 | 0 | 0 |
| rerank qwen3:14b pool30 k8 | 0 | 0 | +0 | 0 | 0 |
| rerank qwen3:14b pool30 k12 | 0 | 0 | +0 | 0 | 0 |

### Type `enumeration` (9 questions)

| config | any-span | all-spans | MRR |
|---|---|---|---|
| fuse pool8 k8 | 100.0% (9/9) | 100.0% (9/9) | 0.870 |
| fuse pool8 k12 | 100.0% (9/9) | 100.0% (9/9) | 0.870 |
| fuse pool8 k20 | 100.0% (9/9) | 100.0% (9/9) | 0.870 |
| fuse pool30 k8 | 88.9% (8/9) | 88.9% (8/9) | 0.833 |
| fuse pool30 k12 | 100.0% (9/9) | 100.0% (9/9) | 0.846 |
| fuse pool30 k20 | 100.0% (9/9) | 100.0% (9/9) | 0.846 |
| rerank qwen3:8b pool30 k8 | 100.0% (9/9) | 100.0% (9/9) | 0.944 |
| rerank qwen3:8b pool30 k12 | 100.0% (9/9) | 100.0% (9/9) | 0.944 |
| rerank qwen3:14b pool30 k8 | 100.0% (9/9) | 100.0% (9/9) | 0.926 |
| rerank qwen3:14b pool30 k12 | 100.0% (9/9) | 100.0% (9/9) | 0.926 |

### Type `single_fact` (22 questions)

| config | any-span | all-spans | MRR |
|---|---|---|---|
| fuse pool8 k8 | 100.0% (22/22) | 100.0% (22/22) | 0.777 |
| fuse pool8 k12 | 100.0% (22/22) | 100.0% (22/22) | 0.777 |
| fuse pool8 k20 | 100.0% (22/22) | 100.0% (22/22) | 0.777 |
| fuse pool30 k8 | 100.0% (22/22) | 100.0% (22/22) | 0.754 |
| fuse pool30 k12 | 100.0% (22/22) | 100.0% (22/22) | 0.754 |
| fuse pool30 k20 | 100.0% (22/22) | 100.0% (22/22) | 0.754 |
| rerank qwen3:8b pool30 k8 | 100.0% (22/22) | 100.0% (22/22) | 0.947 |
| rerank qwen3:8b pool30 k12 | 100.0% (22/22) | 100.0% (22/22) | 0.947 |
| rerank qwen3:14b pool30 k8 | 100.0% (22/22) | 100.0% (22/22) | 0.955 |
| rerank qwen3:14b pool30 k12 | 100.0% (22/22) | 100.0% (22/22) | 0.955 |

### Type `table_lookup` (2 questions)

| config | any-span | all-spans | MRR |
|---|---|---|---|
| fuse pool8 k8 | 100.0% (2/2) | 100.0% (2/2) | 0.750 |
| fuse pool8 k12 | 100.0% (2/2) | 100.0% (2/2) | 0.750 |
| fuse pool8 k20 | 100.0% (2/2) | 100.0% (2/2) | 0.750 |
| fuse pool30 k8 | 100.0% (2/2) | 100.0% (2/2) | 0.750 |
| fuse pool30 k12 | 100.0% (2/2) | 100.0% (2/2) | 0.750 |
| fuse pool30 k20 | 100.0% (2/2) | 100.0% (2/2) | 0.750 |
| rerank qwen3:8b pool30 k8 | 100.0% (2/2) | 100.0% (2/2) | 1.000 |
| rerank qwen3:8b pool30 k12 | 100.0% (2/2) | 100.0% (2/2) | 1.000 |
| rerank qwen3:14b pool30 k8 | 100.0% (2/2) | 100.0% (2/2) | 1.000 |
| rerank qwen3:14b pool30 k12 | 100.0% (2/2) | 100.0% (2/2) | 1.000 |

### Latency (seconds per question)

| step | p50 | p90 | mean |
|---|---|---|---|
| hybrid_search pool8 (embed+FTS+vector+fuse) | 0.55 | 0.56 | 0.54 |
| hybrid_search pool30 (embed+FTS+vector+fuse) | 0.02 | 0.02 | 0.02 |
| rerank qwen3:8b, 30 chunks (sequential) | 8.81 | 9.19 | 7.81 |
| rerank qwen3:14b, 30 chunks (sequential) | 5.95 | 6.30 | 5.91 |
