# Current Architecture: Sequence Diagrams

Status: **Current.** Companion to [18-current-architecture.md](18-current-architecture.md). Each diagram follows one real flow through the code on `main` (commit `8a13c3b`), step by step, with the module that performs each step. Nothing here is a proposal.

How to read: columns are participants (grey cards), numbered arrows are calls in time order, green notes mark the decision or the guarantee that matters, and `alt` / `opt` / `loop` boxes show branches and repetition. Paths are relative to `backend/src/docket/`.

| Part | Flow |
| --- | --- |
| A | Launching Docket and choosing the session |
| B | First visit to a folder: consent, register, index |
| C | One file through ingestion |
| D | A question, fast path |
| E | Citation validation and repair |
| F | A question, agent path |
| G | Asking while indexing |
| H | Freshness: launch catch-up and a watcher batch |
| I | Maintenance: `docket sync` / Update now |
| J | Narrowing a broad source and pruning |
| K | Opening the evidence behind a citation |

---

## A. Launching Docket and choosing the session

```mermaid
%%{init: {'theme':'base','themeVariables':{'primaryColor':'#F3F4F6','primaryBorderColor':'#E5E7EB','primaryTextColor':'#111827','lineColor':'#6B7280','actorBkg':'#F3F4F6','actorBorder':'#E5E7EB','actorTextColor':'#111827','signalColor':'#6B7280','signalTextColor':'#111827','noteBkgColor':'#ECFDF5','noteBorderColor':'#16A34A','noteTextColor':'#111827','activationBkgColor':'#ECFDF5','activationBorderColor':'#16A34A','fontFamily':'ui-sans-serif, system-ui, sans-serif'}}}%%
sequenceDiagram
    autonumber
    actor U as User
    participant CLI as main.py
    participant L as launcher.py
    participant T as Terminal window
    participant UI as docket ui --in-window
    participant S as session.py
    U->>CLI: docket [folder]
    CLI->>CLI: resolve folder argument (default: current directory)
    CLI->>L: spawn_window(folder)
    alt display available, not SSH, not DOCKET_NO_WINDOW
        L->>L: find_terminal() (DOCKET_TERMINAL, $TERMINAL, known emulators)
        L->>T: Popen(env DOCKET_* ... docket ui --in-window --folder F, new session)
        L-->>CLI: True
        CLI-->>U: "Opened Docket in a new window." (shell returns)
        T->>UI: start process, set window title
    else no display, SSH, or opt-out
        L-->>CLI: False
        CLI->>UI: run the interactive session in place
    end
    UI->>S: resolve_session(cwd, sources)
    Note over S: library · covered · new · empty
    S-->>UI: SessionPlan
    UI-->>U: chat, consent, preview, or library view
```

The launcher passes every `DOCKET_*` variable through to the new window, so a scratch data directory set in the calling shell also applies there. If the full-screen UI fails with an unexpected error, the window prints it and waits for Enter instead of closing.

Files: `interfaces/cli/main.py`, `interfaces/cli/launcher.py`, `services/sources/session.py`.

---

## B. First visit to a folder: consent, register, index

```mermaid
%%{init: {'theme':'base','themeVariables':{'primaryColor':'#F3F4F6','primaryBorderColor':'#E5E7EB','primaryTextColor':'#111827','lineColor':'#6B7280','actorBkg':'#F3F4F6','actorBorder':'#E5E7EB','actorTextColor':'#111827','signalColor':'#6B7280','signalTextColor':'#111827','noteBkgColor':'#ECFDF5','noteBorderColor':'#16A34A','noteTextColor':'#111827','activationBkgColor':'#ECFDF5','activationBorderColor':'#16A34A','fontFamily':'ui-sans-serif, system-ui, sans-serif'}}}%%
sequenceDiagram
    autonumber
    actor U as User
    participant UI as TuiApp
    participant B as RealBackend
    participant SS as session.py
    participant SM as SourceManager
    participant P as IngestionPipeline
    UI->>B: plan_for(folder)
    B->>SS: resolve_session(folder)
    SS->>SS: scan_folder(folder), bounded to about 2 s
    SS-->>UI: new folder plan (counts, broad?)
    alt broad or large folder
        UI-->>U: preview: counts by type and subfolder, checkboxes
        U->>UI: tick subfolders, Index N documents
    else small and not yet consented
        UI-->>U: "Read this folder?"
        U->>UI: Continue
        UI->>B: record_consent()
        B->>SS: write preferences.json (folder_consent_v1)
    end
    UI->>B: start_folder(folder, excludes)
    B->>SM: register_source(path, display_name, rules)
    Note over SM: refuses a folder inside an active source (already covered), parents exclude registered children
    B->>SM: set_rules(unticked subfolders)
    UI->>B: run_index(source)
    B->>P: run_ingestion_for_source()
    P-->>UI: per-file progress (footer: Indexing 3/12)
    P-->>UI: finished: indexed, unchanged, failed
    UI-->>U: notice, questions already accepted during the run
```

Consent is asked once per data directory. Nothing is registered or read before the user agrees. The scan is read-only and is limited by time and number of entries, so a huge tree is reported as "broad" instead of being walked completely.

Files: `services/sources/scan.py`, `session.py`, `manager.py`; `interfaces/cli/tui/app.py`, `real_backend.py`.

---

## C. One file through ingestion

```mermaid
%%{init: {'theme':'base','themeVariables':{'primaryColor':'#F3F4F6','primaryBorderColor':'#E5E7EB','primaryTextColor':'#111827','lineColor':'#6B7280','actorBkg':'#F3F4F6','actorBorder':'#E5E7EB','actorTextColor':'#111827','signalColor':'#6B7280','signalTextColor':'#111827','noteBkgColor':'#ECFDF5','noteBorderColor':'#16A34A','noteTextColor':'#111827','activationBkgColor':'#ECFDF5','activationBorderColor':'#16A34A','fontFamily':'ui-sans-serif, system-ui, sans-serif'}}}%%
sequenceDiagram
    autonumber
    participant P as IngestionPipeline
    participant EM as EvidenceManager
    participant ST as Evidence store
    participant PA as Parser
    participant CH as Chunker
    participant CW as ChunkWriter
    participant IM as IndexManager
    participant OL as Ollama (embed)
    participant DB as SQLite
    P->>P: discover(): ignore rules, signature check
    P->>EM: ingest_file(source, path)
    EM->>DB: current version for (source, path)?
    alt same bytes
        EM-->>P: existing version (mtime refreshed)
        P-->>P: status unchanged (no parse)
    else new or changed
        EM->>ST: put(original bytes, content hash)
        EM->>DB: old version SUPERSEDED, new version PENDING, blob reference
        EM-->>P: new version
        P->>IM: delete_version(old) (drop superseded index entries)
        P->>PA: parse(path) by file type
        PA-->>P: parsed document, units
        P->>CH: chunk with the family recipe
        CH-->>P: chunks with index text
        P->>CW: persist_units_and_chunks()
        CW->>DB: evidence_units, chunks (deterministic ids)
        P->>IM: upsert_chunks()
        IM->>OL: embed_texts(batches of 32, 3 attempts)
        IM->>DB: FTS5 upsert
        IM->>IM: vector upsert (LanceDB), manifest guard
        P->>DB: version READY
    end
    opt any step raises
        P->>DB: version FAILED + failure_reason
    end
```

The index text sent to the embedder and the full-text index is the chunk plus its file name, heading breadcrumb and (for spreadsheets) a `Context:` line with fiscal year and units. The stored chunk text stays verbatim, so citations quote the original. A final reconcile after the whole run removes any index row that no longer has a current chunk.

Files: `services/ingestion/pipeline.py`, `chunk_writer.py`; `infra/evidence/manager.py`; `infra/parsing/*`; `infra/index/manager.py`, `context.py`.

---

## D. A question, fast path

```mermaid
%%{init: {'theme':'base','themeVariables':{'primaryColor':'#F3F4F6','primaryBorderColor':'#E5E7EB','primaryTextColor':'#111827','lineColor':'#6B7280','actorBkg':'#F3F4F6','actorBorder':'#E5E7EB','actorTextColor':'#111827','signalColor':'#6B7280','signalTextColor':'#111827','noteBkgColor':'#ECFDF5','noteBorderColor':'#16A34A','noteTextColor':'#111827','activationBkgColor':'#ECFDF5','activationBorderColor':'#16A34A','fontFamily':'ui-sans-serif, system-ui, sans-serif'}}}%%
sequenceDiagram
    autonumber
    actor U as User
    participant Q as QueryService
    participant SC as scope.py
    participant G as Ollama gateway
    participant HS as hybrid_search
    participant R as EvidenceResolver
    U->>Q: ask(question, history, scope)
    Q->>SC: resolve(scope)
    alt scope matches nothing
        Q-->>U: "Nothing is indexed in X yet" (no model call)
    end
    opt there is history
        Q->>G: rewrite follow-up (temp 0, think off, 128 tokens)
        G-->>Q: standalone question or rejected
    end
    Q->>Q: extract signals: named file, period words
    Q->>HS: search(question + rewrite, scope ids, pool_k)
    HS->>HS: FTS5 leg + vector leg, reciprocal rank fusion
    HS-->>Q: ranked chunk ids
    Q->>R: resolve_many(ids)
    R-->>Q: text, citation label, location, version
    Q->>Q: fit prompt to the token budget, drop lowest ranks
    opt several fiscal years match and no period given
        Q->>Q: add the per-year answer note
    end
    Q->>G: generate(system, prompt, temp 0.6, think on)
    G-->>Q: draft answer
    Q->>Q: finalize: validate citations (part E)
    Q-->>U: answer, citations, scope label, warnings
```

If the search returns nothing, the answer is the fixed abstention sentence and no model call is made. The scope is applied before ranking, so a folder scope cannot be widened by a file named in the question; a named file outside the scope produces an explanation instead of a wider search.

Files: `services/query/service.py`, `scope.py`, `signals.py`; `infra/retrieval/hybrid.py`, `resolver.py`; `infra/inference/gateway.py`.

---

## E. Citation validation and repair

```mermaid
%%{init: {'theme':'base','themeVariables':{'primaryColor':'#F3F4F6','primaryBorderColor':'#E5E7EB','primaryTextColor':'#111827','lineColor':'#6B7280','actorBkg':'#F3F4F6','actorBorder':'#E5E7EB','actorTextColor':'#111827','signalColor':'#6B7280','signalTextColor':'#111827','noteBkgColor':'#ECFDF5','noteBorderColor':'#16A34A','noteTextColor':'#111827','activationBkgColor':'#ECFDF5','activationBorderColor':'#16A34A','fontFamily':'ui-sans-serif, system-ui, sans-serif'}}}%%
sequenceDiagram
    autonumber
    participant Q as QueryService
    participant V as citations.py
    participant R as EvidenceResolver
    participant G as Ollama gateway
    Q->>V: validate_citations(answer, resolved chunks)
    alt abstention phrase
        V-->>Q: valid (abstained)
    else tags valid and cited
        V-->>Q: valid
    else uncited, or a tag that was not supplied
        V-->>Q: warnings
        Q->>G: one repair generation (corrective suffix)
        G-->>Q: repaired answer
        Q->>V: validate again
        alt still invalid
            Q->>Q: replace with the fixed abstention sentence
        end
    end
    Q->>R: re-resolve each cited chunk
    alt a cited source was revoked during generation
        Q->>Q: abstain
    end
    Note over Q,V: A valid tag proves the label exists, not that the passage supports the sentence. Claim-level checks are part 07 and are not built.
    Q-->>Q: citations = labels that appear in the final answer
```

This is the whole trust check today. It protects against invented citations, uncited answers and evidence that disappeared mid-answer. It does not verify that a cited passage supports each claim; the answer view says so ("citations are not verified").

Files: `services/query/citations.py`, `service.py` (`_finalize_answer`).

---

## F. A question, agent path

```mermaid
%%{init: {'theme':'base','themeVariables':{'primaryColor':'#F3F4F6','primaryBorderColor':'#E5E7EB','primaryTextColor':'#111827','lineColor':'#6B7280','actorBkg':'#F3F4F6','actorBorder':'#E5E7EB','actorTextColor':'#111827','signalColor':'#6B7280','signalTextColor':'#111827','noteBkgColor':'#ECFDF5','noteBorderColor':'#16A34A','noteTextColor':'#111827','activationBkgColor':'#ECFDF5','activationBorderColor':'#16A34A','fontFamily':'ui-sans-serif, system-ui, sans-serif'}}}%%
sequenceDiagram
    autonumber
    participant Q as QueryService
    participant C as Classifier
    participant A as Agent (LangGraph)
    participant PG as Policy gateway
    participant T as Tools
    participant R as EvidenceResolver
    Q->>C: classify(question)
    C-->>Q: AGENT (regex match) or FAST
    Q->>Q: set RunContext (rewrite, scope, boundary)
    Q->>A: invoke(system, history, question)
    loop up to 8 iterations
        A->>PG: tool calls requested
        alt name not on the allow-list, or over 14 calls
            PG-->>A: blocked
        else allowed
            PG->>T: search_knowledge / read_evidence / read_range / calculate
            T->>T: apply scope boundary on every read
            T-->>A: result (chunk ids, text, cells)
        end
    end
    opt no read_evidence or read_range succeeded
        A->>A: forced read step
    end
    A-->>Q: final answer and tool trace
    Q->>R: rebuild citations from successful tool results
    Q->>Q: finalize and validate (part E)
```

Citations come from what the tools actually returned, not from labels the model wrote. On the measured sets this path scored 47% against 82% for the fast path, because the local model usually searches once and reads the top hit; it is therefore used only for investigative phrasing.

Files: `services/query/classifier.py`, `service.py`; `services/agent/graph.py`, `policy_gateway.py`, `tools.py`, `run_context.py`.

---

## G. Asking while indexing

```mermaid
%%{init: {'theme':'base','themeVariables':{'primaryColor':'#F3F4F6','primaryBorderColor':'#E5E7EB','primaryTextColor':'#111827','lineColor':'#6B7280','actorBkg':'#F3F4F6','actorBorder':'#E5E7EB','actorTextColor':'#111827','signalColor':'#6B7280','signalTextColor':'#111827','noteBkgColor':'#ECFDF5','noteBorderColor':'#16A34A','noteTextColor':'#111827','activationBkgColor':'#ECFDF5','activationBorderColor':'#16A34A','fontFamily':'ui-sans-serif, system-ui, sans-serif'}}}%%
sequenceDiagram
    autonumber
    actor U as User
    participant UI as TuiApp loop
    participant IW as Indexing worker
    participant G as YieldGate
    participant QW as Question worker
    participant B as RealBackend
    UI->>IW: start job (file by file)
    loop each file
        IW->>G: wait_clear() between files
        IW->>IW: process one file
        IW-->>UI: progress (posted to the loop)
    end
    U->>UI: submit a question
    UI->>G: hold()
    UI->>QW: start question worker
    Note over G: indexing finishes the file in progress, then waits
    QW->>B: ask(question, scope, index coverage)
    B->>B: open the latest vector table, build the query service
    B-->>QW: result (adds "31 of 47 documents indexed")
    QW-->>UI: answer posted to the loop
    QW->>G: release() (always, even on error)
    G-->>IW: continue with the next file
```

Two slots exist: one indexing job and one question. A second question is refused with a message and the draft is kept; starting Add or Refresh while a question runs is refused too. Stopping an indexing job takes effect between files. A model call that has started cannot be interrupted.

Files: `interfaces/cli/tui/app.py`, `yield_gate.py`, `real_backend.py`.

---

## H. Freshness: launch catch-up and a watcher batch

```mermaid
%%{init: {'theme':'base','themeVariables':{'primaryColor':'#F3F4F6','primaryBorderColor':'#E5E7EB','primaryTextColor':'#111827','lineColor':'#6B7280','actorBkg':'#F3F4F6','actorBorder':'#E5E7EB','actorTextColor':'#111827','signalColor':'#6B7280','signalTextColor':'#111827','noteBkgColor':'#ECFDF5','noteBorderColor':'#16A34A','noteTextColor':'#111827','activationBkgColor':'#ECFDF5','activationBorderColor':'#16A34A','fontFamily':'ui-sans-serif, system-ui, sans-serif'}}}%%
sequenceDiagram
    autonumber
    participant UI as TuiApp
    participant F as freshness.py
    participant W as FileWatcher thread
    participant D as Debouncer
    participant P as IngestionPipeline
    participant DB as SQLite + indexes
    UI->>F: catch_up(source) on launch
    F->>F: walk with ignore rules, compare path, size, mtime
    F-->>UI: ChangeSet (new, changed, deleted, held back)
    UI->>P: run_ingestion_for_paths(changes)
    W->>D: file events
    D->>D: 5 s quiet period, 60 s max batch
    D-->>UI: batch of paths
    Note over UI: queued until no job and no question is active
    UI->>P: run_ingestion_for_paths(batch, missing=skip)
    alt path exists
        P->>DB: hash, re-parse only if the content changed
    else path is gone
        P->>DB: version SUPERSEDED, index entries deleted
    end
    P-->>UI: "Updated 3 files"
    opt watch cannot start, or tree over 20,000 entries
        F->>F: periodic catch-up every 5 minutes instead
    end
```

An unreachable root deletes nothing and marks the source MISSING; if everything looks "gone" under a reachable but empty root, deletions are held back rather than wiping the index. The whole mechanism stops when the window closes.

Files: `services/sources/freshness.py`, `watcher.py`; `services/ingestion/pipeline.py`; `interfaces/cli/tui/app.py`.

---

## I. Maintenance: `docket sync` / Update now

```mermaid
%%{init: {'theme':'base','themeVariables':{'primaryColor':'#F3F4F6','primaryBorderColor':'#E5E7EB','primaryTextColor':'#111827','lineColor':'#6B7280','actorBkg':'#F3F4F6','actorBorder':'#E5E7EB','actorTextColor':'#111827','signalColor':'#6B7280','signalTextColor':'#111827','noteBkgColor':'#ECFDF5','noteBorderColor':'#16A34A','noteTextColor':'#111827','activationBkgColor':'#ECFDF5','activationBorderColor':'#16A34A','fontFamily':'ui-sans-serif, system-ui, sans-serif'}}}%%
sequenceDiagram
    autonumber
    actor U as User
    participant UI as CLI or TUI
    participant M as LibraryMaintenance
    participant PR as SourcePruneService
    participant P as IngestionPipeline
    participant RI as reindex
    UI->>M: plan()
    M->>PR: find prunable entries
    M->>P: find stale recipes, never-read folders
    M->>M: check index manifest (format, embedding model)
    M-->>UI: plan with plain labels and time estimates
    alt nothing to do
        UI-->>U: "Everything is up to date"
    else
        opt costly steps present (rechunk, reindex)
            UI-->>U: ask once, with the estimate
            U->>UI: confirm
        end
        UI->>M: run(plan)
        M->>PR: 1. prune (supersede and de-index)
        M->>P: 2. refresh unread folders
        M->>P: 3. rechunk stale versions from the stored originals
        M->>RI: 4. reindex into scratch tables, then swap
        Note over RI: the old index keeps serving until the new one is complete
        M-->>UI: progress per stage, second plan is empty
    end
```

Rechunk always works from the stored original bytes, never from the file on disk, so it cannot pick up a newer edit; refresh does that. Reindex is global and is skipped when the plan is limited to one source.

Files: `services/sources/maintenance.py`, `prune.py`; `services/ingestion/pipeline.py`; `infra/index/reindex.py`.

---

## J. Narrowing a broad source and pruning

```mermaid
%%{init: {'theme':'base','themeVariables':{'primaryColor':'#F3F4F6','primaryBorderColor':'#E5E7EB','primaryTextColor':'#111827','lineColor':'#6B7280','actorBkg':'#F3F4F6','actorBorder':'#E5E7EB','actorTextColor':'#111827','signalColor':'#6B7280','signalTextColor':'#111827','noteBkgColor':'#ECFDF5','noteBorderColor':'#16A34A','noteTextColor':'#111827','activationBkgColor':'#ECFDF5','activationBorderColor':'#16A34A','fontFamily':'ui-sans-serif, system-ui, sans-serif'}}}%%
sequenceDiagram
    autonumber
    actor U as User
    participant UI as Sources panel
    participant B as RealBackend
    participant SC as scan.py
    participant SM as SourceManager
    participant PR as SourcePruneService
    U->>UI: Narrow on the Desktop source
    UI->>B: narrow_plan(source)
    B->>SC: scan_folder(root)
    SC-->>UI: subfolders with document counts, current excludes unticked
    U->>UI: untick folders, confirm
    UI->>B: narrow_source(source, kept subfolders)
    B->>SM: set_rules (anchored excludes for unticked folders)
    B->>PR: find() with the new rules
    PR-->>UI: files that are now excluded
    B->>PR: apply_candidates()
    PR->>PR: version SUPERSEDED, index entries deleted
    PR-->>UI: "Removed 23 files from the index"
    Note over PR: no re-parse, original files untouched
```

Rules are stored on the source row (`rules_json`: exclude, include, ignored files) and are read by discovery on every run, so excluded folders stay excluded. The same path handles "Ignore this file" for a permanently failing document.

Files: `services/sources/manager.py`, `scan.py`, `prune.py`; `services/ingestion/ignore.py`.

---

## K. Opening the evidence behind a citation

```mermaid
%%{init: {'theme':'base','themeVariables':{'primaryColor':'#F3F4F6','primaryBorderColor':'#E5E7EB','primaryTextColor':'#111827','lineColor':'#6B7280','actorBkg':'#F3F4F6','actorBorder':'#E5E7EB','actorTextColor':'#111827','signalColor':'#6B7280','signalTextColor':'#111827','noteBkgColor':'#ECFDF5','noteBorderColor':'#16A34A','noteTextColor':'#111827','activationBkgColor':'#ECFDF5','activationBorderColor':'#16A34A','fontFamily':'ui-sans-serif, system-ui, sans-serif'}}}%%
sequenceDiagram
    autonumber
    actor U as User
    participant UI as TuiApp
    participant B as RealBackend
    participant R as EvidenceResolver
    participant DB as SQLite
    U->>UI: Ctrl+E, or /show 1
    UI->>B: show citation n of the latest answer
    B->>R: resolve_many(chunk ids)
    R->>DB: chunk, unit locator, version, source status
    alt source active and version READY
        R-->>UI: verbatim text, location (page / sheet and range / slide), version n of m
        UI-->>U: evidence panel (Previous, Next, Details)
    else revoked, superseded or deleted
        R-->>UI: unavailable
        UI-->>U: "no longer available" with a generic reason
    end
```

Evidence is resolved again every time it is opened, so a disconnected or deleted source cannot be read through an old answer. A citation always refers to the version it was answered from, never to a newer one. "Open original" is disabled for now.

Files: `infra/retrieval/resolver.py`; `interfaces/cli/tui/overlays.py`, `real_backend.py`.

---

## Related

- [18-current-architecture.md](18-current-architecture.md) for the block-level diagrams, [05](05-query-understanding-and-retrieval.md) for the query design, [07](07-citations-trust-and-abstention.md) for the claim-level trust layer that Part E does not yet implement, [17](17-library-and-ingestion-experience.md) for the folder-first design behind Parts A, B, G, H and I.
