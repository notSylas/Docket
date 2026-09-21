// Canned `QueryResult`-shaped examples, matching
// `attest.query.service.QueryResult` / `Citation`
// (backend/src/attest/query/service.py) field-for-field:
//   QueryResult:  question, answer, citations, abstained, validation_warnings, mode
//   Citation:     citation_label, chunk_id, source_display_name
//
// citation_label follows the EXACT real format from
// `attest.retrieval.resolver._citation_label`:
//   f"[{source_display_name} #{chunk_id[:12]}]"
//
// abstained answers use the EXACT real string from
// `attest.query.prompts.ABSTENTION_PHRASE`.
//
// Everything else attached to a citation or result below (claim_type,
// is_historical, as_of, superseded, authority, heading, chunk_text,
// checked_items, conflict_group) is a UI-only extension the real backend
// doesn't return yet -- called out here so it's obvious what's "real
// shape" vs "invented for this preview". The intent is that these are
// plausible *additive* fields the backend could grow into (e.g. resolver
// already has `heading`/chunk text internally via `ResolvedEvidence`,
// see backend/src/attest/retrieval/resolver.py), not a redesign.

import { QUERY_MODE } from "../constants/queryMode";

const ABSTENTION_PHRASE = "I don't know based on the available evidence.";

function chunkId(hex) {
  // pad/trim to a believable-looking sha1-ish hex id
  return hex.padEnd(24, "0").slice(0, 24);
}

function makeCitation({
  sourceDisplayName,
  chunkIdHex,
  heading,
  chunkText,
  claimType = "evidenced",
  isHistorical = false,
  asOf = null,
  superseded = false,
  authority = "Internal Doc",
  conflictGroup = null,
}) {
  const id = chunkId(chunkIdHex);
  return {
    // -- real Citation shape --
    citation_label: `[${sourceDisplayName} #${id.slice(0, 12)}]`,
    chunk_id: id,
    source_display_name: sourceDisplayName,
    // -- UI-only extensions --
    heading,
    chunk_text: chunkText,
    claim_type: claimType, // "evidenced" | "inferred"
    is_historical: isHistorical,
    as_of: asOf,
    superseded,
    authority, // e.g. "Approved Spec" | "Meeting Note" | "Internal Doc"
    conflict_group: conflictGroup,
  };
}

// -- 1. Fast-path answer, plain evidenced citations -------------------------

export const fastAnswer = {
  question: "What vector database does Attest use for the embedding index?",
  answer:
    "Attest uses LanceDB as the embedding index [attest-architecture.md #a1b2c3d4e5f6], chosen for its embedded, file-based deployment model -- no separate server process needs to run alongside the desktop app [attest-architecture.md #a1b2c3d4e5f6]. Chunking for the index uses a 200-word chunk size with 40-word overlap [ingestion-pipeline-spec.docx #7f8e9d0c1b2a].",
  citations: [
    makeCitation({
      sourceDisplayName: "attest-architecture.md",
      chunkIdHex: "a1b2c3d4e5f6",
      heading: "Vector Database Selection",
      chunkText:
        "LanceDB is the primary vector DB candidate for the embedding index, chosen over Chroma and Qdrant primarily for its embedded, file-based deployment model -- no separate server process to run alongside the desktop app, which matters for a local-first tool distributed as a single executable. LanceDB stores its tables directly under the app's data directory and supports hybrid (vector + full-text) search natively, which the retrieval layer relies on for combining semantic and keyword signal in one pass.",
      authority: "Approved Spec",
      asOf: "2026-07-02",
    }),
    makeCitation({
      sourceDisplayName: "ingestion-pipeline-spec.docx",
      chunkIdHex: "7f8e9d0c1b2a",
      heading: "Chunking Recipe",
      chunkText:
        "The default chunk recipe splits parsed document text into windows of 200 words with a 40-word overlap between consecutive chunks. The overlap exists so that a fact stated near a chunk boundary is still fully readable from at least one chunk, rather than being split across two chunks with neither containing the complete sentence.",
      authority: "Approved Spec",
      asOf: "2026-06-18",
    }),
  ],
  abstained: false,
  validation_warnings: [],
  mode: QUERY_MODE.FAST,
};

// -- 2. Agent-mode answer, mix of evidenced + inferred claims ---------------

export const agentAnswer = {
  question:
    "Compare the fast query path and the agent investigation path -- why does Attest need both?",
  answer:
    "The fast path does a single retrieve-then-generate pass: hybrid search over the chunk index, resolve the top matches to evidence, then generate a cited answer in one shot [query-service-design.pdf #4d5e6f7a8b9c]. It was validated at 12/12 correct on the spike's eval set for single-fact lookups [query-service-design.pdf #4d5e6f7a8b9c]. The agent path instead lets a bounded LangGraph loop call search and read-evidence tools itself, re-searching with different terms as needed [query-service-design.pdf #1a2b3c4d5e6f]. Attest needs both because most real questions are single-fact lookups where the extra tool-call latency of the agent path buys nothing [query-service-design.pdf #4d5e6f7a8b9c] -- but comparison, causal \"why\", and history-over-time questions generally can't be answered from one retrieval pass, since they require gathering and relating multiple pieces of evidence before an answer is even well-formed. That second category is inherently rarer in day-to-day use, which is why the classifier is designed to default to the fast path.",
  citations: [
    makeCitation({
      sourceDisplayName: "query-service-design.pdf",
      chunkIdHex: "4d5e6f7a8b9c",
      heading: "Fast Path",
      chunkText:
        "The fast path performs hybrid retrieval over the chunk index, resolves the top-k chunks to citation-ready evidence, and generates a citation-grounded answer in a single pass. This is the original spike-validated flow (see RESULTS.md, 'Retrieval Quality'): 12 out of 12 correct on the eval set, including correct abstention on out-of-corpus questions.",
      claimType: "evidenced",
      authority: "Approved Spec",
      asOf: "2026-07-10",
    }),
    makeCitation({
      sourceDisplayName: "query-service-design.pdf",
      chunkIdHex: "1a2b3c4d5e6f",
      heading: "Agent Investigation Path",
      chunkText:
        "Questions that match the heuristic classifier's agent patterns are routed to a bounded LangGraph agent instead of the single-pass fast path. The agent calls search_knowledge and read_evidence itself, deciding what to look at and whether to search again, up to a configured iteration and tool-call cap. Its citations are reconstructed after the fact from the actual tool-call trace, then validated through the same citation-validation logic as the fast path.",
      claimType: "evidenced",
      authority: "Approved Spec",
      asOf: "2026-07-10",
    }),
    makeCitation({
      sourceDisplayName: "query-service-design.pdf",
      chunkIdHex: "9c8b7a6d5e4f",
      heading: "Routing Rationale",
      chunkText:
        "Routing leans conservative: an investigative question that slips through to the fast path still gets an answer, just a possibly incomplete one, whereas routing an ordinary lookup to the agent costs latency but not correctness. That asymmetry is why the agent pattern list stays short and reviewable instead of exhaustive.",
      claimType: "inferred",
      authority: "Approved Spec",
      asOf: "2026-07-10",
    }),
  ],
  abstained: false,
  validation_warnings: [],
  mode: QUERY_MODE.AGENT,
};

// -- 3. Abstention / insufficient-evidence -----------------------------------

export const abstentionAnswer = {
  question: "What is Attest's pricing model for enterprise customers?",
  answer: ABSTENTION_PHRASE,
  citations: [],
  abstained: true,
  validation_warnings: [],
  mode: QUERY_MODE.FAST,
  // UI-only: itemized "what was checked" list, so the abstention state
  // shows work rather than just the bare sentence.
  checked_items: [
    { label: "Hybrid search over all active sources", result: "12 candidate chunks retrieved" },
    { label: "attest/specs", result: "no pricing-related content found" },
    { label: "attest/meeting-notes", result: "no pricing-related content found" },
    { label: "vendor-contracts", result: "source currently missing, skipped" },
  ],
};

// -- 4. Conflicting-sources example ------------------------------------------

export const conflictAnswer = {
  question: "What embedding model does Attest use, and is that settled?",
  answer:
    "The approved spec states the embedding model is qwen3-embedding:0.6b [embedding-model-spec.pdf #2b3c4d5e6f7a]. A more recent meeting note raises switching to a larger embedding model, but this was a discussion point, not a decision [meeting-notes-2026-08-20.docx #8a9b0c1d2e3f]. Since the spec has higher authority than a meeting note regardless of which document is newer, qwen3-embedding:0.6b remains the answer until the spec itself is updated.",
  citations: [
    makeCitation({
      sourceDisplayName: "embedding-model-spec.pdf",
      chunkIdHex: "2b3c4d5e6f7a",
      heading: "Embedding Model",
      chunkText:
        "The embedding model is fixed at qwen3-embedding:0.6b for this checkpoint. This choice was made for local resource headroom -- a larger embedding model would compete with the generation model for VRAM on the same machine, which is not acceptable given the target hardware profile.",
      claimType: "evidenced",
      authority: "Approved Spec",
      asOf: "2026-06-01",
      conflictGroup: "embedding-model-choice",
    }),
    makeCitation({
      sourceDisplayName: "meeting-notes-2026-08-20.docx",
      chunkIdHex: "8a9b0c1d2e3f",
      heading: "Sync: Retrieval Tuning",
      chunkText:
        "Discussed whether a larger embedding model would meaningfully improve recall on longer technical documents. No decision made -- flagged as worth revisiting once more real ingestion volume is available, but nobody is currently proposing to change the spec.",
      claimType: "evidenced",
      authority: "Meeting Note",
      asOf: "2026-08-20",
      conflictGroup: "embedding-model-choice",
    }),
  ],
  abstained: false,
  validation_warnings: [
    "Citations reference sources with conflicting authority levels; higher-authority source preferred.",
  ],
  mode: QUERY_MODE.FAST,
};

// -- 5. Historical-version citation example (agent mode) --------------------

export const historicalAnswer = {
  question: "How has the chunking recipe changed over the years?",
  answer:
    "The chunking recipe was originally 150 words with no overlap [ingestion-pipeline-spec.docx #6e7f8a9b0c1d]. As of 2026-06-18 the spec was updated to the current recipe -- 200-word chunks with 40-word overlap -- to reduce boundary-split facts [ingestion-pipeline-spec.docx #7f8e9d0c1b2a].",
  citations: [
    makeCitation({
      sourceDisplayName: "ingestion-pipeline-spec.docx",
      chunkIdHex: "6e7f8a9b0c1d",
      heading: "Chunking Recipe (v1)",
      chunkText:
        "Initial chunking recipe: 150-word windows, no overlap between chunks. Simple to reason about, but early eval runs showed facts near a chunk boundary sometimes split across two chunks, with neither chunk containing the complete sentence.",
      claimType: "evidenced",
      authority: "Approved Spec",
      asOf: "2026-05-01",
      isHistorical: true,
      superseded: true,
    }),
    makeCitation({
      sourceDisplayName: "ingestion-pipeline-spec.docx",
      chunkIdHex: "7f8e9d0c1b2a",
      heading: "Chunking Recipe (current)",
      chunkText:
        "The default chunk recipe splits parsed document text into windows of 200 words with a 40-word overlap between consecutive chunks. The overlap exists so that a fact stated near a chunk boundary is still fully readable from at least one chunk.",
      claimType: "evidenced",
      authority: "Approved Spec",
      asOf: "2026-06-18",
      isHistorical: false,
      superseded: false,
    }),
  ],
  abstained: false,
  validation_warnings: [],
  mode: QUERY_MODE.AGENT,
};

export const ALL_MOCK_ANSWERS = [
  fastAnswer,
  agentAnswer,
  abstentionAnswer,
  conflictAnswer,
  historicalAnswer,
];

/**
 * Picks a canned QueryResult for a submitted question. Not a real
 * classifier/retriever -- just enough keyword matching to let a demo user
 * reliably trigger each of the visually distinct answer states described in
 * the brief (normal cited answer, abstention, conflicting sources,
 * historical version) while still returning *something* plausible for any
 * free-text question.
 */
export function pickMockResponse(question, mode) {
  const q = question.toLowerCase();

  const withMode = (result) => ({ ...result, question, mode });

  if (/price|pricing|cost|budget/.test(q)) {
    return withMode(abstentionAnswer);
  }
  if (/conflict|authority|settled|which is correct/.test(q)) {
    return withMode(conflictAnswer);
  }
  if (/history|evolve|over time|over the years|changed/.test(q)) {
    return withMode(historicalAnswer);
  }
  if (mode === QUERY_MODE.AGENT) {
    return withMode(agentAnswer);
  }
  return withMode(fastAnswer);
}
