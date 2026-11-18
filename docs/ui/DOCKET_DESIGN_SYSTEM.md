# Docket design system

Proposal · 2026-11-18 · Docs only. Tokens are specifications, not installed themes, fonts, assets, or dependencies. Apply only after the low-fidelity screen flows in [screen specifications](DOCKET_SCREEN_SPECIFICATIONS.md) have been reviewed.

## 1. Visual intent and exploration

The identity is a professional research desk: composed typography, quiet navigation, precise document structure, and small useful marks. Evidence should be easier to see than model branding. Do not use glowing borders, purple gradients, glass cards, star fields, pulsing orbs, oversized greetings, or a command-center wall of metrics. Warmth comes from surfaces and spacing; distinctiveness comes from the source margin and evidence connections.

| Direction | Surface / ink / accent | Best use | Risk to test |
| --- | --- | --- | --- |
| Mineral desk | Limestone `#F4F1EB`, charcoal `#252925`, oxide `#825B45` | Long reading and document inspection; proposed light default | Beige can look muddy if borders/text are too faint |
| Graphite workshop | Graphite `#202321`, pale stone `#E7E8E2`, sage `#9BAE9C` | Dense lists and evening work; proposed dark theme | Too little surface separation can flatten hierarchy |
| Industrial ledger | Chalk `#F5F6F2`, ink `#283238`, steel `#476575` | Alternate restrained accent exploration | Blue-grey can become generic enterprise software |

Develop the first two as a paired system. Use the third only in a same-layout accent comparison; do not compare a polished layout against an unfinished one. Follow OS theme on first run; allow Light, Dark, System. No animated theme transition. Accent is never the only sign of scope, selection, failure, or trust.

## 2. Theme tokens

Hex values are candidate sRGB tokens. Ratios below are calculated using WCAG relative luminance, not visual guesses. Real widget combinations, disabled rendering, DPI, and antialiasing still require acceptance checks.

| Semantic token | Light | Dark | Use |
| --- | --- | --- | --- |
| canvas | `#F4F1EB` | `#202321` | Main work surface |
| navigation | `#EAE6DE` | `#1A1D1B` | Receding left navigation |
| surface | `#FFFFFF` | `#292D29` | Forms, inspector, popover |
| surface-hover | `#E9ECE5` | `#353B35` | Pointer hover |
| selection | `#DDE6DC` | `#38483B` | Selected row or destination |
| text | `#252925` | `#E7E8E2` | Reading and control labels |
| text-secondary | `#565E55` | `#B3BCB0` | Metadata, subtitles |
| text-disabled | `#687066` | `#9AA596` | Disabled label; also provide reason |
| border-subtle | `#D5D2C9` | `#454C43` | Nonessential separators only |
| border-control | `#737B70` | `#889581` | Essential input/control boundary |
| accent | `#425F49` | `#ACC2AA` | Links, primary actions, selection mark |
| on-accent | `#FFFFFF` | `#202321` | Solid primary button text |
| focus | `#315D78` | `#A6C9E2` | Keyboard focus only |
| evidence-marker | `#825B45` | `#D7B59B` | Citation number / source rule |
| evidence-fill | `#F1E6D6` | `#493E31` | Located quote/span; text stays text token |
| success | `#3E6545` | `#A9C9A6` | Completed / searchable, with label |
| warning | `#765418` | `#E3C286` | Partial/keyword-only/stale, with label |
| danger | `#9B3633` | `#EBA6A0` | Failure/destructive action, with label |
| neutral-status | `#565E55` | `#B3BCB0` | Queued/unknown/cancelled, with label |

Use status text on canvas/surface, not on an arbitrary saturated badge. Tinted badge fills must be checked separately before adoption. Do not dim streamed draft text below normal reading contrast: a visible “Draft — citations not checked” label and side rule carry its status.

Border-subtle is intentionally not a sufficient standalone input boundary. Essential outlines, checkbox marks, graph edges that convey meaning, selected states, and focus must meet the non-text contrast target against adjacent surfaces. Focus is a 2-point outline with 2-point offset where room exists, and an interior outline in clipped rows. Selection has fill plus a 2-point leading rule and explicit accessible selected state.

Calculated checks for the proposed pairs (rounded to two decimals):

| Foreground / background | Light ratio | Dark ratio |
| --- | --- | --- |
| text / canvas | 13.10:1 | 12.87:1 |
| text-secondary / canvas | 5.96:1 | 8.11:1 |
| text-secondary / surface | 6.71:1 | 7.15:1 |
| on-accent / accent | 7.08:1 | 8.35:1 |
| focus / surface | 7.08:1 | 8.04:1 |
| border-control / surface | 4.38:1 | 4.44:1 |
| text / selection | 11.55:1 | 7.89:1 |
| text / evidence-fill | 11.97:1 | 8.45:1 |

## 3. Typography

Proposed family: **Noto Sans** for UI and prose; **Noto Sans Mono** for identifiers, numeric columns, event codes, and CLI snippets. Evaluate licensed local bundling and script fallback in R4/R10; no font is downloaded or added by this proposal. The first egui prototype may use its current bundled fonts, labelled as a typography approximation. Do not assume system-font discovery or Indic shaping works without testing.

| Role | Size / line height (logical points) | Weight / rule |
| --- | --- | --- |
| Destination title | 22 / 28 | Semibold; one per view |
| Section title | 17 / 24 | Semibold |
| Answer / passage | 16 / 25 | Regular; selectable, 60–78 characters per line |
| UI body / table | 14 / 20 | Regular; medium for primary row label |
| Metadata / helper | 13 / 18 | Regular; secondary token |
| Tiny ancillary label | 12 / 16 | Rare, never primary evidence or error instruction |
| Code / task ID | 13 / 20 | Mono; tabular digits where supported |

No artificial uppercase for navigation; ordinary sentence case. No decorative serif is required. Evidence remains proportional for readable prose, mono for literal tables/code. Preserve whitespace, units, leading zeros, and verbatim text; wrapping must not alter copied evidence. Increase text scale to 200% without losing actions or content. Long document names wrap in detail headers; list truncation exposes the full value on focus and via copy.

## 4. Geometry and responsive behavior

All dimensions are egui logical points, not physical pixels. Spacing scale: 4, 8, 12, 16, 24, 32, 48. Default panel padding 24, compact panels 16, label-to-field 8, section gap 24, dialog padding 24. Radius: 4 for inputs/chips, 6 for panels/menus, 8 for dialogs. Reserve shadows for transient overlays; use borders for permanent regions. Icons are 16 or 20 points with consistent stroke, 32-point minimum hit area; comfortable mode targets 40 points, 44 for prominent touch-like controls. Dense tables use 32-point rows, comfortable tables 40; neither reduces text size.

| Available width | Arrangement | Inspector / local navigation |
| --- | --- | --- |
| ≥1280 | 184 nav + main (≥520) + 320–440 inspector | Resizable inspector; local lists may sit inside main |
| 960–1279 | 168 nav + main; inspector explicitly opened | Opening inspector may replace local list or use a sheet; never squeeze answer below 440 |
| 720–959 | Nav behind labelled menu | Single main view; inspector replaces main with Back |
| 640–719 | Compact desktop minimum | One column, wrapping toolbars, labelled navigation sheet |
| <640 effective width, including zoom | Accessibility fallback | Continue single-column scrolling; do not clip controls even if OS permits a smaller window |

Current minimum is 640×420 and default window 1040×720; these are constraints to test, not permission to raise the minimum. At 420 height keep the primary action visible, bound composer to 35% of work height, and allow reading content to scroll. At 200% text scale choose layout from available effective space, not raw window pixels. A modal fits viewport with scrollable body and reachable footer. Tables prioritize name, state, and action; secondary columns become row detail. A graph always has a table equivalent and must not become the only narrow-width experience.

## 5. Navigation, keyboard, focus

Keep the six destination labels visible at normal width; do not replace them with ambiguous icons. Settings sits at the bottom but remains sixth in order. Badges count actionable items only, never fabricated unread activity. One primary action per local toolbar. Breadcrumbs indicate source/document/entity lineage; they are not an additional navigation bar.

Proposed Linux bindings, all exposed in the palette/help:

| Binding | Behavior | Protection |
| --- | --- | --- |
| Ctrl+K | Command palette | In-app, no global keyboard grab |
| Ctrl+1…6 | Six destinations in fixed order | Suppressed during shortcut recording; rebind conflicts later |
| Ctrl+N | New question in Assistant | Does not cancel running work or erase an unsubmitted draft |
| Ctrl+Enter | Submit composer / confirm ordinary form | Never approves Act execution or a destructive dialog |
| Enter / Shift+Enter | Newline in composer / newline | Send always has visible Ctrl+Enter hint; do not hijack IME commit |
| Ctrl+Shift+X | Request Stop for foreground task | Active only when a cancellable task is explicit; no clipboard conflict |
| Ctrl+F | Find/filter within current view | Full content search is separately labelled |
| Alt+Left | Back within object navigation | Preserve list state and draft |
| F6 / Shift+F6 | Next / previous major region | Nav → toolbar → main → inspector → composer if present |
| Escape | Close topmost transient UI, then inspector | Never silently cancels a task, discards a draft, or answers approval |
| F1 | Shortcut/help sheet | Visible Help action also provided |

Arrow keys traverse a focused list; Enter opens selected item; Space toggles an explicit checkbox, not an invisible default action. Tab reaches controls; lists may use one roving tab stop with announced row position. Focus order follows reading order and is stable through async updates. Opening an inspector focuses its heading; closing returns to the triggering citation. New streamed text must not steal focus or force scrolling when the user has scrolled up. Show “New output” instead.

## 6. Component patterns

| Pattern | Contract |
| --- | --- |
| Answer block | Question heading, scope, phase/status, prose, numbered source margin, concise citation-state line, actions beneath; no assistant avatar required |
| Citation | Number + document + location where available; button opens Evidence. Pointer and keyboard share behavior; never only a tooltip |
| Evidence inspector | Document/version/location, availability, verbatim passage, optional neighbors/page, original-change note, copy with citation; no generated paraphrase presented as evidence |
| Source/document row | Name, usable/readiness state, accounting summary, updated time, overflow actions; selected state separate from checkbox selection |
| Status badge | Icon + plain label; unknown/stale/empty/keyword-only are distinct; tooltip is supplemental |
| Progress | State and completed/total within a named unit; indeterminate when total unknown; no invented percentage or ETA |
| Task row | Kind, state, duration, task ID, Stop when allowed; no raw question by default |
| Event timeline | Timestamp, event label, code/details expansion; folds noise, never hides failure/cancel/approval |
| Buttons | Solid primary, bordered secondary, quiet contextual, danger with explicit verb; disabled reason visible nearby |
| Forms | Persistent label, helper before errors, inline validation, explicit Apply/Cancel for runtime/privacy changes; never silent partial save |
| Scope picker | Source names, counts with units, explicit Apply, empty selection explained; all-sources is a deliberate choice |
| Dialog | Named title, consequence, exact object/path where safe, explicit action and Cancel; focus starts on safe control for destructive actions |
| Toast | Noncritical acknowledgement only; actionable errors remain in place until resolved/dismissed |
| Resource sample | Value + unit + sampled time + stale/unavailable; no zero for missing data and no always-on animated charts |

## 7. Empty, loading, and failure vocabulary

| Situation | Copy / action |
| --- | --- |
| No library | “No sources connected.” → “Set up a source” (CLI instructions until R7) |
| No filter matches | “No documents match these filters.” → Clear filters |
| Source has zero usable passages | “No searchable passages yet.” → Review processing; never “Ready” from folder status alone |
| Keyword tier only | “Keyword search available. Semantic indexing is pending.” |
| Busy | “One question is running.” → View task / queue if capability allows; preserve draft |
| Stale health | “Last checked 24 seconds ago. A question is running.” → Show last known state, no fresh-green badge |
| Evidence unavailable | “This passage is no longer available.” → Close / inspect source status; remove cached text |
| Model unavailable | “Answer model unavailable.” → Models & runtime; allow Library browsing |
| Abstention | “I don’t know based on the available evidence.” → Scope and recorded reason; do not invent a cause |
| Cancel accepted | “Stop requested.” followed by actual worker result; “Partial answer discarded. The model engine’s stop cannot be confirmed.” |
| Daemon restart / lost tail | “Earlier task events are unavailable.” → New task; no fake replay or silent resume |

Loading placeholders retain expected structure without perpetual shimmer. Show the phase and elapsed time; retries must be deliberate and cannot duplicate side effects. Detailed technical codes belong in expanded diagnostics, not unexplained red banners.

## 8. Accessibility acceptance and native feasibility

Target WCAG 2.2 AA principles for text contrast (4.5:1 ordinary text, 3:1 large text), controls/focus (3:1 where applicable), keyboard reachability, and visible focus; this is a target, not a claim of native-app conformance. Official references: [contrast](https://www.w3.org/WAI/WCAG22/Understanding/contrast-minimum.html), [non-text contrast](https://www.w3.org/WAI/WCAG22/Understanding/non-text-contrast.html), [focus not obscured](https://www.w3.org/WAI/WCAG22/Understanding/focus-not-obscured-minimum.html). Recheck all text/selection/highlight combinations before visual sign-off.

Provide accessible names, roles, selected/expanded states, row counts, and meaningful reading order. Screen readers announce phase transitions and completion, not every streamed token. Graph relationships have a linear list with source buttons. Errors are associated with their field and recoverable without a pointer. Respect reduced motion; no continuous animation is needed. Match OS high-contrast needs through an explicit high-contrast fallback; theme colors alone do not establish that support.

R4 must test Orca on target Linux X11 and Wayland, keyboard-only passage inspection/copy, multiline input, Latin plus Hindi/other required scripts, IME composition, 200% text scaling, 125/150/200% display scaling, reduced motion, and keyboard resizing of panels. Current eframe features do not establish AccessKit is enabled/configured. Verify the exact build feature and semantics before making an accessibility claim. This task changes no dependencies.

Implementation feasibility: ordinary panels, tables, labels, text editors, scroll areas, and modal state fit the current egui model. Rich answer layout, selectable wrapped evidence, accessible virtualized rows, and PDF rendering are risks to spike. PDF pages may use isolated rendering later; do not embed a browser merely to get a different aesthetic. Reconsider egui only after a reproducible blocker against a required workflow, an attempted mitigation, and a written tradeoff assessment.

## 9. High-fidelity concept briefs, following the low-fidelity inventory

The full structural inventory is in the screen specifications. The following are visual concept specifications for those same layouts, not rendered mockups or implementation. Test structural comprehension with the low-fidelity frames before investing in polished artifacts. Use identical synthetic questions, passages, counts and errors for comparisons.

### H1 — Mineral evidence desk

At 1440×900: 184-point limestone navigation, 48-point title header, 24-point main inset, 400-point white evidence inspector separated by a single control-quality edge. Assistant content uses 16/25 proportional text with a narrow numbered source margin. The question is a 22-point heading; scope appears directly below at 13 points. A 2-point oxide rule connects the active citation number visually to the inspector's document heading; it is a selection cue, not a literal animated line across the application.

Use sage for the one primary Ask action and selected navigation mark. Use oxide only for citation identity and located evidence. A final answer has the same reading contrast as its draft, but its status changes from “Draft — citations not checked” to “Citations checked.” The inspector resembles a clean document leaf, not a rounded chatbot card. Processing screens use ruled rows with ordinary labels and one restrained warning symbol. Main content remains flat; shadows appear only for the palette or a decision dialog.

### H2 — Graphite evidence desk

Use exactly H1's geometry, reading size, sources, interactions and status text. Navigation is the darkest region; main graphite canvas is one step lighter; the evidence inspector uses the surface token. Pale stone text supplies contrast. The selected source number uses muted warm metal; sage primary controls have dark ink. Never render a light paper rectangle inside dark mode unless the user explicitly opens a page image whose original is white.

Library rows use subtle separators and a clearly bounded selected state. A stale status remains readable with an amber icon and timestamp; no desaturation trick is required. The palette is a flat raised surface with aligned labels, group names and keycaps. This concept should remain usable at low display brightness without near-black text or glowing decoration.

### H3 — Industrial ledger accent study

Keep H1's layout and content. Substitute the proposed chalk/steel palette for navigational/action accents while preserving oxide citation markers and semantic status colors. Use ordinary row rules and tabular numerals to support document-accounting work; do not introduce extra metadata columns merely to look technical. This is a controlled accent experiment, not a third competing information architecture. Adopt only if users prefer it without slower source discovery or confusion between links and statuses.

### Concept artifact and review checklist

For each selected direction, produce S02+S03 (answer/evidence), S06 Processing (partial/unknown states), S24 palette, and S00 at 640×420. Include a visible focus state and one model-unavailable error. Compare reading comfort, density, source discovery and trust-state comprehension before asking for an aesthetic preference. Rendered high-fidelity images and an interactive prototype are not claimed as completed by this documentation task. No code, font package or mock data has been installed in the application.
