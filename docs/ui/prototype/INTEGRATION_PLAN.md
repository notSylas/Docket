# Approved UI → rust-app integration plan

Status: **review required; implementation has not begun**. Inspected 2026-11-18. This is a presentation integration, retaining egui/eframe and the approved Obsidian & Silver / Porcelain & Ink themes. No redesign, backend feature integration, migration, dependency update, commit, merge or push is authorized by this document.

The target must be the current `rust-app` application with selected prototype components applied to it. Do not replace the live desktop with the older prototype tree.

## 1. Inspected baseline and worktree boundaries

| Checkout | Branch / HEAD | Observed state | Treatment |
| --- | --- | --- | --- |
| `Local_Agent-rust` | `rust-app`, `3a6a4ca2d9e809c15551623428626c724283c810` | Clean | Integration target; inspected read-only for this plan |
| `Local_Agent/.worktrees/ui-prototype` | `codex/ui-prototype`, `599748a` | Modified `main.rs`, `model.rs`, `ui.rs`; untracked `theme.rs`, `shell.rs`, `demo.rs`, capture tools and documentation | Approved visual source; preserve all existing uncommitted work |
| `Local_Agent` | `main`, `eb00abc` | Existing modified/deleted Upgrade documents and untracked docs/worktrees | Not an integration base; leave unrelated changes alone |
| `Local_Agent-m3` | `m3-1-orphan-entities`, `6868dc1` as listed by Git | Its working state was not inspected | Do not enter, change or run it |

The prototype predates three material additions now on the target:

1. `5027323`: M0 phase 2b GPU-lane tooling. No UI dependency; no evaluation should be launched for this integration.
2. `25d5f1d`: R2a-3, including `d5b84f6` pure status model and `1471c0f` desktop bindings. The live desktop now has separate status chips, banners, explicit Ask-off reasons, source freshness/readiness, answer metadata and improved polling. These are missing from the prototype's control model.
3. `3a6a4ca`: M3-1 merge of `6868dc1`. This removes unreferenced KAG entities/variants transactionally during source purge, supersede/prune, document prune and rebuild-replace; adds the CLI graph cleanup command and lifecycle tests. Shared entities remain. It adds **no desktop, daemon, citation or graph-read API**, and makes no secure-erasure guarantee. It is a completed lifecycle slice, not completion of all M3 provenance work.

The range `599748a..3a6a4ca` changes 39 files, including live desktop `backend.rs`, `main.rs`, `ui.rs`, new `status_view.rs` and its tests. `model.rs`, the protocol crate and desktop Cargo manifest have no changes in this range. The target daemon's new soft health timeout is particularly important: a health timeout marks cached sections stale with `health_timeout`; it does not kill/restart the worker. The default is now 2 seconds. Preserve it exactly.

Recheck target HEAD, dirty state and `git diff 3a6a4ca..HEAD -- app/` before implementation approval is acted on. If new merges alter contracts, update this plan before transferring affected components. Do not reset, stash, cherry-pick or rebase the dirty prototype to obtain the visual code. Use reviewed file/hunk extraction against its recorded base, with backups of existing changes.

## 2. Code-grounded reuse map

Paths in the next table are relative to `app/docket-app/src/`. Line anchors identify the inspected snapshots and will move after integration.

| Component | Approved prototype evidence | Integration decision |
| --- | --- | --- |
| Two theme token sets, typography and global style | `theme.rs` (232 lines), contrast test at `:194` | Reuse without palette changes. Map live semantic `Tone` values into these tokens; do not carry over live hard-coded colors. Keep native fonts and dependency set. |
| Shared controls, icons, layout policy | `shell.rs` (309 lines); breakpoints `:29`, citations `:273`, response heading `:301` | Reuse. Navigation width 216, sidebar from 900, side inspector from 1220, inspector 340–480. Test geometry after real status content is added. |
| Navigation and contextual commands | `ui.rs:431,460,523,578,1612,1715` | Extract shell, palette, appearance dialog and shortcuts. Assistant/Library active, Settings appearance-only; Knowledge/Investigations/Activity clearly planned. Do not imply those destinations work. |
| Conversation and source controls | `ui.rs:810,839,910,958` | Reuse reading hierarchy, selectable prose, citation links, Copy and collapsed diagnostics. Feed actual `AskResult` and R2a `answer_meta`; never raw code strings or demo output. |
| Evidence Inspector | `ui.rs:901,1052,1810` | Reuse selection, resize, close/Escape and narrow contextual view. Live content is a bounded answer-time excerpt only; see section 5. |
| Composer and draft continuity | `ui.rs:278,288,311,691,714` | Reuse appearance and next-draft behavior; bind to the live Ask gate and R1 reducer, not the prototype's simplified connection predicate. |
| Library and onboarding | `ui.rs:1121,1186,1210,1290,1374,1495` | Reuse layout and calm empty-library guidance. Readiness, anomalies, accounting and freshness must come from live R2a-3 view models. Setup remains instructions, not an executed writer action. |
| UI state helpers | Prototype `model.rs:252–300` | Port only helpers still needed after adopting `status_view`. Avoid duplicate readiness logic. Preserve existing citation-numbering and cancellation reducer tests. |
| Live transport | Target `backend.rs:114–245`; prototype backend is still the old base | **Keep target implementation**, including typed `FetchError`, asynchronous source refresh, last-good consumers, `set_asking`, tasks/legacy branches and daemon ownership. No prototype backend replacement. |
| Live status semantics | Target `status_view.rs` (1,237 lines), `status_view/tests.rs` (895 lines) | **Keep intact initially**. Restyle consumers without changing semantic output or deleting tests. |

Presentation can stay as modules around the existing `backend` / `model` / `status_view` seam. If splitting the 2,122-line prototype `ui.rs`, use small view modules within `docket-app` (Assistant, Library, Inspector, shell); state and event reduction remain in one owner. Avoid a broad refactor before parity is established.

### Demo isolation inventory

The following must not supply any value to live mode:

- All of `demo.rs`: Northstar text, seven sample sources, synthetic chunk IDs, source counts, fictional locations/versions, scoped answers and abstentions.
- `DocketApp::demo_scene`, `load_sample_library`, `demo_slow`, elapsed-time simulated phases/deltas/results and demo-specific Stop behavior.
- Sample-passage actions in Library, simulated-answer clipboard text and simulated metadata in Inspector.
- Demo-only palette commands and Settings scenarios, sample-library onboarding action, simulated connection/footer state and ignored auto-ask behavior.
- Prototype app ID/title, screenshot scenario setup and `tools/capture_window.py`; these are review tooling, not production startup behavior.

Keep the existing prototype as the visual fixture. Recommended initial live integration: do not transfer `demo.rs` or demo branches at all; transfer presentation with real typed state inputs. Tests can construct explicit fixtures under `#[cfg(test)]`. If a shipped demo option is later wanted, review it separately and prove that mode selection precedes any backend/socket/auto-launch work. Live failures must never trigger sample fallback. No sample imports, timed responses or synthetic metadata in normal startup, empty states, clipboard or error recovery.

## 3. Ownership and compatibility boundaries

| Owner | Authority retained | UI integration may do |
| --- | --- | --- |
| Python evidence/query/lifecycle engine | Retrieval, citations, abstention, KAG, source lifecycle and evidence database | Display existing outputs; no Python changes or schema writes |
| Daemon/protocol | Task admission, cancellation facts, deadlines, worker generation, process-group cleanup, health cache and socket safeguards | Use existing client methods; no route, timeout, process or protocol changes |
| Desktop transport | Background I/O, capability branching, polling and owned-daemon shutdown | Preserve target implementation and wire state updates |
| Desktop views | Themes, layout, focus, selected citation, draft and in-memory conversation | Change presentation and test pure state transitions |
| M3 owner | Future provenance and graph lifecycle work | Consume already merged effects only; no worktree or pending-feature integration |

No claim verifier, permission broker, persistent history or ingestion control is introduced. Neither theme nor conversation is persisted by this pass. Navigating away and back preserves window-local state; restarting the app does not restore it. No new settings store or SQLite activity log.

## 4. State binding: R1 and R2a-3

### R1 lifecycle

Keep target `Backend::ask` capability selection: `tasks.v1` uses task creation/event stream; absence uses `/v1/ask`. `Pending::set_task`, `request_stop`, `stop_failed` and `task_step` remain the task-state authority. Stop before `TaskStarted` must be remembered and dispatched once the ID arrives. Cancel failure must remain visible and permit retry; do not silently clear the running task.

| Backend/UI fact | Approved component behavior |
| --- | --- |
| Phase / Delta | Stream dimmed provisional text, no trusted citation actions; show retrieval/reading/generation/validation progress |
| Result | Replace provisional text with final `AskResult`; preserve abstention and supplied warnings |
| Stop requested | Disable duplicate Stop; discard provisional display; preserve next draft, submitted question and submitted scope |
| Cancelled terminal event | Show existing `STOPPED_TITLE` / `STOPPED_NOTE`; enable next Ask only when actual service/worker state permits |
| Stop delivery failed | Show action failure, keep active task and restore the appropriate Stop affordance |
| Failed / timed out | Preserve distinct task outcome from cancellation; show failure rather than an answer |
| No `tasks.v1` | Ask through legacy transport; hide or explain unavailable Stop. Escape closes contexts, never masquerades as cancellation |

Do not turn `cancel_requested` into “worker stopped.” R1 distinguishes request accepted, worker terminated, and `inference_stopped: unverified`; the UI must not claim Ollama stopped. Existing backend reduction hides some intermediate cancellation facts, so use “Stopping…” until terminal confirmation and existing conservative terminal wording. Do not add optimistic model-stop status.

The prototype guards against an Answer delivered after local Stop intent. Preserve and test this UX guard alongside daemon cancellation-race tests; define event ordering so a completed answer already accepted by the UI is not retroactively relabelled cancelled. Never publish a late result for a task already cancelled. Test StopFailed followed by a legitimate Result as well.

### R2a-3 health and source state

Keep `StatusModel`, `StatusView`, `SourcesState`, `source_view` and `answer_meta` from the current target. Recompute the view with monotonic elapsed time and request age repaints (currently once a second). Do not convert receipt time into producer freshness.

- `StatusModel::apply` keeps last-good data after polling errors. Local silence over 10 seconds marks service unavailable; source age over 30 seconds is old. Section age adds elapsed time to reported age; missing age/stale flags are not fresh.
- Service, Worker, Model, Index and Database retain separate chips and details. Source readiness stays in Library; “connected” is never a replacement for readiness. Installed, reachable and resident model facts remain distinct.
- Render chips in a wrapping strip using theme tokens, with keyboard-accessible detail disclosure. Freshness and meaningful warning/error summaries remain visible, including on narrow windows. Advanced accounting can collapse; current failures cannot be hidden only in hover tooltips.
- Retain actionable banners from `StatusView`. Normal connecting/checking/empty states use neutral presentation. A successful empty source list may show onboarding; an unreachable service or failed source refresh must not be presented as a new empty library.
- Preserve target `AskGate` and its exact reasons for v2. Add only local guards: nonempty question, no pending task, valid selected scope, and a successfully observed empty library. Unknown or partly-ready sources must not be globally blocked based on an invented readiness rule. Model/index warnings alone do not currently disable Ask; retain that distinction.
- Without v2, retain the existing worker/service fallback, plus the explicitly tested local empty/scope guards. Do not infer health sections from old fields.
- Keep target `backend.set_asking(...)` wired throughout the task lifecycle: status polling every 2 seconds; periodic source refresh every 10 seconds for v2 while idle; source fetch on its own thread so it cannot block status polling; one automatic source fetch in flight. Legacy keeps its current load-once behavior. Do not introduce extra render-triggered requests or continuous evidence polling.
- `SourcesState` keeps last-good rows after a failed refresh, marks them old and displays age. Reuse `source_view`: it handles contradictory “ready” reports, `empty_ready`, unknown counts, inactive lifecycle, anomalies, job outcomes and separate historical counts. The prototype's shorter `readiness_label` is insufficient.
- Use `answer_meta` for recorded abstention reasons, warnings, coverage and file count; unknown codes receive its fixed generic labels. The prototype directly renders some raw reason/diagnostic fields; do not port that bypass. Preserve safe closed-code rendering and hostile-string tests.

One deliberate boundary fix needs an explicit checkpoint: target `ui.rs:205–216` clears a disappeared scope to All sources. The prototype retains the missing selection. Integration should retain the selected ID and show “Source unavailable—choose another source,” disabling scoped Ask until the user explicitly changes it. Do not silently widen scope. Store the submitted scope label separately from the editable next-question selection.

## 5. Real citation and evidence contract

Repository evidence: target `docket-proto/src/api.rs:65–73`, worker `interfaces/worker.py:50,212–218,272–288`, daemon `api.rs:94–102`.

| Available today | Exact UI use / limit |
| --- | --- |
| `Citation.label`, `source_display_name`, `chunk_id`, `excerpt` | Link numbered citations to their exact returned record. Show recorded source name and passage ID. Excerpt is whitespace-normalized and truncated to 400 characters, not an exact full-format passage. |
| `AskResult.answer`, `abstained`, timings | Authoritative final result following provisional deltas. An abstention remains an answer outcome, not a transport error. |
| Optional abstention reason, coverage, scoped file count and warning codes | Display via `answer_meta`; absent means not recorded, never inferred. |
| Worker emits optional citation `note` | Rust `Citation` currently has no `note` field, so it is ignored. Exposing it requires a separate additive protocol change (roadmap M7), outside this pass. |
| Answer-time excerpt resolution may return empty | Show “Passage not available in this answer” and identifiers. Do not diagnose deletion/permission change without a typed fact. |

Worker protocol remains version 1 with `ping`, `status`, `health`, `sources.list`, `query.ask`. Desktop routes are status, sources, ask, tasks/list/events/cancel. There is **no** desktop `evidence.get`, document-list, neighboring-chunk, graph-read or page-render API. Python resolver and read-only MCP capabilities are not additional desktop routes; do not bypass the daemon with direct DB, document, MCP or filesystem reads.

Inspector first integration scope:

1. Open the answer's cached excerpt with mouse or keyboard; selected whole-excerpt tint means selection only.
2. State “Excerpt returned with this answer; up to 400 characters. Current access has not been rechecked.” Do not label it a full verified passage or claim independent semantic support.
3. Do not display fictional page, heading, document version, precise supporting span, provenance edge, confidence or “verified now” badge. Missing metadata is omitted or “not recorded.”
4. Preserve source/citation identity across conversation messages; namespace widget IDs by message and citation. Two answers can cite the same chunk without sharing focus IDs.
5. If a later source snapshot invalidates a selected scope, do not restore it silently. Citation records currently contain no source ID/version to reliably join to that snapshot. Do not pretend to revoke/re-authorize every cached excerpt from display-name matching.
6. Cached excerpts already exist in the live desktop. This integration does not solve revocation after display. Full current-access viewing is deferred to an authorized bounded evidence-read contract (R2/R4, M1/M3), with typed unavailable results. Add no persistent excerpt cache in this pass.

Library has no live document/passage browsing action to connect today. Omit the sample passage action in live mode. Source setup may show reviewed CLI guidance, but must not run ingestion, purge, cleanup or folder-write commands.

## 6. Incremental sequence and acceptance gates

Each checkpoint is an independently reviewable patch, not permission to commit. Start only after this plan is approved. Preserve screenshots as visual references; no new visual direction is proposed.

| Checkpoint | Work and ownership | Required proof before proceeding |
| --- | --- | --- |
| C0 — target parity baseline | UI integrator records then-current target HEAD; captures its R1/R2a behaviors with fake worker. Inventory exact selected prototype hunks. No daemon edits. | Current fmt/Clippy/tests pass, or pre-existing failures are isolated and recorded. Clean distinction between target changes and prototype changes. |
| C1 — tokens and shell | Add approved `theme`/`shell`; wrap existing live views with navigation and appearance controls. Preserve status/transport/model. | Both themes; five status chips, banners, source detail and Ask-off reasons remain reachable at 1440×900 and 640×420. No demo module in live path. |
| C2 — state bridge | Bind new presentation to `StatusModel`/`SourcesState`/`AskGate`/`answer_meta`; preserve polling and capability fallbacks. Add explicit no-silent-scope-widening behavior. | R2a semantic tests unchanged; event-injection tests verify stale/failed refresh, empty versus unavailable and disappeared scope. No extra requests caused by render/navigation/theme changes. |
| C3 — Assistant and composer | Transfer hierarchy, reading width, citation numbering, Copy, provisional/final states, next draft and shortcuts around existing R1 reducer. | End-to-end fake-worker streaming, early Stop, repeated Stop, StopFailed, timeout, terminal cancellation and next-question recovery. Next draft and submitted scope survive. Abstention/metadata parity. |
| C4 — bounded Inspector | Transfer cached-excerpt inspector, namespaced selection/focus IDs, resize and narrow context navigation. | Only actual fields shown; empty excerpt safe; selection and close work for multiple messages. No extra I/O. Disclosure accurately describes truncation and access limits. |
| C5 — Library and first run | Transfer rows/detail, filter, selection, empty onboarding and instruction-only setup using live source view data. | Unknown/indexing/partial/failed/empty/inactive/anomalous states all correct; units and freshness retained. No sample content or writer action reachable. |
| C6 — native acceptance | Exercise integrated app against target daemon with fake worker and temporary sockets, both themes and narrow sizes. Review screenshots and keyboard/accessibility checks. | Regression matrix below passes; no changed Python/protocol/daemon/schema/dependency files. Visual approval of integrated screenshots before considering any further backend feature work. |

At each checkpoint, revert only that presentation patch if needed; preserve prior target state. Do not use `DOCKET_TASKS=off` as a UI rollback: it hides task routes/capability but the daemon still uses its internal task adapter. A UI rollback must not change daemon configuration or cancel behavior. No permanent extra legacy-UI implementation is required; checkpoint patches provide the rollback boundary.

## 7. Narrow windows, continuity and keyboard review

The approved prototype already has an 800-point maximum reading column, independent transcript scroll, persistent composer, a side inspector above 1220 and full contextual inspector below it, plus compact navigation below 900. Preserve these choices, with the following integration corrections/tests:

| Finding / risk from inspected code | Required behavior |
| --- | --- |
| `run_command` changes destination without closing evidence, whereas sidebar navigation has its own handling | One navigation transition policy for sidebar, palette and Ctrl+1/2. Moving to Library closes the Assistant inspector without clearing its selection/scroll state; return restores conversation, not an accidental full-screen overlay. |
| Inspector replaces central content below 1220 | Save conversation scroll anchor and composer state before opening; Close/Escape restores them. Do not recreate the whole app or run submission logic. |
| Citation controls use generated/nested widget IDs | Include stable message identity and citation identity. Return focus to the originating control; if no longer rendered, focus a stable Sources control or composer. Selection must not steal focus on every frame. |
| `stick_to_bottom` transcript and minimum-height clipping | Follow new output only when already at bottom; reading older content must not snap down on theme switch, resize or status tick. Verify anchor restoration after inspector close. |
| Wrapping citation controls and selectable text widgets | No horizontal clipping; preserve Copy answer as complete-copy path. Continuous text selection across separate citation widgets is a documented egui limitation. |
| R2a real labels can be longer than demo labels | Wrap status strips/banners and source details; bound modal bodies with scrolling. At 640×420, composer, Stop and Close remain accessible; transcript may scroll. |
| Existing inspector resize policy uses viewport threshold | Check available central width after navigation and resize, at 1219/1220 and 899/900. Clamp retained inspector width so it cannot consume the readable center. |
| Source setup currently has simpler focus handling than palette/Settings | Give all modal experiences deterministic return focus and single-active-dialog behavior. No keyboard activation through a modal. |

Keyboard acceptance: Tab/Shift+Tab through all visible actions, Enter/Space activate citation/command controls; Enter in composer inserts newline; Ctrl+Enter submits only from valid composer context; Ctrl+Shift+X requests Stop only when supported; Ctrl+K opens palette, arrows choose and Enter activates; Escape closes the top context without cancelling; Ctrl+1/2 navigation and theme switching preserve draft/scope/scroll. Clipboard actions retain real source identifiers and never add simulated text in live mode.

Test 1440×900, 1280×800, 1220/1219 widths, 900/899 widths, 800×600, 640×620 and 640×420 in both themes. Include long source names, long answers, multiple messages, absent excerpts, 125–200% scale and keyboard-only operation. Capture native integrated Assistant/Inspector, Library, empty library, streaming and cancelled states. Check IME and Wayland when available; record unavailable environments rather than claim coverage. Contrast tests support token checks; screen-reader support remains unproven with the current feature set. No egui blocker has been demonstrated, so keep eframe 0.37.0.

## 8. Regression and compatibility matrix

Run in the integration checkout with temporary sockets and fake workers, isolated build artifacts and bounded build jobs. Native screenshots can use software GL; no inference model or GPU evaluation is needed.

| Layer | Regression checks |
| --- | --- |
| Formatting/lints | `cargo fmt --all --check`; `cargo clippy --workspace --all-targets --offline -- -D warnings` from `app/` |
| Workspace | `cargo test --workspace --offline -- --test-threads=1`; retain all target tests, especially new `status_view/tests.rs`. Record count from this target, not the prototype's older 92-test report. Investigate any parallel-run timing failures separately; do not remove tests or weaken assertions. |
| R1 daemon | Existing `cancel_mid_stream_discards_later_output_and_leaves_no_result`, `stale_late_result_after_cancel_is_never_published`, SIGTERM/grace/process-group tests, disconnect permit release, deadline TimedOut, queued cancellation, replay order, idempotency and shutdown tests remain unchanged and pass. |
| R1 UI + transport | Add event-injection and fake-daemon tests for early cancel before ID, cancel once, StopFailed, racing/late result, draft preservation, busy rejection, worker restart and next Ask. Native Stop drives actual `/v1/tasks/{id}/cancel`, not a simulated timer. |
| R2a status | Keep healthy/missing/stale sections, local age progression, last-good after error, no fresh age-less data, worker restart, separate model states, zero-result indexing, interrupted/dead-letter jobs and safe code rendering tests. Assert actual UI displays each result. |
| R2a daemon | Existing soft-health-timeout tests keep worker PID/restart count stable; status stays responsive during slow question, refresh pauses/resumes; cancellation is a relaunch, not a crash. No timeout or polling constants changed. |
| Source views | Known zero versus unknown, partial and contradictory ready reports, unsupported-only source, empty-ready anomalies, separate superseded history, latest whole-job skip/failure accounting, failed refresh retaining list, vanished/revoked selection. No equation across unlike units. |
| Answer metadata | Supplied reason/coverage/scope/warnings displayed; absent reasons “not recorded”; unrecognised codes generic; hostile paths/code strings never become status text. No false semantic verification badge. |
| Old daemon / worker | Independently test both capabilities present, only `tasks.v1`, only `status.v2`, and neither. Old worker has no health calls/status.v2; unknown fields/events are tolerated; missing optional counts/metadata do not become zero. Legacy ask still works; unsupported Stop absent. `DOCKET_TASKS=off` still allows status.v2. |
| Stream compatibility | Existing `old_ask_clients_see_exactly_the_old_vocabulary` and old-document-shape tests unchanged. New UI must not require invented event names or protocol version 2. |
| Isolation | Live build/startup code contains no demo data source; empty and failure states never seed examples. CLI setup runs no process. Inspector performs no file/DB/network reads. |
| Native lifecycle | Start via temporary fake-worker daemon, close UI during active task, verify existing owned-daemon shutdown and no orphan worker. Do not stop another user's daemon or use real library data. |

A CPU-only real-Python-worker smoke is optional only if the existing harness supplies scratch data and fake inference without real-library migrations or model calls. It is not necessary to prove a presentation-only patch; never run the normal worker against the user's library just to obtain screenshots. M3 correctness belongs to its existing tests/owner; this plan does not rerun graph cleanup or ingestion.

## 9. Changed-file and dependency-impact estimate

Estimate is for future integration, not changes made by this plan. Existing module sizes bound the work; line counts are approximate and extraction can redistribute code.

| File / area | Expected impact |
| --- | --- |
| `app/docket-app/src/main.rs` | Approximately 20–50 lines changed for theme/module startup and viewport; retain `mod status_view`, normal app identity, socket and auto-launch behavior. No live demo startup option in initial transfer. |
| New `theme.rs` | About 232 lines reused, including contrast tests. |
| New `shell.rs` | About 300–400 lines reused/adapted for real status wrapping and focus semantics. |
| `ui.rs` and optional view submodules | Largest change: roughly 1,400–2,000 lines of adapted production presentation plus focused tests, replacing existing drawing code. Do not transfer the 2,122-line prototype wholesale. Retain target state/polling bindings and closed-code consumers. |
| `model.rs` | Approximately 20–80 lines for local continuity/scope helpers and tests; R1 reducers/constants stay. Do not duplicate readiness model. |
| `status_view.rs`, `status_view/tests.rs` | Expected zero semantic changes. Add coverage only if a mapping gap is found; review separately if output rules need alteration. |
| `backend.rs` | Expected zero production changes; preserve current target. Test seams may need a small refactor only if event injection cannot be done at the existing UI boundary. Any transport behavior change requires separate review. |
| UI test module / optional fake-daemon harness | Approximately 200–400 lines covering state transitions and displayed mappings, plus native manual acceptance record. Prefer existing fixture infrastructure; no new test crate required. |
| Review docs and screenshots | Integration verification record, target SHA, commands/results and integrated captures. Preserve approved references. |
| `docket-proto`, `docketd`, Python, schemas, Cargo manifests/lockfile | **No planned changes.** Any diff here is a stop-and-review condition. |

Expected application dependencies: **zero added/updated**. Both trees use the same eframe 0.37.0 configuration (`glow`, `default_fonts`, `x11`, `wayland`) plus existing serde/serde_json and docket-proto. No font download, icon crate, web view, renderer switch or packaging change. Capture helper dependencies remain optional tooling and do not enter the app manifest. No reliable elapsed-time estimate is possible until C0 establishes current tests and the event harness; the primary cost is semantic reconciliation and native interaction verification, not theme transfer.

## 10. Risks and decisions required for approval

| Risk | Mitigation / release condition |
| --- | --- |
| Prototype overwrites newly merged status behavior | Build from target; preserve backend/status model/tests; compare target range at C0. |
| Attractive connected/ready badge conceals stale or partial data | Use existing semantic views and visible freshness/error summaries in both layouts. |
| Scope disappears and silently broadens | Explicit unavailable selection, never auto-All; test direct and keyboard submission. |
| Cached excerpt mistaken for current authorized evidence | Accurate bounded-excerpt disclosure; no fabricated metadata, live access badge or Library read action. |
| Cancellation race, next draft lost, or model-stop overclaim | Keep R1 reducers and server tests; add actual native cancel path and UI race coverage. |
| Duplicate view logic diverges from R2a source accounting | Use target view models, closed-code labels and original tests as authority. |
| Real status strip crowds narrow composer | Wrapping strip, bounded scrolling and minimum-size acceptance before moving on. |
| Prototype uncommitted or target advances during work | Preserve existing work and record exact diff/HEAD at every checkpoint; no automatic merge/rebase. |

Approval sought is for C0–C6 presentation integration with the constrained cached-excerpt Inspector, explicit unavailable-scope behavior and no new backend contracts. This document does not approve backend integration, provenance reads, schema work or commits. If any contract change becomes necessary, stop that dependent checkpoint and present a concrete separate plan; continue unaffected presentation checks.

## 11. Exact workflows to prototype and test before release of the integration

1. **Fresh library:** connect → successful empty source snapshot → calm onboarding → setup instructions → return to Library. Contrast with connection failure and source-refresh failure; neither may become an empty library or sample workspace.
2. **Ask → inspect → continue:** choose a real fixture source → type and submit → see provisional output → final answer with metadata → activate inline citation → select another citation → resize Inspector → Escape → resume at the prior reading position with next draft intact.
3. **Cancel and recover:** submit slow fake question → type next draft → Stop immediately before task ID, then in a second run during deltas → confirm discarded partial answer and unverified model stop → worker ready → submit preserved draft. Also exercise failed cancel and timeout.
4. **Readiness without false certainty:** refresh ready, zero-result indexing, partial, unknown, failed, empty, revoked and inconsistent-ready fixtures → inspect current counts versus whole-job outcomes → fail refresh → retain rows with old age → select unavailable source → require explicit scope change.
5. **Legacy compatibility:** connect each capability combination → ask using its actual transport → inspect available excerpt → see missing metadata as absent/not recorded → Stop only where supported; no health/provenance facts invented.
6. **Narrow continuity:** at 640×420 and 800×600, read above the transcript bottom → open/close evidence → navigate Assistant/Library through sidebar/compact header, palette and Ctrl+1/2 → switch theme → restore draft, scope, selected citation and scroll without clipping or unwanted auto-scroll.
7. **Keyboard and focus:** complete source selection, Ask, citation selection, close, palette, appearance and setup without a mouse; verify modal focus return, visible focus, Enter versus Ctrl+Enter and Escape versus Stop.
8. **Integrated visual parity:** review both themes with actual typed health/answer/source fixtures, including long labels and warning banners; approve the native integrated captures against the existing `polish/` references before any later backend feature connection.

## Planning verification

This pass inspected Git state/history and source files only. No application launch, test run, GPU activity, Python worker, database, source write or M3 worktree operation was needed. Historical prototype checks in `POLISH_REVIEW.md` are not proof for the newer target. Only this integration-plan document was added by this planning pass; implementation and fresh regression results remain pending approval.
