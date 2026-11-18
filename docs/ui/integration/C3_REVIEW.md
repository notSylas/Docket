# C3 — Assistant and composer integration

Implemented for review in the existing `codex/ui-integration` worktree, 2026-11-19. **Stopped after C3. C4 has not begun.** No commit, merge, push, rebase or new worktree. The approved Obsidian & Silver / Porcelain & Ink tokens are unchanged.

## C2 review and baseline

Read `C2_REVIEW.md`, its preservation/test evidence and the approved prototype `INTEGRATION_PLAN.md` before editing. C2 contains no unresolved correctness regression. Its remaining boundaries are scheduled work: editable drafts and the local Stop/result guard (handled here), contextual Inspector (C4), Library/onboarding (C5), and broader native/platform acceptance (C6). The existing 131 tests remain; none were removed or weakened.

Integration remains based on `3a6a4ca`. During this pass `rust-app` advanced from `b4cd9cb` to `8356eeb` (R2b-1a). Git-object comparison shows no committed `app/` changes in the entire intervening range, so no merge/rebase was performed. R2b-1a adds a Python worker evidence reader; this checkpoint neither imports nor calls it. The integration's daemon/protocol remain unchanged and have no new desktop evidence-read route. Claude's R2b worktree was not accessed.

## What changed

Application changes relative to C2:

| File | Change |
| --- | --- |
| `app/docket-app/src/ui.rs` | Approved conversation hierarchy, centered reading column capped at 800 logical points, question/scope header, selectable answer prose, inline citation controls and source list, Copy, collapsed details, multiline composer, local terminal-event guards and request capability snapshot. |
| `app/docket-app/src/shell.rs` | Reuse approved prose, response-heading and citation primitives. Appearance shortcut help now describes Enter for newline and Ctrl+Enter for Ask. |
| `app/docket-app/src/model.rs` | Add `Message::Discarded` for a result suppressed after local Stop without confirmed server cancellation. Existing `Pending`, `task_step`, cancellation constants and citation numbering logic are unchanged. |
| `app/docket-app/src/backend.rs` | Carry the originating task ID in the internal `StopFailed` event. This is the only transport-file adjustment; HTTP requests, payloads, response parsing, capabilities, polling, timeout constants and daemon ownership are unchanged. |
| New `app/docket-app/src/ui/assistant_tests.rs` | Eleven C3 regression tests at the real typed UI event boundary, including rendered controls and keyboard editing. |

[C3-only patches](c3/ui.rs.patch), [shell patch](c3/shell.rs.patch), [model patch](c3/model.rs.patch) and [internal event patch](c3/backend.rs.patch) separate this work from the existing C1/C2 changes. Theme tokens, `main.rs`, `StatusModel`/`SourcesState`, their tests and all C0/C1/C2 evidence are preserved. No Python, schema, daemon, protocol, manifest or lockfile changed. No fixture or demo module was added to production.

## State and interaction behavior

- **Authority:** final answers and citations are the supplied `AskResult`. Citation numbering still uses `model::layout`; source names, labels and chunk IDs are preserved. Warning, coverage, scope-file and abstention labels still use `answer_meta`, including its closed-code/hostile-string handling. No support span, confidence or verification badge is inferred.
- **Drafts:** Enter inserts a newline; Ctrl+Enter submits only from the focused composer, with no modal open and the existing R2a/local Ask gates enabled. A pending task disables submission, not editing. A bounded vertical editor keeps long drafts scrollable without hiding actions. Submitted question and scope remain separate from the next draft and next source selection.
- **Streaming:** phase/elapsed state and dimmed provisional prose are explicitly labelled unchecked. Provisional citation-looking text has no evidence actions. Only final results receive interactive citations and Copy.
- **Stop intent before a result:** discard provisional text immediately, retain the next draft, disable duplicate Stop, and wait for a terminal event. A returned result while Stop is still requested is discarded as `Message::Discarded`; its wording says completion arrived before cancellation was confirmed. It does **not** claim the daemon terminated its worker.
- **Completion before Stop:** an already accepted answer remains completed. Stop with no pending request and later terminal events do not relabel or duplicate it.
- **Failed Stop:** restore the existing `Pending` retry state and keep failure feedback visible across health polls. A legitimate result after `StopFailed` is accepted. Task-ID correlation prevents a delayed cancel failure from changing a newer task's Stop intent. Failures for a finished/nonmatching task are ignored.
- **Confirmed cancellation:** keep the existing conservative R1 stopped wording, including inability to confirm model-engine stop. Errors and timeouts remain errors, preserve the draft and permit recovery when the unchanged service gate allows it.
- **Compatibility:** capture `tasks.v1` when dispatching each request. Later status polls cannot switch an in-flight legacy request into a stoppable task or hide Stop on a task already submitted through the task API. Legacy `/v1/ask` remains supported; its active composer explains that Stop is unavailable.
- **Navigation:** Assistant/Library and theme changes retain window-local draft, conversation and submitted scopes. No new backend request is tied to rendering, navigation, theme or citation selection. Existing polling and explicit Library Refresh remain.

Citations currently toggle the **existing inline cached-excerpt view**, with message/citation-scoped widget identities. It displays the actual chunk ID, a bounded-excerpt/current-access disclosure and a neutral missing-excerpt message. No evidence I/O is introduced. C4's contextual Inspector, resize behavior and focus/scroll return are deliberately deferred.

## Verification

All final checks ran from integration `app/`, offline, with two build jobs and `/tmp/docket-c0-target` artifacts:

| Check | Result |
| --- | --- |
| `cargo fmt --all --check` | Passed — [log](c3/fmt.log) |
| `cargo clippy --workspace --all-targets --offline -- -D warnings` | Passed — [log](c3/clippy.log) |
| `cargo test --workspace --offline -- --test-threads=1` | **142 passed**, zero failures/ignored: desktop 66, protocol 24, daemon unit 10, daemon API 42 — [log](c3/tests.log) |
| `cargo build --workspace --offline` | Passed — [log](c3/build.log) |
| Preservation | [Hash checks](c3/preservation.txt): all 680 approved prototype files and all prior evidence unchanged; only four pre-existing desktop files changed relative to C2. |

New tests cover early Stop and late result discard; immutable completed answers; Stop failure/retry and legitimate completion; confirmed cancellation/timeout/recovery preserving drafts/scopes; request capability snapshots; provisional versus final actions and unchanged result fields; bounded missing excerpts; keyboard editing in both themes at 1440×900, 800×600 and 640×420; modal/focus/Escape guards; late cancel failures from another task; and a 40-line next draft with Stop/status still visible. Existing R1/Python-independent reducer and daemon API tests retain process-group, timeout, replay, legacy and soft-health-timeout coverage.

The first sandboxed desktop test invocation hit the existing Unix-listener permission restriction. The full final suite ran with local-socket permission and passed; this was an environment limitation, not a code failure. Intermediate compilation/Clippy/layout issues were corrected before final verification. No unresolved test failure is carried forward.

## Native transport and screenshots

Actual native X11 desktop captures use software GL (`llvmpipe`), the real unchanged daemon and a temporary Rust JSON-RPC fake worker. All source/question/answer data is synthetic; no personal library, database, model or GPU inference was used. Production has no fixture-loading path. The [fixture snapshot](c3/native-fixture.rs) and [manifest](c3/native-fixture.json) are review evidence only.

The [scenario log](c3/native-scenarios.log) records task creation/streaming, a real user cancel POST returning 200, worker termination/relaunch, successful next Ask, a worker error, an abstention and a `TimedOut` task. [Synthetic worker request records](c3/native-requests.jsonl) include actual scoped requests and the preserved next draft sent on recovery. The review used a 12-second ask deadline and 2-second health interval as environment overrides; production defaults were not changed. Timeout captures waited for terminal process cleanup, not merely elapsed deadline.

The [legacy log](c3/native-legacy.log) records `/v1/ask` with `DOCKET_TASKS=off`; native controls correctly omit Stop while retaining editable draft input. This tests the legacy route/capability fallback against the current daemon, not execution of a historical daemon binary. Existing protocol/daemon tests cover old response shapes. [Final answer log](c3/native-final.log) records the final binary's normal startup/Ask/shutdown.

| Native state | Captures |
| --- | --- |
| Final Assistant, 1440×900 | [Dark](c3/screenshots/01-answer-dark-1440.png), [light](c3/screenshots/02-answer-light-1440.png) |
| Final Assistant, 800×600 | [Light](c3/screenshots/03-answer-light-800.png), [dark](c3/screenshots/14-answer-dark-800.png) |
| Final Assistant, 640×420 | [Light](c3/screenshots/04-answer-light-640.png), [dark](c3/screenshots/05-answer-dark-640.png) |
| Provisional response with editable next draft | [Dark](c3/screenshots/06-streaming-dark-next-draft.png), [light](c3/screenshots/07-streaming-light-next-draft.png) |
| Confirmed Stop and retained draft | [Light](c3/screenshots/09-cancelled-light-draft-kept.png) |
| Recovered next Ask | [Dark](c3/screenshots/10-recovered-answer-dark.png) |
| Worker error / recorded abstention / timeout | [Error](c3/screenshots/11-error-dark.png), [abstention](c3/screenshots/12-abstention-light.png), [timeout](c3/screenshots/13-timeout-light.png) |
| Cached excerpt and independent next scope | [Inline excerpt](c3/screenshots/15-cached-excerpt-dark.png), [submitted All / next source selected](c3/screenshots/16-submitted-all-next-scoped.png) |
| Legacy response without Stop | [Dark 800×600](c3/screenshots/17-legacy-stream-dark-800.png), [light 640×420](c3/screenshots/18-legacy-stream-light-640.png) |

Normal/cached-excerpt/scope and legacy captures use the final binary. The earlier lifecycle scenario captures precede the final bounded-editor scrolling adjustment; those paths passed the complete final suite afterward.

Normal/fallback review windows were closed normally. Final host checks found no owned review app, daemon or worker and neither temporary socket remained. C1 screenshots are unchanged and still require their outstanding visual acceptance; these C3 captures also await review.

## Remaining limitations and C4 boundary

- C4 has not started: no contextual Inspector, resize behavior, page rendering, neighboring passages, live evidence access or provenance integration.
- Conversations and submitted-scope records remain window-local. Restart does not restore history. A future reset operation must clear scopes with messages.
- At narrow sizes, the transcript scrolls and may place older question/headings above the viewport; composer/actions/status remain independently available. ScrollArea follows output while at the bottom. Full context/focus restoration across an Inspector belongs to C4/C6.
- Text selection is per prose segment around interactive citation widgets. Copy provides the full original answer plus real source labels/IDs. There is no markdown renderer, table renderer or continuous selection across controls.
- Keyboard edits, modal gating and narrow controls have automated coverage; X11 native actions were exercised. Wayland, IME, screen reader and 125–200% text-scale acceptance remain unverified and belong to C6.
- No dependency/backend feature integration or readiness-policy change is included. The new R2b worker method is not connected here.

**No unresolved correctness regression or failing final check was found. Stop at C3 for review; do not proceed automatically to C4.**
