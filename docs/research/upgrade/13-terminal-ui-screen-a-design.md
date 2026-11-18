# Docket terminal UI redesign: Screen A

Status: **In progress: backend checkpoint 1.** The full-screen prototype has its own visual review checkpoint before backend integration. Features beyond the first release remain planned work.

Date: 10 October 2026.

This document describes the proposed terminal experience, the backend work it requires, and the order in which it should be implemented after approval. It supplements [part 10: Interfaces and User Experience](10-interfaces-and-user-experience.md). The current source code is the reference for existing behavior; older upgrade documents sometimes describe features before their implementation.

This is the detailed Screen A specification feeding part 10, which remains the interface overview. [The Codex handoff](13-codex-handoff.md) defines ownership and checkpoint rules. Parts [06](06-reasoning-and-agent-orchestration.md) and [07](07-citations-trust-and-abstention.md) define the intended reasoning and trust contracts; their unimplemented capabilities must not be presented as available merely because their screens are designed here.

## 1. Decisions already confirmed

The user selected the following during the design discussion:

| Decision | Confirmed direction |
| --- | --- |
| Overall appearance | **Screen A: chat with overlays.** Chat is the main screen; focused panels open over it. |
| Design coverage | The whole everyday workflow: setup, chat, sources/files, indexing, evidence, and settings/status. |
| Default search | Search **all ready sources**, with a picker to narrow to a source or file. |
| Adding a folder | Register it and **start indexing automatically**. Open the indexing progress overlay. |
| Review process | Write and review this design first. Begin implementation only after the user approves it. |

Everything below marked **proposed** is a concrete implementation recommendation for that review, rather than a separately confirmed preference.

The earlier comparison image was a concept with sample data. Its Finance label, file counts, revenue figures, and spreadsheet ranges were illustrative. They must not become hard-coded application content.

## 2. Product shape and boundaries

### 2.1 Screen A behavior

Use one terminal application with a persistent header, scrollable conversation, message composer, and compact status footer. There is no permanent sidebar or permanent evidence column.

Open source management, jobs, evidence, and settings as temporary overlays. Opening or closing an overlay preserves the conversation, draft, and scroll position. Return focus to the control that opened it. An overlay is large enough for its content and independently scrollable.

Small choices use compact pickers: command palette, answering mode, search scope, and model selection. Larger activities use larger panels: source details, file inventories, indexing results, and evidence passages.

Use the same backend actions for keyboard shortcuts, slash commands, and panel actions. Each is a route to the same operation, rather than a separate implementation.

### 2.2 Proposed first implementation scope

Include all six everyday screen groups and the support needed to make them accurate: readiness queries, file/job details, progress events, cooperative cancellation, explicit query scope, source reconnection, and saved UI preferences.

Keep existing one-shot commands usable for scripts. Keep a plain interactive fallback for terminals that cannot display the new application.

The following remain outside this redesign's first implementation:

- Rebuilding the paused Tauri desktop application or adding a browser dashboard.
- Microsoft 365 connectors, account consent, or services not implemented in the current backend.
- Saved conversation sessions, a session picker, or automatic restoration of old conversations.
- Changing retrieval quality, ranking, parsing recipes, or the accuracy milestone.
- Automatically enabling visual retrieval, formula transcription, or deterministic computation.
- A background service that continues indexing after Docket exits.
- Bringing automatic folder watching into the interactive application. Keep the existing explicit watcher available separately.
- Changing the retention policy or automatically deleting retained data.

These boundaries keep the UI redesign reviewable. Later capabilities can use the same overlay system.

## 3. What exists and what must change

| Area | Existing implementation | Work needed for the proposed experience |
| --- | --- | --- |
| Terminal shell | `PromptSession` input, Rich output, autocomplete, input history, toolbar | A composed application layout with transcript scrolling, overlays, focus, and resize handling |
| Setup | Health warning and an optional offer to add the current directory | Readiness screen, folder chooser, automatic indexing, and a resumable path to chat |
| Sources | Register/list/revoke local folders | Human-readable selection, source details, file inventory, and reconnect |
| Indexing | Synchronous pipeline; file start/done callbacks; aggregate job records | Worker execution, stage events, cancellation, persisted per-file outcomes, and results panel |
| Search readiness | Primarily checks whether the vector table exists | Count actually eligible files/chunks and inspect index compatibility |
| Query | Auto/fast/agent paths; rewriting, inferred scope, citations, ambiguity, optional compute | Explicit user scope, progress events, answer details, and reliable retry state |
| Evidence | Resolver returns text, heading, location, source/version IDs | Show location and provenance; inspect earlier answers; original-file access |
| Settings | Environment-backed settings and session mode | Appearance preferences, selected runtime controls, consistent effective-setting display |
| Removal | Revocation without physical deletion | Explain disconnect semantics; reconnect; separately confirmed stored-data deletion |
| Maintenance | Shell commands for re-chunking/reindexing; purge/sweep service methods | Readiness-driven maintenance entry points and progress; no automatic retention sweep |

Implementation anchors:

- `backend/src/docket/interfaces/cli/main.py` and `interfaces/cli/context.py`: entry points and dependency construction.
- `backend/src/docket/interfaces/cli/interactive/`: current session, commands, input, rendering, and shared state.
- `backend/src/docket/services/ingestion/`: parsing/chunking/indexing orchestration and progress.
- `backend/src/docket/services/query/`: answering, conversation context, scope, and result metadata.
- `backend/src/docket/services/sources/` and `infra/evidence/`: source lifecycle, versions, originals, and purge.
- `backend/src/docket/core/db/models.py`: persisted source/version/job information.

## 4. Visual language and shared interaction rules

### 4.1 Appearance

Proposed default: charcoal background, readable neutral text, restrained cyan/mint accents, and amber for attention. Use labels as well as colors: `Ready`, `Failed`, and `Disconnected` must remain distinguishable without color.

Use thin borders, consistent padding, clear titles, and short action labels. Display source names and paths before internal IDs. Put chunk IDs, version IDs, model options, and raw exceptions in Details.

First release: one polished dark appearance, plus terminal-default/no-color compatibility. Light appearance, spacing density controls, and animation preferences are deferred. Use the terminal's font; a TUI cannot set the user's terminal font or font size. Do not animate entire panels or add decorative spinners to idle screens.

Proposed dark-theme tokens for the prototype:

| Role | Value / treatment |
| --- | --- |
| Canvas / panel | `#15191E` / `#1E252D` |
| Primary / secondary text | `#E6EDF3` / `#A8B3BF` |
| Border / focused border | `#526170` / `#77D8C3` |
| Accent / attention / error | `#77D8C3` / `#F0C674` / `#F28B82` |
| Selected row | `#293E4B` background, primary text, leading `>` marker |
| Focused action | Accent border or background plus a visible focus marker |
| Disabled action | Secondary text with an explanation; never color alone |

Use one row of vertical separation between groups and two character cells of horizontal panel padding. Titles are bold; field labels use secondary text; file/evidence content uses primary text. Keep buttons in a stable bottom action row. Approximate colors on limited-color terminals. The prototype review validates these values in an actual terminal before freezing them.

### 4.2 Layout and terminal sizes

At 100 columns or wider, center large overlays with visible chat around them; cap reading width at approximately 100 columns. At 80–99 columns, use nearly the available width. Below 80 columns, use a single-column overlay and put row details below the selection instead of beside it.

Keep the composer and one-line state visible where possible. Below 60 columns or 16 rows, show a resize notice and offer the plain interface. Reflow on resize without discarding drafts, selection, or scroll position. Use an ASCII-border fallback when Unicode borders render poorly.

### 4.3 Keyboard contract

| Key/action | Proposed behavior |
| --- | --- |
| Enter in composer | Send a question or run a command when the backend is available |
| Alt+Enter / Esc then Enter | Insert a newline; preserve the existing terminal-compatible binding |
| Ctrl+J | Optional newline binding only where distinguishable from Enter; Alt+Enter remains the portable contract |
| Tab / Shift+Tab in a panel | Move between controls; composer Tab continues completion |
| Arrow keys in a picker | Change selection; Enter accepts |
| Page Up / Page Down | Scroll the focused transcript or panel |
| Esc | Close the top picker/panel; a nested confirmation returns to its parent |
| Ctrl+C with an overlay open | Dismiss the top overlay; does not secretly cancel indexing |
| Ctrl+C with an operation active and no overlay | Request cooperative cancellation |
| Ctrl+C while idle | Clear the composer if nonempty; otherwise show the exit hint |
| Ctrl+D / `/exit` | Exit immediately if idle; show an exit confirmation if work is active |
| F1 | Open command/help palette |

The composer draft survives panel dismissal. Cancelling an edited settings form asks whether to discard its unsaved changes. Confirmations use focused controls and never enter input history. Keyboard access is required; mouse selection may supplement it.

### 4.5 Selection, copying, and scroll position

Keep mouse capture off initially so normal terminal text selection remains available. Provide a keyboard-accessible plain-text answer/evidence view for selecting and copying text. A Copy action is enabled only when a supported clipboard mechanism is available; otherwise explain how to use terminal selection. Do not depend on clipboard integration to inspect or copy evidence.

If the reader has scrolled upward, preserve that position and show `New messages — go to latest`. Auto-follow only when already at the bottom. Evidence and transcript scrolling have independent positions. Links expose their destination and open only on explicit activation through the platform opener.

### 4.6 Shared screen states

| State | Presentation and next action |
| --- | --- |
| Loading | Specific activity label; preserve existing content where valid; hide/dismiss remains available |
| Empty | Explain what is absent and offer Add folder, Refresh, or a question as appropriate |
| Unavailable | State the known blocker and provide Check again, Settings, or Retry |
| Partial success | Show completed coverage and failures separately; open file-level results |
| Failed | Concise cause and relevant recovery action; technical exception in Details |
| Cancellation requested | `Stopping after the current operation`; remain in this state until the worker stops |
| Cancelled / interrupted | Explain what completed and how to retry; no still-running indicator |

Every major overlay must have a prototype fixture for each applicable state. Do not label a clarification request as failure or abstention.

### 4.4 Commands and discoverability

Preserve current command names, aliases, prefix resolution, and typo suggestions. `/sources`, `/status`, and `/show` open the corresponding views. `/add` without a path opens the folder form; a supplied path goes through the same validation and automatic-indexing action.

Proposed additions: `/scope`, `/jobs`, `/settings`, `/details`, `/reconnect <source-id>`, `/rechunk`, and `/reindex`. The palette describes each action and indicates when it is unavailable and why. Destructive actions remain in source details rather than a prominent global shortcut.

## 5. Screen-by-screen design

### 5.1 Welcome and readiness

Purpose: explain whether the user can ask questions and provide the next useful action.

For a new data directory, show:

```text
+---------------- Welcome to Docket ----------------+
| Ask questions about documents stored on this PC.   |
|                                                   |
| Ollama                 Available                  |
| Answer model           qwen3:14b — installed       |
| Embedding model        installed                  |
| Searchable documents   None yet                   |
|                                                   |
| [ Add a folder ]  [ Check again ]  [ Settings ]     |
+---------------------------------------------------+
```

On subsequent launches with ready evidence, go directly to chat. Put recoverable problems in a compact readiness notice, with Details opening the full readiness view. Keep Sources and existing evidence inspection available when inference is unavailable.

The folder form accepts a path, supports directory completion, and explains supported formats and recursive discovery. Pasted or quoted paths with spaces must work. Do not automatically select or index the current directory, home directory, or filesystem root. Adding the current directory remains an explicit choice.

Selecting Add registers the source and starts indexing automatically. The first-run screen gives way to indexing progress. On completion, show searchable coverage and an action to start asking. If indexing fails, retain the registered source and offer recovery instead of restarting onboarding.

If a required model is missing, show its name and the command to install it. Do not silently install models, start services, or download several GB from this screen. When prerequisites block indexing, retain the source and offer Retry after repair; automatic indexing means an immediate attempt, not endless automatic retries.

### 5.2 Main chat

```text
+ DOCKET | All ready sources | Auto | qwen3:14b ------+
| You                                               |
| What was July revenue?                            |
|                                                   |
| Docket                                            |
| July revenue was ... [1]                          |
|                                                   |
| Sources: [1] Revenue-FY2025-26.xlsx                 |
| Quick search · 8.2s · [Evidence] [Details]          |
|                                                   |
+---------------------------------------------------+
| Ask about your documents...                       |
+---------------------------------------------------+
| 24 files ready · 2 need attention | / Commands     |
+---------------------------------------------------+
```

Use real values from runtime state. Show the selected scope and mode continuously. A file-scoped answer also states its effective file scope so the user can see when a filename in the question narrowed retrieval.

Messages distinguish user, assistant, and system notices. Preserve Markdown paragraphs, lists, code, and tables where the width allows. Wrap long tables with a readable fallback rather than clipping values.

Show operational progress such as `Searching documents`, `Reading evidence`, `Calculating`, `Writing answer`, and `Checking citations`. These are activity labels derived from actual events, not model reasoning. Do not expose raw chain-of-thought or display an invented percentage for generation.

The first implementation displays final answer text after citation validation. Token streaming is a later enhancement because provisional text may be replaced by citation repair or abstention.

Keep answer entries with their own citations and details in session memory. Citation numbering is local to each answer. Selecting an earlier answer's citation opens that answer's evidence; `/show n` continues to address the latest completed answer.

Separate `last_attempted_question` from `last_completed_answer`. Retry targets the latest attempted question, including one that failed or was cancelled. A retry uses the selected mode and scope. Replace a completed conversation turn only after its replacement succeeds; a failed retry must not remove useful context.

`/clear` starts a fresh conversation and clears answer/citation/retry state. Explain that it does not erase the persisted command-input history. Conversation context remains bounded by the backend's existing limits; the displayed number of messages does not imply every message is sent to the model.

### 5.3 Scope and mode pickers

The scope picker starts with **All ready sources**, followed by searchable sources and a file-search entry. Show the path when source or file names collide. The first implementation supports one selected source or one selected file; arbitrary multi-selection can follow later.

All ready sources means eligible evidence under the backend's actual access/version rules, not every registered folder. Resolve current eligibility again for every question. An explicitly selected file follows that file's latest eligible version; an old answer remains associated with the version it cited.

User selection provides an outer search boundary. An explicitly named file in the question may narrow that boundary but cannot expand it. If the two conflict, explain the conflict and let the user change scope; never silently search another source. Apply the same boundary to fast retrieval, agent searches, evidence reads, and spreadsheet tools.

The intended mode picker uses **Auto, Fast, and Plan**, following part 06. Until structured planning passes its backend gate and is implemented, Plan is visible as unavailable with an explanation. Preserve existing `agent` command compatibility during migration; label it as a legacy path rather than silently treating it as Plan. Auto retains its current routing until the accuracy work authorizes a change.

When clarification becomes a backend result, show the focused question inline with answer controls and a Cancel action. The reply resumes the same attempted question and explicit scope. When Plan becomes available, its review overlay shows the objective, approved source scope, steps, and `[Approve] [Revise] [Cancel]`; `/approve`, `/revise`, and `/cancel` use the same controller actions. A revised plan requires approval again. These are capability-dependent screens, not prerequisites for shipping the initial shell.

Changing explicit scope preserves the visible transcript and draft, adds a scope-change notice, and starts fresh model conversation context. Earlier answers retain their original scope labels and citations; they are not fed back to the model across that boundary. Retry uses the currently selected scope and makes that choice visible. If a selected source/file becomes unavailable, keep that selection with an explanation instead of silently switching to All ready sources.

```text
+-------------------- Search scope -----------------+
| > All ready sources                               |
|   Finance       /work/finance                     |
|   Handbook      /work/handbook                    |
| [Find a file]                         [Close]     |
+---------------------------------------------------+
```

### 5.4 Sources and files

```text
+---------------------- Sources --------------------+
| Find:                                             |
| > Finance      18 searchable · 2 failed             |
|   Handbook      6 searchable                       |
|   Old project   Disconnected                       |
|                                                   |
| [Add folder] [Details] [Refresh] [Close]            |
+---------------------------------------------------+
```

Source details show full path, lifecycle status, latest indexing result, last successful indexing time, current ready/failed/pending/empty file counts, and retained storage information. Use ingestion job times for indexing recency; `Source.updated_at` is not a last-successful-sync timestamp.

The file list shows relative path, format, current version's processing status, searchable chunk count, and the latest recorded failure. A successfully processed file with zero chunks is `Processed — no searchable text`, not a failed file and not a searchable file.

Search and filter by Ready, Failed, Pending, and No searchable text. Open a file detail view for parser/version information, observed time, evidence location, and diagnostics. Large inventories must be paginated or incrementally loaded; do not read all document bodies to populate a list.

Source actions:

- **Refresh:** rerun incremental ingestion of the selected source.
- **Retry indexing:** rerun the source incrementally; already-ready unchanged files are skipped. Do not claim failed-files-only execution until that backend operation exists.
- **Reconnect:** revalidate a disconnected local folder, restore authorization through an explicit service transition, and automatically index it. Never create a duplicate source merely to bypass revocation.
- **Disconnect:** confirm that the source will stop being searched while stored originals remain. Invalidate current readiness and scope snapshots immediately.
- **Delete stored data:** available after disconnect; separately confirm the selected source and retained-data consequences, then use the existing hard-delete/purge lifecycle. Explain that unreferenced blobs enter the existing trash grace period. Original user files are never deleted.

Delete stored data is designed here for a later release; the first release provides reconnect/disconnect and preserves existing retained data.

Reconnection applies to a user-disconnected local folder after an explicit user action. Do not invent equivalent permission restoration for future remote connectors.

An unreachable source is shown as Unavailable with a Retry refresh action. Include recoverable `MISSING` sources in refresh-all operations so they can become reachable again. Current retrieval/resolution filters require `ACTIVE`; this redesign must not claim `MISSING` content is searchable while those filters exclude it. Any later cached-serving policy belongs to the lifecycle design.

Do not describe snapshots as live filesystem synchronization. The current pipeline does not fully reconcile disappeared individual files. Show last-indexed timestamps and disclose that limit in freshness details; changing file-deletion semantics is separate backend work.

### 5.5 Indexing and job results

```text
+---------------- Indexing Finance -----------------+
| File 7 of 24: Revenue-FY2025-26.xlsx                |
| Stage: Creating search embeddings                 |
| Elapsed: 00:42                                    |
|                                                   |
| 5 indexed · 1 unchanged · 0 failed                 |
|                                                   |
| [Hide progress] [Stop indexing]                    |
+---------------------------------------------------+
```

Stages come from pipeline events: discovering files, loading parser, reading/storing original, parsing, chunking, embedding, indexing, optional visual/formula work, and reconciliation. Show byte/hash processing only as a stage label; exact byte progress is unnecessary unless actually measured.

Use a file-count progress bar after discovery provides a total. Explain that files vary in processing time. Do not label it a time estimate or hide expensive parser/model initialization behind a frozen percentage.

Hide progress returns to chat while the operation continues in the Docket process. Keep a compact running-job indicator in the footer; `/jobs` reopens it. Completion or failure adds one short system notice, never steals focus from the user's current panel.

The proposed first implementation runs **one expensive backend operation at a time**: a query, ingestion job, re-chunk, reindex, or purge. The UI remains responsive for navigation, evidence already available, and draft editing. During indexing, explain why sending a question is temporarily unavailable; do not silently queue or auto-send a draft. Concurrent querying/indexing is a later optimization requiring dedicated consistency and model-contention testing.

Stop requests cancellation at the next safe boundary. If a parser or inference call is already executing, show `Stopping after the current operation`. Do not mark a task cancelled while its worker is still modifying data. Preserve completed files and finalize the job; unfinished versions remain unavailable and retryable. Reindex cancellation before commit preserves the existing index; its short final commit/restore section must finish safely.

Results show ready documents, newly indexed/unchanged/failed counts, files with no searchable text, elapsed time, and failure reasons with recovery actions. Distinguish unavailable root, unsupported legacy format, corrupt document, missing model, embedding mismatch, and cancellation.

Persist results so `/jobs` remains useful after a restart. Existing aggregate job rows cannot reconstruct every filename and failure; add per-file job outcome records. A job left running after a process crash is marked interrupted on recovery, rather than displayed as still active forever.

```text
+---------------------- Jobs -----------------------+
| > Finance    Partial     <time>                   |
|   Handbook   Completed   <time>                   |
|   Project    Interrupted <time>                   |
|                                                   |
| [Results] [Retry source]                 [Close]  |
+---------------------------------------------------+

+------------------- File details ------------------+
| reports/annual.pdf                                |
| Ready · <chunk count> searchable chunks            |
| Last processed: <time>                            |
| Parser/version: <actual metadata>                  |
| [Evidence] [Diagnostics]                 [Close]  |
+---------------------------------------------------+
```

### 5.6 Evidence and answer details

```text
+------------------- Evidence [1] ------------------+
| Revenue-FY2025-26.xlsx                             |
| Sales · B8:F8                                     |
| Indexed: <time> · Stored version                   |
|                                                   |
| <Verbatim source passage>                         |
|                                                   |
| [Previous] [Next] [Open original] [Details] [Close] |
+---------------------------------------------------+
```

Display the resolver's existing `location`: PDF page/section, spreadsheet sheet/range, or presentation slide/shape. Show an honest unavailable label when no location was extracted. Keep evidence text verbatim and distinguish metadata from the source passage.

Previous/Next navigates citations within the selected answer. Re-resolve before display or file access so disconnection, supersession, or deletion is respected. An unavailable citation receives a specific explanation where the database can establish the reason, and a generic unavailable explanation otherwise.

For Open original, verify the live file still matches the cited stored version before launching it. If it changed or disappeared, explain that the citation concerns a stored snapshot and offer **Save stored copy** with a chosen destination. Confirm overwrites. Use the platform's normal file opener with an argument list, not a shell command built from a filename. Opening a file does not imply support for jumping to an exact cell/page in every external application.

Save stored copy is deferred from the first release. Until available, a changed/missing original disables Open original with the snapshot explanation; the verbatim stored evidence passage remains inspectable while eligible.

Answer Details shows actual mode, elapsed time, effective scope, follow-up rewrite if used, period ambiguity, validation warnings, and whether deterministic computation was used or fell back. Put derivation/cell provenance in expandable details when present. Do not fabricate a confidence percentage or equate valid citation syntax with proven semantic support.

If an answer abstains, show known reasons and the next relevant action. Distinguish no searchable content, no retrieved evidence, unavailable evidence, and failed citation validation. For an unsupported answer with no precise diagnosis, say that the available evidence did not support an answer rather than inventing a cause.

Reuse `AnswerStatus`, `AbstentionReason`, and `TrustSummary` from `services/query/trust.py` when the backend produces them. Part 07's intended Verified/Partial/Conflict/Abstained states distinguish support from coverage. The current module defines types/helpers only; current citation validation is not sufficient to display Verified. Show structured coverage only when supplied. Until calibrated confidence is available, show `Confidence not calibrated`, never a guessed percentage. Keep clarification distinct from these answer states.

### 5.7 Settings, readiness details, and maintenance

Settings groups:

- **Answering:** installed generation-model picker, session Auto/Fast/Agent mode, and the existing answer-thinking setting with its speed/accuracy tradeoff.
- **Appearance:** dark or terminal-default/no-color and default detail expansion initially; Light, density, and animation preferences later.
- **History:** show input-history storage and its opt-out; explain session-only conversation memory.
- **System:** data path, Ollama availability, configured generation/embedding models, index manifest compatibility, and searchable coverage.

Persist appearance and explicitly saved generation-model/answer-thinking preferences in a versioned `<data_dir>/preferences.json`, written atomically. Use one effective-settings loader shared by CLI context construction: built-in defaults, then saved supported preferences, then explicit environment values. Show environment-controlled runtime fields as read-only and explain the controlling variable. `DOCKET_DATA_DIR` continues to determine the data directory; a preference cannot move its own storage directory.

Pass effective runtime settings into gateway construction and model calls. The current gateway reads some module-global settings; changing the screen label alone must not leave inference using a different model. Apply supported runtime changes only between operations, invalidate the cached query service/agent, and recheck model readiness. Do not enable a Save button for a setting whose runtime wiring is incomplete.

The embedding model is read-only in the first settings form. A mismatch detected after an environment/configuration change opens a maintenance explanation and a **Rebuild search index** action. This avoids making a model picker silently trigger a potentially expensive rebuild.

Maintenance distinguishes:

- **Refresh:** read current live files and incrementally ingest changes.
- **Update stored chunks:** preview old recipes/failed versions, then re-chunk stored originals; explain that this does not fetch changed live content.
- **Rebuild search index:** rebuild derived search indexes from stored eligible chunks under the configured embedding model; preserve the existing index until the replacement is built.

Experimental features display their effective state in advanced diagnostics. Do not turn them on by default or describe unverified formula transcriptions as citable evidence. Keep formula-review and evaluation commands available through their existing shell interfaces.

```text
+--------------------- Settings --------------------+
| Answering   Appearance   System                    |
|                                                   |
| Answer model       <installed model picker>        |
| Answer thinking    <effective setting>             |
| Embedding model    <read-only; maintenance link>    |
|                                                   |
| [Save] [Check readiness]                 [Cancel] |
+---------------------------------------------------+

+---------------- Source unavailable ---------------+
| Finance cannot be reached at <path>.              |
| Restore the folder, then retry refresh.            |
| [Retry refresh] [Details]                [Close]  |
+---------------------------------------------------+
```

## 6. Architecture and backend interfaces

### 6.1 Terminal framework

Proposed approach: extend the existing **prompt_toolkit** dependency using `Application`, a composed layout, and modal `FloatContainer` overlays. Use one event loop, not a new `PromptSession` or nested blocking input call for each panel.

Rich can continue formatting Markdown and tables into a captured renderable representation. The terminal application owns actual output; adapt Rich output to prompt_toolkit formatted content instead of printing directly over the screen. Cache transcript rendering by message and terminal width to avoid reformatting the whole conversation every frame.

This keeps the established Python CLI stack. Do not introduce Textual, a Node terminal frontend, a web server, or a desktop rewrite for this design.

### 6.2 Separation of responsibilities

Use three layers:

1. **Views:** layout, fields, transcript rendering, keyboard focus, and presentation of state.
2. **Interaction controller:** command routing, overlay navigation, drafts, operation ownership, cancellation, and state transitions.
3. **Application/services:** source actions, inventory/readiness queries, ingestion/query execution, maintenance, and persistence.

Keep reusable domain behavior out of view callbacks. Existing plain-mode handlers should call the same application actions where behavior is shared. Preserve existing test injection points while migrating; do not make tests start a real terminal or Ollama to exercise command semantics.

### 6.3 Operation execution

Move blocking service work to managed workers. Own the expensive-operation slot in the controller and create dependencies on the appropriate worker rather than racing lazy `AppContext` properties from multiple threads.

Workers publish immutable event snapshots to the application's event loop. Only that loop changes displayed state or focus. Use separate SQLAlchemy sessions per task; never pass an open ORM session between threads. Use bounded/coalesced progress updates so thousands of events cannot stall keyboard handling.

Reopen current search table handles for each new query after indexing/maintenance, or explicitly invalidate the cached query service when an operation completes. Do not rely on a table object cached before a rebuild. Guard index-mutating operations against another Docket process using a scoped operation lock with a useful busy message; reading Sources must not require that lock.

On exit with active work, offer Keep running in this terminal or Stop and exit. Stop and exit waits for safe cancellation. Closing the process does not leave a daemon running. Restore the terminal and cursor after normal exit, exception, and interrupt.

### 6.4 Proposed service additions

| Interface | Required contract |
| --- | --- |
| Readiness/inventory service | Return actual eligible file/chunk counts, source states, job recency, and manifest/model readiness without constructing the document parser |
| `QueryScope` | Typed all/source/file selection; file identity includes its source, not basename alone; resolve eligibility at execution time |
| `RunContext` / `QueryService.ask` | Extend the existing per-run context to carry explicit scope, progress, and cancellation; any new caller-facing parameter remains optional |
| Ingestion progress | Extend current events additively with stage, counts, file identity, outcome, and elapsed time; retain existing start/done callbacks |
| Operation progress | Shared UI-facing events identify the operation and its real stage; query/agent traces expose activity, not reasoning text |
| Cancellation | Optional cooperative token checked before/after expensive stages and between files/tool iterations; expected cancellation bypasses generic per-file failure handlers |
| Source reconnect | Explicit guarded transition for a user-disconnected local source; validate path and reuse the existing source identity |
| Job inspection | Aggregate history plus persisted per-file results and interrupted/cancelled outcomes |
| Effective settings | One loader with saved preferences and environment precedence; inject resolved settings into runtime dependencies |

Scope enforcement must be consistent across retrieval, resolver calls, and numeric tools. Keep new keyword parameters optional so existing CLI, evaluation, tests, and sidecar callers keep working. Add an explicit empty/conflicting-scope outcome; an empty set must not accidentally mean unrestricted search.

Use an explicit unrestricted representation (`None` or a typed All selection) separately from a resolved empty selection (`[]`). Current fast/agent callers drop empty scope filters through truthiness checks; fix both callers and the run-context default together. Reuse one operation-event schema for query activity and future plan-step progress, with optional step identity rather than a second incompatible stream.

Preserve existing abstention and citation validation. Progress reporting must not change which passages become citable. Do not convert generated tool summaries into source evidence.

### 6.5 Persistence and migrations

Add a per-file ingestion-result table linked to the existing ingestion job. Record path, result, current/attempted version ID when available, chunks written, concise error, and timing. Retain aggregate fields for existing readers. Historical jobs without file rows show `File details unavailable for this older run`.

Add explicit cancelled/interrupted job outcomes through a compatible migration; update result serialization and status readers together. Recovery of abandoned running jobs must verify that no live operation owns them before marking them interrupted, including when another Docket process is open.

Use an additive migration; preserve evidence bytes, source identities, chunks, and existing citations. No re-ingestion or index rebuild is required solely to adopt the new layout or job-detail tables.

Keep preferences separate from evidence and conversation data. Invalid preference files produce a recoverable warning and a defaults-based UI; do not overwrite the invalid file automatically. Persist input history with its existing privacy behavior and honor `DOCKET_NO_HISTORY` and `DOCKET_DEBUG`.

## 7. Existing defects to fix as part of the redesign

These are observed code-path issues, not claims established by a live end-to-end run:

1. **Reconnect dead end:** `/add` currently treats an existing revoked path as already registered without restoring it. Route that case to explicit Reconnect.
2. **Retry target/context:** update attempted-question state before execution; preserve the previous successful turn until replacement succeeds.
3. **Misleading empty-source message:** distinguish an unreachable root from a reachable folder containing no supported files.
4. **Invalid maintenance guidance:** `docket ingest --rechunk` requires a source ID or `--all`. Display an executable command, such as `docket ingest --all --rechunk`, or open the maintenance panel.
5. **Readiness based on table existence:** derive searchable counts from eligible evidence and check the index, including the case where all registered sources are disconnected.
6. **Missing-source recovery:** refresh-all currently selects only active sources. Include recoverable missing sources without including revoked/deleted sources.
7. **Hidden locations:** display the resolver's existing location metadata in evidence views.
8. **Source/model cache drift:** refresh the relevant state after indexing, reconnect, disconnect, maintenance, and settings changes.

## 8. Compatibility and honest limitations

- Bare `docket` in a capable TTY opens Screen A; `docket chat` opens the same experience. Bare `docket` outside a TTY continues to print help.
- Add `docket chat --plain` as a reliable escape hatch. Use it automatically for `TERM=dumb` or unavailable advanced terminal support.
- Interactive `/add` follows the confirmed automatic-indexing behavior in both rich and plain modes. **One-shot `docket sources add` remains registration-only** for scripting compatibility.
- Preserve one-shot query output, exit codes, aliases, evaluation subcommands, and formula-review entry points. New interactive commands must not swallow piped input in plain mode.
- Full-screen operation uses the terminal's alternate screen. The transcript scrolls inside Docket; ordinary shell scrollback returns on exit. Saved chat transcripts are not included in this first implementation.
- Indexing runs in the background of the UI process. It does not continue after the process exits, and it initially occupies the expensive-operation slot.
- Cooperative cancellation may wait for the current native parse or Ollama call. Do not claim immediate remote computation cancellation unless the runtime actually supports it.
- Health checks, file inventories, and diagnostics must be bounded. Opening an overlay must not load Docling models or scan entire folder trees unnecessarily.
- Source freshness refers to stored snapshots and successful ingestion times. It is not a claim that every local deletion or external edit has already been synchronized.

## 9. Implementation sequence after approval

Each stage leaves a reviewable result and preserves the existing CLI. The user has authorized starting; the prototype below still has an explicit visual review gate. Follow the handoff's ownership boundaries and work in an isolated branch/worktree.

| Stage | Work | Reviewable completion |
| --- | --- | --- |
| 0. Bounded baseline | Diagnose database/keyboard stalls using scratch data and process timeouts | Distinguish environment restrictions from application failures; baseline completes or fails within a bound |
| 1. Truthful CLI state | Readiness service, reconnect, correct recovery/maintenance guidance, locations, retry, cache refresh | CLI counts match eligibility and existing dead ends are fixed |
| 2. Jobs | Per-file job outcomes, additive migration, interruption ownership/recovery | Inspectable results survive restart; migration rehearsed on a backup copy |
| 3. Scope and operations | Explicit scope carried on RunContext, empty-scope enforcement, progress/cancellation, mutation ownership | Fast/agent/tools respect the boundary; operations report actual progress and safe cancellation |
| 4a. Shell prototype | Transcript, composer, palette, default theme, overlays, focus, and resize with fake data | User reviews the appearance and navigation before backend integration |
| 4b. Everyday integration | Welcome, add-and-auto-index, completion/recovery actions, scope/mode pickers | New user reaches a cited answer without copying source IDs between commands |
| 5. Evidence and details | Earlier-answer citation selection, provenance, original/stored-copy access, answer diagnostics | User can verify a passage and its actual version/location |
| 6. Settings and maintenance | Preferences, effective runtime wiring, readiness diagnostics, re-chunk/reindex, disconnect/delete actions | Displayed settings match execution; maintenance is explicit and recoverable |
| 7. Compatibility and release | Plain fallback, one-shot regressions, terminal QA, documentation, real local-model walkthrough | Acceptance scenarios below pass; limitations are documented |

## 10. Verification and acceptance criteria

Use fake gateways/parsers and temporary data directories for most automated checks. No test may open or migrate the developer's real Docket data directory. Reuse existing unit fixtures where possible.

Set `DOCKET_DATA_DIR` to a scratch directory before importing Docket, including subprocess/PTY tests. Real-model walkthroughs use scratch data or a copied corpus as well. Before adding migrations, rehearse on a copy of the verified `~/docket-backup-2026-10-03` backup; never migrate the backup itself. Keep unit/integration runs bounded and use faulthandler output to diagnose stalls.

At every commit checkpoint, run the complete unit and integration suites required by the handoff and record passed/skipped/failed counts. Any retrieval, scope, chunking, or QueryService change additionally runs the original gold set: recall@8 **33/33**, **zero wrongful abstentions**, plus the extended-set checks from part 08. Scope restriction tests also verify the selected boundary directly; all-source recall alone cannot prove isolation.

### Interaction and rendering

- Open and close each picker/overlay; preserve composer draft, selection, and transcript position; verify focus does not leak behind a modal.
- Navigate everything using a keyboard, including confirmations and long evidence passages.
- Check 120x40, 80x24, and 60x20 layouts, live resizing, wide Unicode text, long paths, multiline questions, and pasted paths with spaces.
- Check no-color/terminal-default appearance, ASCII fallback, and `TERM=dumb` plain mode.
- Use bounded pipe-input tests and a small real-PTY suite with explicit timeouts; an input test must fail rather than hang indefinitely.

### Sources and readiness

- Fresh install, existing indexed install, only disconnected sources, missing folder, recovered folder, partially failed source, and processed file with zero chunks.
- Duplicate add, reconnect, explicit disconnect, cancelled confirmation, and stored-data deletion; original files remain intact.
- Verify displayed counts use the same eligibility rules as answering. Opening Sources must not construct the parser or require working inference.

### Jobs and cancellation

- Automatic indexing after interactive add; missing model; corrupt file; unsupported legacy format; unchanged file; mixed successful/failed run.
- Hide/reopen a running job, responsive draft editing, clear indication that sending is unavailable while the expensive-operation slot is occupied, and no automatic draft submission.
- Stop between files and during a simulated long phase; retain completed work and finalize state only after the worker stops.
- Exit while busy, recover a crashed job, and reject competing index mutations from another process without corrupting ownership.
- Preserve the previous index on reindex build failure/cancellation; test commit/restore failure handling separately.

### Query and evidence

- Default all-ready search, selected source, selected file, duplicate basenames, scope conflicting with a named file, and scope becoming unavailable.
- Fast and agent paths enforce the same selected scope, including spreadsheet range/calculation tools.
- Follow-up history, failed first question, failed later question, retry in a different mode, failed retry, clear, and cancellation without adding a false successful turn.
- Valid citations, citation repair/abstention, earlier-answer evidence, superseded/deleted evidence, missing location, and a live file changed since indexing.
- Show actual scope/compute/fallback details; never invent confidence or display unvalidated text as the final answer.

### Settings and compatibility

- Saved appearance/runtime preferences, environment precedence, invalid preference file, failed save, missing selected model, and actual gateway/model consistency.
- Existing one-shot commands and output contracts; registration-only `sources add`; plain interactive commands and piped chat input.
- Migration of an existing database without losing originals/citations; older jobs without file-detail records.
- Run the relevant existing CLI, interaction, source, ingestion, query, and index tests. Add focused tests for new behavior rather than snapshots that mirror implementation details.
- Once focused checks pass, perform a real local-model walkthrough: launch, add folder, auto-index, ask, follow up, inspect evidence, reconnect a source, and recover a failed job. Record actual behavior and latency; do not claim a measured speed or accuracy improvement from a UI redesign.

The earlier inspection ran 115 focused existing tests successfully, with one test deselected. Broader attempts stalled in a database path and a keyboard-simulation path. Those results are context, not certification of this proposed implementation; investigate the stalled paths with bounded tests during implementation.

## 11. Design defaults proposed for review

These choices have not been separately confirmed and are included for approval with the document:

- Reuse prompt_toolkit for the full-screen shell and overlays; retain the plain fallback and review the prototype before integration.
- Start with one expensive operation at a time; hide/reopen progress while navigating and drafting.
- Keep conversation sessions in memory; save UI preferences and job results, not chat transcripts.
- Support all/source/file scope with single-source or single-file selection initially.
- Keep generation-model and answer-thinking preferences editable where environment values do not control them; keep embedding-model selection read-only initially.
- Provide explicit reconnect/disconnect/delete flows; no automatic retention cleanup or automatic watch service.
- Show validated final answers first; defer token streaming and concurrent query/index execution.

The user may revise individual defaults at the prototype review. First-release deferrals are Light/density/animation preferences, Save stored copy, and stored-data deletion. Plan and claim-verification screens become active only when their backend capabilities pass the gates in parts 06 and 07.

## 12. References and inspiration

- [OpenClaw TUI](https://docs.openclaw.ai/web/tui): persistent chat/status structure, keyboard navigation, pickers, and expandable activity. This is interaction inspiration, not a requirement to copy OpenClaw's agent/session model.
- [NVIDIA NemoClaw OpenClaw quickstart](https://docs.nvidia.com/nemoclaw/latest/user-guide/openclaw/get-started/quickstart): guided readiness/onboarding and recovery. In that workflow, NemoClaw launches the OpenClaw TUI; the two should not be treated as unrelated terminal screen systems.
- [prompt_toolkit full-screen applications](https://python-prompt-toolkit.readthedocs.io/en/stable/pages/full_screen_apps.html): composed layouts, focus, modal containers, and full-screen rendering.
- [prompt_toolkit asyncio integration](https://python-prompt-toolkit.readthedocs.io/en/stable/pages/advanced_topics/asyncio.html): event-loop integration for the proposed shell.
- [Source lifecycle](01-sources-and-lifecycle.md), [evidence storage](03-evidence-storage-and-versioning.md), [retrieval](05-query-understanding-and-retrieval.md), [citations](07-citations-trust-and-abstention.md), and [evaluation](08-evaluation-and-benchmarks.md): related constraints. Read their implementation updates and current source before making changes; earlier current-state descriptions can be stale.
