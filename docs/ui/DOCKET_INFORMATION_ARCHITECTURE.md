# Docket information architecture

Design proposal · 2026-11-18 · Documentation only. No UI implementation is authorized by this document.

Read with [competitor audit](UI_COMPETITOR_AUDIT.md), [design system](DOCKET_DESIGN_SYSTEM.md), and [screen specifications](DOCKET_SCREEN_SPECIFICATIONS.md). This proposal supersedes earlier dashboard concepts for UI direction only; it does not reorder engineering work.

## 1. Product model

Docket is a personal evidence workspace. Its interface should make three questions easy to answer: What can I ask about? Where did this answer come from? What is the application doing?

The central objects are **sources → documents → evidence versions → passages**, **questions → tasks → results**, and, later, **entities → located relationships** and **investigations → plans → evidence → findings**. A conversation is one way to work with those objects; it is not the container for the whole application.

Keep egui/eframe. The existing desktop uses eframe 0.37, with view logic separated from backend/model logic. No concrete blocker has been demonstrated. Accessibility, text selection, IME, and document rendering require prototype evidence, as specified in R4; visual preference alone does not justify a framework change.

## 2. Evidence baseline and status vocabulary

Read-only baseline on 2026-11-18: `rust-app` at `2633c59` in `../Local_Agent-rust`; R1 is merged. `r2a-status` was inspected at `24fb226` in `../Local_Agent-r2a`; it is separate work and must not be treated as merged. The IDE checkout is older `main`. Older planning documents calling cancellation missing or R1 unmerged are historical snapshots.

| Label | Meaning in all four UI documents |
| --- | --- |
| Existing | Implementation read on Rust mainline; test presence does not mean tests were executed in this review |
| Branch | Implementation exists on a separate branch; integration and acceptance remain outstanding |
| Engine only | Python/CLI capability exists, but desktop API or view is missing |
| Experimental | Implemented behind flags; quality/default-adoption gates remain |
| Planned | No end-to-end desktop implementation established |
| Proposed interaction | A new UI behavior assigned to an existing milestone; it is not an approved new backend capability |

Authority: `app/docketd/src/{api,tasks,worker}.rs`, `app/docket-proto/src/{api,task}.rs`, `app/docket-app/src/{main,ui,backend,model}.rs`, `backend/src/docket/interfaces/worker.py` on Rust mainline; the health/readiness code on R2a. Roadmap: [M0–M9](NEXT_PHASE_ROADMAP.md), [R1–R11](RUST_PLATFORM_ARCHITECTURE.md), [beta backlog](DOCKET_PRODUCT_BACKLOG.md). Query-worker read-only enforcement belongs to **M1**, despite contradictory older R2b table wording. Health probes being read-only does not make the query worker read-only enforced.

## 3. Stable navigation

```text
Docket                         Find command… Ctrl K
├── Assistant
│   ├── Current question / conversation
│   └── Session list (current window now; retained history later)
├── Library
│   ├── Sources
│   ├── Source detail → Documents | Processing | Details
│   └── Document detail → Passages | Versions | Original
├── Knowledge
│   ├── Entity list
│   └── Entity detail → Relationships | Documents | Provenance
├── Investigations
│   ├── Investigation list
│   ├── New investigation brief
│   └── Investigation → Findings | Steps | Evidence
├── Activity
│   ├── Tasks → selected task → event detail
│   └── Resources
└── Settings
    ├── General
    ├── Models & runtime
    ├── Privacy & retention
    ├── Permissions
    ├── Keyboard & accessibility
    └── Storage & diagnostics

Contextual: onboarding, command palette, evidence inspector,
scope picker, approval dialog, source-change confirmation.
```

These are internal view identities, not proposed HTTP routes. There is no seventh Home/dashboard destination. Assistant is the first working view; an existing installation reopens the last destination if its backing capability still exists. Persistent restoration is planned R3b/R4; it must not imply task recovery.

| Destination | User intent | Default presentation | Explicit boundary |
| --- | --- | --- | --- |
| Assistant | Ask and inspect an answer | Question-and-answer document with source margin | No persona gallery, model marketplace, or tool-builder canvas |
| Library | Understand and manage indexed material | Source/document lists with processing states | Original files are separate from Docket's retained evidence |
| Knowledge | Understand connections between material | Searchable entity/relationship table; detail inspector | A graph edge must expose provenance; a graph picture is optional later |
| Investigations | Cross-check a question across several steps | Brief, findings, ordered steps, evidence | Not a renamed transcript or a claim that today's agent is reliable |
| Activity | Understand execution and resource use | Task list and event detail; Resources tab | Metadata by default; no permanent recording of document content |
| Settings | Configure deliberate behavior | Category list and labelled forms | Per-question scope belongs beside the question, not here |

## 4. Shell and contextual depth

Use a labelled 184-point navigation column, a 48-point destination header, a local toolbar only where needed, one main work region, and at most one 320–440-point inspector. The lower navigation contains Settings and a concise service state; no decorative utilization dashboard. Place a visible command trigger at the top.

The navigation owns destinations. A destination toolbar owns its filter/search and primary action. Row menus own object actions. An inspector owns the selected object's detail. Modal dialogs are reserved for decisions, permissions, and short setup tasks. This creates predictable levels rather than several unrelated drawers.

Selecting a passage opens the same Evidence inspector from Assistant, Library, Knowledge, or Investigations. Keep its origin, selected passage, scroll position, and return-focus target. Opening another citation updates the inspector; it does not create nested inspectors. Offer an explicit full-width evidence view at narrow widths. Back returns to the previous object and filter without resubmitting a question.

Scope is always named next to the question or investigation brief. Changing scope affects the next request. Old answers retain their original scope and evidence identity. Neither a failed scope nor an empty selection silently expands to all sources. Source revocation invalidates open evidence and disables reuse immediately when observed; the backend must recheck authorization at every read.

## 5. Onboarding as a contextual checklist

On first run open a small setup sheet over Assistant, not a compulsory marketing tour:

1. Explain local processing and the actual storage/privacy state. Do not claim encryption or enforced read-only access before M1.
2. Check daemon, worker, Ollama reachability, and installed answer/embed models. Installed and resident are distinct facts. Reading checks must not implicitly download or load models.
3. Choose an existing library, view a synthetic sample in a prototype, or receive CLI source setup instructions. In-app folder registration/ingestion is deferred to R7. A future R7 flow shows folder scope and storage policy before capture.
4. Show genuine processing outcomes, including zero usable passages, partial processing, and unknown counts.
5. Ask one question and open one citation. Completion means the person inspected evidence, not merely sent a message.

Allow browsing without inference. Preserve progress only when settings persistence exists. Reopen the checklist through Help/command palette. A security policy that blocks confidential use must explain the unmet M1 requirement; it cannot be bypassed by a decorative “private” badge. Downloads, microphone permission, and connectors are separate deliberate flows.

## 6. Universal command palette

`Ctrl+K` opens a local, in-app palette; a visible button offers the same entry. This is not an OS-wide launcher. Groups: Navigate, Current selection, Tasks, Settings, then object search when read APIs exist. Typing `>` narrows to actions; ordinary input searches available commands and object names, not full document content by default. A future content-search mode is explicit and obeys the same scope boundary.

Each command declares its label, destination/object, availability reason, keyboard hint, capability, and risk. Navigation runs immediately. Object actions open the same forms/confirmations used by the pointer path. Purge, delete, send, or execute must never run directly from a search hit. Planned Act commands cannot grant themselves approval. UI commands do not become model tools merely by existing in the palette.

Arrow keys select, Enter invokes the named default, Escape closes and restores focus, Tab moves between actual controls. Selection does not execute on hover. Results have stable category order and deterministic matching; no model or network request is needed. Unsupported features do not appear as enabled commands. “Why unavailable?” entries may explain missing capabilities without promising a release date. No query text or private object names are added to telemetry or activity logs.

## 7. Capability and release behavior

The six destinations form the target IA. A design prototype may show all six with persistent **Prototype / sample data** labelling. Shipping builds show working destinations; unavailable Knowledge/Investigations may appear only in an explicitly enabled preview, with a plain explanation rather than fake data or a dead canvas. Stable order is preserved as destinations become available.

| Experience | Current foundation | Milestone ownership / release gate |
| --- | --- | --- |
| Ask, stream, inspect excerpt, Stop | Existing R1; source list and single-source scope | R4 presentation; R2a continuity/metadata; M7 reliability |
| Complete evidence view | Engine resolver exists; desktop excerpt only | R2b read APIs + R4; M7 citation metadata; M1 boundaries |
| Library transparency | Engine reports; R2a branch health/readiness | R2a + R4; counts reconcile within their own accounting unit |
| Source writes | CLI/engine only | R7 after M3; no ingestion changes in this UI work |
| Knowledge | Experimental KAG-lite engine | R2b + R4 after M3 provenance; M0 quality anchor; M5/M6 later relations |
| Investigations | Experimental Python agent | R5/R6 + M2/M3/M5/M6; M4 for retained records |
| Activity tasks | Existing R1 in-memory task list/tail | R4; R3b/M4/M1 for durable activity |
| Resources | Planned app sampler | R3a + R4; M8 measurement, no capacity promises |
| Settings and palette | Proposed UI | R4, with capability-specific R2/R3/R5/R7 APIs; M1 for privacy |
| Voice and actions | Planned, deferred | R8 and R9 respectively; R5/M1 approval and confinement gates |
| Install/onboarding | Development launcher exists | R10 packaging + R4 checklist + R2a honest health |
| Engine replacements | No new screen | R11 remains measurement-gated; no interface change should expose implementation language |

M0 and M9 are evaluation/release gates, not permanent navigation destinations. M7 failures appear inline and in Activity. M8 measurements inform Resources. Do not add menus merely because an engineering milestone exists.

## 8. Distinctions the interface must preserve

- Library processing status describes searchable evidence, not whether a folder exists.
- Knowledge describes extracted relationships; Investigations describes a bounded process and its findings.
- Activity records what happened; retained conversation history is separately opt-in.
- A citation can be available and correctly formatted without its claim being semantically verified. Use “Citations checked”; reserve “Claims checked” for a connected M2 verifier with disclosed coverage.
- `cancel_requested`, `worker_terminated`, and `inference_stopped: unverified` are separate facts. Stop must never claim that Ollama has stopped.
- R1 event replay is a bounded retained tail, not an audit log. After daemon restart, unavailable history is explained; no silent resume.

Prototype sequences and decision thresholds are specified at the end of [screen specifications](DOCKET_SCREEN_SPECIFICATIONS.md). No high-fidelity concept should precede review of those low-fidelity flows.
