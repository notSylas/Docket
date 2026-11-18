# Docket screen specifications and low-fidelity wireframes

2026-11-18 · Design proposal only. No application code, live prototype, dependency, database, or engine behavior is changed. These wireframes precede any high-fidelity concept. Brackets indicate controls, not implemented features. All example documents, counts, tasks, and names below are synthetic fixtures.

Companions: [IA and status baseline](DOCKET_INFORMATION_ARCHITECTURE.md), [visual system](DOCKET_DESIGN_SYSTEM.md), [research](UI_COMPETITOR_AUDIT.md). Existing means read on `rust-app` `2633c59`; Branch means separate R2a `24fb226`; no test execution is claimed.

## 1. Complete screen inventory and common contract

| ID | Screen / contextual experience | Scope |
| --- | --- | --- |
| S00 | Application shell, narrow layout, disconnected state | Shared navigation |
| S01 | Assistant: new question and session list | Primary destination |
| S02 | Assistant: running, final, abstention, stopped | Same screen, distinct task states |
| S03 | Evidence inspector / full-width evidence | Shared context |
| S04 | Scope picker | Shared context |
| S05 | Library sources | Primary destination |
| S06 | Source detail: Documents, Processing, Details | Library drill-down |
| S07 | Document detail: Passages, Versions, Original | Library drill-down |
| S08 | Source setup and lifecycle confirmation | Context; R7 deferred |
| S09 | Knowledge list | Primary destination |
| S10 | Entity detail: Relationships, Documents, Provenance | Knowledge drill-down |
| S11 | Investigations list | Primary destination |
| S12 | New investigation brief | Context |
| S13 | Investigation detail: Findings, Steps, Evidence | Investigation drill-down |
| S14 | Tool approval | Shared context; deferred Act |
| S15 | Activity task list and event detail | Primary destination |
| S16 | Activity resources | Activity subview |
| S17 | Settings: General | Settings category |
| S18 | Settings: Models & runtime | Settings category |
| S19 | Settings: Privacy & retention | Settings category |
| S20 | Settings: Permissions | Settings category |
| S21 | Settings: Keyboard & accessibility | Settings category |
| S22 | Settings: Storage & diagnostics | Settings category |
| S23 | Onboarding checklist: privacy, runtime, sources, first evidence | Context |
| S24 | Universal command palette and help | Context |

Every screen has a title, context, primary task, first focus, loading/empty/failure behavior, and a route back. Shared keyboard and contrast contracts are in the design system. Empty results never fabricate records. Async updates never steal focus. No destination is allowed to contain mock content in a shipping build. In a research prototype, label sample data throughout.

Wireframes use a single shared nav, abbreviated as `NAV` after S00. Settings category navigation is local within main, not a second application sidebar. Tab labels in a frame are subviews; all shown subviews are specified below.

## 2. S00 — Shell and responsive variants

```text
┌───────────────────────────────────────────────────────────────────────────┐
│ Docket                 Assistant                         [Commands Ctrl K]│
├──────────────────┬──────────────────────────────────┬─────────────────────┤
│ Assistant        │ Destination toolbar / scope       │ Inspector header [x]│
│ Library          │                                  │                     │
│ Knowledge        │ Work region                      │ Selected detail     │
│ Investigations   │                                  │                     │
│ Activity         │                                  │                     │
│                  │                                  │                     │
│ Settings         │                                  │                     │
│ Worker: available│                                  │                     │
└──────────────────┴──────────────────────────────────┴─────────────────────┘

Compact:                            Disconnected (same shell):
┌──────────────────────────┐        ┌──────────────────────────────────────┐
│ [Menu] Assistant [Ctrl K] │        │ Cannot reach Docket service          │
│ Scope: Contracts [Edit]  │        │ [Retry connection] [Diagnostics]     │
│ Work region             │        │ Draft preserved; actions unavailable │
│ [Open evidence]          │        │ Last-known data labelled with age    │
│ [Question…] [Ask]        │        └──────────────────────────────────────┘
└──────────────────────────┘
```

Default focus is the destination heading, then the primary work control on deliberate navigation. On initial Assistant entry it is the composer. At narrow widths the inspector replaces main with a labelled Back action; the nav menu restores focus to its trigger. Destination change preserves draft, selection/filter and scroll state in memory. It does not cancel a task. Daemon loss preserves drafts but removes any claim of live readiness. Existing shell is a simpler two-panel app; six-view navigation is R4 planned.

## 3. S01/S02 — Assistant

```text
┌─────┬─────────────────────────────────────────────────────────────────────┐
│ NAV │ Assistant                  [Sessions] [New question] [Commands]     │
│     │ Source scope: All active sources [Change]                           │
│     │                                                                     │
│     │ Ask about your library                                             │
│     │ Answers link to stored passages.                                  │
│     │ [Review Library]                  No large logo or greeting         │
│     │                                                                     │
│     │ Question                                                           │
│     │ [What notice period applies to termination?                      ] │
│     │ [                                                        multiline]│
│     │ Local answer model                         [Ask · Ctrl Enter]       │
└─────┴─────────────────────────────────────────────────────────────────────┘

After submit:
┌─────┬───────────────────────────────────────────────┬─────────────────────┐
│ NAV │ Termination notice · scope: Contracts         │ Sources             │
│     │ Reading evidence · 00:08             [Stop]   │ 1  Services.pdf p.12│
│     │ Draft — citations not checked                 │ 2  Amendment p.2    │
│     │ Partial answer…                               │                     │
│     │ ────────────────────────────────────────────  │ Selected citation → │
│     │ [Follow-up draft retained]                    │ Evidence inspector  │
└─────┴───────────────────────────────────────────────┴─────────────────────┘

Same frame, terminal states:
  Final:      Answer prose [1] [2] · Citations checked · [Copy with sources]
  Abstained:  I don't know based on the available evidence.
              Recorded reason if present · [Review scope] [Library]
  Stopping:   Stop requested. Worker termination pending.
  Cancelled:  Partial answer discarded.
              Worker terminated: killed / exited / already dead (actual event)
              The model engine's stop cannot be confirmed. [Ask another]
  Timed out:  Question timed out. Partial answer discarded. [Task details]
  Failed:     Answer unavailable · typed error summary [Details] [Retry]

Sessions context:
  Current window [Session A] [Session B] [New]
  “History is not retained after restart” until retention exists.
```

**Interactions:** Ctrl+Enter submits once. Empty text cannot submit. Running task shows phase, elapsed time and Stop; citation clicks are real buttons. A subsequent draft is editable while busy, but submit explains one-running-task limits; offer queue only through supported task API, not the legacy ask route. Session switching does not stop the foreground task. Final text replaces draft, not appends to it. Retry creates a new task using the original question/scope only after a deliberate click. A cancelled draft is not promoted to a final answer or automatically put in history. A successful abstention is distinct from a service failure. Do not invent an abstention reason when the engine omitted it.

**Empty/error:** no source → CLI setup link; missing model → Models & runtime; empty selected scope → choose scope, never expand automatically. “Citations checked” never means “all claims verified.” Preserve unknown reasons. First focus after submit remains in the composer/Stop region; phase announcements are polite and throttled. Opening Sessions must be keyboard accessible and return focus.

**Mapping:** ask/stream/Stop/task states Existing R1; history Engine only and proposed R2a wiring; full evidence R2b/R4; citation notes/location M7; claim coverage M2 future; persisted sessions M4/R3b/M1. Assist stays baseline; Investigate is an explicit transition to S12 (R6), Act to a separately gated future flow (R9), not an unexplained composer toggle. Voice attachment is absent until R8 and its memory gate.

## 4. S03 — Evidence inspector and full view

```text
┌─────────────────────────────────────────────┐
│ Evidence · 1 of 2          [Expand] [Close]  │
│ Services.pdf                                │
│ Page 12 · Termination                       │
│ Stored version: v… · source: Contracts      │
│ ──────────────────────────────────────────  │
│ Verbatim passage                            │
│ “Either party may terminate …”               │
│ Located span [subtle highlight]              │
│ [Previous passage] [Next passage]            │
│                                             │
│ [Page image, when supported]                 │
│ Original has changed since capture.         │
│ [Copy passage + citation] [Open original…]   │
└─────────────────────────────────────────────┘
Full view: [Back to answer] Evidence / Services.pdf, same content and actions.
```

Citation click focuses heading and announces document/location. Copy preserves verbatim text and source identity; copied derived/neighbor text is clearly labelled. Neighbors must obey scope and lifecycle just like the primary passage. Missing location says “Location not recorded”; absent page rendering is a plain unavailable message, not a fake PDF placeholder. “Open original…” resolves an allowed path and discloses that it may differ from stored evidence. It is not permission to run arbitrary commands.

If source access is revoked or passage is purged, clear cached visible text and replace with availability explanation. Return to the originating citation on close. A quote highlight must correspond to a stored located span; do not fabricate support highlights from model prose. Passage, page, and metadata have a linear reading order. Existing 400-character excerpt is the fallback, explicitly labelled excerpt.

**Mapping:** Existing excerpt; R2b read contract + R4 viewer; M7 note/location; M1 boundaries; M3 located graph spans; later R7 lifecycle notifications. No PDF renderer dependency is chosen here.

## 5. S04 — Scope picker

```text
┌─────────────────────────────────────────────┐
│ Choose sources for the next question        │
│ [Find a source…]                            │
│ ( ) All active sources                      │
│ (•) Selected sources                        │
│ [x] Contracts        12 usable versions     │
│ [ ] Reports          4 keyword-only         │
│ [ ] Archived         unavailable            │
│ Selection: 1 source                         │
│ [Cancel]                         [Apply]    │
└─────────────────────────────────────────────┘
```

The current desktop supports one source or all. Multi-select/folder hierarchy is proposed R4 backlog B20 and requires additive R2 support; a prototype must label it planned. Apply with zero selected sources explains “Nothing selected” rather than searching everything. Disabled sources have a reason. Counts retain their unit and unknown state. Opening from Library with “Ask about this source” preselects that source but requires actual task creation to capture scope. Space toggles a checkbox; arrows navigate rows; Escape abandons picker changes, not the underlying draft.

## 6. S05 — Library sources

```text
┌─────┬─────────────────────────────────────────────────────────────────────┐
│ NAV │ Library                             [Set up a source…]             │
│     │ [Find source…] [State: all v] [Sort: name v]                        │
│     │ Source             Search readiness       Usable versions   Updated │
│     │ Contracts          Fully indexed         12                10:41  …│
│     │ Reports            Partially indexed      8                10:38  …│
│     │ Old folder         Folder missing         —                09:12  …│
│     │                                                                     │
│     │ Processing summary · sample checked at 10:42                        │
│     │ [Review processing]                                                │
└─────┴─────────────────────────────────────────────────────────────────────┘
```

Row opens S06; overflow contains only supported actions. Setup opens CLI guidance until R7, not a nonfunctional upload button. Distinguish folder/source lifecycle from search readiness. Unknown readiness renders Unknown, not green “Ready.” Refresh during a question uses genuinely cached data with age and stale indication; it does not initiate a blocking worker status call. Search/filter is local to available rows, with unknown pagination totals disclosed. No sources → source setup; filter empty → Clear filters; backend error → retry plus last-known labelled rows.

**Mapping:** Existing source list/single-source scope; R2a Branch health/readiness; R4 table/navigation; R7 source writes deferred. Privacy rules M1. Large-library UI limits measured through M8, not promised from a row count.

## 7. S06 — Source detail (three subviews)

```text
┌─────┬─────────────────────────────────────────────────────────────────────┐
│ NAV │ Library / Contracts                [Ask about source] […]          │
│     │ Documents | Processing | Details                                  │
│     │ DOCUMENTS: [Name…] [State v]                                       │
│     │ Services.pdf       Fully indexed       24 passages    [Open]        │
│     │ Scan.pdf           No usable passages   0 passages    [Reason]      │
│     │ Terms.doc          Failed               —             [Reason]      │
│     │ [Next page]   Total unknown if API cannot supply it                 │
├─────┴─────────────────────────────────────────────────────────────────────┤
│ PROCESSING subview                                                       │
│ Active versions (one snapshot): 10 total = 7 ready + 1 keyword-only        │
│                                  + 1 pending + 1 failed + 0 empty-ready   │
│ Queue: 2 pending items · 1 dead letter     [separate unit, not summed]    │
│ Latest whole-source job: 13 discovered · outcomes and unlisted bucket     │
│ [Failed] [Skipped] [Empty] rows: file | reason | recorded time             │
│ Tier/path-only job: skipped history unknown — no whole-source record      │
├───────────────────────────────────────────────────────────────────────────┤
│ DETAILS subview                                                          │
│ Path: …  Lifecycle: active  Storage policy: captured / reference           │
│ Source ID: …  Sync: not paused  Last checked: …                            │
│ [Pause refresh…] [Disconnect…]      [Purge retained data…] (later)           │
└───────────────────────────────────────────────────────────────────────────┘
```

These three labelled sections are alternate tab bodies, not stacked content in the shipped layout. Documents and report APIs are R2 work; do not infer individual filenames from privacy-limited R2a counts. Counts only reconcile within a complete, exclusive snapshot and named unit. A READY version with no chunks is anomalous/empty, never fully searchable. A RUNNING job with no writer may be interrupted, only if the actual probe establishes it. A free writer lock does not establish that every queued item failed. Unknown skipped counts are not zero; capped lists show “N not listed” or “Additional count unknown.”

First focus is source heading, then selected tab. Tabs use arrow navigation with explicit selected state; filter changes preserve tab state. Errors remain adjacent to the affected report; a failed processing report does not hide source identity. Selecting a failure shows plain reason and a diagnostic code, not a raw stack trace. Retry/reindex controls stay absent until the writer API exists.

**Mapping:** Engine reports Existing outside desktop; aggregate health/readiness R2a Branch; documents/skips R2a/R2b contract work + R4; source actions R7/M3; read-only enforcement M1.

## 8. S07 — Document detail (three subviews)

```text
┌─────┬─────────────────────────────────────────────────────────────────────┐
│ NAV │ Library / Contracts / Services.pdf                                  │
│     │ Fully indexed · stored version v…        [Ask about document]       │
│     │ Passages | Versions | Original                                     │
│     │ PASSAGES: [Find within loaded text…]                                │
│     │ 01  Introduction · page 1          [Open evidence]                   │
│     │ 02  Definitions · pages 1–2       [Open evidence]                    │
│     │ 03  Termination · page 12         [Open evidence]                    │
│     │ VERSIONS: current | captured time | processing recipe                │
│     │           earlier superseded version (availability only)            │
│     │ ORIGINAL: captured/reference policy · changed/missing disclosure     │
│     │           [Open original…] [View stored passage]                     │
└─────┴─────────────────────────────────────────────────────────────────────┘
```

Only one tab body is visible. This is an inspection surface, not a document editor. Historical-version metadata is not permission to retrieve superseded text through the active query boundary. Comparison/diff is absent until an explicitly authorized read contract and feature exist; M6 is not automatically a document-diff implementation. Missing original leaves retained evidence usable where eligible. Page rendering and text selection follow S03. Empty document explains zero passages and links to Processing. Document detail, document-specific scope, and version-list reads are Planned R2b/R4; storage/version engine exists; family semantics Experimental and M3/M6 future.

## 9. S08 — Source setup and lifecycle confirmation

```text
Current beta bridge:                 Future R7 source setup:
┌──────────────────────────────┐    ┌──────────────────────────────────────┐
│ Set up a source              │    │ Connect a folder                    │
│ Add and ingest with CLI.     │    │ Folder [Choose…]                    │
│ [Copy documented command]    │    │ Scope preview / exclusions          │
│ Return here to review state. │    │ Store copy / reference [explanation] │
│ [Close] [Review Library]     │    │ [Cancel] [Connect and index]         │
└──────────────────────────────┘    └──────────────────────────────────────┘

Future lifecycle dialog:
┌──────────────────────────────────────────────────────┐
│ Disconnect Contracts?                                │
│ Its evidence will stop appearing in new answers.      │
│ Retained data follows the stated retention policy.    │
│ Your original folder is not deleted.                 │
│ [Cancel]                                [Disconnect] │
└──────────────────────────────────────────────────────┘
Purge variant: exact retained objects/counts if known + consequence;
             distinct “Purge retained data” action, never called Disconnect.
```

Focus Cancel first for destructive operations. Escape cancels the dialog. Stop, pause refresh, disconnect, and purge are separate actions with separate copy. Pause leaves previously eligible evidence available; disconnect changes eligibility; purge removes retained data. No guessed counts in consequences. Command copying uses safe documented placeholders, never executes shell or interpolates arbitrary paths. R7/M3 own writer/lifecycle operations; backup/security behavior M1, UI R4. These are future interaction specifications, not permission to add writes during R2a.

## 10. S09/S10 — Knowledge list and entity detail

```text
┌─────┬─────────────────────────────────────────────────────────────────────┐
│ NAV │ Knowledge                       Source scope [Contracts v]         │
│     │ [Find entity…] [Type v]                                             │
│     │ Entity                 Documents  Located relationships             │
│     │ Northstar Ltd.         3          2                                 │
│     │ Supplier               —          role label, not a party identity  │
│     │ Selected: Northstar Ltd. → [Open]                                   │
└─────┴─────────────────────────────────────────────────────────────────────┘

┌─────┬────────────────────────────────────┬────────────────────────────────┐
│ NAV │ Knowledge / Northstar Ltd.         │ Evidence                      │
│     │ Variants: Northstar Limited        │ Stored located passage        │
│     │ Relationships | Documents | Provenance                             │
│     │ RELATIONSHIPS: party to → Contract │ [Source chunk]                │
│     │       location available [Read]   │                               │
│     │ DOCUMENTS: file | version | role  │                               │
│     │ PROVENANCE: extractor/version     │                               │
│     │ source/version/chunk/span         │                               │
│     │ Unlocated: filename-derived name  │ “Location not established”    │
│     │ [Ask about selected evidence]     │                               │
└─────┴────────────────────────────────────┴────────────────────────────────┘
```

Relationships are a readable table first. A future graph uses the same selection and evidence model, with no unbounded force simulation by default. Exact provenance opens S03. “Unlocated” cannot become a factual edge merely by being drawn with a solid line. Separate role words from party identities. Confidence is extractor metadata, not an accuracy guarantee. If KAG is off, explain unavailable Knowledge; do not extract on page open. Ambiguous entities remain separate and labelled; merge/split editing is not in this UI scope.

Keyboard list/relationship traversal has a table equivalent to every graph action. No matching entities → Clear filters. No located provenance → explicit warning and no factual highlight. Revoked source → remove edge and visible evidence. **Mapping:** KAG-lite Experimental engine; R2b reads/R4 views; M3 provenance and lifecycle mandatory before displaying relationships as facts; M0 evaluation anchor; M5 planner and M6 expanded relations later. Graph browsing does not make reasoning features implemented.

## 11. S11/S12 — Investigations list and brief

```text
┌─────┬─────────────────────────────────────────────────────────────────────┐
│ NAV │ Investigations                                [New investigation]  │
│     │ [Find…] [State v]                                                   │
│     │ Termination cross-check   Completed   Contracts   3 findings        │
│     │ Conflicting policy dates  Running     Policies    [View]            │
│     │ Empty: No investigations. Explain bounded work + evidence.          │
└─────┴─────────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────┐
│ New investigation                                    │
│ Question [Compare the notice terms across…         ] │
│ Sources [Choose explicitly…]                         │
│ Limits: tool calls / iterations / time [Details]      │
│ Evidence tools only · experimental quality            │
│ Retain record? [Off] (only when supported)            │
│ [Cancel]                         [Start investigation]│
└──────────────────────────────────────────────────────┘
```

Never upgrade an Assist question to Investigate silently. First focus is question; required scope and limits have ordinary labels. Start requires explicit selection/confirmation of scope; it captures an immutable run boundary. No template claims “audit complete” or “compliance verified.” If capability is absent, show one explanatory preview state, not enabled Start. Existing Python agent is not a persisted investigation system. **Mapping:** R6 after R5/M5, M6 for contradictions, M2 verification; M4/R3b/M1 retained list/history. M0/M9 determine quality/default release gates.

## 12. S13 — Investigation detail

```text
┌─────┬──────────────────────────────────────────────┬──────────────────────┐
│ NAV │ Investigations / Termination cross-check     │ Evidence inspector   │
│     │ Running · 5/14 calls · 00:42 / deadline      │ Selected passage     │
│     │ Scope: Contracts                      [Stop]│                      │
│     │ Findings | Steps | Evidence                 │                      │
│     │ FINDINGS: supported statements [1] [2]       │                      │
│     │ Missing evidence / unresolved questions     │                      │
│     │ STEPS: 1. Find agreements         complete  │                      │
│     │        2. Read notice clauses     running   │                      │
│     │        3. Cross-check dates       pending   │                      │
│     │ EVIDENCE: document | location | read step   │                      │
│     │ Verification coverage: actual checks only   │                      │
└─────┴──────────────────────────────────────────────┴──────────────────────┘
```

Tabs display alternate bodies. Steps expose action descriptions, tool names, bounded argument summaries, outcomes and evidence links, never hidden chain-of-thought. Error/budget exhaustion produces a clearly partial result, not a complete-looking finding list. Contradictions require actual M6 output with both sources; the UI cannot infer conflict from different wording. Approval pause, when applicable later, is linked visibly to S14 and budget clock semantics are explicit. Stop follows the exact R1 facts; cancelled partial findings are not exported as completed work. Reopening after restart explains lost state; a future “Restart from brief” is a new task, not silent continuation.

**Mapping:** R5 tool events, R6 integration; M2 claim checks, M3 provenance, M4 records, M5 plans, M6 conflict/authority. R1 supplies task cancellation only, not these richer events. Export is future R9 approved action and must identify partial/complete state; it is not an implicit R6 feature.

## 13. S14 — Tool approval

```text
┌──────────────────────────────────────────────────────────┐
│ Approve writing this report?                             │
│ Investigation: … · Tool: export_report · Risk: WriteLocal │
│ Destination: /chosen/folder/report.md                     │
│ Exact validated arguments [Show]                         │
│ Existing file: no / overwrite consequence if yes          │
│ Approval expires at …                                    │
│ [Deny] [Allow once] [Allow for this session…]              │
└──────────────────────────────────────────────────────────┘
```

Deny initially focused; Escape is not approval and leaves a clear pending/deny policy. Approval is bound to call ID, task, nonce, exact validated arguments and expiry. Editing arguments invalidates approval. Allow-session is only offered for supported WriteLocal operations inside the designated folder; never blanket Network/Exec. Voice cannot approve. On expiry or unavailable broker, no execution. A changing background selection cannot change what this dialog approves. **Mapping:** Planned R5 broker/UI, R9 sandbox and Act tools, M1 policies, M4/R3b audit. Existing Python read-tool allow-list is not this approval system.

## 14. S15/S16 — Activity and resources

```text
┌─────┬──────────────────────────────────────┬──────────────────────────────┐
│ NAV │ Activity     Tasks | Resources        │ Task t-…                    │
│     │ [State v] [Kind v] [Time v]            │ Current state: Cancelled    │
│     │ Ask t-12    Cancelled    00:08         │ 10:42 cancel_requested:user │
│     │ Ask t-11    Succeeded    00:12         │ 10:42 worker_terminated:…   │
│     │ Ask t-10    Timed out    05:00        │ 10:42 inference_stopped:    │
│     │ [Select → event detail]               │       unverified            │
│     │ Current daemon session only           │ [Expand event metadata]     │
└─────┴──────────────────────────────────────┴──────────────────────────────┘

┌─────┬─────────────────────────────────────────────────────────────────────┐
│ NAV │ Activity / Resources                         Checked at 10:43       │
│     │ Process          RSS        PSS           Measurement state         │
│     │ Desktop          … MiB      … MiB         measured / unknown       │
│     │ Daemon           … MiB      … MiB         measured / unknown       │
│     │ Python tree      … MiB      … MiB         measured / unknown       │
│     │ Ollama           separate process/service                           │
│     │ Model            Installed   Reachable    Resident   VRAM          │
│     │ qwen3:14b        yes         yes          unknown    —             │
│     │ Disk and queue: values only when measured; units always labelled   │
└─────┴─────────────────────────────────────────────────────────────────────┘
```

Select task without exposing raw questions by default. Event detail never turns transient result/delta bodies into a permanent metadata log. Replay requests use sequence IDs; if a retained tail starts later than requested, show a gap, never claim complete history. Daemon restart resets in-memory history; durable task recovery remains future. Task-list Stop targets that task explicitly. Resources require sample age and attribution; process-tree RSS may double-count shared pages, so PSS is separately named. Installed/reachable/resident cannot collapse into one model-ready icon. An unavailable sampler displays Unknown; no zero-valued gauge. Keyboard lists and expanded event metadata remain selectable/readable.

**Mapping:** S15 R1 task API Existing, R4 view Planned; durable activity R3b/M4 after M1. S16 R3a Planned; R2a Branch model health; M8 measurement context. R8 adds optional helper visibility later; R11 migrations require before/after evidence. Resource controls that evict/load models are not authorized by read-only monitoring.

## 15. S17–S22 — Settings category wireframes

```text
Shared settings frame:
┌─────┬─────────────────────┬───────────────────────────────────────────────┐
│ NAV │ Settings            │ Category title                                │
│     │ General             │ Labelled controls + consequences              │
│     │ Models & runtime    │ Inline validation / unavailable reason        │
│     │ Privacy & retention │                                               │
│     │ Permissions         │ [Cancel changes] [Apply] where persistence    │
│     │ Keyboard & access.  │ and update API exist                          │
│     │ Storage & diag.     │                                               │
└─────┴─────────────────────┴───────────────────────────────────────────────┘

S17 General                     S18 Models & runtime
┌────────────────────────────┐ ┌───────────────────────────────────────────┐
│ Theme [System v]           │ │ Answer model: qwen3:14b (locked decision) │
│ Density [Comfortable v]    │ │ Embedding: qwen3-embedding:0.6b           │
│ Startup destination [v]    │ │ Installed / reachable / resident + age    │
│ [Preview scale]            │ │ Timeout [value] (read-only until API)     │
│ Current session only if    │ │ Changing embedding model needs reindex.  │
│ settings persistence absent│ │ [Refresh checks] [Diagnostics]             │
└────────────────────────────┘ └───────────────────────────────────────────┘

S19 Privacy & retention         S20 Permissions
┌────────────────────────────┐ ┌───────────────────────────────────────────┐
│ Storage protection: actual │ │ Assist: deterministic evidence pipeline   │
│ state / policy unmet       │ │ Investigate: evidence tools only          │
│ Retain conversation [Off]  │ │ Act: unavailable until broker + sandbox  │
│ Retention [period]         │ │ Tool | risk | allowed mode | scope        │
│ [Forget this session…]     │ │ Session grants [Revoke…] (when supported)│
│ Audio storage: off (future)│ │ No permission widening from model output │
└────────────────────────────┘ └───────────────────────────────────────────┘

S21 Keyboard & accessibility   S22 Storage & diagnostics
┌────────────────────────────┐ ┌───────────────────────────────────────────┐
│ Text scale [100%]          │ │ Library location: …                       │
│ Reduced motion [System]    │ │ Store mode: capture / reference           │
│ High contrast [choice]     │ │ Usage: measured / unknown                  │
│ [Find shortcut…]           │ │ [Doctor results] [Copy safe diagnostics]   │
│ Command | binding | edit   │ │ [Backup guidance] [Restore guidance]       │
│ Conflict [Reset binding]   │ │ Data relocation unavailable; explain       │
└────────────────────────────┘ └───────────────────────────────────────────┘
```

**S17:** theme/density/text scale may preview locally and cancel reversibly; label session-only if no persistence. Proposed R4, durable settings R3/R4. Typography change never removes focus. Startup restoration does not resume tasks.

**S18:** reads models and runtime; no automatic model download/load, no reopening the locked model bake-off. Show configured values as read-only until a settings update API exists. Distinguish connect/read/overall limits if M7 exposes them. Invalid values retain user input and identify the field. Proposed R4; R2a Branch reads; M7 reliability; R3a residency/resources. Any future configuration write needs a daemon-owned contract.

**S19:** explain actual plaintext/encrypted-volume state and what retained text means. No default capture of questions, passages, prompts, audio or hidden reasoning. Forget requires supported cascade deletion; a disabled control cannot imply data was erased. M1, M4, R3b/R4; R8 voice controls later. Retention setting is absent or read-only until implemented. “Local” never substitutes for “encrypted.”

**S20:** read-only summary before R5. No checkbox that pretends to sandbox a tool. New grant/revoke operations must match broker semantics and active-call handling; removal of a grant must not retroactively claim an action was prevented. R5/R9 and M1; audit M4/R3b. Screen reader reads the risk and path boundary with the permission name.

**S21:** explicit reset and shortcut conflict detection. Recording shortcut enters an announced mode; Escape exits without global commands firing. Accessibility settings must be real, not display-only toggles. R4; packaging fonts/platform semantics R10. Include F1 help accessible without knowledge of F1.

**S22:** doctor and backup/restore exist in CLI; the desktop can show guidance before control APIs. Copy diagnostics is redacted, reviewable, and excludes questions, paths/secrets unless deliberately included. Restore, relocation and purge are not generic “repair” buttons. R4 display, R7 writer contracts, R10 packaging, M1 encrypted-backup/security. Old backups and originals must never be silently changed by a settings action.

## 16. S23 — Onboarding checklist wireframes

```text
┌────────────────────────────────────────────────────────┐
│ Set up Docket                            [Browse first]│
│ 1 Privacy → 2 Runtime → 3 Sources → 4 First evidence    │
│                                                        │
│ PRIVACY: Local processing; actual retention/protection. │
│ RUNTIME: daemon / worker / Ollama / installed models    │
│          unknown and stale states retain timestamps    │
│ SOURCES: existing library [Review] / CLI setup [Copy]   │
│          future R7 folder preview is separately gated  │
│ FIRST EVIDENCE: ask, then open a real citation          │
│                                                        │
│ [Back]                         [Continue / Open Library]│
└────────────────────────────────────────────────────────┘
```

Each step is a distinct checklist body in this shared frame. Start focus at step heading. Runtime failure provides a concrete diagnostic action; Library stays browsable. No empty “all set” state if source processing produced zero passages. Do not auto-index an existing folder on discovery. Sample data exists only inside a labelled prototype, never masquerades as the user's library. Reopen from palette/help; interruption preserves progress only if supported. R4/R10 setup, R2a Branch checks, R7 deferred ingestion, M1 privacy, M9 later user validation.

## 17. S24 — Universal command palette and help

```text
┌────────────────────────────────────────────────────┐
│ [Find a command or available object…             ] │
│ Navigate                                           │
│ > Library                                  Ctrl 2  │
│   Activity                                 Ctrl 5  │
│ Current selection: Services.pdf                    │
│   Open evidence                           Enter    │
│   Ask about this document                          │
│ Tasks                                              │
│   Stop task t-12                    Ctrl Shift X   │
│ [Unavailable actions and reasons, only on request] │
│ ↑↓ Select · Enter Open · Escape Close               │
└────────────────────────────────────────────────────┘
Help variant: shortcut table + “Set up Docket” + “Diagnostics”;
              search filter, close, and return-focus behavior unchanged.
```

Enter invokes a visible named default; no hidden “best guess” action. Do not rank destructive actions as the default. Palette actions dispatch through the same validation as buttons. Search entry is local; matching private names must not become logs. Empty match → explain and offer Clear search. Capability loss removes an action or disables with reason before activation. Current-selection context is announced; no accidental action on a newly selected background item. Navigation/help Planned R4; object reads R2b; task Stop Existing R1 API; setting actions depend on their R3/R5/R7/M1 contracts. This palette is a UI mechanism, not an external MCP client or OS launcher.

## 18. Cross-screen engineering traceability

| Milestone | Screen/interaction coverage | Implementation distinction / gate |
| --- | --- | --- |
| R1 | S02 Stop/states; S15 task list/replay; S24 Stop | Existing API and Stop foundation; new layouts Planned; no confirmed inference-stop claim |
| R2 (R2a/R2b) | S03–S07 evidence/source reads; S09/S10 graph; S18/S23 health; continuity S01 | R2a partial separate Branch; R2b Planned; no worker protocol-v2 claim from additive status fields |
| R3 (R3a/R3b) | S15 retained events, S16 resources, S19 retention | Live samples and persistence are different slices; metadata policy M1 |
| R4 | S00–S24 view composition, palette, selection, accessibility | All revised views Proposed; capability-gated delivery, no backend work assumed |
| R5 | S13 tool steps, S14 approval, S20 permissions | Planned broker/events; Python gateway exists but cannot satisfy Rust approvals |
| R6 | S11–S13 investigation workflows | Planned integration; agent quality is Experimental |
| R7 | S08 source writes, S06 pause/reindex/purge, S22 writer operations | Deferred after M3; CLI bridge first |
| R8 | Optional microphone/transcript context in S01, S19/S16 | Deferred; no new destination or voice approval |
| R9 | S14 approved export/action and S20 confinement | Deferred; deny when unsupported, no generic shell command |
| R10 | S23 setup; S22 diagnostics; S00 startup/shutdown | Planned packaging and clean-install gate |
| R11 | S16 resource evidence, runtime diagnostics only | No new view required; replacement depends on accuracy/resource measurements |
| M0 | Synthetic prototype content and quality language across S02/S10/S13 | Evaluation work protected; no new benchmark or judge run in UI task |
| M1 | S03 scope/lifecycle, S08 destructive flows, S19/S20/S22 | Security minimum includes enforced worker read-only; not established today |
| M2 | S02/S13 explicit verification coverage | Claim-verification UI only when actual verifier connected; no trust inflation |
| M3 | S06/S07 lifecycle; S10 per-edge provenance; S08 writes | No stale/eligible orphan graph facts; graph display gated |
| M4 | S11/S13/S15/S19 durable investigation/audit records | Planned; joins activity by identifiers, content stays evidence-owned |
| M5 | S12 bounded brief, S13 typed plan | Planned constrained planning before autonomous investigation |
| M6 | S10 relations, S13 conflict/authority | Planned; no inferred conflicts or fabricated relation types |
| M7 | S02 timeouts/retry, S03 citation metadata, S18 settings | Python timeout/retry gaps remain; R1 hard-kill is not cooperative cancellation |
| M8 | S16 measured limits, S05–S07 growing lists | Performance evidence before capacity claims |
| M9 | Prototype/pilot feedback on S01–S10; security-gated real-document pilot | No private documents used in this design task |

## 19. Exact workflows to prototype and test before implementation

First use paper/static frames or a design-tool click-through made from these wireframes, with synthetic data. No functional egui code is part of this task. After low-fidelity revision, create high-fidelity concepts using the same content; only then authorize a small egui feasibility spike. Proposed sample: 5–8 target Linux users including evidence-heavy readers, one keyboard-only participant, and an assistive-technology user where available. This is formative research, not a statistically representative accuracy evaluation.

| Order / workflow | Exact sequence and injected case | Observe / proposed pass criterion | Engineering gate |
| --- | --- | --- | --- |
| P1 First useful evidence | S23 runtime check → missing model → browse Library → CLI setup explanation → partially indexed source → S01 question → S02 result → S03 citation | At least 4/5 can explain what is searchable and inspect a passage without coaching; nobody believes “installed” means “resident” or “encrypted” | R2a/R4/R10; M1 before confidential pilot |
| P2 Read and verify | S01 scope Contracts → ask → S02 draft/final → open citation 2 → S03 neighbor/location → copy passage with citation → Back | At least 4/5 find the supporting passage in ≤30 s; all distinguish draft vs final and citation availability vs claim verification | R2b/R4/M7; M2 language |
| P3 Stop and recover | S02 slow draft → Stop → cancel accepted → delayed worker termination → inference status unverified → ask another question | Every participant understands the engine stop is unconfirmed; no cancelled text treated as final; next-question path discoverable | Existing R1 semantics, R4 presentation |
| P4 Explain missing files | S05 Reports → S06 Processing → inspect keyword-only, empty-ready, failure, unknown skipped history, capped list → S07 | At least 4/5 locate the reason in ≤45 s; all avoid summing version/queue/job counts or interpreting unknown as zero | R2a/R2b/R4 |
| P5 Follow-up and scope | S02 answer from Contracts → draft follow-up → switch source → see new scope → cancel scope change → send → inspect evidence; inject revoked source | No silent scope widening; draft preserved; revoked passage cleared; users know old answer retains original scope | R2a continuity; R2b/R4; M1 |
| P6 Understand Knowledge | S09 select entity → S10 relationship → located evidence → unlocated filename-derived name → return to entity list | Every participant can identify the evidence for a located edge and knows an unlocated label is not a verified relationship | M3 before R2b/R4 graph exposure |
| P7 Distinguish investigation from activity | S11 → S12 brief/limits → S13 steps/findings → budget exhaustion → S15 event detail → return | At least 4/5 choose Investigations for findings, Activity for runtime failure; partial work never mistaken for complete | R5/R6/M2/M4/M5/M6 |
| P8 Review an action safely | S13 export request → S14 exact destination → deny → allow-once new call → changed args/expired call rejected; session permission variant | Zero unintended approvals; all can state destination and effect; Ctrl+Enter/voice never approve | R5/R9/M1 |
| P9 Navigate by keyboard | From S01 Ctrl+K → Library → source → evidence → close → Settings → shortcut help → return to preserved draft | All controls reachable, visible focus throughout, no focus loss/trap outside modal, Escape never cancels work | R4; later native feasibility test |
| P10 Degraded runtime and restart | Running S02 → stale health → S15 → daemon loss → reconnect → retained-tail gap → S18 diagnostics | Unknown/stale distinct; nobody assumes durable history or resumed work; draft remains available | R1/R2a/R3b/R4/M7 |
| P11 Small window and assistive use | Repeat P2/P4/P9 at 640×420, 200% text, keyboard-only; later Orca and IME on X11/Wayland | No unreachable control, lost evidence text, broken composition, obscured focus, or forced graph-only interaction | R4 egui blocker assessment |
| P12 Visual system comparison | After P1–P11 low-fi review, same S02+S03 and S06 in Mineral/Graphite, narrow/error variants | Reading comfort, hierarchy, legibility, focus and source discovery assessed before aesthetic preference; calculated contrasts pass | High-fidelity sign-off before code |

Thresholds are proposed research criteria, not measurements. Record task completion, wrong turns, time to evidence, trust misinterpretations, scope errors, accidental actions, and verbatim participant comments. If any trust/scope/approval misunderstanding occurs, revise copy/layout and repeat the affected flow. If Knowledge and Investigations are confused, revise local labels and entry explanations without collapsing the six-destination model prematurely. Obtain explicit design selection only after reviewable low-fi and matched high-fi artifacts exist; this documentation does not imply those tests have happened.
