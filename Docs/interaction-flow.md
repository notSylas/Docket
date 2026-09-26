# Docket — one interactive session, sequence diagram

Internal reference: what actually happens, in order, for a single session —
from typing `docket` to getting a cited answer. Based on the real code
(`cli/main.py`, `cli/interactive/session.py`, `sources/manager.py`,
`ingestion/pipeline.py`, `query/service.py`, `retrieval/hybrid.py`).

Renders as a real diagram in GitHub, VS Code (Markdown preview), or any
Mermaid-aware viewer.

```mermaid
sequenceDiagram
    actor U as User
    participant CLI as Docket CLI
    participant Doc as Docling (local)
    participant Oll as Ollama (local)
    participant DB as SQLite (FTS5) + LanceDB

    U->>CLI: docket
    CLI->>Oll: health check
    Oll-->>CLI: running, models present
    CLI-->>U: prompt to add a source (first run)

    U->>CLI: /add ~/Documents/my-project
    CLI->>DB: register folder as a source
    Note over CLI,DB: nothing parsed yet

    U->>CLI: /ingest
    loop each file
        CLI->>Doc: parse (layout, OCR, tables)
        Doc-->>CLI: text + tables as markdown
        CLI->>CLI: chunk (heading-aware, ~400 tokens)
        CLI->>Oll: embed each chunk
        Oll-->>CLI: vector
        CLI->>DB: write chunk + vector (FTS5 + LanceDB)
    end
    CLI-->>U: live progress, then done

    U->>CLI: What is the project's main goal?
    CLI->>DB: keyword search (bm25)
    CLI->>Oll: embed the question
    Oll-->>CLI: query vector
    CLI->>DB: vector search
    DB-->>CLI: two ranked lists
    CLI->>CLI: fuse (RRF), drop revoked/stale sources
    CLI->>Oll: generate (question + cited chunks)
    Note over Oll: system prompt: answer only from the<br/>given chunks, cite every claim,<br/>or abstain if unsupported
    Oll-->>CLI: answer with citation tags
    CLI-->>U: answer + numbered citations [1][2] + Sources

    U->>CLI: /show 1
    CLI-->>U: exact source passage behind [1]
```

## What matters about this flow

- **Nothing leaves the machine.** Parsing, embedding, retrieval, and
  generation are all local (Docling + Ollama). No API keys, no cloud calls.
- **Ingestion and question-answering are separate steps.** `/ingest` has to
  run (once, then incrementally on changes) before questions can be
  answered — Docket never reads files live off disk to answer a question.
- **Retrieval always runs both search types and fuses them**, not one or
  the other — that's what "hybrid" means here.
- **Abstention is designed behavior, not a bug**: if the retrieved chunks
  don't actually support an answer, the system prompt tells the model to
  say so rather than guess.
- **`/show n`** exists specifically so an answer's citation can be checked
  against the real source text, not just trusted.
