# Docket documentation

One lowercase `docs/` tree. Application code lives in [`../app/`](../app/) (Rust) and [`../backend/`](../backend/) (Python); evaluation material in [`../backend/eval-public/`](../backend/eval-public/README.md); frozen history in [`../archive/`](../archive/README.md).

| Folder | What is in it |
| --- | --- |
| [`product/`](product/) | Product requirements and engineering specifications (`01`–`06` .docx: PRD, SRS, SAD, TDD, WBS, database design) and product analysis: roadmap, backlog, gap analysis, competitor pain points, implementation plan. |
| [`architecture/`](architecture/) | Architecture notes: the next-phase and Rust-platform architecture, the interaction flow and the accuracy-evaluation design. |
| [`api/`](api/) | Interface contracts. [`evidence-api-contract.md`](api/evidence-api-contract.md) is the desktop contract for `evidence.v1`. |
| [`ui/`](ui/README.md) | Design system, information architecture, screen specifications, the UI competitor audit, and the preserved prototype and C0–C3 integration reports and screenshots. |
| [`research/upgrade/`](research/upgrade/README.md) | The numbered Upgrade research series (`00`–`18`): requirements, sources, parsing, storage, chunking, retrieval, reasoning, citations, evaluation, runtime, interfaces and the roadmap. |
| [`adr/`](adr/) | Architecture decision records. |

## Old paths

Many documents were written when the repository had a different layout and describe files by their old location. Those mentions are left as written, because they record what was true at the time. Use this table to find the current location:

| Old path | Current path |
| --- | --- |
| `Docs/*.docx` | `docs/product/` |
| `Docs/accuracy-evaluation.md`, `Docs/interaction-flow.md` | `docs/architecture/` |
| `Docs/R2b-evidence-api-contract.md` | `docs/api/evidence-api-contract.md` |
| `Upgrade/` | `docs/research/upgrade/` |
| `desktop/` (Tauri prototype) | `archive/desktop-tauri/` |
| `spike/` | `archive/spike/` |
| `docs/ui-prototype/` in the `ui-prototype` checkout (prototype reports and screenshots) | `docs/ui/prototype/` |
| `docs/ui-prototype/` in the `ui-integration` checkout (C0–C3 reports, logs and screenshots) | `docs/ui/integration/` |
| `docs/*.md` design and product documents in the main checkout | `docs/product/`, `docs/architecture/`, `docs/ui/` (same file names) |
