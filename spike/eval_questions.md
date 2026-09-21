# Retrieval Quality Evaluation Questions

Run each through `python query.py "<question>"` and grade manually in RESULTS.md.

## Should be answerable (factual lookup)
1. What vector database was chosen as the primary candidate, and what is the challenger being benchmarked against it?
2. What are the exit criteria for the G1 release gate?
3. What generation model is proposed, and how is it served initially?
4. What are the six user journeys defined in the PRD?
5. What lifecycle states can a source move through, from active to fully removed?

## Should trigger abstention (not covered by the corpus)
6. What is the company's revenue target for this product in year one?
7. Who is the CEO of the company building this?
8. What programming language was used for a previous version of this product?

## Trap questions (require distinguishing similar concepts)
9. What's the difference between an evidence_unit and a chunk in the database design?
10. Is FTS5 used for lexical or semantic search, and what does LanceDB handle?
11. What's the difference between the control plane and the intelligence plane?
12. What distinguishes "derived" from "inferred" in the epistemic status enum?

## Notes
- Grade each on: (a) citation correctness — does it point to the right doc/chunk,
  (b) faithfulness — no claims beyond what the cited chunk supports,
  (c) correct abstention where expected.
