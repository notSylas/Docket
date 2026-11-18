> Historical review of the rejected Mineral/Graphite iteration. Current work: [refined prototype](README.md).

# Docket native UI prototype review

2026-11-18 · interactive prototype · uncommitted, unmerged, unpublished.

Branch: `codex/ui-prototype`. Worktree: `/home/pc/Desktop/Rajdeep_Dev/Local_Agent/.worktrees/ui-prototype`.
Base: latest local `rust-app` when created, `599748a` — Merge R2a data plane: worker health, source readiness and counts, abstention metadata. The base already includes R1 and the R2a data plane; older planning documents describe an earlier checkout.

This is an egui/eframe application, using the existing 0.37 workspace dependencies and bundled fonts. It implements Assistant, Library and a contextual Evidence inspector. Knowledge, Investigations, Activity and Settings are disabled, explicitly future destinations. There is no framework switch, model change, protocol change, new application dependency or database migration.

The design baseline remains the four documents in the original checkout: [competitor audit](../../../../docs/UI_COMPETITOR_AUDIT.md), [information architecture](../../../../docs/DOCKET_INFORMATION_ARCHITECTURE.md), [design system](../../../../docs/DOCKET_DESIGN_SYSTEM.md), and [screen specifications](../../../../docs/DOCKET_SCREEN_SPECIFICATIONS.md). Those relative links depend on this nested worktree location; the documents were untracked in the original checkout and were not copied or altered here.

## Run and compare

From this worktree's `app/` directory:

```sh
CARGO_TARGET_DIR=/tmp/docket-ui-target CARGO_BUILD_JOBS=2 cargo run -p docket-app --offline -- --demo
CARGO_TARGET_DIR=/tmp/docket-ui-target CARGO_BUILD_JOBS=2 cargo run -p docket-app --offline -- --demo --theme graphite
```

`--demo` is required for an isolated visual review. It never constructs `Backend`, resolves a socket, launches a daemon, fetches sources, loads a model, reads a library, or invokes the auto-ask environment variable. Its source names, document names, passages, answers, counts and version/location labels are fictional in-memory fixtures. A persistent header marks them SIMULATED. Copied simulated content carries an explicit simulation prefix. Nothing is saved between launches.

Without `--demo`, normal mode retains the existing socket connection, daemon discovery/auto-launch, polling, scoped ask, task-capability fallback, streaming and cancellation transport in the unchanged `backend.rs`. Normal mode can launch the existing backend just as before; use demo mode for visual-only work.

Use the theme button or **Ctrl Shift T** to compare identical layouts. **Example states** and the command palette offer sample answer, empty workspace, slow response, and answer failure. These controls exist only in demo mode. Source setup help documents existing CLI commands; it executes no source operations.

## Screenshots

Native X11 captures, using llvmpipe software rendering to avoid GPU evaluation contention. These are actual egui windows, not generated mockups. Images 01–11 are explicitly simulated. The final Library refinement puts readiness on its own line to avoid fragmented labels in a narrow source list.

| Scene | Capture |
| --- | --- |
| Assistant, Mineral | [01](screenshots/01-assistant-mineral.png) |
| Assistant, Graphite, same hierarchy | [02](screenshots/02-assistant-graphite.png) |
| Library, source list and processing details | [03](screenshots/03-library.png) |
| Evidence beside the answer | [04](screenshots/04-evidence-inspector.png) |
| Universal command palette | [05](screenshots/05-command-palette.png) |
| Evidence at 640 × 520 | [06](screenshots/06-narrow-evidence.png) |
| Return to Assistant at 640 × 520 | [07](screenshots/07-narrow-assistant.png) |
| Simulated streaming with visible Stop | [08](screenshots/08-streaming-stop.png) |
| Simulated Stop with discarded text | [09](screenshots/09-stopped.png) |
| Empty workspace | [10](screenshots/10-empty.png) |
| Answer failure | [11](screenshots/11-error.png) |

Additional integration evidence: [normal transport after Stop and recovery](screenshots/12-normal-transport-fake-worker.png), [normal cancellation](screenshots/13-normal-stop-fake-worker.png). **These two images use the existing test fake worker, not genuine evidence or inference.** The normal UI correctly has no demo banner because it is using the normal backend path; the test worker supplies synthetic responses. They establish transport behavior, not answer quality or real-model cancellation.

The optional review helper `app/docket-app/tools/capture_window.py` uses system X11/XTest and Pillow, already available on this machine. These are tooling requirements only, not application dependencies. It targets and captures only the named application window, prefers its client over the window-manager frame, and can send keys, click or resize for review. It does not capture the desktop. It is X11-specific; Wayland needs a separate native capture method.

## Interaction contract

| Action | Behavior |
| --- | --- |
| Ctrl 1 / Ctrl 2 | Assistant / Library; keep the unfinished draft |
| Ctrl K | Open/close local command palette; filter by typing, Up/Down, Enter, Escape |
| Ctrl Enter | Submit the question; ordinary Enter inserts a newline |
| Ctrl Shift X | Stop, when an active question supports it |
| Ctrl Shift T | Switch theme without resetting state |
| F1 | Keyboard and source-setup help |
| Tab / Shift Tab | Move through native egui controls; Enter/Space activates a button |
| Escape | Close contextual UI; never silently stop a question |
| Change scope | Stage one source or all active sources, then explicitly Apply |
| Select a citation/source chip | Open its excerpt; Back/Close/Escape returns to the originating control |
| Library → Ask about this source | Set next-question scope and focus the composer |

The selected source is retained if it disappears or becomes ineligible. Asking is disabled until the user chooses an eligible scope; it never silently expands to all sources. Each submitted question records its scope label, independent of the next question's selection. In demo mode the in-flight result also uses the scope captured at submission.

Stop retains the existing R1 meaning: partial text is discarded, but Docket cannot confirm that Ollama stopped generating. No demo wording claims a real worker or model was stopped. Normal Stop uses the existing capability-gated backend path; a Stop requested before task creation is forwarded when the task id arrives, as before. A racing result after a Stop request is not shown as an answer.

## Reusable visual and layout components

`theme.rs` holds runtime-switchable semantic colors and global typography/focus styling. `shell.rs` holds navigation/layout thresholds and section, caption, notice and primary-action primitives. Sizes are logical points, not physical pixels.

| Token | Mineral | Graphite |
| --- | --- | --- |
| Canvas | `#F4F1EB` | `#202321` |
| Navigation | `#EAE6DE` | `#1A1D1B` |
| Surface | `#FFFFFF` | `#292D29` |
| Selected surface | `#E9E0D6` | `#38483B` |
| Text | `#252925` | `#E7E8E2` |
| Secondary text | `#565E55` | `#B3BCB0` |
| Border | `#D5D2C9` | `#454C43` |
| Accent | `#825B45` | `#ACC2AA` |
| Text on accent | `#FFFFFF` | `#202321` |
| Focus | `#315D78` | `#A6C9E2` |
| Warning | `#765418` | `#E3C286` |
| Danger | `#9B3633` | `#EBA6A0` |

Body 16, heading 24, button 14, caption/monospace 13; existing bundled proportional and monospace fonts. Item spacing 10, button padding 12 × 8, normal interactive minimum height 34. Standard buttons/inputs use visible focus strokes; selected states also use text and placement. Color alone never conveys readiness or cancellation. Compact copy controls still need target-size evaluation with users.

At widths below 900 the rail becomes a top destination switcher. At 1220 and wider, evidence is a resizable 320–440-point side inspector, initially 360. Below 1220 it becomes a full contextual view with Back/Close; no squeezed three-pane layout. Library uses a 320-point source list beside details when its content has at least 900 points available, otherwise expandable source rows. The composer remains below an independently scrollable conversation. Narrow screenshots retain the conversation's current scroll position; earlier text remains scrollable above.

## Changed files

| File | Purpose |
| --- | --- |
| `app/docket-app/src/main.rs` | Demo/theme arguments, isolated demo construction and viewport |
| `app/docket-app/src/theme.rs` (new) | Mineral/Graphite semantic tokens and typography |
| `app/docket-app/src/shell.rs` (new) | Shared layout thresholds and control primitives |
| `app/docket-app/src/demo.rs` (new) | Explicit synthetic fixtures and scoped sample results |
| `app/docket-app/src/model.rs` | Preserve health snapshot; honest readiness/count helpers; unavailable-scope guard |
| `app/docket-app/src/ui.rs` | Shell, three focused surfaces, palette, demo scenes, keyboard/focus behavior and regression tests |
| `app/docket-app/tools/capture_window.py` (new) | Optional native review capture/interaction helper |
| `docs/ui-prototype/` (new) | This handoff and native captures |

`backend.rs`, `docket-proto`, `docketd`, all backend Python, Cargo manifests/lockfile, evidence/KAG/retrieval/ingestion code and databases are unchanged. No source writes were added. M0 and R2a worktrees were not edited or run. Build artifacts are under `/tmp/docket-ui-target`; no GPU/model run was performed.

## Verification

Final checks on this prototype:

| Check | Result |
| --- | --- |
| `cargo fmt --all --check` | Pass |
| `cargo clippy --workspace --all-targets --offline -- -D warnings` | Pass |
| `cargo test --workspace --offline` | **87 passed**, 0 failed: app 14, proto 24, daemon unit 10, daemon API integration 39 |
| `git diff --check` | Pass |
| Native build and launch | Pass on X11, software OpenGL |
| Native theme, navigation, citation, Escape return, palette filtering/Enter | Pass |
| Native narrow Assistant and Evidence | Inspected at 640 × 520; headless rendering also covers 640 × 420 and 1040 × 720 |
| Slow response → Stop | Pass, simulated partial text discarded |
| Normal transport with existing fake worker | Auto-launch, ask/result, hanging request → Stop → worker restart → next successful answer |
| Backend/protocol/manifests diff | No changes |

The initial restricted test run could not create Unix sockets. The final daemon regression run used local Unix-socket permissions and existing fake workers in temporary directories; it accessed no real library or inference service. Unit tests exercise demo backend isolation, draft continuity across navigation/themes, palette execution, evidence layouts/focus return, empty/failure states, unknown counts, missing-source scope handling and submission-time scope capture. The original model regression tests remain in place.

The native normal-mode smoke test used `/tmp/docket-ui-native-smoke-20261118/docketd.sock`, the existing `fake_worker.py`, and explicit `DOCKETD_BIN`/`DOCKET_WORKER_CMD` overrides. Its task sequence completed, cancelled, then completed after worker generation 2. The app and test daemon were shut down afterward. This does not establish actual Ollama stop behavior.

Not verified: human usability, full keyboard-only traversal of every control on every compositor, screen-reader announcements, non-Latin IME, high-DPI/multiple monitors, Wayland, real-model streaming latency, real-document citation quality, or large-library performance. No claim of production accessibility compliance is made. The backend Python suite was not rerun because no Python or backend behavior changed.

## Backend limits and R2a-3 handoff

The prototype already reads the additive R2a fields present at `599748a`: readiness, version/queue/job counts, indexing health state/code/age/staleness, abstention reason and warning codes. Active lifecycle is not reported as indexing-ready. Missing or producer-unknown counts remain Unknown. Version counts, queue rows and historical job outcomes are displayed as separate accounting units. Ready/total is explicitly a version ratio, not an ETA or universal progress percentage. Source-list `files` is the existing reported usable-file field; it is not a total-discovered-file count.

For the next R2a-3 integration, agree on a capability-gated read contract before adding controls:

1. Keep the existing transport seam and source identity/scope checks. Bind the data actually supplied by that branch; do not make a second client or derive readiness from `active`, a nonzero chunk count, or process liveness.
2. Preserve producer observation time, freshness, unknown reasons, count units and reconciliation status. The prototype's “snapshot received” timestamp only measures client receipt; it does not prove the producer observation is recent. Source refresh is explicit; status polling continues on the existing path.
3. If per-file failed/skipped outcomes are supplied, add a filtered Processing detail surface with stable document/version ids, reason codes, cursor pagination and bounded payloads. Current job aggregates cannot identify filenames, and missing old job history is not “no failures.” Until that contract exists, keep the explicit aggregate-only explanation.
4. Expose new structured health/abstention codes through agreed human labels, with technical details disclosed on demand. Do not convert every health warning into a modal or disable useful keyword retrieval just because vector indexing is partial.
5. Full passage, neighboring text, document location/version and live access checks need the later authorized evidence read contract (R2b/R4, M7 metadata, M1 boundaries). Current real-mode citations contain only the answer-time excerpt and limited identifiers. The inspector labels location/version Unknown instead of inventing them, and states that the excerpt is cached. It cannot yet prove revocation freshness or implement secure original-file/PDF navigation. This remains a release gate for a complete evidence viewer.
6. Keep R1 cancellation semantics and the deterministic Assist path. Persisted history belongs to M4/R3, source writes to R7/M3, Knowledge to located provenance plus R2b/R4, and permissions to R5/R9. Do not add these through mock-only controls or reuse this in-memory UI transcript as authoritative audit history.

The displayed transcript is session-local; subsequent questions still use the existing independent `query.ask` contract rather than implying backend multi-turn memory. There is no history persistence, ingestion control, PDF renderer, rich Markdown engine, approval flow, voice, sandbox or automatic source purge notification. Citation-number markers appear inline; the keyboard-focusable source chips below the answer open evidence. Custom inline link navigation and span highlighting remain future work.

## Exact workflows to review before implementation proceeds

Use this branch for a short moderated prototype round; the following are acceptance tasks, not claims already established by screenshots:

1. **Ask within a source:** Library → inspect partially indexed source → Ask about this source → type question → Ctrl Enter. Participant must identify the request scope and distinguish the earlier question's scope after changing the next one.
2. **Check an answer:** choose each numbered source chip → inspect passage/location/version → return with Escape. Participant must identify simulated versus genuine evidence and understand that citation checks do not verify every claim. Repeat at 640 × 520 and a normal desktop size.
3. **Understand processing:** compare fully indexed, partially indexed, pending, failed, skipped and unknown history. Participant must distinguish version totals from historical job outcomes and explain why those numbers need not match.
4. **Stop and recover:** begin the slow scene → find Stop using mouse, then repeat using keyboard → confirm partial text discarded → ask again. Participant must not infer that Ollama stop was confirmed. Repeat with the fake-worker normal transport path before connecting real inference.
5. **Navigate without losing work:** leave a composer draft → Library → palette → Assistant → switch both themes → resume. Draft and scope must remain; Tab focus and Escape must return predictably.
6. **Recover from absence or failure:** empty workspace → identify source-setup route; answer failure → edit and retry; remove/revoke a selected source in a future contract fixture → verify asking blocks without widening scope.
7. **Choose the visual direction:** compare the same answer/Library/evidence in Mineral and Graphite at normal scale and increased OS scale, with keyboard-only users. Record readability, target size and focus visibility; validate screen reader and IME on target Linux desktops before calling the UI accessible.

No commit, merge or push was made. The prototype remains reviewable as working-tree changes on `codex/ui-prototype`.
