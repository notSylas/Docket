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

## Verify

```bash
docket --version
```

## Interactive mode

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
docket watch <source-id>        # re-ingest automatically on file changes (Ctrl+C to stop)
```

By default, Docket stores its local index and data under `~/.local/share/docket`. Override it with the `DOCKET_DATA_DIR` environment variable if you want it somewhere else.

## Desktop app

A Tauri + React desktop GUI exists in [`desktop/`](desktop/) but is currently paused in favor of stabilizing the CLI-first experience. It is not ready for use today — the CLI above is the supported way to run Docket.

## License

Apache License 2.0 — see [LICENSE](LICENSE).
