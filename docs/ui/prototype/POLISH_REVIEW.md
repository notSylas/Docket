# Docket — focused conversation and interaction polish

2026-11-18 · existing `codex/ui-prototype` worktree · awaiting visual approval.

This pass keeps **Obsidian & Silver** and **Porcelain & Ink**, the navigation, semantic color tokens, egui/eframe architecture and existing transport. It refines the approved prototype rather than introducing another design direction. No backend integration was started. Nothing was committed, merged or pushed.

## Review the actual native UI

All captures are running egui windows with explicit simulated data, rendered using llvmpipe software OpenGL. No model, real library or GPU inference workload was used. Matching theme captures use the same source data and selection.

| Required view | Dark | Light |
| --- | --- | --- |
| Assistant with Evidence open | [01](polish/01-assistant-evidence-dark.png) | [02](polish/02-assistant-evidence-light.png) |
| Library | [03](polish/03-library-dark.png) | [04](polish/04-library-light.png) |
| Empty-library onboarding | [10](polish/10-empty-library-dark.png) | [05](polish/05-empty-library-light.png) |
| Narrow Assistant, 640 × 620 | [08](polish/08-narrow-assistant-dark.png) | [06](polish/06-narrow-assistant-light.png) |
| Minimum-size composer, 640 × 420 | [16](polish/16-minimum-composer-dark.png) | [17](polish/17-minimum-composer-light.png) |
| Narrow Library | [20](polish/20-narrow-library-dark.png) | [19](polish/19-narrow-library-light.png) |

Other interaction evidence: [narrow Evidence](polish/07-narrow-evidence-light.png), [drag-resized inspector](polish/09-evidence-resized.png), [narrow onboarding](polish/11-empty-library-narrow.png), [source setup instructions](polish/12-source-setup.png), [streaming composer](polish/13-streaming-composer.png), [editable next draft while streaming](polish/14-streaming-next-draft.png), [Stop preserves that draft](polish/15-cancelled-with-draft.png), and [selection of the second passage](polish/18-second-passage.png).

## What changed

- **Conversation hierarchy:** the user's question has a compact, right-aligned surface with source context. A distinct Docket response header anchors the generated answer. Answer paragraphs use 16-point text with 24-point line height; the centered work column is capped at 800 logical points and shrinks with the inspector. This replaces the oversized question-heading/document-page composition without adding outlined cards around every message.
- **Interactive citations:** numbered inline buttons and persistent source controls both open Evidence. Source controls explicitly say Open passage or Viewing passage, use the approved interactive colors, and reflect the selected source. Unknown citation numbering still goes through the existing tested layout model; document content does not create new tool or permission paths.
- **Evidence:** the existing contextual inspector is retained. It highlights the selected whole simulated passage and shows its fictional location/version. Selecting another citation updates the passage, selected control and return-focus origin. Mouse Close and Escape return to the conversation. The actual side-panel handle was dragged from x=1056 to x=980 at 1440 width, expanding the panel from 384 to 460 points. The panel becomes a full contextual view at narrow widths.
- **Composer:** normal, observed-empty-library, worker-unavailable, busy, streaming, stop-requested and cancelled states have explicit presentation. The Ask action stays disabled with an explanation when appropriate. Streaming allows editing the next draft and displays Stop. Stopping discards partial output, keeps that next draft, and restores Ask once allowed. Focus has a stronger visible boundary. Ctrl Enter, ordinary Enter, Ctrl Shift X, Ctrl K and existing navigation/theme shortcuts remain.
- **Library:** rows pair a distinct readiness label with a separately reported usable count. The seven-source fixture now covers ready, indexing, partly ready, unknown, failed, empty and disconnected states. Unknown counts say Unknown; observed empty counts say zero. Focused source details explain the state in ordinary language; diagnostic accounting remains collapsed. Source lifecycle is never presented as proof of indexing readiness.
- **First run:** a genuine observed empty library gets a calm three-step introduction and actionable setup guidance, without an error banner or an empty search/detail table. Setup opens documented CLI instructions and runs no commands. In demo mode, Explore sample library loads only synthetic source rows and preserves a draft written before setup. Connection failures remain distinct from a normal empty snapshot.

The approved color tokens and theme implementation were not changed. New surfaces, highlights, status text and controls use the existing semantic tokens. No dependencies, fonts, protocol fields or data schemas were added.

## Changed files in this pass

| File | Scope |
| --- | --- |
| `app/docket-app/src/ui.rs` | Question/response hierarchy, inline citation actions, centralized evidence selection, composer state presentation, onboarding/setup and sample-library loading, readiness rows, focused UI regression tests |
| `app/docket-app/src/shell.rs` | Shared response header and interactive citation source control using existing tokens |
| `app/docket-app/src/model.rs` | UI-only source-summary helper distinguishing missing counts from an observed zero |
| `app/docket-app/src/demo.rs` | Unknown/failed/empty fixtures and readiness/count regression test |
| `app/docket-app/tools/capture_window.py` | Optional window-local drag and scroll support for native resize/reading checks |
| `docs/ui-prototype/POLISH_REVIEW.md`, `polish/`, README pointer | Current handoff and actual native captures |

The branch already contained earlier uncommitted prototype work. This table describes this focused pass; the broader working-tree diff also includes that earlier work. `main.rs` and `theme.rs` retain the previous approved implementation. `backend.rs`, all `docket-proto`, `docketd`, Python/KAG/ingestion/retrieval files, Cargo manifests/lockfile and database schemas remain unchanged from the branch base. The main `rust-app`, M3 and evaluation worktrees were not edited or integrated.

## Verification performed

- `cargo fmt --all --check`: pass.
- `cargo clippy --workspace --all-targets --offline -- -D warnings`: pass.
- `cargo test --workspace --offline -- --test-threads=1`: **92 passed**, zero failures — app 19, protocol 24, daemon unit 10, daemon API integration 39. Serial execution avoids the parallel socket-test timing failure documented in the previous pass. No daemon/test suppression or protocol changes were made.
- `git diff --check`: pass.
- The existing theme contrast tests pass with the unchanged palette.
- New regression coverage checks all requested source-readiness fixtures, unknown versus known-zero counts, draft continuity through sample-library setup, composer state transitions through streaming/cancelling/cancelled, and evidence selection/close preserving draft and scope. Existing scope, citation numbering, cancellation, fake-worker and protocol regressions still pass.
- Native checks exercised inline citation selection, selection of another source control, mouse Close, Escape return, inspector drag-resizing, runtime theme switching, normal/narrow Assistant and Library, empty onboarding, setup instructions, streaming, next-draft editing and Stop. At 640 × 420 the composer and primary action remain reachable; the transcript scrolls independently.

Build jobs were limited to two, with artifacts in `/tmp/docket-ui-target`. The native app runs in `--demo`; test daemons use existing fake workers and temporary Unix sockets. No real worker, database, ingestion, M3 functionality or inference service was used for this pass. The optional capture helper targets only the named Docket window.

## Remaining UI limits

- This is an isolated visual candidate. Real source setup still uses the CLI; there is no folder-import/writer integration. No unfinished M3 feature is represented as working.
- Inline citation controls are separate egui widgets between selectable text spans. Copy answer with sources retains a whole-answer copy path; drag selection across every text span and citation widget is not equivalent to a browser's continuous selection. A citation can wrap to the next line when space is limited.
- Evidence still uses the current answer-time excerpt contract. Full authorized passages, neighbor navigation, page rendering, stored located spans and live purge/revocation checks need later read APIs. The selected-passage tint denotes selection, not independent semantic verification. Real-mode missing metadata remains unrecorded rather than fabricated.
- UI text remains window-local. No conversation persistence, backend multi-turn memory, production ingestion or permission flow was added. Normal-mode model-stop wording remains unverified as required by R1; the demo explicitly states no model was running.
- Screen readers, IME/non-Latin shaping, Wayland, high DPI, 200% text scaling, every keyboard-only path and large-library performance have not been validated by these captures. The unchanged tokens' contrast test does not establish full native accessibility conformance.
- Narrow windows show a scrollable reading or setup region. At the desktop minimum, earlier transcript content may be above the viewport; the current draft and primary action remain visible.

## Run for visual approval

From the isolated worktree's `app/` directory:

```sh
CARGO_TARGET_DIR=/tmp/docket-ui-target CARGO_BUILD_JOBS=2 cargo run -p docket-app --offline -- --demo
# Start in the light theme:
CARGO_TARGET_DIR=/tmp/docket-ui-target CARGO_BUILD_JOBS=2 cargo run -p docket-app --offline -- --demo --theme porcelain
```

Demo scenarios remain in the command palette or Settings → Demo scenarios. Use “empty workspace” to reach onboarding, “slow response” to exercise the composer, and “restore sample answer” to review citations. Theme switching preserves the current UI state. Exploring the sample library through onboarding preserves an unfinished draft; deliberately choosing a reset-style demo scenario resets its fixtures.

**Stopped for visual approval. Backend integration has not begun.** Review the question/answer distinction, citation affordances, selected passage, composer states and first-run setup before authorizing any integration work.
