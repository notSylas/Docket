# C2 — Live state bridge

Implemented for review in the existing `codex/ui-integration` worktree. **Stopped at C2; C3 has not begun.** C1's functional approval is retained; its screenshots still await visual acceptance and are preserved unchanged. No commits, merges, rebases, pushes or new worktrees.

## State ownership and changes

Only `app/docket-app/src/ui.rs` and new `app/docket-app/src/ui/state_bridge_tests.rs` changed in application code relative to C1. This report and `c2/` add review evidence; a [C2-only UI patch](c2/c2-ui.patch) separates this checkpoint from the existing uncommitted C1 changes, which remain in place.

`StatusModel`, `StatusView`, `SourcesState`, `AskGate`, `source_view` and `answer_meta` remain the authorities. `status_view.rs` and its tests are byte-identical to HEAD. There is no parallel health, readiness, count or abstention model. `backend.rs`, `model.rs`, daemon/protocol files, Python, schemas and dependencies are unchanged.

| Behavior | C2 implementation |
| --- | --- |
| Disappeared/inactive source | Retain the selected ID. A successful source snapshot that omits it or reports an inactive lifecycle invalidates that selection. Library and Assistant explain that the source is unavailable; scoped Ask is disabled. |
| Explicit recovery | Only explicit selection clears the invalidation latch. A reappearing ID does not silently re-enable Ask. Choose a listed source or All sources in Library. Failed fetches alone never invalidate a source or change selection. |
| Submitted scope | Capture source ID and display label before dispatch, separately from the editable selection. The captured ID is passed to the unchanged backend; the question displays its scope at submission. Later source removal, renaming or selection changes do not rewrite that record. |
| Ask admission | Re-evaluate the existing R2a `AskGate` with the current monotonic time instead of using a previous frame's cached view. Preserve its disabled reasons first, with existing legacy worker fallback and only the approved local scope/known-empty/pending/input guards. |
| Unknown/partial readiness | Active lifecycle sources remain eligible for routing when readiness is unknown, indexing or partial. No invented readiness threshold disables Ask; the engine remains responsible for answering or abstaining. |
| Confirmed empty library | Show a neutral empty-library notice only after a successful empty snapshot that is still current under `SourcesState`, while the service is available under `StatusModel`. This is the state distinction, not C5's full onboarding flow. |
| Failed refresh | Retain the last-good source list, receipt time, selected ID and age. Use `SourcesState.note/is_old/age_ms`; never treat failure as an empty result. An old empty snapshot remains a reason to block Ask, but does not become fresh onboarding. |
| Source error diagnostics | Discard untrusted source error strings for presentation. Display safe generic loading/failure text and existing model-supplied age text; do not echo paths or arbitrary worker messages. |
| Auto-ask ordering | Drain incoming source/status events before considering the existing one-shot auto-ask, so a queued empty/missing snapshot cannot be bypassed. |
| Navigation/theme | Change UI state only. No backend fetch/ask is added to navigation or appearance changes. Existing Refresh remains explicit; existing source retries/polling remain in the backend. |

The composer receives enough vertical room for disabled reasons at narrow sizes. Its existing single-line, disabled-during-task behavior remains; next-draft editing and conversation/composer redesign belong to C3. Existing R1 task branches, cancellation wording, pending reduction and Stop dispatch are preserved.

## Verification

Integration base remains `3a6a4ca`. The authoritative `rust-app` advanced from `47d4277` to `b4cd9cb` (M3-5 merge) during this pass and is clean. The full intervening range still has no committed `app/` delta. The final check inspected Git objects and the authoritative checkout status only; no update/merge was needed and Claude's M3 worktree was not accessed.

Commands ran from integration `app/`, offline, with two build jobs and `/tmp/docket-c0-target` build artifacts:

| Check | Result |
| --- | --- |
| `cargo fmt --all --check` | Passed — [log](c2/fmt.log) |
| `cargo clippy --workspace --all-targets --offline -- -D warnings` | Passed — [log](c2/clippy.log) |
| `cargo test --workspace --offline -- --test-threads=1` | **131 passed**, zero failures: desktop 55, protocol 24, daemon unit 10, daemon API 42 — [log](c2/workspace-tests.log) |
| Native build | Passed — [log](c2/build.log) |
| Protected files, C1 evidence and prototype | [Preservation checks](c2/preservation.txt) |

Ten new C2 tests cover missing/reappearing/inactive source selection; unknown/partial/indexing readiness; captured request scope; loading versus confirmed-empty versus failed/offline states; retained rows and age after failed refresh for v2 and legacy; exact R2a Ask-off reasons and local age expiry; legacy capabilities; hostile source/abstention codes; queued auto-ask with empty sources; and navigation/theme rendering that opens no backend connections. The no-request test uses an unstarted real desktop backend pointing at a temporary Unix listener; it isolates accidental view-triggered I/O from the established poller.

The unchanged 42 daemon API tests continue to cover R1 cancellation/races, worker process-group cleanup, timeouts, replay, legacy responses and R2a soft health timeouts. Socket tests used the same explicit local-socket permission established during C0. No tests were removed, skipped or weakened.

## Native evidence

These are actual X11 captures of the compiled desktop, using software rendering and synthetic protocol fixtures through the **real unchanged daemon**. There is no demo or fixture-loading path in production.

The review uses a temporary Rust fake worker under `/tmp/docket-c2-review`, linked against the already-built serde_json library without dependency changes. Its health document comes from the existing repository fake-worker fixture; source responses are controlled by a temporary JSON file. This permits source disappearance, empty snapshots and typed RPC failures without modifying Python or a database. Polling is allowed to observe the response before state captures are accepted. Review health refresh is set to 2 seconds; production desktop polling and daemon defaults are unchanged. No personal library, model inference or GPU evaluation is used.

| State | Capture |
| --- | --- |
| Active source, readiness unknown | [Dark Library](c2/screenshots/01-unknown-readiness-dark.png) |
| Submitted question captures Fixture A | [Submitted scope](c2/screenshots/04-submitted-scope-A.png) |
| Selected source disappears | [Library](c2/screenshots/05-missing-source-library.png), [dark Assistant](c2/screenshots/06-missing-source-dark.png), [light Assistant](c2/screenshots/07-missing-source-light.png) |
| Explicit next selection does not rewrite submission | [Next selection All sources](c2/screenshots/09-next-selection-all-retains-submitted-A.png) |
| Confirmed empty source list | [Light](c2/screenshots/10-confirmed-empty-light.png), [dark](c2/screenshots/11-confirmed-empty-dark.png) |
| Failed refresh after empty snapshot | [No first-run notice](c2/screenshots/12-empty-refresh-failure-not-onboarding.png) |
| Failed refresh with retained positive snapshot | [Dark](c2/screenshots/14-failed-refresh-last-good.png), [light](c2/screenshots/15-failed-refresh-last-good-light.png) |
| Service unavailable | [Retained Library](c2/screenshots/16-service-unavailable-library.png), [Ask disabled](c2/screenshots/17-service-unavailable-assistant.png) |

[Native transport log](c2/native.log) records real status/source requests and the controlled source failures. [Fixture manifest](c2/native-fixture.json) identifies the temporary fixture and rendering environment. The owned review daemon was stopped normally for the offline captures, then the native window was closed normally; no owned review process or socket remained. Native data is explicitly synthetic; source names, counts and the abstention answer are not claims about a real library.

## Remaining boundaries

- C3 must adapt conversation/composer presentation and the additional local Stop/result race guard. The existing R1 behavior is retained here; no cancellation policy changed.
- Question scope records are window-local metadata attached to current message indices, not persisted history or an audit record. Any future conversation-reset operation must reset them together with the messages.
- Known-empty local gating is conservative after stale/failed refresh: Ask remains off until a later positive snapshot. Global unknown source-load state alone is not an invented empty-library veto; existing R2a/legacy admission and explicit scope guards still apply.
- Legacy source lifecycle/count detail is retained and labelled as legacy, without inventing indexing readiness. Modern counts/readiness/abstention text continues through the existing R2a view helpers.
- At 640×420, long transcripts may require scrolling to see the original question/scope label; source errors and disabled Ask remain reachable. Full scroll-anchor/focus restoration belongs to the later interaction checkpoints.
- Full onboarding, source lifecycle writes and source browsing remain C5/R7 work. The C2 empty notice runs no commands and offers no sample data.
- Evidence still uses the existing answer-time excerpt contract. No span, provenance, current-access read or verification status is invented.
- Native accessibility, Wayland, IME and high text scaling remain unverified. The approved C1 palette/layout have not been redesigned.

Review C2 before any C3 implementation. C1's original integrated screenshots remain unchanged and still require final visual acceptance.
