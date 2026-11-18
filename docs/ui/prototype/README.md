> **Latest focused polish:** [conversation, citations, composer and first-run review](POLISH_REVIEW.md), with updated native screenshots and 92 passing tests. The review below describes the earlier approved-theme refinement.

# Docket — refined native desktop prototype

2026-11-18 · Obsidian & Silver / Porcelain & Ink · uncommitted, unmerged, unpublished.

Continues the existing **`codex/ui-prototype`** worktree at `/home/pc/Desktop/Rajdeep_Dev/Local_Agent/.worktrees/ui-prototype`. Base remains local `rust-app` **`599748a`**, including R1 and the R2a data plane. No second implementation or new branch was created. The original `rust-app`, R2a and M0 worktrees were not edited.

This iteration supersedes Mineral/Graphite. The earlier review and screenshots remain archived in [FIRST_PROTOTYPE_REVIEW.md](FIRST_PROTOTYPE_REVIEW.md) and `screenshots/`; use **`refined/`** for the current direction. The four design documents in the original checkout were read as the structural baseline; this review and the implementation supersede their earlier theme names and palette. Those original documents were not changed.

## Run

From this worktree's `app/` directory:

```sh
CARGO_TARGET_DIR=/tmp/docket-ui-target CARGO_BUILD_JOBS=2 cargo run -p docket-app --offline -- --demo
CARGO_TARGET_DIR=/tmp/docket-ui-target CARGO_BUILD_JOBS=2 cargo run -p docket-app --offline -- --demo --theme porcelain
```

Default theme: Obsidian & Silver. `--theme porcelain` starts Porcelain & Ink; `--theme obsidian` selects the default explicitly. Settings and the command palette switch themes without resetting draft, source, conversation or evidence. Ctrl Shift T remains a keyboard shortcut. Appearance is window-local and is not saved after restart.

**Use `--demo` for design review.** Demo mode never constructs `Backend`, resolves its socket, launches a daemon, loads a model, reads a library, or honors normal-mode auto-ask. Source names, passages, counts, locations and versions are fictional in-memory fixtures. Wide layouts identify the workspace as Demo with “Simulated sources & answers”; compact layouts show Demo, and individual answer/source/passage surfaces also identify simulation. Copied simulated content includes a simulation prefix.

Demo scenarios are available only inside the demo configuration: palette commands or Settings → Demo scenarios (collapsed). There is no persistent example-state toolbar, theme toolbar, prototype footer, keyboard-help button or debugging overlay in the everyday interface. Normal mode retains the existing backend startup and transport behavior.

## Design decisions

One interface, two token sets. Geometry, layout, typography, icons, keyboard bindings and component behavior are shared. Obsidian uses charcoal/graphite with silver text and steel-blue interaction colors; Porcelain uses off-white with ink text and dark teal. [All implemented tokens and contrast ratios](CONTRAST.md) are recorded separately.

- The 216-point sidebar carries Docket branding, Commands, destination selection and a Settings action. The system indicator appears once in its footer. At widths below 900 the header supplies compact destination, command, Settings and health/demo controls instead. Future destinations are readable noninteractive labels with a Planned description; only Assistant, Library and appearance Settings are actionable here.
- Quiet controls use a hover surface and focus treatment instead of outlining every button. Inputs retain a deliberate essential boundary. Primary actions have explicit idle/hover/pressed colors. Small geometric icons are drawn in Rust with consistent strokes, avoiding missing platform glyphs or a new icon dependency.
- Assistant centers a reading column up to 880 points, bounded by actual available width. Question → Answer → Sources → optional Evidence is the hierarchy. No avatar, glowing mark, decorative dashboard or unrelated statistics competes with the content. Answer diagnostics are collapsed. The composer stays reachable below an independently scrollable transcript and accepts a next draft during streaming.
- Library uses compact source rows and a focused detail region. Readiness, indexing ratio and a current-version warning are immediately visible. Historical failed/skipped aggregates, accounting units and technical probes are disclosed under Processing outcomes and Source details & diagnostics. No filenames are inferred from aggregate data.
- Evidence opens only when requested. At 1220 points and wider it is a resizable 340–480-point side panel, initially 384. Below that it becomes a contextual full view with Close/Escape. The selected whole passage has a quiet tint and a leading rule; this denotes selection, not a claim-support span or semantic verification.
- Modals share a 24-point inset, 10-point radius, subtle border and bounded scroll body. Source selection uses explicit Apply/Cancel. Essential radio boundaries have control contrast rather than the faint surface treatment used by quiet actions.

Typography uses existing egui bundled fonts: headings 23, body 15, reading prose 16 with 25-point line height, control labels 14, captions 13, fine ancillary labels/IDs 12. No font download or dependency was added. Common spacing is 8/12/16/24/28/32, control height at least 36 (rows 38), radii 6 for controls and 10 for composer/dialogs. Text and meaningful state use contrast-tested tokens; subtle dividers are not used as the sole indication of an interactive boundary. Native disabled primary actions remain visibly dimmed.

These choices apply familiar desktop navigation and shortcuts, group the question with its answer and sources, provide larger complete-row hit targets, disclose advanced details only on demand, show system/task state, and preserve deliberate source selection and reversible context navigation. No new framework or backend architecture was needed.

## Actual desktop screenshots

All main review captures use the same synthetic data for matched light/dark views. These are actual running egui windows captured through X11 with **llvmpipe software rendering**, not image-generated concepts. No GPU inference or benchmark was run.

| View | Obsidian & Silver | Porcelain & Ink |
| --- | --- | --- |
| Populated Assistant | [01](refined/01-assistant-obsidian.png) | [02](refined/02-assistant-porcelain.png) |
| Library, same selected source | [04](refined/04-library-obsidian.png) | [05](refined/05-library-porcelain.png) |
| Evidence, same passage | [06](refined/06-evidence-obsidian.png) | [07](refined/07-evidence-porcelain.png) |

Other requested views: [empty conversation](refined/03-assistant-empty.png), [command palette](refined/08-command-palette.png), [narrow Evidence](refined/09-narrow-evidence.png), [Assistant at 640 × 420](refined/10-narrow-assistant.png), and [narrow Library](refined/20-narrow-library.png).

Additional normal-transport captures: [Stop](refined/18-normal-stop-fake-worker.png) and [successful next question](refined/19-normal-recovery-fake-worker.png). These two captures use the existing **test fake worker**, not real documents or inference. Normal mode correctly has no demo banner; those images validate transport behavior only.

Interaction evidence: [processing disclosure](refined/11-processing-disclosure.png), [scope picker at 640 × 420](refined/12-scope-picker.png), [appearance Settings at 640 × 420](refined/13-settings.png), [streaming with Stop](refined/14-streaming-stop.png), [cancelled](refined/15-cancelled.png), [failure](refined/16-failure.png), [unavailable passage](refined/17-evidence-unavailable.png).

Narrow conversation captures retain their scroll position. Earlier messages are accessible by scrolling above; the primary composer and context actions remain visible. Screenshot inspection caught a source row that extended behind the inspector, a faint light-theme progress fill, a wrapped command label and insufficient modal inset. Those were corrected and the affected views recaptured.

## Keyboard and interaction checks

| Action | Behavior |
| --- | --- |
| Ctrl 1 / Ctrl 2 | Assistant / Library, preserving unfinished draft |
| Ctrl K | Command palette; typing filters, Up/Down select, Enter executes, Escape closes |
| Settings → Appearance | Same-window theme switch; selected choice is explicit |
| Ctrl Shift T | Theme shortcut retained for keyboard users |
| Ctrl Enter | Submit once; ordinary Enter inserts a newline |
| Ctrl Shift X | Request Stop when foreground question supports it |
| Tab / Shift Tab, Enter / Space | Traverse and activate egui controls |
| Escape | Close top context; never silently cancel a question or discard a draft |
| Library → Ask about this source | Set next-question scope and focus composer; no automatic submission |
| Scope picker | Stage a choice, then explicitly Apply or Cancel |
| Source row / citation | Open evidence; Close/Escape returns to its trigger |
| F1 or Settings → Keyboard shortcuts | Contextual help, with no permanent help control |

A missing/revoked selected source stays selected and blocks asking until an explicit valid choice; the UI never widens scope automatically. The submitted question records its original scope while the next-question selection can change. The demo completion uses submission-time scope as well. Stop discards partial output, including a racing result; normal-mode wording still cannot confirm the model engine stopped.

## Changed-file summary

Changes remain within `app/docket-app/` and prototype documentation:

| File | Refinement |
| --- | --- |
| `src/theme.rs` | Two approved themes; semantic surface/text/state/evidence/control tokens; contrast test |
| `src/shell.rs` | Shared row buttons, vector icons, planned labels, prose, notices, primary actions, input/modal frames, radio styling and responsive constants |
| `src/ui.rs` | Quieter shell; Settings/palette integration; reading column and composer; Library rows/disclosure; selected-passage viewer; focus/Escape handling; new UI regression |
| `src/demo.rs` | Concise synthetic answer/passages; unavailable, abstention and empty-conversation scenarios remain visibly simulated |
| `src/main.rs` | New theme names/default and updated CLI help |
| `src/model.rs` | Earlier prototype's health/readiness/count/scope helpers retained; R1 reducer retained |
| `tools/capture_window.py` | Existing optional X11 window-only capture helper reused |
| `docs/ui-prototype/` | Current review, token/contrast report and native screenshot set; first iteration archived |

`backend.rs`, `docket-proto`, `docketd`, all Python/KAG/retrieval/ingestion code, Cargo manifests/lockfile and database schemas are unchanged from `599748a`. No new application dependencies, source writes or inference calls were introduced.

## Verification

- `cargo fmt --all --check`: pass.
- `cargo clippy --workspace --all-targets --offline -- -D warnings`: pass.
- `cargo test --workspace --offline -- --test-threads=1`: **89 passed**, no failures — desktop 16, protocol 24, daemon unit 10, daemon API integration 39.
- `git diff --check`: pass.
- Contrast test: reading text ≥4.5:1 across reading/selected/highlight/hover/pressed surfaces; metadata and semantic-state text ≥4.5:1 on principal surfaces; essential control and focus tokens ≥3:1; primary-action text ≥4.5:1 at idle/hover/pressed. See exact ratios in [CONTRAST.md](CONTRAST.md).
- Native interaction: both themes, direct Settings selection, palette filtering/Enter, destination shortcuts, Library source action, staged source selection and Apply, citation opening, Escape return, narrow layouts, processing disclosure, streaming Stop, failure and unavailable passage were exercised. Headless tests additionally check draft continuity, missing-source scope, unknown counts, unavailable/abstention fixtures, Settings dismissal and evidence focus return.
- Normal transport smoke: real daemon on a temporary Unix socket with the existing **fake worker**, then normal desktop auto-ask, hanging fake request → UI Stop → worker restart → next successful answer. This proves UI/transport continuity, not real inference quality or Ollama stop behavior.

One parallel full-suite run hit a `BrokenPipe` in the unchanged `task_routes_enforce_the_same_input_limits` test. That test passed in isolation, and the final complete serial run passed all 89 tests. No daemon/test code was changed to suppress the failure. The cause was not established by this UI task.

The daemon tests need local Unix-socket permission and use only fake workers/temp data. Build concurrency was limited to two jobs with an isolated `/tmp/docket-ui-target`. No real-library test, Python suite or M0 evaluation was run. Native capture requires the helper's existing system X11/XTest libraries and Pillow; these are review-tool requirements, not app dependencies.

## Known limits and main-application integration

The prototype is a visual and interaction candidate, not a production accessibility claim. Orca, Wayland, IME/non-Latin input, high DPI, 200% text scaling, keyboard-only traversal of every control, large-library performance and real inference latency still need target-platform testing. The egui bundled fonts are retained; no claim is made that all required scripts are shaped correctly.

The normal evidence API still supplies an answer-time excerpt and limited identifiers. Location/version are shown only when supplied by the demo fixture; normal mode says not recorded. Highlighting selects the entire returned passage and never invents a supporting span. Full passages, neighbors, PDF pages, original-file navigation and live revocation/purge checks remain unavailable. The missing-excerpt state removes passage text. It does not establish that a cached excerpt with text is currently authorized; live access needs the later read API. The Library passage action is explicitly a sample action in demo mode; genuine Library→document→evidence navigation awaits that contract.

There is no retained chat history, backend multi-turn memory, document-management writer flow, permission broker, voice or Knowledge/Investigations/Activity implementation. Appearance Settings covers theme selection only. Conversation and theme state are window-local. Citation markers are inline text with actionable source rows below; custom inline link navigation and located span highlighting remain future work. The health indicator reports service connection, not proof of indexing readiness. Source-list usable counts retain the legacy reported numeric field; rich unknown handling applies to optional producer counts, not inferred document totals.

Recommended integration order:

1. Review the paired screenshots and the scope/evidence/Stop workflows with users before moving the component set into `rust-app`. Keep this work isolated until explicitly approved; no commit, merge or push has been made.
2. Reuse the theme/components and shell around the unchanged transport. Preserve R1 task/cancel semantics, fallback, source boundaries and citation resolver behavior. Run the existing protocol/fake-worker suite on integration.
3. For the next R2a-3 slice, bind only actual capability-gated read contracts. Preserve producer freshness/unknown reasons and count units. Do not infer per-file skipped/failed names from aggregates or equate active lifecycle with readiness. Keep advanced diagnostic codes behind disclosure.
4. Gate full evidence viewing on authorized R2b/R4 reads, M7 metadata and M1 boundaries: source/version/location, bounded exact passage, located span, neighbors and typed unavailable states. Add live purge/revocation handling before calling the viewer complete. Normal-mode source resolution must never be labelled independent semantic verification.
5. Connect persisted sessions to M4/R3 and source lifecycle writes to R7/M3 separately. Do not use the UI's local transcript as an authoritative audit record. Keep future destinations inactive until their contracts and acceptance gates exist.
6. Validate native accessibility and scale with the actual compiled feature set before release; preserve egui unless a specific required workflow remains blocked after mitigation.

Before integration, moderate these exact tasks: choose a partially indexed source and ask within it; inspect each cited passage and return; explain failed/skipped/unknown processing units; stop a slow request and ask again; navigate/change theme without losing a draft; recover from unavailable evidence and a missing selected source; repeat with keyboard and narrow windows. Record confusion about simulation, citation resolution and model-stop uncertainty instead of relying on visual preference alone.
