# C1 — Approved themes and application shell

Status: implemented for review in the existing `codex/ui-integration` worktree. **C1 only; C2 has not begun.** No commit, merge, rebase, push or new worktree.

## Baseline verification

The full approved integration plan and C0 report/evidence were reviewed. C0 recorded formatting/Clippy success and 116 passing Rust tests, with the initial sandbox socket restriction recorded separately.

Authoritative `rust-app` is `47d427736c5f56049fb6828eea1586cd0a9c9841`; integration remains based on `3a6a4ca2d9e809c15551623428626c724283c810`. The intervening M3-2 merge changes six Python/database files only. `git diff HEAD..rust-app -- app` is empty: desktop, daemon, protocol and dependencies are identical across these bases. M3-2 adds backend graph provenance storage/auditing, but no desktop span/evidence contract. There was no reason to merge/rebase solely to advance the base. Claude's M3 worktree was not accessed or changed.

## What changed

| File | C1 scope |
| --- | --- |
| `app/docket-app/src/theme.rs` | Approved Obsidian & Silver and Porcelain & Ink token sets, typography, control states, focus and contrast test; copied unchanged from the approved prototype. |
| `app/docket-app/src/shell.rs` | Presentation-only shell state, sidebar and compact navigation, selected destination styling, geometric icons, explicitly Planned destinations, appearance modal, searchable command palette, focus return and reusable controls. No transport or simulated content. |
| `app/docket-app/src/main.rs` | Register theme/shell modules; approved 1440×900 initial viewport, retaining 640×420 minimum, normal `docket` app ID, title and socket startup. |
| `app/docket-app/src/ui.rs` | Wrap existing views in shell; map existing R2a tones to approved tokens; focusable status-detail menus; wrapping/bounded status and warning regions; restyle existing controls and excerpt surface. Add headless layout/continuity and keyboard regression tests. |

No `demo.rs`, sample-library action, timed fake result, synthetic metadata or prototype startup option was transferred. Native review uses the existing fake-worker fixture **through the real daemon and normal desktop transport**, not demo mode. The production application contains no fixture fallback.

Assistant and Library remain the current production views, with presentation restyling. Library is accessed through its destination instead of occupying a permanent source sidebar. Choosing a source there still controls the existing Assistant scope. Conversation/draft/selection remain window-local. Complete Assistant, composer, Inspector and Library replacements remain later checkpoints.

Sidebar appears at 900 logical points and above, using the approved 216-point width. Below that, Assistant, Library, Commands and Settings move into a wrapping header. The sidebar itself scrolls at short heights. Knowledge, Investigations and Activity are noninteractive Planned labels. Appearance Settings changes themes without resetting the view and explicitly says it is not persisted.

Commands: Go to Assistant, Go to Library, Focus question, Switch theme, Appearance settings and Stop current question (only while supported and stoppable). Ctrl K opens/searches the palette; arrows select, Enter activates and Escape closes/returns focus. Ctrl 1/2 navigate; Ctrl Shift T switches themes; Ctrl Shift X invokes existing Stop. Escape does not cancel a task. Existing single-line composer Enter behavior is retained; Ctrl Enter/multiline draft adaptation belongs to C3. Submission is blocked behind shell modals.

## Operational preservation

- `backend.rs`, `model.rs`, `status_view.rs` and `status_view/tests.rs` remain byte-identical to integration HEAD and authoritative application counterparts.
- Existing `pump`, `submit`, `stop`, `can_ask`, `ask_off_reason` and monotonic-clock method bodies are unchanged (ignoring formatting). This retains R1 task/cancellation reduction, source selection, capability fallbacks, last-good state, freshness, safe status labels and abstention metadata.
- Polling cadence, `set_asking`, daemon startup/ownership/shutdown, process-group cancellation, health soft timeout and socket rules are unchanged.
- All five R2a statuses remain independent. Details formerly hover-only can be opened as focusable menus. Age/stale labels and warning banners remain visible. Auxiliary regions scroll when unusually verbose instead of displacing the composer.
- No Python, migration/schema, daemon, protocol, Cargo manifest/lockfile or dependency change.

## Verification

Commands from integration `app/`, offline with two build jobs and `/tmp/docket-c0-target` artifacts:

| Check | Result |
| --- | --- |
| `cargo fmt --all --check` | Passed — [log](c1/fmt.log) |
| `cargo clippy --workspace --all-targets --offline -- -D warnings` | Passed — [log](c1/clippy.log) |
| `cargo test --workspace --offline -- --test-threads=1` | **121 passed**, zero failed: desktop 45, protocol 24, daemon unit 10, daemon API 42 — [log](c1/workspace-tests.log) |
| Native build | Passed — [log](c1/build.log) |
| Protected files and prototype preservation | [Recorded checks](c1/preservation.txt) |

Daemon tests require local Unix-socket permission, as established by C0. No tests were removed, ignored or weakened. Five additional tests cover token contrast, shell command availability/modal focus, and native-view state/layout behavior. The layout test inspects fully unclipped painted text for all five statuses at 1440×900, 800×600 and 640×420, including a longer model-status label, warning banner and both destinations/themes. It also checks draft, source selection and conversation continuity. All existing R1/R2a/legacy regression tests pass.

Native X11 review used software rendering (`LIBGL_ALWAYS_SOFTWARE=1`, `GALLIUM_DRIVER=llvmpipe`), a private `/tmp/docket-c1-review` socket, an owned temporary daemon and the unchanged `fake_worker.py --health --scenario partial`. No real library or model inference was accessed. The fixture deliberately produces partly-ready/inconsistent indexing states, exercising real warning/status presentation. There was no GPU evaluation.

Native checks covered theme changes, compact/sidebar navigation, palette search/activation/Escape, appearance selection, source selection retained into Assistant, a real task-backed fixture answer, streaming, Stop and worker relaunch. The [native transport log](c1/native.log) records task creation, event streams, cancellation requests and generation changes. Cancellation was confirmed independently through the palette, the existing wide-layout Stop button and Ctrl Shift X. The fake worker restarted and the next task was accepted. The review app was then closed normally through the window manager.

## Native screenshots

These are actual compiled application captures with synthetic protocol-fixture data, **not personal documents or the prototype demo**.

| Size | Obsidian & Silver | Porcelain & Ink |
| --- | --- | --- |
| 1440×900 Assistant | [Dark](c1/screenshots/obsidian-assistant-1440.png) | [Light](c1/screenshots/porcelain-assistant-1440.png) |
| 1440×900 Library | [Dark](c1/screenshots/obsidian-library-1440.png) | [Light](c1/screenshots/porcelain-library-1440.png) |
| 800×600 Assistant | [Dark](c1/screenshots/obsidian-assistant-800.png) | [Light](c1/screenshots/porcelain-assistant-800.png) |
| 800×600 Library | [Dark](c1/screenshots/obsidian-library-800.png) | [Light](c1/screenshots/porcelain-library-800.png) |
| 640×420 Assistant | [Dark](c1/screenshots/obsidian-assistant-640.png) | [Light](c1/screenshots/porcelain-assistant-640.png) |
| 640×420 Library | [Dark](c1/screenshots/obsidian-library-640.png) | [Light](c1/screenshots/porcelain-library-640.png) |

Additional captures: [palette](c1/screenshots/porcelain-palette-640.png), [appearance](c1/screenshots/porcelain-settings-640.png), [source selected](c1/screenshots/source-selected-640.png), [fixture answer](c1/screenshots/fixture-answer-640.png), [streaming](c1/screenshots/streaming-640.png), [cancelled](c1/screenshots/cancelled-640.png), [wide Stop result](c1/screenshots/cancelled-1440.png), [keyboard Stop result](c1/screenshots/shortcut-cancel-1440.png).

## Limits and C2 dependencies

- C1 retains the existing single-line composer and its disabled-during-question behavior. Next-draft editing, premium conversation layout and composer state adaptation belong to C3.
- The pre-existing vanished-source behavior still clears scope to All sources. Fixing this and explicitly guarding observed-empty/missing-scope states remains C2; no silent semantic change was slipped into C1.
- R2a view model rules, unknown/count-unit handling and legacy fallback remain the existing authority. Later components must consume these rather than introduce prototype readiness logic.
- The pre-existing answer-after-local-Stop UI race guard gap remains C3; daemon race protections remain intact and tested.
- Evidence remains the existing answer-time excerpt of at most 400 normalized characters, with inline expansion. There are no current-access reads, graph spans/pages, new verification badges or full Inspector in C1. Existing unavailable-excerpt wording is unchanged pending C4's bounded-excerpt treatment.
- At 640×420, Library details and longer status/warning content may require independent scrolling; navigation and Ask/Stop stay reachable. Screen-reader support, IME/non-Latin input, Wayland and 200% scaling have not been validated in this X11 pass.
- No persistent appearance setting, chat history, ingestion control, Knowledge/Investigations/Activity implementation or backend feature connection.

C1 stops here for review. C2 has not started, and the authoritative application has not been merged with this work.
