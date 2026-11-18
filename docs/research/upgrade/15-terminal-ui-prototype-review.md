# Screen A prototype review (stage 4a: navigable shell, fake data)

Status: **Ready for visual review (polish pass applied after the first screenshot review).** Nothing here is connected to a backend. Section 1.1 lists what the polish pass changed; the screenshots below are regenerated from the current code.

Date: 10 October 2026. Implements stage 4a of [the Screen A design](13-terminal-ui-screen-a-design.md) section 9. Read it with [the checkpoint 1 note](14-terminal-ui-checkpoint-1.md) and [the handoff](13-codex-handoff.md). Its job is to let you judge appearance, navigation and wording before any wiring (stage 4b).

## 1. What this is

A full-screen terminal application with one prompt_toolkit event loop: a persistent header, a scrollable transcript, a multiline composer and a compact footer, with modal overlays for the command palette, scope, mode, sources, indexing, evidence, answer details, jobs, settings and the welcome/readiness screen.

Every record on screen (sources, files, answers, citations, models, job history) comes from one module, `fake_data.py`. There is no model, database, network or data-directory access. The header carries a `DEMO DATA` tag so a screenshot cannot be mistaken for the product. All fake actions change in-memory demo state only and never touch the file system.

Code lives in `backend/src/docket/interfaces/cli/tui/`:

| File | Responsibility |
| --- | --- |
| `app.py` | `DemoUI`: layout, key bindings, overlay stack, fake operations, command dispatch (the "interaction controller") |
| `overlays.py` | One class per overlay; each renders itself to fixed-width rows and handles abstract keys |
| `fake_data.py` | All demo content and the `World` demo state; the only place product-like strings live |
| `model.py` | `Scope`, `Message`, `IndexJob`, `QueryOp` view-model types |
| `textfmt.py` | Width-aware wrapping, Markdown-ish blocks (paragraphs, lists, fenced code, tables with a record fallback) |
| `theme.py` | Dark / Light / Terminal-default styles, ASCII glyphs and the ASCII text fallback |
| `headless.py` | Pipe-input driver used by the tests and to produce the screenshots below |

The plain interactive session (`interactive/`) is untouched. The prototype reuses its command registry (`default_registry()`), so names, aliases, prefix resolution and typo suggestions are the existing ones; `/scope`, `/jobs`, `/settings`, `/details`, `/rechunk` and `/reindex` are the proposed additions from section 4.4.

### 1.1 What changed in the polish pass

Driven by your review of the first screenshots. Fake data only; no service, retrieval, config, migration or eval code was touched.

Transcript

- The amber citation warning is separated from the Sources list by a blank line, so it cannot read as another source.
- Tables right-align numeric columns (header included) and keep a rule with column joints. Result words get a glyph and keep the text: `✓ Met`, `✗ Missed`; the ASCII fallback is `OK Met`, `X Missed`.
- Number-based shortcuts sit next to the F5/F6 hints: `[Evidence F5] [Ctrl+E] [Details F6]`, then `Open a source: /show 1 to /show 6`. The footer repeats them (`Ctrl+E evidence · /show N`) when there is room.
- A greeting such as "Hey" gets a short friendly reply with no citations, no sourced claims and a pointer to `/help`. Answer details then says "None (no search was made)".
- Duplicate file names in the Sources list show the shortest unique parent path (`policies/handbook.pdf` and `2023/handbook.pdf`). The demo answer now cites two files both named `handbook.pdf` (citations 5 and 6).
- The selected-answer marker `›` is explained: a line under the answer and the footer say "Enter opens evidence, Esc clears". Enter on an empty composer opens that answer's evidence; Esc clears the selection.

Overlays

- Evidence: spreadsheet passages render as a real grid with column letters, row numbers, a header rule and right-aligned numbers. The fake data now says `Sheet Summary · B7:D10` and has four rows (header plus three), which matches the grid; the old `B8:F8` with four rows was inconsistent. The cells the answer cited (`C8:C10`) are shown in `[ ]` and highlighted. Prose passages stay verbatim with their breaks.
- Evidence has a `Supports` line quoting the answer claim(s) that cite the passage, `Indexed 10 Oct 2026 10:42 · version 3 of 3 (current)` instead of "stored version 3", and a single `1 of 6` position text on the title row.
- One button shape everywhere: `[ Label ]`; focus adds a leading `▸` (and the reverse/colour style); a disabled button keeps its shape and is muted/italic. The mixed `>( )` and `[ ]` forms are gone. The duplicated key legend is reduced to one line.
- Answer details keeps the two-column layout and adds Status ("Not verified (prototype)"), Model and Passages used ("5 passages from 5 files, 14 searched"); wording is short and parallel ("Computation: Not used"). The honest "syntax valid; meaning not machine-verified" note and "Confidence: Not calibrated" remain.
- One width rule for every overlay: capped at 100 columns, two columns narrower than the terminal from 80 to 100, the whole width below 80; always centred.
- The transcript area behind a modal is cleared, so no cut-off transcript fragments appear beside or above it. The header, composer and footer stay.
- Long paths use a middle ellipsis; Sources shows the full path under its details toggle ("Full path").
- Sources details, Indexing and Welcome use labelled, aligned rows; the failed/no-text file list is an aligned table; Jobs is a table (Source, Result, When, Summary) with the selected row's summary shown in full below it. Dates and times use one format: `10 Oct 2026 10:42`.

## 2. How to run

No setup, no data directory, no Ollama. From `backend/` with the project environment:

```text
docket tui-demo                       # chat with a finished answer
docket tui-demo --state welcome       # new data dir: readiness screen
docket tui-demo --state welcome-blocked   # same, with a missing model and Ollama down
docket tui-demo --state indexing      # indexing progress overlay at file 7 of 24
docket tui-demo --state sources       # Sources panel open
docket tui-demo --state evidence      # Evidence panel open on citation [1]
docket tui-demo --state settings      # Settings open
docket tui-demo --reduced-motion      # no timers; press N in the indexing panel to step
docket tui-demo --ascii               # force ASCII borders (also DOCKET_ASCII=1)
```

The command is hidden from `--help`. It refuses to run without an interactive terminal. Exit with Ctrl+D, or `/exit`.

Try these paths; each exercises a different part of the design:

1. `--state welcome`: Enter on "Add a folder", type `/demo/work/re`, Enter (fills the suggestion), Enter (adds). Watch indexing start by itself, press Hide, then try to send a question.
2. `--state chat`: press `/`, type `scope`, Enter; pick Finance. Then F4 and try to choose Plan.
3. `--state chat`: F5, then N / P through citations; citation 3 has no location and citation 4 is unavailable.
4. `--state sources`: select each source, Tab to the action row, try Retry on a Ready source (disabled, with a reason) and Disconnect (confirmation, safe default).
5. Resize the terminal below 60x16 and back; type half a question first.

## 3. Key map

| Key | Behaviour |
| --- | --- |
| Enter | Send the question, or run a `/command` (composer); activate (overlay) |
| Alt+Enter, Ctrl+J | Newline in the composer (Alt+Enter is the portable one) |
| F1, or `/` on an empty composer | Command palette |
| F2 / F3 / F4 | Sources / Scope picker / Mode picker |
| F5 / F6 | Evidence / Answer details for the selected answer |
| Ctrl+E | Evidence (same as F5); `/show N` opens citation N of the latest answer |
| Enter on an empty composer | With an answer selected (`›`): open its evidence |
| F7 / F8 | Jobs / Settings |
| Page Up / Page Down | Scroll the transcript (overlay: scroll the panel) |
| Ctrl+End | Jump to the latest message |
| Alt+Up / Alt+Down | Select an earlier / later answer (marked with `›`), then F5/F6, Ctrl+E and Enter act on it |
| Esc | Close the top overlay; a confirmation returns to its parent. With no overlay: clear the selected-answer marker |
| Ctrl+C | Overlay open: dismiss it (never cancels indexing). Work active, no overlay: request a cooperative stop. Idle with a draft: clear it. Idle and empty: show the exit hint |
| Ctrl+D | Exit; with work active, a confirmation offers Keep running / Stop and exit |
| Up / Down | Move selection in a list |
| Tab / Shift+Tab | Move between zones and buttons in an overlay (composer: reserved for completion later) |
| Left / Right | Move between buttons; change a value in Settings |
| Type in a picker | Filter (palette, scope, sources); in Add folder it edits the path |
| N | Indexing panel in `--reduced-motion`: advance one step |
| P / N / O / D | Evidence: previous / next / open original / details |

Slash commands also work typed (for example `/show 4`, `/ls`, `/mode fast`). Because `/` on an empty composer opens the palette, you can keep typing the command and arguments there: Enter runs the best match with the text after the first space as its argument, and an unknown name falls through to the usual "did you mean" notice.

## 4. Screens as text

These are real renderings of the prototype to a plain-text buffer (`headless.py`), at the size shown. Colour is not visible in text; the labels (Ready, Failed, Disconnected, unavailable, current) and the `›` (selected answer) / `▸` (selected row, focused button) markers carry the meaning without it. Every overlay follows one width rule (100 columns at most, centred, two columns short of the terminal from 80 up, full width below 80) and sits on a cleared backdrop. The transcript is bottom-anchored, so older messages scroll up under the header.

### 4.1 Welcome and readiness

*Welcome / readiness (new data dir)* (100x30)

```text
 DOCKET · All ready sources · Auto · demo-model-14b                                       DEMO DATA
 Nothing searchable yet. Press F2 for Sources, then Add folder.






 ┌─ Welcome to Docket ────────────────────────────────────────────────────────────────────────────┐
 │  Ask questions about documents stored on this PC.                                              │
 │                                                                                                │
 │  Ollama              Available                                                                 │
 │  Answer model        demo-model-14b — installed                                                │
 │  Embedding model     installed                                                                 │
 │  Searchable documentsNone yet                                                                  │
 │                                                                                                │
 │  ▸[ Add a folder ]  [ Check again ]  [ Settings ]  [ Close ]                                   │
 │  Tab move · Enter activate · Esc close                                                         │
 └────────────────────────────────────────────────────────────────────────────────────────────────┘







┌─ Ask ────────────────────────────────────────────────────────────────────────────────────────────┐
│ Ask about your documents...                                                                      │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
 0 files ready · 0 need attention                                                     F1 / Commands
```

Blocked prerequisite (the source would still be kept; indexing then fails with a recoverable result):

*Welcome with a blocked prerequisite* (100x30)

```text
 DOCKET · All ready sources · Auto · demo-model-14b                                       DEMO DATA
 Not ready: the answer model is missing. Open Settings (F8) for details.




 ┌─ Welcome to Docket ────────────────────────────────────────────────────────────────────────────┐
 │  Ask questions about documents stored on this PC.                                              │
 │                                                                                                │
 │  Ollama              Not reachable at the configured address                                   │
 │  Answer model        demo-model-32b — not installed                                            │
 │  Embedding model     installed                                                                 │
 │  Searchable documentsNone yet                                                                  │
 │                                                                                                │
 │  Start Ollama, then run: ollama pull demo-model-32b                                            │
 │  Docket never installs models or starts services for you.                                      │
 │                                                                                                │
 │  ▸[ Add a folder ]  [ Check again ]  [ Settings ]  [ Close ]                                   │
 │  Tab move · Enter activate · Esc close                                                         │
 └────────────────────────────────────────────────────────────────────────────────────────────────┘






┌─ Ask ────────────────────────────────────────────────────────────────────────────────────────────┐
│ Ask about your documents...                                                                      │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
 0 files ready · 0 need attention                                                     F1 / Commands
```

### 4.2 Add folder

*Welcome -> Add folder form* (100x30)

```text
 DOCKET · All ready sources · Auto · demo-model-14b                                       DEMO DATA
 Nothing searchable yet. Press F2 for Sources, then Add folder.






 ┌─ Add folder ───────────────────────────────────────────────────────────────────────────────────┐
 │  Folder: /path/to/folder▏                                                                      │
 │                                                                                                │
 │  Docket reads PDF, Word, Excel, PowerPoint, text and Markdown in this folder and its           │
 │  subfolders, then starts indexing right away. Original files are never changed.                │
 │                                                                                                │
 │                                                                                                │
 │   [ Add and index ]  [ Cancel ]                                                                │
 │  Paste or type a path (quotes and spaces are fine) · Enter add · Esc cancel                    │
 └────────────────────────────────────────────────────────────────────────────────────────────────┘








┌─ Ask ────────────────────────────────────────────────────────────────────────────────────────────┐
│ Ask about your documents...                                                                      │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
 0 files ready · 0 need attention                                                     F1 / Commands
```

### 4.3 Main chat

*Chat with answer (table, bullets, chips, Sources, meta line)* (120x46)

```text
 DOCKET · All ready sources · Auto · demo-model-14b                                                           DEMO DATA
    Notice: This is a design prototype. Every answer, source and number is invented demo data. Type a
    question, or press F1 for commands.

  You
    How did each region do against target last quarter?

  Docket
    The demo quarterly report shows revenue growing in most regions [1].

    Key points:
    • Revenue rose 12% on the prior quarter [1]
    • The southern region missed its target because renewals slipped [2]
    • Hiring is paused until the next financial year [3]
    • Travel above the limit needs a manager sign-off [5], unchanged from the older handbook [6]

    Region │ Revenue │ Target │ Result
    ───────┼─────────┼────────┼─────────
    North  │     4.2 │    4.0 │ ✓ Met
    South  │     3.1 │    3.4 │ ✗ Missed
    West   │     2.8 │    2.5 │ ✓ Met

    One related figure [4] could not be checked.

    Sources:
      [1] q3-summary-2025.xlsx
      [2] regional-review.pdf
      [3] hiring.docx
      [4] forecast-draft.xlsx  (unavailable)
      [5] policies/handbook.pdf
      [6] 2023/handbook.pdf

    Warning: Citation [4] refers to a version that is no longer available.

    Quick search · 8.2s   [Evidence F5] [Ctrl+E] [Details F6]
    Open a source: /show 1 to /show 6






┌─ Ask ────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│ Ask about your documents...                                                                                          │
└──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
 24 files ready · 3 need attention · Ctrl+E evidence · /show N                                            F1 / Commands
```

At 80x24 and 60x20:

*Chat at 80x24* (80x24)

```text
 DOCKET · All ready sources · Auto · demo-model-14b                   DEMO DATA
    North  │     4.2 │    4.0 │ ✓ Met
    South  │     3.1 │    3.4 │ ✗ Missed
    West   │     2.8 │    2.5 │ ✓ Met

    One related figure [4] could not be checked.

    Sources:
      [1] q3-summary-2025.xlsx
      [2] regional-review.pdf
      [3] hiring.docx
      [4] forecast-draft.xlsx  (unavailable)
      [5] policies/handbook.pdf
      [6] 2023/handbook.pdf

    Warning: Citation [4] refers to a version that is no longer available.

    Quick search · 8.2s   [Evidence F5] [Ctrl+E] [Details F6]
    Open a source: /show 1 to /show 6

┌─ Ask ────────────────────────────────────────────────────────────────────────┐
│ Ask about your documents...                                                  │
└──────────────────────────────────────────────────────────────────────────────┘
 24 files ready · 3 need attention · Ctrl+E evidence · /show N    F1 / Commands
```

*Chat at 60x20* (60x20)

```text
 DOCKET · All ready sources · Auto                DEMO DATA
    Sources:
      [1] q3-summary-2025.xlsx
      [2] regional-review.pdf
      [3] hiring.docx
      [4] forecast-draft.xlsx  (unavailable)
      [5] policies/handbook.pdf
      [6] 2023/handbook.pdf

    Warning: Citation [4] refers to a version that is no
    longer available.

    Quick search · 8.2s   [Evidence F5] [Ctrl+E]
    [Details F6]
    Open a source: /show 1 to /show 6

┌─ Ask ────────────────────────────────────────────────────┐
│ Ask about your documents...                              │
└──────────────────────────────────────────────────────────┘
 24 files ready · 3 need attention            F1 / Commands
```

A greeting gets a short reply with no citations or sourced claims, and the new answer is the selected one (marker explained in the footer):

*Chat after a greeting (no citations)* (100x34)

```text
 DOCKET · All ready sources · Auto · demo-model-14b                                       DEMO DATA
    North  │     4.2 │    4.0 │ ✓ Met
    South  │     3.1 │    3.4 │ ✗ Missed
    West   │     2.8 │    2.5 │ ✓ Met

    One related figure [4] could not be checked.

    Sources:
      [1] q3-summary-2025.xlsx
      [2] regional-review.pdf
      [3] hiring.docx
      [4] forecast-draft.xlsx  (unavailable)
      [5] policies/handbook.pdf
      [6] 2023/handbook.pdf

    Warning: Citation [4] refers to a version that is no longer available.

    Quick search · 8.2s   [Evidence F5] [Ctrl+E] [Details F6]
    Open a source: /show 1 to /show 6

  You
    Hey

  › Docket
    Hello. I answer questions about the documents in your sources, with a citation for every
    claim. Ask about a topic or a file, or type /help to see what else I can do.

    Auto · 0.4s   [Details F6]
    Selected answer: Esc clears

┌─ Ask ────────────────────────────────────────────────────────────────────────────────────────────┐
│ Ask about your documents...                                                                      │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
 24 files ready · 3 need attention · Esc clears the selection                         F1 / Commands
```

A second sourced answer shows the selected-answer marker, its hint line, the shortcuts, and the disambiguated duplicate names:

*Chat with a selected answer (marker explained)* (100x40)

```text
 DOCKET · All ready sources · Auto · demo-model-14b                                       DEMO DATA

  You
    compare each region with its target

  › Docket
    The demo quarterly report shows revenue growing in most regions [1].

    Key points:
    • Revenue rose 12% on the prior quarter [1]
    • The southern region missed its target because renewals slipped [2]
    • Hiring is paused until the next financial year [3]
    • Travel above the limit needs a manager sign-off [5], unchanged from the older handbook [6]

    Region │ Revenue │ Target │ Result
    ───────┼─────────┼────────┼─────────
    North  │     4.2 │    4.0 │ ✓ Met
    South  │     3.1 │    3.4 │ ✗ Missed
    West   │     2.8 │    2.5 │ ✓ Met

    One related figure [4] could not be checked.

    Sources:
      [1] q3-summary-2025.xlsx
      [2] regional-review.pdf
      [3] hiring.docx
      [4] forecast-draft.xlsx  (unavailable)
      [5] policies/handbook.pdf
      [6] 2023/handbook.pdf

    Warning: Citation [4] refers to a version that is no longer available.

    Auto · 6.1s   [Evidence F5] [Ctrl+E] [Details F6]
    Open a source: /show 1 to /show 6
    Selected answer: Enter opens evidence, Esc clears

┌─ Ask ────────────────────────────────────────────────────────────────────────────────────────────┐
│ Ask about your documents...                                                                      │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
 24 files ready · 3 need attention · Enter opens evidence, Esc clears                 F1 / Commands
```

### 4.4 Command palette

*Command palette* (100x34)

```text
 DOCKET · All ready sources · Auto · demo-model-14b                                       DEMO DATA


 ┌─ Commands ─────────────────────────────────────────────────────────────────────────────────────┐
 │  Find: type a command or description▏                                                          │
 │                                                                                                │
 │  ▸ /help  show this help                                                                       │
 │    /sources  list registered sources                                                           │
 │    /add <folder>  register a folder as a source                                                │
 │    /ingest [<source-id>|all]  index a source (default: active and missing sources)             │
 │    /mode [auto|fast|agent]  show or set the query mode (default: auto)                         │
 │    /remove <source-id>  stop searching a source (asks first)                                   │
 │    /reconnect <source-id>  reconnect a disconnected local folder and index it                  │
 │    /show <n>  read the evidence behind citation n                                              │
 │    /retry  re-ask the previous question in the current mode                                    │
 │    /status  show data dir, models, sources and mode                                            │
 │    /clear  forget the conversation so far                                                      │
 │    /exit  leave (Ctrl-D works too)                                                             │
 │    /scope  choose what to search                                                               │
 │    /jobs  show indexing jobs and results                                                       │
 │    /settings  answering, appearance, history, system                                           │
 │    /details  details of the latest answer                                                      │
 │    /rechunk  update stored chunks                                                 unavailable  │
 │    /reindex  rebuild the search index                                             unavailable  │
 │                                                                                                │
 │   [ Close ]                                                                                    │
 │  Type to filter · Up/Down select · Enter run · Esc close                                       │
 └────────────────────────────────────────────────────────────────────────────────────────────────┘


┌─ Ask ────────────────────────────────────────────────────────────────────────────────────────────┐
│ Ask about your documents...                                                                      │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
 24 files ready · 3 need attention                                                    F1 / Commands
```

Filtering, and an unavailable command with its reason:

*Command palette filtered to 'recon' and an unavailable command* (100x24)

```text
 DOCKET · All ready sources · Auto · demo-model-14b                                       DEMO DATA





 ┌─ Commands ─────────────────────────────────────────────────────────────────────────────────────┐
 │  Find: recon▏                                                                                  │
 │                                                                                                │
 │  ▸ /reconnect <source-id>  reconnect a disconnected local folder and index it                  │
 │                                                                                                │
 │   [ Close ]                                                                                    │
 │  Type to filter · Up/Down select · Enter run · Esc close                                       │
 └────────────────────────────────────────────────────────────────────────────────────────────────┘






┌─ Ask ────────────────────────────────────────────────────────────────────────────────────────────┐
│ Ask about your documents...                                                                      │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
 24 files ready · 3 need attention                                                    F1 / Commands
```

*Command palette with an unavailable command selected* (100x24)

```text
 DOCKET · All ready sources · Auto · demo-model-14b                                       DEMO DATA




 ┌─ Commands ─────────────────────────────────────────────────────────────────────────────────────┐
 │  Find: rech▏                                                                                   │
 │                                                                                                │
 │  ▸ /rechunk  update stored chunks                                                 unavailable  │
 │                                                                                                │
 │  Unavailable: Maintenance arrives in a later stage; not wired in this prototype.               │
 │                                                                                                │
 │   [ Close ]                                                                                    │
 │  Type to filter · Up/Down select · Enter run · Esc close                                       │
 └────────────────────────────────────────────────────────────────────────────────────────────────┘





┌─ Ask ────────────────────────────────────────────────────────────────────────────────────────────┐
│ Ask about your documents...                                                                      │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
 24 files ready · 3 need attention                                                    F1 / Commands
```

### 4.5 Scope and mode pickers

*Scope picker* (100x24)

```text
 DOCKET · All ready sources · Auto · demo-model-14b                                       DEMO DATA



 ┌─ Search scope ─────────────────────────────────────────────────────────────────────────────────┐
 │  Find: filter sources▏                                                                         │
 │                                                                                                │
 │  ▸ All ready sources  everything searchable                                           current  │
 │    Finance  /demo/work/finance                                                                 │
 │    Handbook  /demo/work/handbook                                                               │
 │    Contracts  Failed — not searchable                                                          │
 │    Old project  Disconnected — not searchable                                                  │
 │                                                                                                │
 │   [ Find a file ]  [ Close ]                                                                   │
 │  Type to filter · Up/Down select · Enter select · Tab actions · Esc close                      │
 └────────────────────────────────────────────────────────────────────────────────────────────────┘




┌─ Ask ────────────────────────────────────────────────────────────────────────────────────────────┐
│ Ask about your documents...                                                                      │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
 24 files ready · 3 need attention                                                    F1 / Commands
```

File search (the path is shown instead of the source name when basenames collide):

*Scope picker: file search* (100x24)

```text
 DOCKET · All ready sources · Auto · demo-model-14b                                       DEMO DATA



 ┌─ Search scope ─────────────────────────────────────────────────────────────────────────────────┐
 │  Find: minutes▏                                                                                │
 │                                                                                                │
 │  ▸ board/minutes-01.pdf  Finance                                                               │
 │    board/minutes-02.pdf  Finance                                                               │
 │    board/minutes-03.pdf  Finance                                                               │
 │    board/minutes-04.pdf  Finance                                                               │
 │    board/minutes-05.pdf  Finance                                                               │
 │    board/minutes-06.pdf  Finance                                                               │
 │                                                                                                │
 │   [ Back to sources ]  [ Close ]                                                               │
 │  Type to search files · Enter select · Esc close                                               │
 └────────────────────────────────────────────────────────────────────────────────────────────────┘



┌─ Ask ────────────────────────────────────────────────────────────────────────────────────────────┐
│ Ask about your documents...                                                                      │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
 24 files ready · 3 need attention                                                    F1 / Commands
```

*Mode picker with Plan unavailable* (100x24)

```text
 DOCKET · All ready sources · Auto · demo-model-14b                                       DEMO DATA





 ┌─ Answering mode ───────────────────────────────────────────────────────────────────────────────┐
 │  ▸ Auto  Docket chooses the route. Recommended.                                       current  │
 │    Quick search  Search and answer directly from retrieved passages.                           │
 │    Plan  Plan steps and ask you to approve them first.                            unavailable  │
 │                                                                                                │
 │   [ Close ]                                                                                    │
 │  Up/Down select · Enter accept · Esc close                                                     │
 └────────────────────────────────────────────────────────────────────────────────────────────────┘






┌─ Ask ────────────────────────────────────────────────────────────────────────────────────────────┐
│ Ask about your documents...                                                                      │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
 24 files ready · 3 need attention                                                    F1 / Commands
```

### 4.6 Sources

*Sources: Ready source selected (side-by-side details)* (100x30)

```text
 DOCKET · All ready sources · Auto · demo-model-14b                                       DEMO DATA


 ┌─ Sources ──────────────────────────────────────────────────────────────────────────────────────┐
 │  Find: filter sources▏                                                                         │
 │                                                                                                │
 │  ▸ Finance                    Ready │ Finance                                                  │
 │      18 searchable · 2 failed       │ Path         /demo/work/finance                          │
 │    Handbook                   Ready │ State        Ready                                       │
 │      6 searchable                   │ Last indexed 10 Oct 2026 10:42                           │
 │    Contracts                 Failed │ Files        18 ready · 2 failed · 0 pending · 1 no      │
 │    Old project         Disconnected │              text                                        │
 │                                     │                                                          │
 │                                     │ Needs a look                                             │
 │                                     │   budget/legacy-plan.xls  Failed    Unsupported legacy   │
 │                                     │                                     format .xls          │
 │                                     │   budget/forecast.xlsx    Failed    Workbook is corrupt  │
 │                                     │                                     (demo)               │
 │                                     │   scans/cover-page.pdf    No text                        │
 │                                                                                                │
 │   [ Add folder ]  [ Refresh ]  [ Retry ]  [ Reconnect ]  [ Disconnect ]  [ Details ]           │
 │   [ Close ]                                                                                    │
 │  Type to filter · Tab actions · Enter toggles details · Esc close                              │
 └────────────────────────────────────────────────────────────────────────────────────────────────┘


┌─ Ask ────────────────────────────────────────────────────────────────────────────────────────────┐
│ Ask about your documents...                                                                      │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
 24 files ready · 3 need attention                                                    F1 / Commands
```

*Sources: Failed source selected* (100x30)

```text
 DOCKET · All ready sources · Auto · demo-model-14b                                       DEMO DATA





 ┌─ Sources ──────────────────────────────────────────────────────────────────────────────────────┐
 │  Find: filter sources▏                                                                         │
 │                                                                                                │
 │    Finance                    Ready │ Contracts                                                │
 │      18 searchable · 2 failed       │ Path         /demo/work/contracts                        │
 │    Handbook                   Ready │ State        Failed                                      │
 │      6 searchable                   │ Last indexed 9 Oct 2026 17:20                            │
 │  ▸ Contracts                 Failed │ Files        4 ready · 0 failed · 0 pending · 0 no text  │
 │    Old project         Disconnected │ The folder cannot be reached at /demo/work/contracts.    │
 │                                     │ Restore it, then retry.                                  │
 │                                                                                                │
 │   [ Add folder ]  [ Refresh ]  [ Retry ]  [ Reconnect ]  [ Disconnect ]  [ Details ]           │
 │   [ Close ]                                                                                    │
 │  Type to filter · Tab actions · Enter toggles details · Esc close                              │
 └────────────────────────────────────────────────────────────────────────────────────────────────┘





┌─ Ask ────────────────────────────────────────────────────────────────────────────────────────────┐
│ Ask about your documents...                                                                      │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
 24 files ready · 3 need attention                                                    F1 / Commands
```

*Sources: Disconnected source selected* (100x30)

```text
 DOCKET · All ready sources · Auto · demo-model-14b                                       DEMO DATA





 ┌─ Sources ──────────────────────────────────────────────────────────────────────────────────────┐
 │  Find: filter sources▏                                                                         │
 │                                                                                                │
 │    Finance                    Ready │ Old project                                              │
 │      18 searchable · 2 failed       │ Path         /demo/archive/old-project                   │
 │    Handbook                   Ready │ State        Disconnected                                │
 │      6 searchable                   │ Last indexed 3 Oct 2026 14:05                            │
 │    Contracts                 Failed │ Files        5 ready · 0 failed · 0 pending · 0 no text  │
 │  ▸ Old project         Disconnected │ Disconnected by you. Stored originals are kept; it is    │
 │                                     │ not searched.                                            │
 │                                                                                                │
 │   [ Add folder ]  [ Refresh ]  [ Retry ]  [ Reconnect ]  [ Disconnect ]  [ Details ]           │
 │   [ Close ]                                                                                    │
 │  Type to filter · Tab actions · Enter toggles details · Esc close                              │
 └────────────────────────────────────────────────────────────────────────────────────────────────┘





┌─ Ask ────────────────────────────────────────────────────────────────────────────────────────────┐
│ Ask about your documents...                                                                      │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
 24 files ready · 3 need attention                                                    F1 / Commands
```

Disconnect asks first; the safe choice is focused by default:

*Sources: confirm disconnect* (100x30)

```text
 DOCKET · All ready sources · Auto · demo-model-14b                                       DEMO DATA









 ┌─ Disconnect Finance? ──────────────────────────────────────────────────────────────────────────┐
 │  Finance will stop being searched. Your original files and Docket's stored copies are kept,    │
 │  and you can reconnect later. (Demo: nothing is changed on disk.)                              │
 │                                                                                                │
 │   [ Disconnect ] ▸[ Keep connected ]                                                           │
 │  Left/Right choose · Enter confirm · Esc go back                                               │
 └────────────────────────────────────────────────────────────────────────────────────────────────┘









┌─ Ask ────────────────────────────────────────────────────────────────────────────────────────────┐
│ Ask about your documents...                                                                      │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
 24 files ready · 3 need attention                                                    F1 / Commands
```

Below 80 columns the details go under the list:

*Sources at 60x20 (details below the list)* (60x20)

```text
 DOCKET · All ready sources · Auto                DEMO DATA
┌─ Sources ────────────────────────────────────────────────┐
│ Find: filter sources▏                                    │
│                                                          │
│ ▸ Finance                                          Ready │
│     18 searchable · 2 failed                             │
│   Handbook                                         Ready │
│     6 searchable                                         │
│   Contracts                                       Failed │
│   Old project                               Disconnected │
│                                                          │
│ Finance                                                  │
│ 0 above · 11 below · PgUp/PgDn to scroll                 │
│                                                          │
│  [ Add folder ]  [ Refresh ]  [ Retry ]  [ Reconnect ]   │
│  [ Disconnect ]  [ Details ]  [ Close ]                  │
│ Type to filter · Tab actions · Enter toggles details ·   │
│ Esc close                                                │
└──────────────────────────────────────────────────────────┘
 24 files ready · 3 need attention            F1 / Commands
```

### 4.7 Indexing

*Indexing: running (seeded 7 of 24)* (100x30)

```text
 DOCKET · All ready sources · Auto · demo-model-14b                                       DEMO DATA






 ┌─ Indexing Finance ─────────────────────────────────────────────────────────────────────────────┐
 │  File      7 of 24 · file-07.pdf                                                               │
 │  Stage     Creating search embeddings                                                          │
 │  Elapsed   00:42                                                                               │
 │  So far    5 indexed · 1 unchanged · 0 failed                                                  │
 │                                                                                                │
 │  ██████████░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░ 6/24 files                                           │
 │  Files vary in processing time; this is a file count, not a time estimate.                     │
 │                                                                                                │
 │  ▸[ Hide progress ]  [ Stop indexing ]                                                         │
 │  Hide returns to chat; work continues · N advance (reduced motion) · Esc hides                 │
 └────────────────────────────────────────────────────────────────────────────────────────────────┘







┌─ Ask (sending is paused while indexing) ─────────────────────────────────────────────────────────┐
│ Ask about your documents...                                                                      │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
 24 files ready · 3 need attention · Indexing Finance 6/24                            F1 / Commands
```

*Indexing: stop requested* (100x24)

```text
 DOCKET · All ready sources · Auto · demo-model-14b                                       DEMO DATA


 ┌─ Indexing Finance ─────────────────────────────────────────────────────────────────────────────┐
 │  File      7 of 24 · file-07.pdf                                                               │
 │  Stage     Creating search embeddings                                                          │
 │  Elapsed   00:42                                                                               │
 │  So far    5 indexed · 1 unchanged · 0 failed                                                  │
 │                                                                                                │
 │  ██████████░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░ 6/24 files                                           │
 │  Files vary in processing time; this is a file count, not a time estimate.                     │
 │                                                                                                │
 │  Stopping after the current operation...                                                       │
 │  Completed files are kept. Unfinished files stay unavailable and can be retried.               │
 │                                                                                                │
 │  ▸[ Hide progress ]  [ Stop indexing ]                                                         │
 │  Hide returns to chat; work continues · N advance (reduced motion) · Esc hides                 │
 └────────────────────────────────────────────────────────────────────────────────────────────────┘


┌─ Ask (sending is paused while indexing) ─────────────────────────────────────────────────────────┐
│ Ask about your documents...                                                                      │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
 24 files ready · 3 need attention · Stopping Finance 6/24                            F1 / Commands
```

After the worker stops (the state is not "stopped" until then):

*Indexing: stopped (after the worker stops)* (100x24)

```text
 DOCKET · All ready sources · Auto · demo-model-14b                                       DEMO DATA





 ┌─ Indexing Finance ─────────────────────────────────────────────────────────────────────────────┐
 │  Cancelled: Stopped before finishing                                                           │
 │  5 indexed · 1 unchanged · 0 failed                                                            │
 │  6 of 24 files processed in 00:44                                                              │
 │  Completed files were kept; the rest are unavailable until you retry.                          │
 │                                                                                                │
 │  ▸[ Start asking ]  [ Retry indexing ]  [ Close ]                                              │
 │  Esc close                                                                                     │
 └────────────────────────────────────────────────────────────────────────────────────────────────┘





┌─ Ask ────────────────────────────────────────────────────────────────────────────────────────────┐
│ Ask about your documents...                                                                      │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
 24 files ready · 3 need attention                                                    F1 / Commands
```

*Indexing: finished with failures* (100x30)

```text
 DOCKET · All ready sources · Auto · demo-model-14b                                       DEMO DATA






 ┌─ Indexing Finance ─────────────────────────────────────────────────────────────────────────────┐
 │  Partial: Indexing finished                                                                    │
 │  19 indexed · 3 unchanged · 2 failed                                                           │
 │  24 of 24 files processed in 02:26                                                             │
 │                                                                                                │
 │  Failures                                                                                      │
 │    file-12.pdf Failed — Document is corrupt (demo)                                             │
 │    file-18.pdf Failed — Document is corrupt (demo)                                             │
 │  Recovery: fix or replace the file, then Retry indexing.                                       │
 │                                                                                                │
 │  ▸[ Start asking ]  [ Retry indexing ]  [ Close ]                                              │
 │  Esc close                                                                                     │
 └────────────────────────────────────────────────────────────────────────────────────────────────┘






┌─ Ask ────────────────────────────────────────────────────────────────────────────────────────────┐
│ Ask about your documents...                                                                      │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
 24 files ready · 3 need attention                                                    F1 / Commands
```

Hidden: the job keeps running, the footer shows it, and the composer says why sending is paused:

*Indexing hidden: footer indicator and paused composer* (100x30)

```text
 DOCKET · All ready sources · Auto · demo-model-14b                                       DEMO DATA
    • The southern region missed its target because renewals slipped [2]
    • Hiring is paused until the next financial year [3]
    • Travel above the limit needs a manager sign-off [5], unchanged from the older handbook [6]

    Region │ Revenue │ Target │ Result
    ───────┼─────────┼────────┼─────────
    North  │     4.2 │    4.0 │ ✓ Met
    South  │     3.1 │    3.4 │ ✗ Missed
    West   │     2.8 │    2.5 │ ✓ Met

    One related figure [4] could not be checked.

    Sources:
      [1] q3-summary-2025.xlsx
      [2] regional-review.pdf
      [3] hiring.docx
      [4] forecast-draft.xlsx  (unavailable)
      [5] policies/handbook.pdf
      [6] 2023/handbook.pdf

    Warning: Citation [4] refers to a version that is no longer available.

    Quick search · 8.2s   [Evidence F5] [Ctrl+E] [Details F6]
    Open a source: /show 1 to /show 6

┌─ Ask (sending is paused while indexing) ─────────────────────────────────────────────────────────┐
│ Ask about your documents...                                                                      │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
 24 files ready · 3 need attention · Indexing Finance 6/24                            F1 / Commands
```

### 4.8 Evidence and answer details

*Evidence [1]* (100x30)

```text
 DOCKET · All ready sources · Auto · demo-model-14b                                       DEMO DATA



 ┌─ Evidence [1] of 6 ────────────────────────────────────────────────────────────────────────────┐
 │  q3-summary-2025.xlsx                                                                  1 of 6  │
 │  Source    Finance                                                                             │
 │  Location  Sheet Summary · B7:D10                                                              │
 │  Indexed   10 Oct 2026 10:42 · version 3 of 3 (current)                                        │
 │  Supports  “The demo quarterly report shows revenue growing in most regions [1].”              │
 │            “Revenue rose 12% on the prior quarter [1]”                                         │
 │                                                                                                │
 │  Cells B7:D10 · cited cells shown in [ ]: C8:C10                                               │
 │      B        C           D                                                                    │
 │   7  Region │  Revenue  │ Target                                                               │
 │      ───────┼───────────┼───────                                                               │
 │   8  North  │     [4.2] │    4.0                                                               │
 │   9  South  │     [3.1] │    3.4                                                               │
 │  10  West   │     [2.8] │    2.5                                                               │
 │                                                                                                │
 │  ▸[ Previous ]  [ Next ]  [ Open original ]  [ Details ]  [ Close ]                            │
 │  P N O D shortcuts · PgUp/PgDn scroll · Esc close                                              │
 └────────────────────────────────────────────────────────────────────────────────────────────────┘



┌─ Ask ────────────────────────────────────────────────────────────────────────────────────────────┐
│ Ask about your documents...                                                                      │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
 24 files ready · 3 need attention                                                    F1 / Commands
```

Position text (`1 of 6`) sits on the title row and changes with Previous/Next. The cited cells `C8:C10` are in `[ ]` (and highlighted in colour); the `Supports` lines quote the answer claims that cite this passage. A citation with no extracted location says so:

*Evidence [3]: location not extracted* (100x30)

```text
 DOCKET · All ready sources · Auto · demo-model-14b                                       DEMO DATA





 ┌─ Evidence [3] of 6 ────────────────────────────────────────────────────────────────────────────┐
 │  hiring.docx                                                                           3 of 6  │
 │  Source    Handbook                                                                            │
 │  Location  Location not extracted for this passage                                             │
 │  Indexed   10 Oct 2026 09:15 · version 3 of 3 (current)                                        │
 │  Supports  “Hiring is paused until the next financial year [3]”                                │
 │                                                                                                │
 │  Passage (verbatim)                                                                            │
 │  │ New hires are paused until the start of the next financial year unless a role is            │
 │  │ explicitly approved by the executive team.                                                  │
 │                                                                                                │
 │  ▸[ Previous ]  [ Next ]  [ Open original ]  [ Details ]  [ Close ]                            │
 │  P N O D shortcuts · PgUp/PgDn scroll · Esc close                                              │
 └────────────────────────────────────────────────────────────────────────────────────────────────┘






┌─ Ask ────────────────────────────────────────────────────────────────────────────────────────────┐
│ Ask about your documents...                                                                      │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
 24 files ready · 3 need attention                                                    F1 / Commands
```

An unavailable citation explains and disables Open original:

*Evidence [4]: unavailable citation* (100x30)

```text
 DOCKET · All ready sources · Auto · demo-model-14b                                       DEMO DATA





 ┌─ Evidence [4] of 6 ────────────────────────────────────────────────────────────────────────────┐
 │  forecast-draft.xlsx                                                                   4 of 6  │
 │  Source    Finance                                                                             │
 │  Status    Unavailable: this evidence can no longer be shown.                                  │
 │  Supports  “One related figure [4] could not be checked.”                                      │
 │                                                                                                │
 │  Its stored version was superseded after this answer was written, so the original passage can  │
 │  no longer be shown.                                                                           │
 │                                                                                                │
 │  The answer text is unchanged; only the passage behind this citation is missing.               │
 │                                                                                                │
 │  ▸[ Previous ]  [ Next ]  [ Open original ]  [ Details ]  [ Close ]                            │
 │  P N O D shortcuts · PgUp/PgDn scroll · Esc close                                              │
 └────────────────────────────────────────────────────────────────────────────────────────────────┘






┌─ Ask ────────────────────────────────────────────────────────────────────────────────────────────┐
│ Ask about your documents...                                                                      │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
 24 files ready · 3 need attention                                                    F1 / Commands
```

With the Details toggle, and at 60x20:

*Evidence with details expanded* (100x30)

```text
 DOCKET · All ready sources · Auto · demo-model-14b                                       DEMO DATA
 ┌─ Evidence [1] of 6 ────────────────────────────────────────────────────────────────────────────┐
 │  q3-summary-2025.xlsx                                                                  1 of 6  │
 │  Source    Finance                                                                             │
 │  Location  Sheet Summary · B7:D10                                                              │
 │  Indexed   10 Oct 2026 10:42 · version 3 of 3 (current)                                        │
 │  Supports  “The demo quarterly report shows revenue growing in most regions [1].”              │
 │            “Revenue rose 12% on the prior quarter [1]”                                         │
 │                                                                                                │
 │  Cells B7:D10 · cited cells shown in [ ]: C8:C10                                               │
 │      B        C           D                                                                    │
 │   7  Region │  Revenue  │ Target                                                               │
 │      ───────┼───────────┼───────                                                               │
 │   8  North  │     [4.2] │    4.0                                                               │
 │   9  South  │     [3.1] │    3.4                                                               │
 │  10  West   │     [2.8] │    2.5                                                               │
 │                                                                                                │
 │  Details                                                                                       │
 │  Source    Finance                                                                             │
 │  File      reports/q3-summary-2025.xlsx                                                        │
 │  Chunk     chunk-demo-0101                                                                     │
 │  0 above · 2 below · PgUp/PgDn to scroll                                                       │
 │                                                                                                │
 │  ▸[ Previous ]  [ Next ]  [ Open original ]  [ Hide details ]  [ Close ]                       │
 │  P N O D shortcuts · PgUp/PgDn scroll · Esc close                                              │
 └────────────────────────────────────────────────────────────────────────────────────────────────┘
┌─ Ask ────────────────────────────────────────────────────────────────────────────────────────────┐
│ Ask about your documents...                                                                      │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
 24 files ready · 3 need attention                                                    F1 / Commands
```

*Evidence at 60x20* (60x20)

```text
 DOCKET · All ready sources · Auto                DEMO DATA
┌─ Evidence [1] of 6 ──────────────────────────────────────┐
│ q3-summary-2025.xlsx                              1 of 6 │
│ Source    Finance                                        │
│ Location  Sheet Summary · B7:D10                         │
│ Indexed   10 Oct 2026 10:42 · version 3 of 3 (current)   │
│ Supports  “The demo quarterly report shows revenue       │
│           growing in most regions [1].”                  │
│           “Revenue rose 12% on the prior quarter [1]”    │
│                                                          │
│ Cells B7:D10 · cited cells shown in [ ]: C8:C10          │
│     B        C           D                               │
│  7  Region │  Revenue  │ Target                          │
│ 0 above · 4 below · PgUp/PgDn to scroll                  │
│                                                          │
│ ▸[ Previous ]  [ Next ]  [ Open original ]  [ Details ]  │
│  [ Close ]                                               │
│ P N O D shortcuts · PgUp/PgDn scroll · Esc close         │
└──────────────────────────────────────────────────────────┘
 24 files ready · 3 need attention            F1 / Commands
```

Answer details is a two-column label/value list; at 80x24 it looks like this:

*Overlays at 80x24: Answer details* (80x24)

```text
 DOCKET · All ready sources · Auto · demo-model-14b                   DEMO DATA
 ┌─ Answer details ───────────────────────────────────────────────────────────┐
 │ Status         Not verified (prototype)                                    │
 │ Mode           Quick search                                                │
 │ Model          demo-model-14b                                              │
 │ Scope          All ready sources                                           │
 │ Elapsed        8.2 s                                                       │
 │ Passages used  5 passages from 5 files, 14 searched                        │
 │ Follow-up      Rewritten as "quarterly revenue by region against target"   │
 │ Period         Not stated; the most recent quarter was used                │
 │ Computation    Not used                                                    │
 │ Citations      6 · syntax valid; meaning not machine-verified              │
 │ Confidence     Not calibrated                                              │
 │ Warning        Citation [4] refers to a version that is no longer          │
 │                available.                                                  │
 │                                                                            │
 │ ▸[ Evidence ]  [ Close ]                                                   │
 │ Esc close                                                                  │
 └────────────────────────────────────────────────────────────────────────────┘

┌─ Ask ────────────────────────────────────────────────────────────────────────┐
│ Ask about your documents...                                                  │
└──────────────────────────────────────────────────────────────────────────────┘
 24 files ready · 3 need attention                                F1 / Commands
```

*Answer details* (100x30)

```text
 DOCKET · All ready sources · Auto · demo-model-14b                                       DEMO DATA




 ┌─ Answer details ───────────────────────────────────────────────────────────────────────────────┐
 │  Status         Not verified (prototype)                                                       │
 │  Mode           Quick search                                                                   │
 │  Model          demo-model-14b                                                                 │
 │  Scope          All ready sources                                                              │
 │  Elapsed        8.2 s                                                                          │
 │  Passages used  5 passages from 5 files, 14 searched                                           │
 │  Follow-up      Rewritten as "quarterly revenue by region against target"                      │
 │  Period         Not stated; the most recent quarter was used                                   │
 │  Computation    Not used                                                                       │
 │  Citations      6 · syntax valid; meaning not machine-verified                                 │
 │  Confidence     Not calibrated                                                                 │
 │  Warning        Citation [4] refers to a version that is no longer available.                  │
 │                                                                                                │
 │  ▸[ Evidence ]  [ Close ]                                                                      │
 │  Esc close                                                                                     │
 └────────────────────────────────────────────────────────────────────────────────────────────────┘




┌─ Ask ────────────────────────────────────────────────────────────────────────────────────────────┐
│ Ask about your documents...                                                                      │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
 24 files ready · 3 need attention                                                    F1 / Commands
```

### 4.9 Settings, jobs, confirmations

*Settings: answering* (100x24)

```text
 DOCKET · All ready sources · Auto · demo-model-14b                                       DEMO DATA



 ┌─ Settings ─────────────────────────────────────────────────────────────────────────────────────┐
 │   Answering   Appearance   History   System                                                    │
 │                                                                                                │
 │  ▸ Answer model      ‹ demo-model-14b ›                                                        │
 │    Answering mode    ‹ Auto ›                                                                  │
 │    Answer thinking   ‹ Balanced ›                                                              │
 │    Embedding model   demo-embed-small (read-only)                                              │
 │  demo-model-32b: not installed — run: ollama pull demo-model-32b                               │
 │                                                                                                │
 │   [ Save ]  [ Check readiness ]  [ Cancel ]                                                    │
 │  Tab move · Left/Right change · Enter activate · Esc cancel                                    │
 └────────────────────────────────────────────────────────────────────────────────────────────────┘




┌─ Ask ────────────────────────────────────────────────────────────────────────────────────────────┐
│ Ask about your documents...                                                                      │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
 24 files ready · 3 need attention                                                    F1 / Commands
```

*Settings: appearance (unsaved change)* (100x24)

```text
 DOCKET · All ready sources · Auto · demo-model-14b                                       DEMO DATA



 ┌─ Settings ─────────────────────────────────────────────────────────────────────────────────────┐
 │   Answering   Appearance   History   System                                                    │
 │                                                                                                │
 │  ▸ Appearance        ‹ Light ›                                                                 │
 │    Density           Comfortable (read-only)                                                   │
 │                                                                                                │
 │  The terminal font and size are set by your terminal, not by Docket.                           │
 │                                                                                                │
 │  Unsaved changes                                                                               │
 │                                                                                                │
 │   [ Save ]  [ Check readiness ]  [ Cancel ]                                                    │
 │  Tab move · Left/Right change · Enter activate · Esc cancel                                    │
 └────────────────────────────────────────────────────────────────────────────────────────────────┘



┌─ Ask ────────────────────────────────────────────────────────────────────────────────────────────┐
│ Ask about your documents...                                                                      │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
 24 files ready · 3 need attention                                                    F1 / Commands
```

*Settings: system* (100x24)

```text
 DOCKET · All ready sources · Auto · demo-model-14b                                       DEMO DATA



 ┌─ Settings ─────────────────────────────────────────────────────────────────────────────────────┐
 │   Answering   Appearance   History   System                                                    │
 │                                                                                                │
 │    Data folder       /demo/data (not a real directory)                                         │
 │    Ollama            Available (demo)                                                          │
 │    Answer model      demo-model-14b                                                            │
 │    Embedding         demo-embed-small                                                          │
 │    Index             Compatible with the embedding model (demo)                                │
 │    Coverage          24 files searchable                                                       │
 │                                                                                                │
 │   [ Save ]  [ Check readiness ]  [ Cancel ]                                                    │
 │  Tab move · Left/Right change · Enter activate · Esc cancel                                    │
 └────────────────────────────────────────────────────────────────────────────────────────────────┘



┌─ Ask ────────────────────────────────────────────────────────────────────────────────────────────┐
│ Ask about your documents...                                                                      │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
 24 files ready · 3 need attention                                                    F1 / Commands
```

Unsaved changes ask before closing:

*Settings: discard confirmation* (100x24)

```text
 DOCKET · All ready sources · Auto · demo-model-14b                                       DEMO DATA






 ┌─ Discard changes? ─────────────────────────────────────────────────────────────────────────────┐
 │  You have unsaved settings. Discard them and close?                                            │
 │                                                                                                │
 │   [ Discard changes ] ▸[ Keep editing ]                                                        │
 │  Left/Right choose · Enter confirm · Esc go back                                               │
 └────────────────────────────────────────────────────────────────────────────────────────────────┘







┌─ Ask ────────────────────────────────────────────────────────────────────────────────────────────┐
│ Ask about your documents...                                                                      │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
 24 files ready · 3 need attention                                                    F1 / Commands
```

*Jobs* (100x24)

```text
 DOCKET · All ready sources · Auto · demo-model-14b                                       DEMO DATA



 ┌─ Jobs ─────────────────────────────────────────────────────────────────────────────────────────┐
 │    Source        Result      When               Summary                                        │
 │  ────────────────────────────────────────────────────────────────────────────────────────────  │
 │  ▸ Finance       Partial     10 Oct 2026 10:42  18 indexed · 0 unchanged · 2 failed            │
 │    Handbook      Completed   10 Oct 2026 09:15  6 indexed · 0 unchanged · 0 failed             │
 │    Old project   Interrupted 3 Oct 2026 14:05   Stopped when the process exited; retry to fi…  │
 │                                                                                                │
 │  Summary  Finance: 18 indexed · 0 unchanged · 2 failed                                         │
 │                                                                                                │
 │   [ Results ]  [ Close ]                                                                       │
 │  Up/Down select · Enter results · Esc close (history is demo data)                             │
 └────────────────────────────────────────────────────────────────────────────────────────────────┘




┌─ Ask ────────────────────────────────────────────────────────────────────────────────────────────┐
│ Ask about your documents...                                                                      │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
 24 files ready · 3 need attention                                                    F1 / Commands
```

Jobs at 60x20 drops the summary column and shows the selected row's summary below the table:

*Jobs at 60x20 (summary moves below the table)* (60x20)

```text
 DOCKET · All ready sources · Auto                DEMO DATA
┌─ Jobs ───────────────────────────────────────────────────┐
│   Source        Result      When                         │
│ ──────────────────────────────────────────────────────── │
│ ▸ Finance       Partial     10 Oct 2026 10:42            │
│   Handbook      Completed   10 Oct 2026 09:15            │
│   Old project   Interrupted 3 Oct 2026 14:05             │
│                                                          │
│ Summary  Finance: 18 indexed · 0 unchanged · 2 failed    │
│                                                          │
│                                                          │
│                                                          │
│                                                          │
│                                                          │
│                                                          │
│  [ Results ]  [ Close ]                                  │
│ Up/Down select · Enter results · Esc close (history is   │
│ demo data)                                               │
└──────────────────────────────────────────────────────────┘
 24 files ready · 3 need attention            F1 / Commands
```

*Exit confirmation while indexing* (100x24)

```text
 DOCKET · All ready sources · Auto · demo-model-14b                                       DEMO DATA






 ┌─ Exit while work is running? ──────────────────────────────────────────────────────────────────┐
 │  Indexing or an answer is still in progress. Docket does not keep running after it exits.      │
 │                                                                                                │
 │   [ Stop and exit ] ▸[ Keep running ]                                                          │
 │  Left/Right choose · Enter confirm · Esc go back                                               │
 └────────────────────────────────────────────────────────────────────────────────────────────────┘







┌─ Ask (sending is paused while indexing) ─────────────────────────────────────────────────────────┐
│ Ask about your documents...                                                                      │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
 24 files ready · 3 need attention · Indexing Finance 6/24                            F1 / Commands
```

### 4.10 Too small, and the ASCII fallback

*Too small (50x12)* (50x12)

```text
Terminal too small
Docket needs at least 60x16; this is 50x12.
Resize the window to continue. Your draft is kept.
Or run `docket chat --plain`.
```

*ASCII fallback, sources at 80x24* (80x24)

```text
 DOCKET | All ready sources | Auto | demo-model-14b                   DEMO DATA
 +- Sources ------------------------------------------------------------------+
 | Find: filter sources|                                                      |
 |                                                                            |
 | > Finance                    Ready | Finance                               |
 |     18 searchable | 2 failed       | Path         /demo/work/finance       |
 |   Handbook                   Ready | State        Ready                    |
 |     6 searchable                   | Last indexed 10 Oct 2026 10:42        |
 |   Contracts                 Failed | Files        18 ready | 2 failed | 0  |
 |   Old project         Disconnected |              pending | 1 no text      |
 |                                    |                                       |
 |                                    | Needs a look                          |
 |                                    |   budget/le.plan.xls  Failed          |
 |                                    |                                 Unsup |
 | 0 above | 16 below | PgUp/PgDn to scroll                                   |
 |                                                                            |
 |  [ Add folder ]  [ Refresh ]  [ Retry ]  [ Reconnect ]  [ Disconnect ]     |
 |  [ Details ]  [ Close ]                                                    |
 | Type to filter | Tab actions | Enter toggles details | Esc close           |
 +----------------------------------------------------------------------------+
+- Ask ------------------------------------------------------------------------+
| Ask about your documents...                                                  |
+------------------------------------------------------------------------------+
 24 files ready | 3 need attention                                F1 / Commands
```

*ASCII fallback, chat at 80x24* (80x24)

```text
 DOCKET | All ready sources | Auto | demo-model-14b                   DEMO DATA
    North  |     4.2 |    4.0 | OK Met
    South  |     3.1 |    3.4 | X Missed
    West   |     2.8 |    2.5 | OK Met

    One related figure [4] could not be checked.

    Sources:
      [1] q3-summary-2025.xlsx
      [2] regional-review.pdf
      [3] hiring.docx
      [4] forecast-draft.xlsx  (unavailable)
      [5] policies/handbook.pdf
      [6] 2023/handbook.pdf

    Warning: Citation [4] refers to a version that is no longer available.

    Quick search | 8.2s   [Evidence F5] [Ctrl+E] [Details F6]
    Open a source: /show 1 to /show 6

+- Ask ------------------------------------------------------------------------+
| Ask about your documents...                                                  |
+------------------------------------------------------------------------------+
 24 files ready | 3 need attention | Ctrl+E evidence | /show N    F1 / Commands
```

## 5. Review checklist

Appearance

- [ ] Dark theme tokens (section 4.1) read well in your real terminal; accent, attention and error colours are distinguishable. Try Light and Terminal default under Settings > Appearance.
- [ ] Panel padding, titles and button rows are clear enough without colour: every button is `[ Label ]`, focus adds `▸`, disabled keeps its shape (muted/italic).
- [ ] The overlay width rule (100 max, centred, full width below 80) and the cleared backdrop look intentional at 120x40, 100x30, 80x24 and 60x20.
- [ ] The persistent header (scope, mode, model, `DEMO DATA`) carries the right information, and its priority order when narrow (model drops first, then scope is truncated) is acceptable.
- [ ] The footer (ready count, "need attention", running job, `F1 / Commands`) is the right amount, including the shortcut hints (`Ctrl+E evidence · /show N`, `Enter opens evidence, Esc clears`).
- [ ] Result glyphs (`✓ Met`, `✗ Missed`; ASCII `OK`/`X`) and right-aligned numbers read well in the answer table.

Navigation

- [ ] Opening and closing every overlay keeps your draft and scroll position, and returns focus to the composer.
- [ ] Esc behaviour with stacked overlays (Sources -> Add folder, Sources -> confirmation) matches expectations.
- [ ] Ctrl+C meanings (section 4.3) feel safe, especially "never cancels indexing while an overlay is open".
- [ ] `/` opening the palette (instead of typing into the composer) is acceptable.
- [ ] Function keys F1-F8 and Alt+Up/Down are not intercepted by your terminal or window manager. Say which ones are.
- [ ] Alt+Enter inserts a newline in your terminal (Ctrl+J as a fallback).

Content and wording

- [ ] Source states Ready / Failed / Disconnected, file states (including "Processed - no searchable text") and the action labels read correctly.
- [ ] Mode names: Auto, Quick search, Plan (unavailable). The registry still lists `auto|fast|agent`; decide whether `agent` should be shown as a legacy path.
- [ ] Indexing copy: file-count bar with "not a time estimate", "Stopping after the current operation", partial and failed results with a recovery line.
- [ ] Evidence layout: Source/Location/Indexed/Supports rows, spreadsheet grid with column letters, row numbers, cited cells in `[ ]`, and a location that matches the grid (B7:D10); prose passage verbatim with a quote bar; unavailable variant; details toggle; one `n of N` position text.
- [ ] Duplicate file names show a parent folder (`policies/handbook.pdf` vs `2023/handbook.pdf`); long paths use a middle ellipsis with the full path under Details.
- [ ] A greeting is answered without citations and points to `/help`.
- [ ] Selected-answer marker `›`: the hint, Enter opens evidence, Esc clears.
- [ ] Sources, Jobs, Indexing and Settings use aligned labelled rows or tables; dates read `10 Oct 2026 10:42` everywhere.
- [ ] Answer details: Status, Mode, Model, Scope, Elapsed, Passages used, Follow-up, Period, Computation, Citations, Confidence; no confidence percentage ("Not calibrated"); Part 07 vocabulary deferred until the backend produces it. Is "Not verified (prototype)" the right placeholder for Status?

Layout

- [ ] 120x40, 100x30, 80x24, 60x20 all look intentional; below 60x16 the resize notice is shown and nothing is lost.
- [ ] `DOCKET_ASCII=1` output is acceptable on a terminal that cannot show box drawing.
- [ ] The overlay keeps the composer visible at 24+ rows; on shorter terminals it covers it (header and footer stay).

## 6. What is intentionally fake

Note: this section describes `docket tui-demo`. `docket ui` runs the same views on the real services; see `16-terminal-ui-wiring.md` for what is real there and what is still deferred.

- Every source, file, answer, citation, passage, model name, job and time. "Ask" picks one of a few canned answers by keyword (`table`/`region`/`target`, `list`/`policy`, an abstention for `nothing`/`weather`, a short no-citation reply for a greeting such as `hey`, otherwise a default).
- Indexing progress is a timer (or N in reduced motion); failures at files 12 and 18 are scripted. In `welcome-blocked` indexing fails immediately, to show the recovery state.
- Add folder never reads the path; "suggestions" are a fixed list; `/demo/...` paths are not checked.
- Refresh, Retry, Reconnect and Disconnect only change the in-memory demo world. Open original only prints what it would do.
- Settings Save changes the running demo (theme, model label, mode) and persists nothing. Embedding model and density are read-only on purpose.
- Plan is shown as unavailable. `/rechunk` and `/reindex` are listed as unavailable with a reason.
- Job history, "Indexed" times (all 10 Oct 2026 or a few days earlier), version numbers ("version 3 of 3"), passage counts ("14 searched"), cited cells and the data folder label.

Not built in this stage (still in the design): copy/plain-text view and a Copy action (section 4.5), a per-file detail view with diagnostics, Save stored copy, stored-data deletion, a real folder chooser with path completion, input history and Tab completion in the composer, token streaming, clarification and Plan review screens, mouse support (kept off, as designed).

## 7. Backend interfaces each screen will need

Mapped to section 6.4. "Exists" means present in `main` today; the rest is the work of stages 2 to 6.

| Screen / element | Needs | Status |
| --- | --- | --- |
| Header (scope, mode, model) | Effective settings loader; selected `QueryScope`; `UserMode`; configured generation model | Settings loader: new. `UserMode` in `services/query/routing.py`: exists |
| Footer counts, readiness notice | Readiness/inventory service returning eligible file and chunk counts, source states, job recency without constructing the parser | `services/sources/readiness.py` counts exist; recency and model/manifest readiness to add |
| Welcome / readiness | Health check (Ollama, answer model, embedding model installed), eligible-count query, explicit "Check again" | `infra/inference/health.check_ollama` exists; model-installed detail to add |
| Add folder | `SourceManager.register_source` then start ingestion immediately; path validation, duplicate and disconnected-path detection; broad-location guard stays in the UI | Register: exists. Auto-index action: new (stage 4b) |
| Composer, transcript | One expensive-operation slot owned by the controller; `last_attempted_question` vs `last_completed_answer`; bounded conversation context | Session state partly exists (checkpoint 1); slot ownership new |
| Answering | `QueryService.ask` accepting optional `scope`, progress callback and cancellation token via `RunContext` | New (stage 3). Result fields used: answer, citations, mode, elapsed, validation warnings, compute details |
| Activity labels | Operation-progress events with real stages (search, read evidence, write, check citations), optional step identity | New (stage 3) |
| Scope picker | `QueryScope` (all / source / file identity including source) with eligibility resolved per question; source and file listing, paginated, without reading bodies | New (stage 3); file inventory new |
| Mode picker | `UserMode.AUTO` / `FAST` / `PLAN` and a capability flag for Plan | Enum exists; capability flag new |
| Sources list and details | Per-source state, ready/failed/pending/no-text counts, last successful job time, retained storage; per-file status, chunk count, latest failure | New inventory queries; job times from ingestion job rows |
| Refresh / Retry | Incremental `ingest` for one source with progress and cancellation (no failed-files-only claim) | Pipeline exists; progress and cancellation new |
| Reconnect | Guarded source transition for a user-disconnected local folder, then auto-index | Exists from checkpoint 1 (`/reconnect`); needs a service-level event for the UI |
| Disconnect | Source revocation, then invalidate readiness and scope snapshots | Exists; snapshot invalidation to wire |
| Indexing overlay | Ingestion events: stage, file identity and index, counts, outcome, elapsed; cooperative cancellation; "stopping" until the worker returns | Callbacks for file start/done exist; stage events and cancellation new |
| Jobs | Aggregate job history plus persisted per-file results; cancelled and interrupted outcomes; recovery of abandoned jobs | New (stage 2, additive migration) |
| Evidence | Resolver result: text, heading, `location`, source/version IDs; re-resolve before display; original-file verification against the stored version; reason for unavailability | Resolver and location exist; availability reasons and original check new |
| Answer details | Mode actually used, elapsed, effective scope, rewrite, period ambiguity, validation warnings, `compute` details; `AnswerStatus`, `AbstentionReason`, `TrustSummary` when produced | Types in `services/query/trust.py`; not yet produced by `QueryService` |
| Settings | `preferences.json` (versioned, atomic), one effective-settings loader with environment precedence, settings injected into the gateway, installed-model listing, index manifest compatibility | New (stage 6) |
| Command palette | The shared `CommandRegistry` plus availability and reason per command | Registry exists; availability flags are UI-side |

Adaptation seam: replace `fake_data.World`, `answer_for`, and the `IndexJob`/`QueryOp.advance` calls in `DemoUI.tick` with a controller that owns a worker and posts immutable snapshots to the application's event loop (section 6.3). Overlays already read only `ui.world`, `ui.job`, `ui.answers()` and `ui.scope`, so they do not change.

## 8. Tests

`backend/tests/unit/tui/test_tui_prototype.py` drives the real application headlessly (pipe input plus a sized dummy output, each scenario under a hard timeout). It covers: opening and closing every overlay with draft, scroll and focus restoration; the Esc stack; Ctrl+C semantics; palette filtering and unavailable commands; typed slash commands and typo hints; scope and mode selection updating the header; Plan refusal; Sources actions and confirmations; add-and-auto-index; indexing stop, hide, completion and blocked states; sending blocked while indexing with the draft kept; newline bindings; evidence navigation and the unavailable variant; no confidence percentage; Settings save and discard; exit confirmation; layout at 120x40, 100x30, 80x24, 60x20, the too-small notice and reflow with draft and selection preserved; ASCII fallback on every main screen; and that product-like strings are not hard-coded in views.

`backend/tests/unit/tui/test_tui_polish.py` covers the polish pass: right-aligned table columns, result glyphs and the ASCII fallback, greeting replies without citations, the blank line before the warning, number shortcuts, duplicate-name disambiguation, the selected-answer marker with Enter/Esc, spreadsheet location/range consistency and the cited-cell grid, position text, a single key legend, one button shape with arrow focus marker, the Answer details rows, overlay width and centring at 120x40, 100x30, 80x24 and 60x20, a clean backdrop beside and around the modal, middle ellipsis, and consistent dates and labelled rows in Sources, Jobs and Indexing.

No test opens a terminal, a database or the home directory; `DOCKET_DATA_DIR` is a scratch directory.

## 9. Caveats: what was not verified visually

- Polish pass: the cited-cell highlight (`selected` style, bold) and the result-glyph colours were verified only structurally and in text (the `[ ]` brackets and glyphs carry the meaning without colour); how they look in your terminal and theme is unverified.
- Ctrl+E replaces the emacs "end of line" binding in the composer; check it does not clash with a terminal or multiplexer shortcut. A bare Esc that clears the selected answer waits for the key timeout (about 0.3 s) because Alt+Up/Down are also Esc sequences.
- The cleared backdrop hides the transcript while an overlay is open; if you prefer a dimmed transcript, that is a style change, not a layout change.

- Only a pseudo-terminal smoke test (start, F2, Esc, type, Ctrl+C, Ctrl+D; alternate screen entered and left, clean exit status 0) was run against a real terminal stack. Colours, bold/reverse and the exact look in your emulator are unverified; the screenshots above are text only.
- The terminal-default theme and Light theme are styled by token but only structurally tested.
- Live window resizing is simulated by changing the reported size and re-rendering; real `SIGWINCH` delivery relies on prompt_toolkit.
- Key delivery of F-keys, Alt+Enter and Alt+Up/Down depends on the emulator; the escape timeout is 50 ms, which can mis-read Esc over very slow remote links.
- Wide (CJK) and combining characters are measured with prompt_toolkit's width function but were not reviewed visually.
- The 60x20 Sources panel keeps the list and actions visible but the details are below the fold (scroll with Page Down); this is a known compromise at that size.
- The ASCII fallback maps known glyphs; user-typed non-ASCII text is shown as typed.
