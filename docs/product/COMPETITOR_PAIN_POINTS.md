# Competitor Pain Points (source-linked)

Status: research synthesis, 2026-11-17. Planning only; no code was changed.
Companion documents: `docs/PRODUCT_GAP_ANALYSIS.md`, `docs/DOCKET_PRODUCT_BACKLOG.md`.

Sources: two web-research passes (products: AnythingLLM, Open WebUI, Msty/Msty Studio, RAGFlow; LightRAG, OpenSPG KAG, Khoj, OpenJarvis, Screenpipe) plus a 12-URL spot-check done for this document (section 6). Complaint ids are prefixed `C-` to avoid clashing with Docket milestone names (M0-M9, R1-R11).

## 1. How to read this

Fields per complaint, in table order: id and product, source URL, report date (opened date), version, status, behavior, repro, severity or impact (JUDGEMENT of the research agent, not a measurement), independent reports, still relevant to the current version.

Verification labels:

- OPENED: the issue or page itself was fetched and read by a research agent.
- LISTING: only a title/state row from a GitHub search listing was seen. UNVERIFIED. Kept labelled, never used as the only support for a pattern.
- SPOT-CHECKED: re-fetched in this pass and the core claim confirmed (section 6).
- FEATURE REQUEST: a request, not a bug.
- VENDOR: vendor changelog, docs or advisory (primary, but not a user complaint).

Table-shape note: sections 2.3 and 2.6 to 2.9 drop the separate "Repro" and (where marked) "Version" columns to stay readable; repro steps are omitted there (some are mentioned inside the Behavior cell) and the full ten-field research text remains in the two source files `competitors-A.txt` and `competitors-B.txt` in the session scratchpad (temporary; not durable).

Rules applied: "closed" is not "fixed" (bot closures and "not planned" are inactivity or triage, not fixes); same-thread "me too" comments are weaker than separate issues by different authors; no version of any product was installed or run, so "still relevant" is almost always "not verified"; severity is judgement. Latest releases seen: AnythingLLM v1.19.0 (2026-10-26), Open WebUI v0.13.4 (2026-11-17), RAGFlow v1.0.0 (2026-11-06), Msty Studio 3.0.1 (2026-11-17), LightRAG v1.5.8rc1, KAG v0.8.0 (2025-06), Khoj 2.0.0-beta.28, OpenJarvis Desktop 1.0.5, Screenpipe App 2.7.145.

## 2. Complaints per product

### 2.1 AnythingLLM (github.com/Mintplex-Labs/anything-llm)

| Id | URL | Opened | Version | Status | Behavior | Repro | Severity (judgement) | Independent reports | Still relevant | Label |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| C-A1 | https://github.com/Mintplex-Labs/anything-llm/issues/7045 | 2026-11-06 | all (collector, master) per reporter | Closed 2026-11-09, PR #7046 | Scanned PDF whose text layer is whitespace only is never sent to OCR; upload fails "No text content found" | One-page scan plus whitespace text string | Medium: silent-ish ingestion failure on a core case | 1 (related #5623 not fetched) | Marked fixed; release not verified | OPENED, SPOT-CHECKED |
| C-A2 | https://github.com/Mintplex-Labs/anything-llm/issues/4582 | 2025-10-27 | n/s (Docker) | Closed 2025-11-19, PR #4630 | Many public EPUBs crash ingestion (epub2 dependency) | Upload affected EPUB | Low-medium | 1 | Fixed in dependency patch; not verified on 1.19.0 | OPENED |
| C-A3 | https://github.com/Mintplex-Labs/anything-llm/issues/6998 | 2026-11-04 | Desktop 1.19.0 | Closed same day before diagnosis; maintainer reproduced 2026-11-09; fix PR #6829 post-1.19.0 | "Move to workspace" silently does nothing for a document selected without expanding its folder; no error | Select search-result doc, move | Medium: silent no-op in core flow | 1 | Fix unreleased as of 2026-11-09 | OPENED |
| C-A4 | https://github.com/Mintplex-Labs/anything-llm/issues/4666 | 2025-11-20 | n/s | Closed 2026-06-01 (reopened once) | File attached mid-thread "not recognized"; context injected in system prompt ignored by some models; others report only first attachment read | Chat, then attach file | Medium-high (ungrounded answers) | Other users in thread | Not verified | OPENED |
| C-A5 | https://github.com/Mintplex-Labs/anything-llm/issues/4593 | 2025-10-30 | Desktop 1.9.0-r2 | Closed 2025-11-21, labels still "investigating"; no root cause | "Accuracy Optimized" (rerank) search: second prompt network error, app hangs; suspected reranker eviction | Embed, set mode, two prompts | Medium-high | 2 other commenters in thread | Unresolved on page | OPENED |
| C-A6 | https://github.com/Mintplex-Labs/anything-llm/issues/3103 | 2025-02-04 | Desktop 1.7.3 (others 1.7.2, 1.7.8, 1.11.2) | Closed 2025-03-20, maintainer: fixed 1.7.7 | After ~10 min idle next message hangs, then "engine instance could not be reached"; restart helps | Idle 10 min, send | High (forces restarts) | Many commenters across OS/models; recurrences 2025-03-28 (1.7.8) and 2026-03-25 (1.11.2, qwen3:8b) | Recurred after claimed fix | OPENED, SPOT-CHECKED |
| C-A7 | https://github.com/Mintplex-Labs/anything-llm/issues/5817 | 2026-06-13 | 1.13.0/1.14.0 | Closed 2026-06-15, PR #5833 | Opening a ~500-message chat drives CPU to ~96% (frontend) | Open long conversation | Medium | 1 (maintainer reproduced) | Likely included in 1.19.0; not verified | OPENED |
| C-A8 | https://github.com/Mintplex-Labs/anything-llm/issues/4017 | 2025-06-18 | 1.8.2+ | Closed 2025-12-05, PR #4709 | MCP servers fail unless desktop app launched from a terminal (macOS PATH/AppTranslocation) | Launch from Finder | Medium | Several in thread | Not verified | OPENED |
| C-A9 | https://github.com/Mintplex-Labs/anything-llm/issues/6273 | 2026-09-02 | claimed since 1.16.1 | Closed 2026-09-03, no fix; reporter later blamed own custom skill | @agent 400 invalid_type | Unclear | Low-medium, probably config | 1 (#4386, #5603 LISTING) | Not verified | OPENED |
| C-A10 | https://github.com/Mintplex-Labs/anything-llm/issues/5149 | 2026-03-05 | n/s | Closed "not planned", locked | User claims a local model still generates network traffic; maintainer: only downloads and anonymous telemetry (toggle in Settings) | Chat offline | Low technical; trust perception | 1 (second commenter contradicted) | Not verified | OPENED |
| C-A11 | https://github.com/Mintplex-Labs/anything-llm/issues/5305 | 2026-03-30 | n/s | Closed (details not read) | Lemonade embedder returns empty instead of throwing; failed docs look embedded | Embedding failure | Medium-high if accurate | 1 | Not verified | LISTING |
| C-A12 | https://github.com/Mintplex-Labs/anything-llm/issues/6843 | 2026-10-26 | n/s | Closed (details not read) | Orphan cleanup deletes live attachments when DB lookup fails ("orphan" means files) | DB lookup failure | Medium (data loss) | 1 | Not verified | LISTING |
| C-A13 | https://github.com/Mintplex-Labs/anything-llm/issues/3738 ; /3590 ; /5393 ; /2950 | 2025-01 to 2026-04 | n/s | Open feature requests | Requests for citation preview pane with pages, open-file from citation, clickable "+X citations" | n/a | Low-medium (transparency) | 4 separate authors (requests, not bugs) | Open per listing | LISTING, FEATURE REQUEST |
| C-A14 | https://github.com/Mintplex-Labs/anything-llm/issues/866 | 2024-03-06 | n/s | Closed, screenshot only | "Document Processor Unavailable" on desktop launch | Unknown | Medium historically (old) | many comments, unread | Old | LISTING |
| C-A15 | https://github.com/Mintplex-Labs/anything-llm/issues/4443 | 2025-09-28 | n/s | Closed (chore) | Connection failure after ~5 min of slow CPU Ollama; asks for configurable timeout | Slow CPU Ollama | Medium for CPU-only | 1 (+ C-A6 recurrences) | Not verified | LISTING |

### 2.2 Open WebUI (github.com/open-webui/open-webui)

| Id | URL | Opened | Version | Status | Behavior | Repro | Severity (judgement) | Independent reports | Still relevant | Label |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| C-O1 | https://github.com/open-webui/open-webui/issues/27745 | 2026-07-30 | v0.11.0 (Docker) | Closed 2026-08-23 after PR #27663 (indexes); briefly closed 07-30 as duplicate of #27622 | 1.3 GB SQLite (~15k chats): high CPU, RAM >7 GB, OOM exit 137; RAM stayed high after fix | Large DB, 3 chats at once | High at scale | Another reporter (#28626, not fetched) | Fix is an index; v0.13.4 not verified | OPENED, SPOT-CHECKED |
| C-O2 | https://github.com/open-webui/open-webui/issues/19421 | 2025-11-24 | 0.6.38 (0.6.37 ok) | Closed; no fix shown | Saving embeddings freezes the app 2-30 minutes, blocks other users; IndexError tied to HTTP 429 | Upload file | High multi-user | Others in thread; #19423 duplicate | Unclear | OPENED |
| C-O3 | https://github.com/open-webui/open-webui/issues/17998 | 2025-10-02 | v0.6.30 | Closed 2025-12-20 (pagination); hybrid search still slow | 70K docs: Knowledge list 15-30 s; hybrid search loads all docs to build BM25 per query (38 s on 25K) | ~70K docs | High at scale | 1 thread | BM25-per-query limitation described as open | OPENED |
| C-O4 | https://github.com/open-webui/open-webui/issues/21164 | 2026-02-04 | v0.7.2; later 0.8.7, 0.8.10, 0.9.5 | Closed same day; later comments say still occurring | Native tool mode: query_knowledge_files returns empty or result never reaches model; model hallucinates or says no info; full-context files not visible to knowledge search | Native tools + knowledge, ask answerable question | High (silent ungrounded answers) | Several users across versions (same thread) | Reports to v0.9.5 | OPENED, SPOT-CHECKED |
| C-O5 | https://github.com/open-webui/open-webui/issues/14807 | 2025-06-09 | 0.6.13 to 0.6.28 | Closed 2025-12-30; some say unresolved | Docling extraction "Task result not found" for >1MB, OCR or picture description | Upload >1MB with OCR | Medium-high | Several in thread; duplicate #14821 | Mixed | OPENED |
| C-O6 | https://github.com/open-webui/open-webui/issues/25038 ; /25585 ; /25710 | 2026-05-24 to 06-04 | v0.9.5, 0.9.6; repro v0.11.3 | All closed; #25038 closed before fix commits | Web search returns URLs but UI says "No sources found"; model says no web access | SearXNG JSON search | Medium-high | 3 separate issues, different authors | Repro to v0.11.3 | #25038 OPENED; others LISTING |
| C-O7 | https://github.com/open-webui/open-webui/issues/20600 | 2026-01-12 | v0.7.2 | Closed 2026-02-15, PR #20755 | Tool results HTML-escaped when re-sent to the model in later turns | Tool call then follow-up | Medium | Several confirmed | Fixed; not verified later | OPENED |
| C-O8 | https://github.com/open-webui/open-webui/issues/20520 | 2026-01-09 | 0.7.0 (fixed 0.7.1) | Closed same day, PR #20522 | Admin pages time out after upgrade on small SQLite hosts | Upgrade, open admin users | Medium | Several (#23939 LISTING) | Fixed 0.7.1 | OPENED |
| C-O9 | https://github.com/open-webui/open-webui/issues/27645 | 2026-07-28 | 0.11.0 | Closed same day not reproducible; 0-byte file in local layer | Restart loop after upgrade | Official compose | Low, probably local corruption | 1 | Not verified | OPENED |
| C-O10 | https://github.com/open-webui/open-webui/security/advisories/GHSA-vpq8-f445-hcq7 ; GHSA-9cxp-636w-4997 ; GHSA-jx3w-3pqq-7f3w ; GHSA-wvm9-9g5j-623f ; GHSA-2rr4-q6pg-m5g3 | 2026-09 to 2026-10 | ranges >=0.6.0 <0.11.4 etc. | Patched (0.11.1, 0.11.4, 0.12.2) | Token theft via message handler (High); knowledge collaborators keep access to removed files; directory structure disclosure; OAuth revoked users can still sign in | See advisories | High to medium, shared deployments | Separate advisories | Newer 0.13.4 should include fixes; not verified | OPENED (API), VENDOR |

### 2.3 Msty / Msty Studio (closed source; thin coverage)

| Id | URL | Date | Version | Status | Behavior | Severity (judgement) | Independent reports | Still relevant | Label |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| C-M1 | https://ludditus.com/2025/02/23/me-not-know/ | 2025-02-23 | n/s | Anecdotal single-author blog | Embedding model "cannot be changed"; image OCR slow; image PDFs poorly digested; generic RAG answer; phi4 crashed twice (CPU-only i3 laptop, so partly hardware). Vendor changelog (https://msty.ai/changelog) corroborates 2.7.0 (2026-04-29) "uploaded files disappearing after saving an embedding model" | Medium; anecdotal | 1 source | Not verified for 3.0.x | OPENED, secondary |
| C-M2 | https://docs.msty.app/troubleshooting/knowledge-stack | undated | n/s | Documented limitation | Files under 50 characters "will most likely be ignored"; model sees only retrieved chunks so answers may be misleading; check for "rogue Msty-App processes" | Low-medium | vendor only | Page current when fetched | OPENED, SPOT-CHECKED, VENDOR |
| C-M3 | https://docs.msty.app/getting-started/changelog ; https://docs.msty.app/troubleshooting/stuck-at-app-update | 2025-04-28 | v1.9 (legacy) | Fixed per changelog; troubleshooting page still documents manual kills | Stale msty-local / ollama-runner processes after close block updates | Medium | vendor only | Not verified for Studio 3 | OPENED, VENDOR |
| C-M4 | https://msty.ai/changelog | 2025-09-27 to 2026-11-05 | 2.0.0-beta.3 .. 3.0.0 | Fixed per vendor | Knowledge Stack compose hangs, items failing abort compose, stuck pending, SQL errors, access after deleting chunks | Medium | vendor only | Still evolving in 3.0.0 | OPENED, VENDOR |
| C-M5 | https://msty.ai/changelog | 2026-05-20 to 2026-08-22 | 2.7.4 .. 3.0.0 Beta 8 | Fixed per vendor | Memory growth and lag rendering long conversations; Skills Studio freezes | Medium | vendor only | Not verified on 3.0.1 | OPENED, VENDOR |
| C-M6 | https://msty.ai/changelog | 2026-04-08 to 2026-07-07 | 2.6.3 .. 2.9.0 | Fixed per vendor | Tool chats stuck during retries; orphaned tool-call history breaks chats; duplicate/missing tool call ids | Medium | vendor only | Not verified | OPENED, VENDOR |
| C-M7 | https://www.promptquorum.com/power-local-llm/msty-review | 2026-09-05 | Studio 2.9.6 | Secondary review | Closed source, "local-first, not local-only"; web search paid tier; no published hardware minimums | Low technical | 2 secondary sources | Not verified | OPENED, secondary |

### 2.4 RAGFlow (github.com/infiniflow/ragflow)

| Id | URL | Opened | Version | Status | Behavior | Repro | Severity (judgement) | Independent reports | Still relevant | Label |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| C-R1 | https://github.com/infiniflow/ragflow/issues/7957 | 2025-05-29 | image v0.18.0-178 (also 0.19.0) | Closed (reason not visible; only bot reply read; second half of 182k-char page not read) | GraphRAG will not finish; title cites 36 hours and ~$200 on OpenAI-mini; reporter says ~8 of 100 files complete; expected resume | Many PDF/MD, GraphRAG on | High (cost/time) | #7959 by another author | v1.0.0 not verified. The $200 figure comes from the title only | OPENED, SPOT-CHECKED |
| C-R2 | https://github.com/infiniflow/ragflow/issues/7959 | 2025-05-29 | 0.19.0 slim | Open feature request, unassigned | KG build very slow, no visible progress, restart from scratch; tasks stuck near 97% for a week; 10-doc KB never finishing | GraphRAG light + Ollama | High usability (request, not confirmed bug) | 3 different users | Still open per page (maybe stale) | OPENED, FEATURE REQUEST |
| C-R3 | https://github.com/infiniflow/ragflow/issues/8619 | 2025-07-01 | v0.19.1-slim | Open, 23 comments | Retrieval test shows correct chunk first but chat says nothing found | Not read | High if accurate | 1 | Open | LISTING |
| C-R4 | https://github.com/infiniflow/ragflow/issues/8490 | 2025-06-25 | n/s (v0.19.1 commenter) | Closed as completed (no confirmed fix) | Parsing 4 PDF/docx at once freezes UI/executor; executor sometimes dies on 16-core/128GB | Submit 4 files | Medium-high | 1 | Not verified | OPENED |
| C-R5 | https://github.com/infiniflow/ragflow/issues/11930 | 2025-12-12 | v0.22.1 | Closed | Table parser: chat returns chunks without LLM answer | Excel + table method | Medium | 1 | Not verified | OPENED |
| C-R6 | https://github.com/infiniflow/ragflow/issues/14307 | 2026-04-22 | 0.25.0 (0.25.1) | Closed 2026-07-13; fixes in v0.26.0 | Upgrade loses metadata; "Tenant Model not found"; ES sort error | Pull 0.25.0 | Medium-high | 2 others in thread | v1.0.0 newer; not verified | OPENED |
| C-R7 | https://github.com/infiniflow/ragflow/issues/7881 | 2025-05-27 | 0.19.0 | Open | KB search >10 min with Ollama on CPU, GPU idle | Docker | High local perf | Several users | Not verified on v1.0.0 | OPENED |
| C-R8 | https://github.com/infiniflow/ragflow/issues/5598 | 2025-03-04 | n/s | Open question | 55 MB PDF ~1 h on CPU; OCR on CPU by default; 413 on large scans; GPU OOM | Large PDFs | Medium-high | Multiple in thread | Not verified | OPENED |
| C-R9 | https://github.com/infiniflow/ragflow/issues/11144 | 2025-11-10 | v0.21.1 | Closed 2026-10-14 | PDF parse stalled at ~59%; root cause CPU Ollama embedding | Local mxbai-embed-large CPU | Medium (looks like a hang) | Similar #8343 LISTING | Not verified | OPENED |
| C-R10 | https://github.com/infiniflow/ragflow/security/advisories/GHSA-c6q8-23rp-32cv ; GHSA-wpg4-h5g2-jxm6 ; GHSA-vvwj-fvwh-4whx ; GHSA-v7cf-w7gj-pgf4 ; GHSA-9j5g-g4xm-57w7 | 2025-12 to 2026-10 | <=0.27.2 etc. | Published; patched field empty on most | Template-injection RCE (two), Zip Slip RCE in MinerU parser, auth bypass, agent auth bypass | See advisories | High for exposed/multi-tenant | Separate advisories | v1.0.0 not verified | OPENED (API), VENDOR |
| C-R11 | https://github.com/infiniflow/ragflow/issues/8353 ; /17077 | 2025-06, 2026-07 | n/s | Closed questions | Users cannot trace entities to source chunks; Graph toggle missing in 0.26.4 | n/a | Low-medium | questions | Not verified | LISTING |
| C-R12 | https://github.com/infiniflow/ragflow/issues/13001 ; /11451 ; /9991 ; /14066 | 2025-09 to 2026-04 | various | Closed | MySQL sync empty docs; wrong answers from local qwen2:7b; RTX 5090 unsupported; Keycloak OIDC errors | n/a | unknown | n/a | n/a | LISTING |

### 2.5 LightRAG (github.com/HKUDS/LightRAG)

| Id | URL | Opened | Version | Status | Behavior | Repro | Severity (judgement) | Independent reports | Still relevant | Label |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| C-L1 | https://github.com/HKUDS/LightRAG/issues/3597 | 2026-08-07 | 1.5.6 | Closed 2026-08-12, commit d7b5480 | Thinking-capable Ollama model: extraction returns 0 entities while document shows "processed"; KG empty | Local Ollama, POST text | High: silent empty graph | Family: #3601 (maintainer-filed, https://github.com/HKUDS/LightRAG/issues/3601), #2252 (open, https://github.com/HKUDS/LightRAG/issues/2252) | Fixed on main; stable tag not verified; #2252 open | OPENED, SPOT-CHECKED |
| C-L2 | https://github.com/HKUDS/LightRAG/issues/2720 | 2026-02-26 | latest (Postgres+AGE) | Closed 2026-02-27, labelled question, no code fix | ~30B reasoning model yields corrupted entity names; one class crashes merge, doc stuck failed, same chunk each retry | 500+ page handbook | High (graph pollution) | 1 exact crash; related L1 | Mitigation was "use non-reasoning model" | OPENED |
| C-L3 | https://github.com/HKUDS/LightRAG/issues/1323 | 2025-04-09 | milestone 1.4.8 | Open feature request (comment 2026-11-15) | Entity merge is exact-name only; duplicates and isolated subgraphs | n/a | Medium | ~12 thumbs-up, 35 comments (same thread) | Open | OPENED, FEATURE REQUEST |
| C-L4 | https://github.com/HKUDS/LightRAG/issues/985 ; https://github.com/HKUDS/LightRAG/issues/3400 ; https://github.com/HKUDS/LightRAG/issues/3833 | 2025-03-03; 2026-07-15; not recorded (#3833) | n/s | #985 closed; #3400 closed 2026-07-17; #3833 open | Delete did not remove records; multi-store merge/purge has no shared transaction so crash or cancel leaves orphan graph data and chunk references; orphaned LLM cache rows | Call DELETE then GET | Medium-high | #985 and #3400 separate authors | Release notes say interrupted deletions retryable and repair tool exists; #3833 open (LISTING) | #985, #3400 OPENED; #3400 SPOT-CHECKED; #3833 LISTING |
| C-L5 | https://github.com/HKUDS/LightRAG/issues/284 ; https://github.com/HKUDS/LightRAG/issues/2686 | 2024-11-15; 2026-02-08 | n/s | #284 closed; #2686 open | Extraction slow: ~400k tokens 12 h unfinished on T4; ~1,310 files ~20 h; hours-slow ingestion | Informal | High for local-first (user anecdotes) | 2 issues | #2686 open | OPENED |
| C-L6 | https://github.com/HKUDS/LightRAG/issues/3679 | 2026-08-20 | 1.5.7 | Closed 2026-08-23, PR #3680 | Ingestion throughput falls ~150 docs/h to ~10 docs/h by doc 500 (vector matrix rebuilt per merge) | repro.py in issue | Medium-high | #3681, #2909 LISTING | Not verified in stable | OPENED |
| C-L7 | https://github.com/HKUDS/LightRAG/issues/852 | 2025-02-19 | n/s | Closed 2025-07-19 | WebUI froze during multi-file upload; files stuck "processing" after restart | Upload many files | Medium | multiple commenters | Not verified | OPENED |
| C-L8 | https://github.com/HKUDS/LightRAG/issues/323 ; /137 | 2024-10; 2025-02 | n/s | Closed | Questions on returning source files for an answer | n/a | Low-medium | 2 threads | Not verified | LISTING, FEATURE REQUEST |
| C-L9 | https://github.com/HKUDS/LightRAG/issues/2904 | 2026-04-06 | n/s | Open (title only) | LIGHTRAG-WORKSPACE header ignored in /query | unknown | Potentially high, UNVERIFIED | related requests | Open | LISTING |
| C-L10 | https://github.com/HKUDS/LightRAG/releases | v1.5.8rc1 | | Release notes | Embedding-model mismatch guard added (wrong/empty results were possible); PDF extraction moved to resource-limited child process | n/a | signal only | n/a | n/a | VENDOR |

### 2.6 OpenSPG KAG (github.com/OpenSPG/KAG)

| Id | URL | Opened | Version | Status | Behavior | Severity (judgement) | Independent reports | Still relevant | Label |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| C-K1 | https://github.com/OpenSPG/KAG/issues/493 | 2025-04-25 | v0.7 vs 0.6 | Open | Extraction regression: ~4 points vs many; nearly everything becomes an entity, relations rarely extracted | High | 3 commenters in thread | Open | OPENED |
| C-K2 | https://github.com/OpenSPG/KAG/issues/278 | 2025-01-15 | 0.6/0.7 | Closed 2025-08-16 | JSONDecodeError from LLM output truncated by token limit | Medium-high | several in thread | Fix reportedly merged | OPENED |
| C-K3 | https://github.com/OpenSPG/KAG/issues/367 | 2025-02-23 | n/s | Closed 2025-08-16 (pointed at V0.8) | 300-page markdown on Ollama gemma2 ran over 3 days stuck at extractor | High local-first | 1 more in thread (24 h on 2 MB) | Improvement not verified | OPENED, SPOT-CHECKED |
| C-K4 | https://github.com/OpenSPG/KAG/issues/371 | 2025-02-25 | n/s | Closed 2025-08-16, no code fix; advice: convert externally | PDF ingest yields 0 nodes/edges | High | several in thread | Not verified | OPENED |
| C-K5 | https://github.com/OpenSPG/KAG/issues/565 | 2025-05-29 | 0.7.1, 0.8 | Open | Retrieval returns no chunks with self-hosted vLLM Qwen3; reasoning text leaks | High | 1 + later asks | Open | OPENED |
| C-K6 | https://github.com/OpenSPG/KAG/issues/572 ; /629 | 2025-06-07 | n/s | #572 open, no maintainer reply | Cannot wire Ollama (config keys, vectorizer type rejected, Docker cannot reach host) | Medium-high | many similar (#626, #664, #665 LISTING) | Open | OPENED |
| C-K7 | https://github.com/OpenSPG/KAG/issues/667 | 2025-08-01 | n/s | Closed | Long multi-step answers fail with token limit | Medium | 1 | Not verified | OPENED |
| C-K8 | https://github.com/OpenSPG/KAG/issues/728 | 2025-11-28 | 0.8.0.20250703 | Open | Build "succeeds" with 2 nodes, 0 edges (135M extractor model); partly user misconfig | High for small local models | related #371, #493 | No release after 0.8.0 | OPENED |
| C-K9 | https://github.com/OpenSPG/KAG/issues/614 ; /627 ; /625 ; /634 ; /718 ; /513 | 2025 | n/s | Open (titles only) | Project creation, embedding config, KB query timeout, vectorization failures | pattern | n/a | Open per listing | LISTING |

### 2.7 Khoj (github.com/khoj-ai/khoj)

| Id | URL | Opened | Version | Status | Behavior | Severity (judgement) | Independent reports | Label |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| C-H1 | https://github.com/khoj-ai/khoj/issues/1437 | 2026-09-26 | master | Open, PR #1440 | A note with a title-less heading marker makes markdown indexing loop forever at 100% of a core (reporter disclosed AI-agent-found) | Medium-high | 1 | OPENED |
| C-H2 | https://github.com/khoj-ai/khoj/issues/1368 | 2026-07-04 | 2.0.0-beta.28 | Open, PRs open | PDF indexing fails on Windows (temp file lock); no entries created | High on Windows | 2nd user in thread; #1289 LISTING | OPENED |
| C-H3 | https://github.com/khoj-ai/khoj/issues/1354 | 2026-06-28 | 1.42.10 | Open | CJK chunking undercounts tokens; retrieval returns frontmatter | Medium-high CJK | 1 | OPENED |
| C-H4 | https://github.com/khoj-ai/khoj/issues/1215 | 2025-08-16 | 1.42.10 | Open | 16 GiB KV-cache allocation OOM on 16 GB GPU by default | Medium-high | #1050 related | OPENED |
| C-H5 | https://github.com/khoj-ai/khoj/issues/195 | 2023-03-29 | n/s | Closed 2023-11-16 | First-run indexing exhausts RAM (~14 GB peak on a ~5 GB vault) | Medium (historical) | 1 | OPENED |
| C-H6 | https://github.com/khoj-ai/khoj/issues/1438 ; /1439 ; /1395 ; /1362 | 2026-09-26 and others | n/s | Open | Agent tool and date-filter faults; no feedback when Ollama model becomes unavailable mid-chat | Medium | same reporter | LISTING |
| C-H7 | https://github.com/khoj-ai/khoj/issues/1407 | 2026-08-20 | 1.42.10 (:latest) | Open | Backend omitting content on role chunks crashes every chat; fix on main since 2025-11-16 never backported | High for Docker users | #1370 closed (title only) | OPENED |
| C-H8 | https://github.com/khoj-ai/khoj/issues/1374 | 2026-07-13 | commit 1e30154d | Closed 2026-08-02, PR #1382 | Self-hosted telemetry payload carried client IP contrary to docs | Low-medium | 1 | OPENED |
| C-H9 | https://github.com/khoj-ai/khoj/issues/1327 | 2026-05-27 | master | Open, PR #1328 | Forked shared chat retains source user's private agent (source analysis only) | Medium multi-user | 1 | OPENED |
| C-H10 | https://github.com/khoj-ai/khoj/issues/1461 ; /1360 ; /1380 | 2026-07 to 2026-10 | n/s | Open (titles only) | pip install fails Debian 13; Docker first run hangs on Win11 WSL2 | install friction | n/a | LISTING |

### 2.8 OpenJarvis (github.com/open-jarvis/OpenJarvis; young project, issues from ~2026-03)

| Id | URL | Opened | Version | Status | Behavior | Severity (judgement) | Independent reports | Label |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| C-J1 | https://github.com/open-jarvis/OpenJarvis/issues/986 | 2026-09-13 | Desktop (reporter: v2.8) | Open, P0 | Desktop shows "Unable to reach memory service" while CLI works; diagnosis is a commenter hypothesis | High | 1 | OPENED |
| C-J2 | https://github.com/open-jarvis/OpenJarvis/issues/331 | 2026-05-08 | desktop v1.0.1 | Closed 2026-05-23 (fix v1.0.2) | Boot discarded setup errors, waited 600 s, showed generic error | High then | #200 same error | OPENED |
| C-J3 | https://github.com/open-jarvis/OpenJarvis/issues/1077 | 2026-10-01 | n/s | Open, PR #1094 | Missing optional voice model: health check calls sys.exit and takes down the server | Medium-high | 1 | OPENED |
| C-J4 | https://github.com/open-jarvis/OpenJarvis/issues/1184 | 2026-10-24 | Desktop 1.0.5 | Open, P0 Blocked | Windows Defender flags Rust toolchain download; unsigned installer; setup stops | High (Windows install) | 3 in thread + #1278 duplicate | OPENED |
| C-J5 | https://github.com/open-jarvis/OpenJarvis/issues/276 | 2026-04-22 | n/s | Closed 2026-08-21, PR #612 | Agent stops after few turns on null content; dashboard still shows "running" | High local agents | 1 | OPENED |
| C-J6 | https://github.com/open-jarvis/OpenJarvis/issues/414 | 2026-05-25 | v1.0.1/1.0.2 | Closed 2026-05-31, PR #454 | Tool calls dropped when routed through server; agent appears to succeed | High (silent) | 1 | OPENED |
| C-J7 | https://github.com/open-jarvis/OpenJarvis/issues/472 | 2026-06-01 | n/s | Open feature request | Imported skills run with shell/file/OAuth access; no install-time scan | Medium-high risk, not an exploit | 1 | OPENED, FEATURE REQUEST |
| C-J8 | https://github.com/open-jarvis/OpenJarvis/issues/1197 | n/r | | Closed 2026-11-11 (fixed in 1.0.5 notes) | Model selector empty after setup | Low-medium | 1 | LISTING |

### 2.9 Screenpipe (github.com/screenpipe/screenpipe; many issues auto-closed "not planned" by a bot, which is NOT a fix)

| Id | URL | Opened | Version | Status | Behavior | Severity (judgement) | Independent reports | Label |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| C-S1 | https://github.com/screenpipe/screenpipe/issues/7958 | 2026-11-16 | 2.7.145 | Open | macOS app heap grows to ~27 GB in ~5h47m (mostly swapped; resident ~0.7 GB) | High | Windows #7322 (different version/OS) | OPENED, SPOT-CHECKED |
| C-S2 | https://github.com/screenpipe/screenpipe/issues/7955 | 2026-11-15 | 2.7.145 | Open | ScreenCaptureKit stream churn drives CPU in system daemons, heat | High battery | same reporter as S1 | OPENED |
| C-S3 | https://github.com/screenpipe/screenpipe/issues/7364 | 2026-09-29 | 2.7.76/79 | Closed "not planned" (bot) 2026-10-19; reporter saw improvement in 2.7.84 | UI polling every ~4 s of slow queries, 370-440% CPU, battery 100 to 5% in 107 min | High, apparently fixed | 1 | OPENED |
| C-S4 | https://github.com/screenpipe/screenpipe/issues/7850 | 2026-11-01 | 2.7.132 | Closed 2026-11-13 (commit fd98ea6, PR #7900); fix post-dates latest release | SQLite WAL grows to 26 GB, checkpoint stuck 42 h | High (disk) | 2nd user; #7704, #7770 | OPENED |
| C-S5 | https://github.com/screenpipe/screenpipe/issues/7679 ; https://github.com/screenpipe/screenpipe/issues/7858 | 2026-10-17; 2026-11-02 | main; 2.7.132 | #7679 closed "not planned" by inactivity bot 2026-10-31, draft PR #7681 unmerged; #7858 open | Capture stalls or OCR fails on every frame while /health says OK | High (silent data loss) | 2 separate issues, different authors | OPENED; #7679 SPOT-CHECKED |
| C-S6 | https://github.com/screenpipe/screenpipe/issues/7523 | 2026-10-07 | 2.7.84 | Closed 2026-10-08 | Windows UI capture restart loop, ~40% core CPU | High while present | #7569, #7317 LISTING | OPENED |
| C-S7 | https://github.com/screenpipe/screenpipe/issues/7626 | 2026-10-14 | 2.7.108 | Closed "not planned" by bot 2026-10-29 | Old desktop/CLI processes retain ports and DB, blocking recording after relaunch. The report links an interrupted update (#7623) but, per the spot-check, does NOT establish that update caused it | High (recording blocked) | #7623 (LISTING), #7858 note on tray quit | OPENED, SPOT-CHECKED (cause downgraded) |
| C-S8 | https://github.com/screenpipe/screenpipe/issues/321 | 2024-09-13 | n/s | Closed 2025-05-08 | Defender quarantines unsigned app (maintainer-opened) | Medium-high historical | 1 (+ LISTING installs) | OPENED |
| C-S9 | https://github.com/screenpipe/screenpipe/issues/7959 ; /7691 ; /7693 | 2026-11 | n/s | titles only | Privacy-filter and gate leads | UNVERIFIED | n/a | LISTING |

## 3. Recurring pain-point patterns

Counts are "separate issues or documents by different authors" unless stated; same-thread commenters are listed separately because they are weaker.

| # | Pattern | Supporting ids | Independent reports | Evidence grade |
| --- | --- | --- | --- | --- |
| P1 | Silent success: ingestion, retrieval or health reports OK while output is empty or degraded | C-A1, C-A3, C-A11(L), C-O4, C-O6, C-R3(L), C-R5, C-M2, C-L1 (+#3601, #2252), C-K8 (+C-K4), C-S5 (+#7858), C-J5, C-J6 | At least 6 separate issue families by different authors (L1 family 3, O6 3, S5 2, A1/A3, K8/K4) | B+ (multiple opened, several still reproducing) |
| P2 | Local inference stalls, idle failures, CPU-only slowness, no timeouts or feedback | C-A6, C-A5, C-A15(L), C-R7, C-R9, C-H4, C-K5, C-H6 (#1362, L) | 6 products; C-A6 has many commenters and recurrences after a fix claim | B |
| P3 | KG/GraphRAG build cost, time and no progress/resume | C-R1, C-R2, C-L5 (+#2686), C-K3 (+#371), C-L6 | 5 products' threads; 6 issues by different authors | B+ |
| P4 | PDF/OCR parsing is weak or delegated; scans fail | C-A1, C-O5, C-R4, C-R8, C-R9, C-K4, C-K1, C-H2, C-M1 | 8 separate issues | B |
| P5 | Small/reasoning models break structured extraction (empty graph, truncated JSON, corrupted names) | C-L1, C-L2, C-K2, C-K5, C-K8, C-J5 | 6 | B+ |
| P6 | Deletion/lifecycle consistency, orphan graph data, retention | C-L4 (#985, #3400, #3833L), C-S4 (+#7770, #7704), C-M4 | 4-5 | B |
| P7 | Scale degrades UI and retrieval | C-O1, C-O2, C-O3, C-A7, C-M5, C-L6, C-S1 | 7 | B+ |
| P8 | Background work resource blowups (CPU, RAM leak, disk) | C-S1, C-S2, C-S3, C-S4, C-S6, C-O1, C-H1, C-H5 | 8 (Screenpipe dominant; memory leak recurs across years) | B+ |
| P9 | Upgrade regressions; fixes on main not released; premature closure | C-O1, C-O4, C-O8, C-R6, C-H7, C-S4, C-K8, C-A3, C-A5, C-S5 | 8+ | B |
| P10 | Orphaned helper processes, install and update friction (Windows worst) | C-M3, C-S7, C-J1 to J4, C-K6, C-H10(L), C-A8, C-S8 | 8+; no orphaned-worker complaint found for Open WebUI or RAGFlow | B- (several LISTING; S7 cause unproven) |
| P11 | Citation inspectability requested (not a defect) | C-A13 (4 requests), C-L8 (2) | 6 requests, no verified citation-correctness bug | C (requests, LISTING) |
| P12 | Entity quality and merging | C-L3, C-L2, C-K1 | 3; no sourced KG entity/relationship ACCURACY complaint for RAGFlow | C+ |
| P13 | Security / access-control flaws in server-style products and skill execution | C-O10, C-R10, C-H9, C-J7 | 4 products (advisories, vendor-published) | A- for advisories, not applicable to a single-user local desktop except as design reference |

## 4. Evidence quality grading

- A: vendor advisory or changelog, or opened issue with reproduced and acknowledged defect.
- B: opened issue with several commenters or separate issues by different authors, behavior described concretely.
- C: single report, request, anecdotal blog, or LISTING-only.
- Fixes are graded on what the page shows: "closed" and bot closures are not fixes; only a linked merged PR plus reporter or maintainer confirmation counts, and even then release status is mostly unverified.

## 5. Research limitations

- Reddit, HN, forums, Discord and G2 were not reached (domain blocked or 403). Findings skew toward maintainer-visible GitHub bugs and under-represent end-user sentiment.
- Msty is closed source with no public tracker; its coverage is vendor changelog, docs and two secondary reviews.
- No sourced complaint was found about KG entity or relationship accuracy (RAGFlow KG items are cost, slowness, no progress). The absence is a research gap, not proof.
- No orphaned-worker complaint was found for Open WebUI or RAGFlow.
- Pages were read through a small summarizing model; long threads were truncated (RAGFlow #7957 second half unread). Dates, versions and counts should be re-checked on the page before quoting.
- LISTING items are title-only and unverified. No software was installed or run, so "still relevant" is mostly unverified.
- Severity ratings are judgement. Several 2026 issues may be AI-agent-authored (some say so).
- No user counts, ratings or market-share claims are made.

## 6. Spot-check log (this pass, 2026-11-17, WebFetch)

| URL | Result |
| --- | --- |
| https://github.com/Mintplex-Labs/anything-llm/issues/7045 | Confirmed: title, opened 2026-11-06, closed 2026-11-09 via PR #7046, whitespace-only text layer skips OCR |
| https://github.com/Mintplex-Labs/anything-llm/issues/3103 | Confirmed: opened 2025-02-04, closed 2025-03-20 "fixed in 1.7.7", persisted on 1.7.8, recurrence 2026-03-25 on 1.11.2 |
| https://github.com/open-webui/open-webui/issues/21164 | Confirmed: opened and closed 2026-02-04; later comments v0.8.7, 0.8.10, 0.9.5 still failing. Maintainer replied "fixed in .8" then could not reproduce on 0.8.10 |
| https://github.com/open-webui/open-webui/issues/27745 | Confirmed with nuance: briefly closed as duplicate of #27622 on 07-30; fixed by index PR #27663; RAM stayed high |
| https://github.com/infiniflow/ragflow/issues/7957 | Confirmed with correction: the 36 h and $200 figures are in the title; body says GraphRAG does not finish even on single-page docs and ~8/100 files complete; closure reason not visible (second half of page unread) |
| https://github.com/HKUDS/LightRAG/issues/3597 | Confirmed: 1.5.6, silent 0 entities with status processed, closed 2026-08-12 via d7b5480 |
| https://github.com/HKUDS/LightRAG/issues/3400 | Confirmed: maintainer-filed, no shared transaction across stores, orphan graph data, closed 2026-07-17 |
| https://github.com/OpenSPG/KAG/issues/367 | Confirmed: ">3 days" on a 300-page doc, Ollama gemma2, closed 2025-08-16. The reporter says "stuck in the extractor stage", not "indexing" |
| https://github.com/screenpipe/screenpipe/issues/7679 | Confirmed: closed "not planned" by inactivity bot 2026-10-31; neither cause independently reproduced; draft PR #7681 unmerged |
| https://github.com/screenpipe/screenpipe/issues/7958 | Confirmed: open, 2.7.145, ~27 GB footprint in 5 h 47 min, mostly swapped, resident ~0.7 GB |
| https://github.com/screenpipe/screenpipe/issues/7626 | DOWNGRADED: stuck processes confirmed; closed by inactivity bot; but the issue does not establish that the interrupted update (#7623) caused it (research file implied it). Pattern P10 grade lowered |
| https://docs.msty.app/troubleshooting/knowledge-stack | Confirmed: "<50 characters ... most likely be ignored" and "rogue Msty-App processes" advice |

12 URLs checked; 11 confirmed, 1 downgraded, 2 refinements (title-only figures, briefly closed duplicate).
