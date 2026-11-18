# Docket Product Gap Analysis

Status: planning only, 2026-11-17. No code, database or dependency was changed; no tests, GPU or heavy jobs were run (an M0 GPU evaluation is in progress and was not disturbed).
Inputs: `docs/COMPETITOR_PAIN_POINTS.md` (complaint ids `C-xx`), the Docket planning documents (`docs/NEXT_PHASE_ROADMAP.md` M0-M9, `docs/RUST_PLATFORM_ARCHITECTURE.md` R1-R11), and the LIVE code on branch `rust-app` at `/home/pc/Desktop/Rajdeep_Dev/Local_Agent-rust` (HEAD `8da99a5`). Live code is authoritative over older notes. Paths below are relative to that checkout.
Companion: `docs/DOCKET_PRODUCT_BACKLOG.md`.

Evidence labels used throughout: **VERIFIED** = read in code or a fetched page in this pass; **INFERENCE** = engineering reasoning from verified facts, not executed; **UNMEASURED** = an assumption that needs a measurement; **Not established from repository evidence** = the repo does not answer.

Classification: (1) implemented and tested, (2) implemented but incomplete, (3) experimental or feature-flagged, (4) planned but not implemented, (5) unknown / not verified. "Tested" means a test exists in the tree; test execution was NOT run in this pass.

## 1. Current verified implementation

| Area | Class | What exists (VERIFIED) | Evidence |
| --- | --- | --- | --- |
| Chat and streaming (desktop) | 1 | `POST /v1/ask` returns JSON or SSE (`phase`, `delta`, `result`, `error`); deltas shown dimmed as unverified draft; final result replaces them. Streaming only on the FAST path (agent path emits no deltas) | `app/docketd/src/api.rs:206-315`; `app/docket-proto/src/api.rs:53-61,156-189`; `app/docket-app/src/ui.rs:396-406`; `backend/src/docket/interfaces/worker.py:14-19,222-235`; `backend/src/docket/services/query/service.py:366-373` docstring; tests in `app/docketd/tests/api.rs` |
| Fail-closed citations, typed abstention | 1 | Citation tags validated after generation; invalid citations produce the abstention; typed `AbstentionReason` | `services/query/citations.py`, `services/query/trust.py:37,131-152`; tests `backend/tests/unit/test_bare_citation_repair.py`, `test_citation_regex_sharing.py` |
| Multi-turn in the engine | 1 | `QueryService.ask(history=...)`, `trim_history`; CLI session keeps `self.history` | `services/query/service.py:366-373,416`; `interfaces/cli/interactive/session.py:76` |
| Multi-turn in the desktop worker path | 4 | Not wired: `AskRequest` has only `question`, `scope`, `stream`; `query_ask` calls `service.ask(question, on_progress, on_delta, scope)` with no history | `app/docket-proto/src/api.rs:53-61`; `worker.py:198-238` |
| Source selection (desktop) | 2 | One source or "All sources"; protocol also accepts `folder` scope but the UI never offers it; no multi-select | `app/docket-app/src/ui.rs:156-205`; `worker.py:202-214` |
| Source management (desktop) | 4 | Worker is read-only (no add/remove/reindex); daemon error text says "Add a folder with the docket CLI first" | `worker.py:21-23`; `app/docketd/src/api.rs:114-118` |
| Source management (CLI) | 1 | `docket ingest`, `docket sources ...` | `interfaces/cli/main.py` (e.g. `sources_skipped` at 713) |
| Ingestion status (CLI/TUI/doctor) | 1 | Tiered indexing with counts per state, ETAs, coverage, dead letters; one-line `docket doctor` check | `services/ingestion/tiered.py:187-259,272`; `services/doctor.py:97-111` |
| Ingestion status (desktop) | 2 | Worker computes an `indexing` payload in `status` (`worker.py:159-163`) but the proto `WorkerStatus` has only `model`, `embed_model`, `chunks` (`app/docket-proto/src/api.rs:107-115`) so serde drops it; UI shows per-source "Ready / Folder missing / Disconnected" plus a file count only | `app/docket-app/src/ui.rs:156-195`; `app/docket-app/src/model.rs:143-154` |
| Skipped-files report | 1 (CLI) / 4 (desktop) | Reason codes with plain sentences, per-file list capped at 5000 per run, `UNSUPPORTED_OTHER` counted but not listed per file; `docket sources skipped`; no desktop view | `services/ingestion/skips.py:15-49`; `interfaces/cli/main.py:713-739`; `interfaces/cli/quiet.py:207-229` |
| Parsing failure handling | 1 | Every per-file exception marks the version FAILED with a condensed reason; plain-language reasons; retry and dead-letter queue | `services/ingestion/pipeline.py:943-1067,1208,1338`; `services/ingestion/failure.py:15-70`; `tiered.py:49-58` |
| Scan / whitespace-text-layer handling | 1 (Tier 1 guard) / 5 (Tier 2 OCR outcome) | Pages whose stripped text is shorter than the minimum are not counted as text pages; fewer than 50% text pages raises `NoTextLayer` and the file proceeds to Tier 2 (Docling, OCR-capable). This avoids the AnythingLLM whitespace case (C-A1) at Tier 1. What Docling OCR then returns for an image-only file: Not verified | `infra/parsing/fast_text.py:42,78-80`; `pipeline.py:1166-1167`; OCR skip option off by default `core/config.py:49-50` |
| Zero-chunk files | 5 | A READY version with zero chunks is a recognised state in the code comment; whether it is surfaced in the skipped report or coverage: Not established from repository evidence | `pipeline.py:1250-1256` |
| Embedding error handling | 1 | Ollama errors translated to typed `InferenceError`; batch embedding raises if the vector count differs from the text count (the AnythingLLM Lemonade failure mode, C-A11, is guarded) | `infra/inference/gateway.py:99,205-213,220-245,351-357` |
| Citation navigation | 2 | Citation chips with a 400-character excerpt; "passage no longer available" if unresolved; no page, location, document opening or evidence viewer. Worker sends an optional `note` per citation but the Rust `Citation` struct has no `note` field so it is dropped | `worker.py:41,190-196,247-261`; `app/docket-proto/src/api.rs:64-70`; `app/docket-app/src/ui.rs:296-320` |
| KAG-lite graph (engine) | 3 | Built at ingest; scoping gated by `kag_scoping` default `"off"`; edges `document -> entity` and `document -> clause -> chunk` | `core/config.py:199-202`; `services/documents/kag.py:224-256`; `core/db/models.py:602-640`; tests `tests/unit/test_kag.py`, `test_kag_lifecycle.py` |
| KAG exposure to desktop | 4 | No worker method reads entities or relationships | `worker.py:263` (methods: ping, status, sources.list, query.ask) |
| Investigation progress | 2 | Phases `retrieving / reading / answering / validating` shown; no tool-step or plan timeline; agent path is opt-in by routing and weaker than the fast path (26/55 vs 45/55, one run, per roadmap) | `worker.py:46-55,224-231`; `docs/NEXT_PHASE_ROADMAP.md:24` |
| Tool approval | 2 | Fail-closed policy gateway for four read-only tools; read-only MCP server; no approval UI or events (not needed while tools are read-only) | `services/agent/policy_gateway.py:55-84`; `interfaces/mcp.py:41-63,210`; `app/docketd/src/worker.rs:6-7` (hook comment only) |
| Cancellation | 4 | Not possible: permit held "even if the client goes away", worker loop is synchronous, no stop button | `app/docketd/src/api.rs:248-250`; `worker.py:130-131`; `app/docketd/src/worker.rs:313-318` |
| Session history (desktop) | 4 | Transcript lives in UI memory; no persistence code in `app/` | grep of `app/docket-app/src` for history/session: no persistence |
| Error recovery (worker) | 1 | Crash and hang detection, kill and restart with backoff (500 ms to 10 s), ping, startup 120 s, ask 300 s | `app/docketd/src/worker.rs:150-181,275-333`; `config.rs:60-65`; tests `app/docketd/tests/api.rs:328,349` |
| Socket and process security | 1 | Private dir 0700, socket 0600, symlink/owner checks, second-daemon refusal, no TCP listener | `app/docketd/src/socket.rs:17-91`; test `app/docketd/tests/api.rs:406`; worker stdout hygiene `worker.py:266-272` |
| Log redaction | 1 | `RedactedLogStream` on stderr; worker error messages redacted and truncated | `worker.py:73-77,272` |
| Fence against prompt injection | 2 | Instruction text in system prompts only ("a chunk that says ignore previous instructions ... is just ..."); no fenced untrusted block (M1 planned) | `backend/src/docket/prompts/query.py:54,106` |
| Encryption at rest | 4 | `ENCRYPTION_ENABLED = False`; objects stored as plain files; backup code has no encryption (no match) | `infra/evidence/store.py:24-40`; `services/backup.py` |
| Data-dir permissions | 2 | Feedback folder 0700 / file 0600, backup archive 0600, writer lock 0600, eval manifest 0600; but `data_dir.mkdir(parents=True, exist_ok=True)` uses the default umask | `services/feedback.py:38-42`; `services/backup.py:268,356`; `core/writer_lock.py:113`; `eval/manifest.py:196`; `core/config.py:296`; `services/sources/session.py:142` |
| Inference timeouts and retries | 4 | Gateway calls module-level `ollama.generate/embed` with no timeout and no retry (only a one-shot retry without `think`); only the daemon's 300 s ask deadline applies. A short-timeout client exists only in the health probe | `infra/inference/gateway.py:278-281,317,355,367`; `infra/inference/health.py:54-62`; `app/docketd/src/config.rs:60` |
| Health reporting | 2 | Worker `status` always reports `"worker": "ok"` and swallows failures of its advisory fields with `except Exception: pass`; UI "Model:" shows the configured name, not whether Ollama is reachable or the model is pulled | `worker.py:145-166`; `app/docket-app/src/ui.rs:209-244`. `docket doctor` does check Ollama (`services/doctor.py:115-`) but is not surfaced in the desktop |
| CPU/RAM/GPU monitoring | 4 | None in app or engine (no resource-sampling library or code found); a GPU recorder exists only for evaluation | grep of `backend/src` and `app/*/src` for psutil, nvidia, sysinfo: no match; roadmap note on commit `de94b8d` |
| Packaging and updates | 4 | `app/dev.sh` only for the desktop; the Python CLI has a console script entry `docket` | `app/dev.sh`; `backend/pyproject.toml:37-38` |
| Voice | 4 | No audio/speech dependency; planned R8, optional | grep; `docs/RUST_PLATFORM_ARCHITECTURE.md` sections 1.2 and 7 |
| Telemetry | 1 (absent) | No telemetry/analytics code found in `backend/src` or `app/*/src` | negative grep; this avoids the C-A10 and C-H8 class of complaint, but "absent" was established by grep only |
| Lifecycle, versioning, SQLite authority, FTS5 + LanceDB, single writer | 1 | Evidence versions with READY/FAILED/SUPERSEDED/REVOKED/TOMBSTONED; writer flock; lifecycle tests | `core/writer_lock.py`; `tests/unit/test_kag_lifecycle.py` |

Test inventory (VERIFIED by count only): 17 Rust test attributes in src files plus `app/docketd/tests/api.rs` (511 lines, includes `only_one_question_in_flight`, `socket_and_directory_permissions`); about 104 files under `backend/tests/unit`. Pass/fail status: Not established in this pass (not run by instruction).

## 2. Gaps (per-deficiency detail)

Fields: location and line; existing behavior; competitor evidence; architectural cause; Docket-compatible fix; dependencies; regression test (arrange / assert); complexity; priority. Priority: P0 = blocks a trustworthy desktop beta, P1 = needed soon after or for a credible beta, P2 = later. Complexity: S about a day or two, M about a week, L multi-week.

### G01 No way to cancel a question (P0, M)

- Location: `app/docketd/src/api.rs:248-250`, `app/docketd/src/worker.rs:313-318`, `backend/src/docket/interfaces/worker.py:130-131`; no stop button in `app/docket-app/src/ui.rs`.
- Behavior: a started question runs to completion or the 300 s deadline; the permit is held after client disconnect; the next question gets HTTP 429 `busy`; a deadline expiry kills and restarts the worker with backoff.
- Competitor evidence: related hangs that force restarts: C-A6, C-A5, C-O2, C-R4, C-R7. No competitor complaint specifically about cancel was found; Docket-specific, and a bad hang is the worst case.
- Cause: single-owner synchronous worker loop and an exchange with no cancel token; Ollama behaviour on client disconnect is Not established from repository evidence.
- Fix (existing R1, split): cancel token in the supervisor, hard-kill via existing `stop_child`, restart without failure penalty, Stop button, disconnect release, `POST /v1/ask/cancel`; cooperative worker `cancel` stays in R2.
- Dependencies: none (R1 needs no M-prerequisite); the Python half belongs to M7.
- Test: arrange the fake worker (`app/docketd/tests/fixtures/fake_worker.py`) with a never-ending question; assert cancel returns within 3 s, worker pid gone, next question succeeds, `restarts` and backoff unchanged, SSE client disconnect releases the permit, deadline gives `TimedOut` not `Failed`.
- Why M: touches proto, daemon, supervisor and UI, but no Python and no schema.

### G02 No inference timeout or bounded retry in the Python gateway (P0, S-M)

- Location: `infra/inference/gateway.py:278-281,300,317,355,367`.
- Behavior: Ollama calls have no client timeout; a stalled Ollama blocks the worker for up to the daemon's 300 s, then the worker is killed (which does not stop Ollama itself).
- Competitor evidence: C-A6 (idle then "could not be reached", recurred after fix), C-A15, C-R7, C-R9, C-H6 (#1362, LISTING).
- Cause: module-level `ollama` default client; only the daemon deadline exists.
- Fix (M7 unchanged): a configured `ollama.Client(timeout=...)` with separate connect/read budgets, bounded retry for connection errors only (not for generation), typed `InferenceError` returned within N seconds.
- Dependencies: none.
- Test: fake HTTP server that accepts and never replies; arrange gateway timeout 2 s; assert `InferenceUnavailableError` within about 3 s; a server that fails once then succeeds returns after one retry; a mid-stream stall raises rather than hangs.
- Why S-M: one module plus tests; risk is choosing timeouts for slow CPU models (UNMEASURED; make the read timeout per-token-gap, not total).

### G03 Desktop shows no indexing progress (P0, S-M)

- Location: worker produces `indexing` (`worker.py:159-163`); proto drops it (`app/docket-proto/src/api.rs:107-115`); UI shows a file count only (`ui.rs:184-188`).
- Behavior: while the CLI is indexing, the desktop says "Ready" for an active source and gives no sign that only a keyword tier exists, how many files are pending, or an ETA.
- Competitor evidence: C-R2 (no visible progress, restart from scratch), C-L7 (stuck "processing"), C-M4, C-R9 (parse looks hung), C-A3 (no feedback).
- Cause: the daemon's `WorkerStatus`/`WorkerInfo` mirror only three fields; UI polls `/v1/status`.
- Fix: extend `WorkerStatus` with optional `indexing` (counts per state, tier coverage, ETA, summary string) using `#[serde(default)]` (additive, protocol-compatible); show a library-level progress panel and per-source "keyword-only / fully indexed" state; refresh while jobs are pending.
- Dependencies: none for the read path. Live updates while a separate CLI process writes depend on the worker seeing a fresh DB state (`worker.py:217-218` already re-reads the table per question).
- Test: golden test on `docket-proto` that a status JSON with and without `indexing` both decode; a fake-worker daemon test that `GET /v1/status` returns the payload; Python test that `status` returns `indexing` with queued/dead counts from a scratch queue; UI model unit test that a source with pending files is not labelled "Ready".
- Why S-M: additive fields and one UI panel; no new backend capability.

### G04 Desktop has no skipped or failed files view (P0, M)

- Location: report exists only via CLI `interfaces/cli/main.py:713-739`; nothing in worker methods (`worker.py:263`).
- Behavior: a user who sees a missing file in answers cannot learn from the desktop whether it was skipped, failed, password-protected or unsupported.
- Competitor evidence: C-A1 and C-M2 (files silently dropped), C-K4 and C-H2 (PDFs produce nothing), C-L1 (processed but empty). This is the most repeated frustration (pattern P1).
- Cause: worker is read-only and exposes four methods; the engine already stores reports and failure reasons.
- Fix: read-only worker method `source.skipped` (and `source.failures`) returning counts by reason and a capped list of paths with plain reasons; desktop "Documents" view; keep the documented cap and show "N more not listed" rather than hiding it (note: `UNSUPPORTED_OTHER` is counted but never listed, `skips.py:16`).
- Dependencies: R2 pattern (read methods), but it can be delivered as a standalone read method before full R2.
- Test: scratch library with one valid file, one password-protected PDF, one unsupported extension, one corrupt PDF; assert the method returns four rows with distinct reason codes and that totals equal files discovered.
- Why M: new method, proto types, UI list, golden tests; must preserve read-only.

### G05 Follow-up questions are independent on the desktop (P0, S-M)

- Location: `app/docket-proto/src/api.rs:53-61`, `worker.py:198-238`, `ui.rs` (no history sent).
- Behavior: "and what about the termination clause?" has no antecedent; the engine could rewrite it using history but is never given any.
- Competitor evidence: no competitor complaint about continuity; C-A4 and C-O7 are different (attachments, tool results in multi-turn). Docket-specific, but a chat product without continuity feels broken. INFERENCE.
- Cause: protocol v1 carries a single question.
- Fix: optional `history: [{role, text}]` (last N turns, answer text only, never citation internals) in `AskRequest` with `skip_serializing_if` so old clients are byte-compatible; worker maps to `ConversationTurn` and passes `history=`; engine already treats history as reference resolution only, citations validated against this turn's evidence (`service.py:366-373`).
- Dependencies: none; later M4 audit record.
- Test: scratch library with two facts; ask "Who is the supplier in the Acme contract?" then "What law governs it?" and assert the second question retrieves from the same document (with a fake gateway that records the standalone rewrite). Assert a request without `history` is unchanged, and that history text never appears as a citation.
- Why S-M: additive field and plumbing; behaviour quality UNMEASURED.

### G06 Citation inspection is shallow (P1, M)

- Location: `worker.py:41,190-196`; `app/docket-proto/src/api.rs:64-70`; `ui.rs:296-320`.
- Behavior: a 400-character excerpt; no page or section label although the resolver carries a `location` (`infra/retrieval/resolver.py`, referenced from `services/query/citations.py:34`); the worker `note` field is dropped by the Rust client; no way to see surrounding text or open the original.
- Competitor evidence: C-A13 (four feature requests: preview pane with pages, open file, clickable citations), C-L8 (two requests), C-R11 (cannot trace entities to chunks). Requests, not bugs.
- Cause: citations were built for the CLI; read methods beyond `query.ask` do not exist.
- Fix: add `note`/`location` to `Citation` (M7 already lists the note field), then `evidence.get(chunk_id, context)` returning the full chunk, neighbours, page/section and original path (R2), and an evidence viewer (R4). Original file opening must resolve only inside registered sources.
- Dependencies: R2; M1 redaction rules if text is logged.
- Test: citation for a PDF chunk returns page number and section; `evidence.get` for a chunk id outside the library or a revoked source returns an error, not text; viewer for a deleted source shows "no longer available".
- Why M: proto, worker, UI; fail-closed behaviour is the risk.

### G07 KAG entity edges do not point at the evidence for the entity (P0 for the KAG claim, M)

- Location: `services/documents/kag.py:226,250` (`first_chunk = chunks[0].id`; every `KagDocumentEntity` edge gets it); `core/db/models.py:625-640` docstring says `chunk_id` is "the chunk the name was read from"; `KagDocumentEntity` has no `char_span` or `extractor_version` while `DocumentField` has both (`models.py:595-598`).
- Behavior: the edge cites the document's first chunk. M0 silver labels measured the entity name appearing in that chunk only about 37% of the time (UNMEASURED beyond M0 evidence; not re-run here).
- Competitor evidence: C-R11 (users cannot trace entities to chunks; question only), C-K1 (everything becomes an entity). No sourced accuracy complaint exists for competitors; Docket-specific and measured by M0.
- Cause: preamble-derived entities were assumed to live in the first chunk; no span is recorded when the name is read from the preamble or filename.
- Fix (M3 expanded): per-edge provenance: `evidence_version_id`, chunk id where the name actually occurs, char span, source of the name (preamble text, parties field, filename), extractor version; if the name is not found in any chunk, mark the edge `unlocated` and never cite it.
- Dependencies: M0 KAG gold; migration 0019 (per M3).
- Test: scratch contract where the party is in chunk 3 (preamble is in the second block); assert the edge's chunk contains the normalised name and the span slices to it; filename-only entity has source `filename` and is not presented as a quoted fact; property test: for every edge with a chunk, `name in chunk_text`.
- Why M: schema migration plus extractor change; risk of changing KAG results, so gate on the M0 gold.

### G08 Hard delete leaves orphan `kag_entities` (P1, S-M)

- Location: `tests/unit/test_kag_lifecycle.py:183-190` (strict xfail `test_purge_leaves_no_orphan_entities`); schema `models.py:602-640` (cascade only on document edges and variants).
- Behavior: after a purge the party name stays queryable; an inactive plan echoes it ("named entity has no document in scope"). Evidence ids are not returned (the neighbouring test at `:193` passes).
- Competitor evidence: C-L4 (#985, #3400 orphan graph data after delete/purge, maintainer-filed), C-S4 (retention never evicts). Docket-specific but same class.
- Cause: entities are shared across documents and only edges cascade.
- Fix (M3): delete entities with no remaining edges inside the purge transaction (and on supersede/modify), add `docket doctor --graph` check.
- Dependencies: M3.
- Test: the existing strict xfail becomes the regression test: remove the marker when fixed; plus a shared-entity case (two documents, delete one, entity must remain).
- Why S-M: a delete step plus consistency checker; shared-entity case needs care.

### G09 Entity duplication and role confusion (P1, M)

- Location: `services/documents/kag.py:231` (`preamble_entities(parties, preamble, file_name)`), `kag.py:288`; M0 findings: filename-derived entity vs party entity duplicate; party stored only as a role mention in 5 of 12 contracts.
- Behavior and cause: VERIFIED only that filename feeds the entity list and entities are keyed on `core_name`; the specific duplication counts come from M0 evaluation notes and were not re-derived here. Resolution quality beyond that: UNMEASURED.
- Competitor evidence: C-L3 (exact-name merge, duplicates), C-K1, C-L2 (corrupted names).
- Fix (M3/M5 extraction improvements, gated by the M0 KAG gold): resolve a filename-derived name against party entities before creating a node, record `kind`/source, report false-merge rate.
- Test: synthetic contracts where the filename is "Acme_MSA.pdf" and the party is "Acme Corporation Ltd."; assert one entity; and "Acme" vs "Acme Holdings" stay separate (false-merge guard).
- Complexity M, priority P1.

### G10 KAG relationships are not visible to the user (P1, M)

- Location: no worker method (`worker.py:263`); flag default off (`core/config.py:202`).
- Competitor evidence: C-R11 (traceability), C-A13; competitors that expose graphs (LightRAG WebUI) have the accuracy complaints C-L3.
- Cause: product choice so far was evaluation first.
- Fix: R2 `kag.entities`/`kag.edges` read methods after G07 is fixed (so every displayed edge links to a located span), "Why this document" panel in the evidence viewer; show unlocated edges as such.
- Dependencies: G07, M3, R2, R4. Do not ship before provenance is correct.
- Test: for each displayed edge the viewer opens a chunk containing the entity text; revoked or superseded versions show no edges.
- Complexity M, priority P1 (P0 only if the beta claims KAG explorer).

### G11 Health does not equal capability (P0, S-M)

- Location: `worker.py:145-166` (constant `"worker": "ok"`, `except Exception: pass` on two advisory blocks), `app/docket-app/src/ui.rs:209-244`.
- Behavior: the green "Worker up" and "Model: qwen3:14b" appear even if Ollama is stopped or the model is not pulled; the first question then fails with a `worker_error` message; indexing advisory failures are invisible.
- Competitor evidence: C-S5 (+#7858) health OK while capture is dead, C-J1 (memory service unreachable), C-J5 (agent shown running while dead), C-L1 (processed but 0 entities), C-H6 (#1362, LISTING).
- Cause: status reports process liveness, not capability; the probe code exists (`infra/inference/health.py:54-62`, `services/doctor.py`).
- Fix: `status.capabilities`: `inference_reachable`, `gen_model_present`, `embed_model_present`, `index_present`, each with a reason and age; UI shows red/amber states with a one-line remedy (reuse `docket doctor` strings). Never swallow silently: report `advisory_errors` count.
- Dependencies: G02 for timeouts; none otherwise.
- Test: fake Ollama endpoint that refuses connections; assert `/v1/status` reports `inference_reachable=false` within 2 s and the UI model marks "Not ready"; model absent from tags list gives `gen_model_present=false`; a raising `tier_status` yields `advisory_errors>=1`.
- Complexity S-M, priority P0.

### G12 Abstention reasons and validation warnings are not surfaced (P1, S)

- Location: `worker.py:247-261` returns `abstained` only; `QueryResult.abstention_reason` (`service.py:156`) and `validation_warnings` (`service.py:128`) are not forwarded; UI shows one fixed sentence (`ui.rs:248-262`).
- Behavior: "nothing indexed in this scope", "no evidence found" and "citations failed validation" look identical to the user.
- Competitor evidence: C-O4 and C-R3 (model says nothing found while the data exists), C-O6 ("No sources found"). Users cannot tell retrieval failure from absence. Docket types the reason but hides it.
- Fix: add optional `abstention_reason` and `warnings` to the result (additive), UI shows a plain explanation and a next step.
- Test: scoped empty source returns reason `NO_SOURCES`-equivalent; a stubbed generation with an invalid citation returns the citation-failure reason; old client ignores the new fields.
- Complexity S, priority P1.

### G13 No source management from the desktop (P1, L)

- Location: `worker.py:21-23`, `app/docketd/src/api.rs:114-118`.
- Behavior: adding, removing, pausing or reindexing needs the CLI.
- Competitor evidence: C-A3 (silent no-op in move-to-workspace), C-L7 (hung upload UI), C-R4 (parse several files freezes). Competitors have UI ingestion and it is a major source of their silent failures; deferring it is a tradeoff, not an oversight.
- Cause: single-writer rule (`core/writer_lock.py`) and read-only worker.
- Fix (R7): a separate supervised writer worker, status events, pause/cancel, bounded parsing; depends on M3 so lifecycle events keep the graph consistent.
- Test: two concurrent add attempts: one waits or fails cleanly; removal purges chunks, vectors, graph rows (G08).
- Complexity L, priority P1 for the beta-plus-one, not for the first beta (see backlog).

### G14 No resource monitor (P1, M)

- Location: none; `docs/RUST_PLATFORM_ARCHITECTURE.md` section 6 (measurement plan).
- Competitor evidence: C-S1 to C-S4, C-S6 (leaks, CPU, WAL growth), C-O1 (OOM), C-A7, C-M5, C-H4 (GPU OOM), C-R7 (GPU idle, CPU busy). Strong evidence that unpredictable resource use is a top frustration.
- Fix (R3): sample process-tree RSS/CPU, Ollama loaded model and VRAM (`ollama ps` equivalent), disk/WAL size; show tiles and write metadata-only samples; a soft warning threshold. No capacity claims until measured (M8).
- Test: fake process tree sampler returns known values; assert tile values and that samples contain no question text; sample matches `ollama ps` on a real machine (manual).
- Complexity M, priority P1.

### G15 Data-at-rest and injection controls before confidential documents (P0 for confidential use, L)

- Location: `infra/evidence/store.py:24-40`; `core/config.py:296`; `prompts/query.py:54,106`; `services/backup.py`.
- Behavior: plaintext stores; default umask for the data directory; prompt-level injection instruction only; plaintext backups.
- Competitor evidence: C-O10 and C-R10 (server-style vulnerabilities), C-J7 (skills with shell access), C-A10 and C-H8 (telemetry trust). Docket has no telemetry code (negative grep) and a private socket, which are advantages. Encryption at rest is not a competitor strength either; this is a product-trust requirement, Docket-specific.
- Fix (M1): require encrypted volume and `docket doctor` check, create the data dir 0700, encrypted backups, fenced untrusted-context block with hostile-gold tests, redaction rules; per-object encryption design only.
- Test: `doctor` fails on an unencrypted store when the policy is set; data dir created with mode 0700 under umask 022; encrypted backup round trip; hostile-chunk gold shows no regression.
- Complexity L (mostly design plus checks; per-object encryption deferred), priority P0 before any real confidential library.

### G16 Packaging and update path (P1, L)

- Location: `app/dev.sh`.
- Behavior: the desktop runs only from a checkout with a venv and cargo.
- Competitor evidence: C-J2, C-J4, C-K6, C-H10 (L), C-A8, C-S8 (install friction, unsigned binaries, Defender); C-M3 and C-S7 (stale processes block update). Strong, repeated.
- Fix (R10): installable bundle that starts the daemon and worker and removes the socket/worker on stop; start the beta on Linux; signing decisions deferred (Not established from repository evidence).
- Test: clean-VM install without a checkout; uninstall leaves no orphan process (pid check); upgrade with a running daemon refuses or stops it.
- Complexity L, priority P1 (mandatory for a public beta, not for an internal one).

### G17 Calls queue behind a running question (P1, S) INFERENCE

- Location: `app/docketd/src/worker.rs:82-104` (single owner channel), `api.rs:145-165`.
- Inference: `GET /v1/sources` (Refresh) issued during a 300 s question waits in the worker queue behind it, then runs with its own deadline. Not executed.
- Competitor evidence: C-L7, C-R4 (UI freezes while backend works), C-O2.
- Fix: answer read-only status/sources calls from a cached copy while a question runs, or via a second read-only worker (later); R1 task registry.
- Test: fake worker with a 5 s question; request `/v1/sources` at t+1 s; assert response within 1 s using cached data flagged `stale: true`.
- Complexity S, priority P1.

### G18 Desktop session history absent (P2, M)

- Location: no persistence in `app/`.
- Behavior: closing the app loses the conversation; the engine records questions under `@record_question` (feedback/RunRecord, details Not established here).
- Competitor evidence: C-A7 and C-M5 (long-history performance) show persistent history creates its own problems; no competitor complaint about missing history. Docket-specific.
- Fix: M4 audit record (metadata-only by default, cascade on source delete) then R3 activity log; transcript restore is optional and must respect retention.
- Test: restart restores the list of past questions with citations resolving or "no longer available"; deleting a source removes dependent records.
- Complexity M, priority P2 (first beta can state "history is not saved").

### G19 Zero-chunk or scan-only outcome visibility (P1, S) UNKNOWN

- Location: `pipeline.py:1250-1256` shows a READY version can have zero chunks; Tier 2 OCR outcome for an image-only PDF is Not verified.
- Competitor evidence: C-A1, C-K4 (PDF yields nothing), C-M2 (small files ignored), C-L1.
- Fix: treat "parsed but zero text" as a reported reason (`no_text_found`) in the skipped report, never READY-looking.
- Test: image-only PDF and a 10-byte text file in a scratch library; assert each appears in the skipped/failed report with a reason, never silently counted as indexed.
- Complexity S, priority P1.

### G20 Single-source scope only (P2, S)

- Location: `ui.rs:156-205`; protocol supports folder (`worker.py:211-212`).
- Fix: multi-select and folder scope in UI after G04 and G03; empty-scope reason from G12.
- Test: select two sources, ask, assert evidence only from those; the empty scope returns the typed reason.
- Complexity S, priority P2.

## 3. Comparison with recurring competitor complaints

| Pattern (from `COMPETITOR_PAIN_POINTS.md`) | Docket today | Evidence | Verdict |
| --- | --- | --- | --- |
| P1 silent success | Avoids parts: per-file FAILED with reason, skipped report with plain reasons, embedding count check, whitespace text layer sent to OCR path, fail-closed citation validation, typed abstention. Shares: desktop hides indexing state, skipped files, abstention reason, and reports health as ok regardless of Ollama | `pipeline.py:943-1067`, `skips.py`, `gateway.py:205-213`, `fast_text.py:78-80` vs G03, G04, G11, G12, G19 | Partly avoids, partly shares (the desktop surface) |
| P2 inference stalls/timeouts | Shares: no gateway timeout or retry, no cancel | G01, G02 | Shares |
| P3 KG build cost and no progress | Avoids the LLM cost: KAG-lite is deterministic, with no LLM graph extraction (roadmap rule); a 200-contract build is measured by M0/M8, other sizes UNMEASURED. Shares: no progress visibility in the desktop | `docs/NEXT_PHASE_ROADMAP.md:5`, G03 | Avoids cost, shares visibility gap |
| P4 PDF/OCR weakness | Uses Docling with a fast text tier; scan fallback exists; accuracy on real scans and what Docling returns for image-only files is Not verified | `fast_text.py`, `docling_wrapper.py` | Unknown; M9 and G19 |
| P5 small/reasoning models break extraction | Avoids: no LLM extraction; reasoning blocks stripped from generations (`strip_think_block`, `gateway.py:300`). The 14b answering model is locked per memory | | Avoids (for extraction), UNMEASURED for generation on smaller models |
| P6 deletion and orphan graph | Shares in a narrow form: entity rows survive purge (strict xfail), but evidence ids are not returned after purge (`test_inactive_plan_for_deleted_source_returns_no_evidence_ids`) | G08 | Shares partly |
| P7 scale degradation | SQLite + FTS5 + LanceDB with ANN eligibility and post-ingest compaction are on main; capacity claims unmeasured; no per-query "load all docs" BM25 pattern was observed but this was not audited in this pass | M8 | UNMEASURED |
| P8 resource blowups | No monitor; no leak data; the app is not a background recorder, so Screenpipe-class risks are lower; unmeasured | G14 | UNMEASURED |
| P9 upgrade regressions | Migrations directory exists; upgrade tests for the desktop: Not established | `core/db/migrations` | Unknown |
| P10 orphan processes, install | Daemon stops its worker (EOF then kill) and removes the socket; test `clean_shutdown_removes_socket_and_kills_worker`; second daemon refused. Install is checkout-only, so install friction is avoided only by not being distributed yet | `worker.rs:323-333`; `api.rs` tests | Avoids orphan workers in tests; install not started |
| P11 citation inspectability | Has validated citation chips and excerpts, which is more than most competitors' bare links; still shallow | G06 | Better than baseline, incomplete |
| P12 entity quality | Facing the same problem; measured honestly by M0 | G07, G09 | Shares |
| P13 security | Private unix socket, no TCP, read-only MCP, no telemetry code; no auth surface to attack remotely. Plaintext at rest and prompt-only injection defence | G15 | Avoids the server class, shares the data-at-rest gap |
| Telemetry trust (C-A10, C-H8) | No telemetry code found | negative grep | Avoids (by grep) |

## 4. Architectural causes (summary)

1. The Rust daemon is a thin synchronous bridge to a single read-only worker, so anything that is not `ask` (progress, cancel, viewing, skipped files) needs a new worker method.
2. The Python engine is richer than what crosses the boundary (tier status, skip reports, abstention reasons, citation notes, history), so most P0 gaps are plumbing, not new engine work.
3. Status reports liveness, not capability.
4. KAG edges were written for scoping, where "the document" was the unit, not for evidence display where the span is the unit.
5. Shared graph nodes make deletion a reference-counting problem.

## 5. Fact, inference and assumption register

| Statement | Class |
| --- | --- |
| Daemon cannot cancel; worker loop is synchronous; no gateway timeout; proto drops `indexing`; `AskRequest` has no history; entity edges use the first chunk; strict xfail exists for orphan entities; `ENCRYPTION_ENABLED = False`; data dir uses default mode; no resource monitor; dev.sh only | VERIFIED (read in code) |
| Competitor items marked OPENED/SPOT-CHECKED in `COMPETITOR_PAIN_POINTS.md` | VERIFIED (page read); fix/release status mostly not verified |
| `/v1/sources` queues behind a long question; killing the worker does not stop Ollama generation | INFERENCE (not executed) |
| Missing continuity would make desktop chat feel broken | INFERENCE |
| Entity name found in the first chunk only about 37% of the time; party stored as role mention in 5 of 12 contracts; filename/party duplication | M0 evaluation findings, taken from the task brief; not re-derived in this pass |
| Any latency, memory, capacity, accuracy gain from the proposed fixes; sensible timeouts for CPU-only users; whether egui meets accessibility needs | UNMEASURED / Not established from repository evidence |
| Ollama behaviour on client disconnect mid-generation | Not established from repository evidence (listed in R1 appendix) |
| Test suite pass state at HEAD | Not established in this pass (tests not run) |
