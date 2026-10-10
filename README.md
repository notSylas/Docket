# Docket

Docket is a local-first, evidence-backed work assistant. Point it at your folders, it ingests your documents into a local index, and answers your questions with citations back to the exact source passages — no cloud, no API keys, everything runs against a local [Ollama](https://ollama.com) model on your own machine.

## Prerequisites

- **Python 3.12+** (`python3 --version`)
- **[Ollama](https://ollama.com)** installed and running, with both required models pulled:
  ```bash
  ollama pull qwen3:14b
  ollama pull qwen3-embedding:0.6b
  ```
- **[pipx](https://pipx.pypa.io/)**, to install Docket into its own isolated environment:
  ```bash
  # Debian/Ubuntu
  sudo apt install pipx

  # macOS
  brew install pipx

  # Anything else / no system package available
  python3 -m pip install --user pipx
  ```
  Then run `python3 -m pipx ensurepath` and open a **new terminal** — it doesn't take effect in your current shell. (On Debian/Ubuntu, `pip install --user` is blocked by default for system Python — installing via `apt` avoids that entirely.)

## Install

```bash
pipx install "git+https://github.com/notSylas/Docket.git@main#subdirectory=backend"
```

First install downloads several GB (Docling pulls in PyTorch for document parsing) and can take a few minutes.

If your system's default `python3` is older than 3.12, point pipx at a specific interpreter instead:

```bash
pipx install --python python3.12 "git+https://github.com/notSylas/Docket.git@main#subdirectory=backend"
```

On first use Docket also downloads a small tokenizer (`Qwen/Qwen3-Embedding-0.6B`) from Hugging Face, used to keep chunks within the embedding model's limits. Offline, it falls back to an estimate and logs a warning; set `DOCKET_EMBED_TOKENIZER=heuristic` to skip the download.

## Verify

```bash
docket --version
```

## What Docket reads

PDF, Word (`.docx`), Excel (`.xlsx`) and PowerPoint (`.pptx`) files. Legacy `.xls`, `.xlsm` and `.ppt` files are not supported. Spreadsheets are indexed row by row with their headers, and with the fiscal-year label, units and notes the workbook itself states, so questions like "What was July revenue in FY2025-26?" can find the right sheet.

## Interactive mode

On a Linux desktop, `docket` opens the session in its own terminal window and returns your shell immediately. `docket chat` always runs in the current terminal, and `docket launch` always opens a window (it is what the app-menu icon runs). Over SSH, without a display, or with `DOCKET_NO_WINDOW=1`, `docket` runs in place as before.

- `docket install-launcher` adds a "Docket" entry and icon to your app menu (user files only, under `~/.local/share`); `docket uninstall-launcher` removes them. Docket also offers this once on the first interactive run.
- `DOCKET_TERMINAL` picks the terminal emulator (for example `DOCKET_TERMINAL=kitty`); otherwise `$TERMINAL`, `x-terminal-emulator` and common emulators are tried in turn.
- `DOCKET_NO_WINDOW=1` disables the separate window.

Run `docket` with no arguments in a terminal (or `docket chat`) to open an interactive session. Ask questions back-to-back — follow-ups like "what about X instead?" work because the session remembers recent turns — and manage sources with slash commands:

```
$ docket
docket> /add ~/Documents/my-project
docket> /ingest
docket> What is the project's main goal?
docket> Who is it aimed at?
```

| Command | What it does |
|---|---|
| `/help` | show all commands |
| `/sources` | list registered sources |
| `/add <folder>` | register a folder as a source |
| `/ingest [<source-id>\|all]` | index a source (default: all active) |
| `/mode [auto\|fast\|agent]` | show or set how questions are answered (default: auto) |
| `/clear` | forget the conversation so far |
| `/exit` | leave (Ctrl-D also works) |

## One-shot commands

The same functionality is available as individual commands, handy for scripts:

```bash
# Register a folder as a source
docket sources add ~/Documents/my-project

# List registered sources (prints the source id you'll need below)
docket sources list

# Ingest it (parses, chunks, embeds, indexes — incremental on re-runs)
docket ingest <source-id>

# Ask a question, answered with citations back to your documents
docket query "What is the project's main goal?"
```

A few more useful commands:

```bash
docket ingest --all             # ingest every active source
docket ingest --rechunk --dry-run   # list already-ingested files whose chunks are out of date
docket ingest --rechunk         # re-chunk those files (from the stored originals), then see "Upgrading"
docket reindex                  # rebuild the search indexes under the current embedding model
docket watch <source-id>        # re-ingest automatically on file changes (Ctrl+C to stop)
```

By default, Docket stores its local index and data under `~/.local/share/docket`. Override it with the `DOCKET_DATA_DIR` environment variable if you want it somewhere else.

## How answers behave

- Every answer cites the passages it used, like `[report.pdf #a1b2c3d4]`. If the evidence doesn't contain the answer, Docket says `I don't know based on the available evidence.` instead of guessing.
- Answers can take 10–20 seconds, because the local model reasons before it answers. `DOCKET_ANSWER_THINK=false` makes them much faster (a few seconds) at the cost of some correct answers on harder questions.
- Naming a file explicitly, like `in Revenue-FY2024-25.xlsx`, restricts the search to that file. Topical words alone never do.
- If a question doesn't say which fiscal year and several workbooks hold different values, Docket answers for each year (with units and source) and asks which one you meant.
- Arithmetic across many rows (totals, averages, percentage changes) is done by the language model and is the weakest area: check those figures against the cited rows.

## Upgrading

A newer Docket upgrades its database automatically the first time it opens it, and an older Docket can no longer query or ingest against the upgraded database. **Back up your data directory before the first run after an upgrade:**

```bash
cp -a ~/.local/share/docket ~/docket-backup        # or wherever DOCKET_DATA_DIR points
pipx install --force "git+https://github.com/notSylas/Docket.git@main#subdirectory=backend"
```

Files you have already ingested are not re-chunked automatically, so improvements to how documents are split and indexed only reach new or changed files until you apply them:

```bash
docket ingest --all                 # ends with a notice if some files have chunks from an older recipe
docket ingest --rechunk --dry-run   # see which files, changing nothing
docket ingest --rechunk             # re-chunk them; a failed file is marked and retried next run
docket reindex                      # bring the search indexes up to date (also needed after changing the embedding model)
```

Re-chunking works from the stored copy of each original, so a file you have since edited should be ingested normally instead. If you change `DOCKET_EMBED_MODEL`, queries refuse to run until you run `docket reindex`, rather than mixing incompatible vectors.

## Settings

All settings are environment variables with a `DOCKET_` prefix:

| Variable | Default | Effect |
|---|---|---|
| `DOCKET_DATA_DIR` | `~/.local/share/docket` | where the index and stored originals live |
| `DOCKET_GEN_MODEL` | `qwen3:14b` | Ollama model that writes answers |
| `DOCKET_EMBED_MODEL` | `qwen3-embedding:0.6b` | Ollama embedding model (run `docket reindex` after changing it) |
| `DOCKET_TERMINAL` | auto-detected | terminal emulator used for the separate window |
| `DOCKET_NO_WINDOW` | unset | `1` runs `docket` in the current terminal |
| `DOCKET_ANSWER_THINK` | `true` | `false` trades accuracy for speed |
| `DOCKET_REWRITE_ENABLED` | `true` | rewrite follow-up questions into a standalone search query |
| `DOCKET_QUERY_SCOPE_ENABLED` | `true` | restrict search when a file is named explicitly |
| `DOCKET_QUERY_AMBIGUITY_ENABLED` | `true` | answer for each fiscal year when the question doesn't say which |
| `DOCKET_EMBED_TOKENIZER` | `Qwen/Qwen3-Embedding-0.6B` | `heuristic` skips the tokenizer download |

## Measuring accuracy

`docket eval` runs a set of gold questions against a corpus and reports accuracy, retrieval recall and wrongful abstentions. Two public gold sets, a design-document set and a synthetic spreadsheet-heavy set, ship in [`backend/eval-public/`](backend/eval-public/README.md), together with the commands and the recorded results.

## Desktop app

A Tauri + React desktop GUI exists in [`desktop/`](desktop/) but is currently paused in favor of stabilizing the CLI-first experience. It is not ready for use today — the CLI above is the supported way to run Docket.

## License

Apache License 2.0 — see [LICENSE](LICENSE).
