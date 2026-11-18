# C0 — Target parity baseline

2026-11-18 · **C0 only; C1 has not begun.** The approved themes and architecture are unchanged. No application code was transferred or modified.

## Checkout and preservation

- New integration worktree: `/home/pc/Desktop/Rajdeep_Dev/Local_Agent/.worktrees/ui-integration`, branch `codex/ui-integration`.
- Base: **`3a6a4ca2d9e809c15551623428626c724283c810`**, latest local `rust-app` at creation. Both target and new checkout were clean before C0 documentation. No commits, merges or pushes.
- Approved prototype remains at `599748ae40d438a7186a7437a8e7d16e073dee05`, branch `codex/ui-prototype`, with its existing modified and untracked work preserved. Its tracked diff and all tracked/nonignored untracked file contents were fingerprinted before work and checked afterward; see preservation evidence below.
- Git worktree creation required permission to write repository metadata because `.git` is read-only in the sandbox. No source branch history was altered. The new branch points to the existing target commit.
- Worktree inventory also lists `m3-2-exact-provenance`; it was not entered or inspected. Neither M3 worktree nor any M0 evaluation was touched.

Recorded inventory: [worktrees](c0/worktrees.txt), [initial integration state](c0/integration-initial-status.txt), [base commit](c0/integration-base.txt), [prototype initial state](c0/prototype-initial-status.txt), [approved source hashes](c0/approved-source-manifest.json).

Since prototype base, target has **39 changed files, 4,188 insertions, 74 deletions**: [summary](c0/changes-since-prototype.stat.txt), [paths](c0/changes-since-prototype.paths.txt). Material additions are M0 phase 2b tooling (`5027323`), R2a-3 desktop/status/polling plus soft health timeout (`25d5f1d`), and M3-1 orphan-entity lifecycle cleanup (`3a6a4ca`, implementation `6868dc1`). M3-1 adds no desktop evidence/provenance API. Protocol, desktop manifest and `model.rs` are unchanged in this committed range. The target's newer `backend.rs`, `status_view.rs`, tests and daemon safeguards must survive integration.

## Verification results

Build environment: rustc 1.98.1, cargo 1.98.1. Offline builds, `CARGO_BUILD_JOBS=2`, `CARGO_TARGET_DIR=/tmp/docket-c0-target`. Commands ran from integration `app/`. No native desktop launch, real Python application worker, ingestion, library data, inference service or GPU evaluation was used. The real daemon implementation was exercised by tests on temporary Unix sockets with the repository's fake worker.

| Check | Result / evidence |
| --- | --- |
| `cargo fmt --all --check` | Passed; [log](c0/fmt.log) (empty successful output) |
| `cargo clippy --workspace --all-targets --offline -- -D warnings` | Passed; [log](c0/clippy.log) |
| Initial `cargo test --workspace --offline -- --test-threads=1` inside sandbox | 40 desktop + 24 protocol passed; daemon unit run 9 passed / 1 failed because temporary Unix socket bind returned EPERM. API suite was not reached. [Original failure](c0/tests-sandbox.log). This is an environment restriction, not a confirmed code regression. |
| Same unchanged workspace command with local-socket permission | **Passed: 116 tests, zero failed** (desktop 40, protocol 24, daemon unit 10, daemon API 42; doc-tests contain no tests); [log](c0/tests-local-sockets.log) |
| Source preservation and protected-file checks | Passed: 680 prototype file hashes and Git status unchanged; target clean/unchanged; integration tracked diff empty. [Evidence](c0/preservation-check.txt) |

No pre-existing code/test failure was observed with the required socket permission. No failing assertion was edited, skipped or weakened. Any remaining failure is to be recorded here rather than fixed under C0. Historical prototype's 92-test count is not used as this target's baseline.

## Existing test coverage and its limits

[Test source inventory](c0/test-source-inventory.txt) records names and source anchors; successful run output records actual executed tests.

- **Desktop: 40 pure tests**, comprising 8 `model` tests and 32 `status_view` tests. Model covers delta/validation accumulation, early Stop with deferred task ID, single cancel, retry after failed delivery, task reduction, conservative model-stop wording, multibyte deltas and citation ordering. Status tests cover last-good snapshots, age/stale/unknown data, separate health/model facts, worker states/restart, Ask gate, zero-result indexing, partial/failed/skipped counts, anomalies, legacy fallback, abstention/coverage and hostile-code handling.
- **Protocol: 24 tests** cover old-document identity/default fields, source/answer extensions, status sanitization, unknown enum/event behavior, task wire shape and SSE parsing.
- **Daemon unit: 10 tests** cover socket rules and bounded task state/tails/replay/queue behavior.
- **Daemon API harness:** real `docketd::serve` with `TempDir` and `tests/fixtures/fake_worker.py`; `Config.data_dir=None`. Fake worker imports only standard-library modules and simulates protocol responses. Tests assert running/queued cancellation, stale-output rejection, process-group termination and SIGKILL grace, restart/backoff, timeout, disconnect release, shutdown/orphans, bounded queue/replay, old client events, capability fallback, status/readiness scenarios, hostile output and metadata passthrough. Soft health timeout tests assert no worker replacement; user cancellation relaunches without counting a crash.
- **Coverage gap:** desktop tests exercise pure state, not `DocketApp` rendering or `Backend` HTTP threads end to end. The daemon API suite exercises the shared Unix-socket client and real server; it is not proof of native keyboard/focus/layout behavior. No native visual parity or screenshot check is claimed by C0. These gaps remain C1–C6 acceptance work.

## Current contracts and behavior to preserve

| Area | Observed baseline |
| --- | --- |
| Capabilities | `tasks.v1` when task routes enabled; `status.v2` when health refresh enabled and worker advertises `health.v1`. Independent capabilities; `DOCKET_TASKS=off` can leave status.v2 active. Worker protocol stays **1**, with `health.v1`, `sources.counts.v1`, `ask.meta.v1` feature names. |
| Transport | `backend.rs` background threads; status every 2s, periodic sources every 10s for v2 while idle, source fetching independent from status polling, one automatic source fetch in flight. Preserve `set_asking`, typed `FetchError`, daemon ownership/auto-launch and owned-child shutdown. Legacy source refresh remains load-once plus explicit refresh/retry. |
| Task lifecycle | With tasks capability use create/events/cancel; otherwise legacy `/v1/ask`. Stop waits for ID when needed; terminal cancellation discards partial text. Daemon confirms its process group gone but reports model-engine stop **unverified**. Timeout remains distinct from cancellation; user cancel has no failure-backoff penalty. |
| Status/readiness | `StatusModel`/`SourcesState` keep last-good snapshots and age them; local status silence >10s is unavailable, source age >30s old. Distinct Service/Worker/Model/Index/Database chips, banners and Ask-off reasons. Health timeout default 2s is advisory/stale-only, not a worker-kill reason. Unknown is not zero or healthy. |
| Ask gating | v2 uses `AskGate`, pending task blocks input; legacy checks connected/worker Up. Empty question is prevented by send logic. Model/index warnings alone do not currently disable Ask. Current UI has no observed-empty-library or missing-scope guard like the prototype. |
| Source scope | `model::source_ready` means lifecycle `active`, not indexing readiness. Live `ui.rs:205–216` resets a disappeared source to All sources. This is a recorded boundary risk; do not fix in C0. Planned C2 will require explicit scope change instead of silently widening it. |
| Conversation | In-memory, single-line composer submits with Enter; disabled while asking. Current UI renders every received Answer event without the prototype's additional local Stop-intent guard. Record this UI race gap; retain daemon safety and test the later guard. No persistent chat or backend multi-turn contract. |
| Evidence | `Citation` contains label, source display name, chunk ID, excerpt. Worker collapses whitespace and truncates excerpt to 400 characters; resolution failure can yield empty text. It is answer-time data, not a current-access check. No source ID/version/page/span/full-passage/provenance contract. Worker optional `note` is ignored by Rust type. |
| Evidence routes | Existing status/sources/ask/task routes only; worker methods ping/status/health/sources.list/query.ask. No desktop `evidence.get`, graph reads, neighbors, document browsing or page rendering. Do not use direct DB/MCP/filesystem reads to fill the gap. |
| Answer metadata | Recorded abstention reason, warning codes, index coverage, scope count and timings; display through `answer_meta`, with safe unknown labels and absent “not recorded.” Citation resolution is not independent semantic verification. |

## Exact approved source inventory for later extraction

All prototype anchors below refer to its unchanged working files, not committed HEAD alone. The [hash manifest](c0/approved-source-manifest.json) pins the exact source snapshot; approved visual references remain prototype `docs/ui-prototype/polish/` and `POLISH_REVIEW.md`.

| Source component | Reuse and dependencies | Planned checkpoint |
| --- | --- | --- |
| `theme.rs` (232 lines) | `Theme`, `Tokens`, `Theme::tokens/apply`, two approved palettes, native typography and contrast test. egui only; **no simulated state dependency**. | C1 |
| `shell.rs` (309 lines) | `Destination`, `Layout::for_width`, spacing, vector icons, primary/input/modal controls, row/status/prose helpers, citation link and response header. Depends on theme tokens and egui only; **no demo calls**. | C1 shared primitives; later citation consumers C3/C4 |
| Prototype `ui.rs:405–632,1612–1808,1809–1873` | Appearance, navigation/header, palette/help and shell panel geometry. Mixed with `Command::Demo*`, simulated footer and demo Settings branches; extract only real navigation/appearance. Keep live `StatusView` consumers. | C1 shell, with later state mapping C2 |
| `ui.rs:278–334,691–1051` | Composer, submitted scope, hierarchy, inline citations and copy. Depends on actual pending/result/source state but also demo timer and simulated answer labels; omit simulation, keep live AskGate. Raw metadata bypasses must use target `answer_meta`. | C2/C3 |
| `ui.rs:410–415,901–909,1052–1109` | Evidence selection/close/focus and inspector. Optional demo metadata must be removed; use only returned excerpt and conservative contract disclosure. | C4 |
| `ui.rs:1121–1611` | Empty-library setup, source list/detail and counts. Sample-load action and demo sample passage must be omitted; replace simplified readiness/count decisions with target `source_view`/`SourcesState`. | C5 |
| Prototype `model.rs` additions | Clone/continuity/scope/count helpers; no wholesale replacement. Preserve target R1 reducer. Do not introduce duplicate readiness authority. | C2 onward |
| `demo.rs`, timer pump, `demo_scene`, `load_sample_library`, demo commands, fictional metadata and sample actions | **Excluded from live transfer.** They depend on fake Northstar answers, IDs, locations, counts and elapsed-time simulation. No fallback from live failure to demo. | Never in initial integration |
| Prototype `main.rs`, `tools/capture_window.py` | Do not copy startup wholesale: retain normal app ID/socket behavior and `mod status_view`; omit `--demo` startup. Capture tool remains optional review tooling. | C1 selected startup edits only |

### Future C1 file boundary

C1 is **not authorized by this C0 execution**. Once separately approved:

- Add `app/docket-app/src/theme.rs` and `shell.rs` from reviewed primitives.
- Modify `main.rs` only for module/theme startup and approved viewport geometry; retain app ID `docket`, live socket startup and status module.
- Modify `ui.rs` for shell/navigation/appearance and tokenized existing drawing. Keep its live state/event handling intact initially. Map `Tone` through tokens; keep all five status chips, banners and source/answer metadata accessible.
- No planned changes to `backend.rs`, `model.rs`, `status_view.rs`, protocol, daemon, Python, manifest/lockfile or schema. Later C2 scope/state changes remain separate.
- Expected added dependencies: **zero**; eframe remains 0.37.0 with existing glow/default-font/X11/Wayland features. No downloaded fonts, icon crates or new persistence.

Main integration risks are overwriting R2a-3 with the older prototype, silently widening missing scope, conflating cached excerpts with current authorized evidence, transferring simulation, Stop/result UI races, and longer real status labels crowding narrow views. Navigation/scroll/focus and native accessibility remain unverified. Resolve them at the approved checkpoints, not by altering baseline code here.

## C0 stop condition

C0 stops with this report and its evidence. No theme, shell, UI, backend, protocol, daemon, Python, M3 or database implementation changes have been made. Review C0 findings before authorizing C1.
