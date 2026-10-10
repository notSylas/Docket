# Hand-off brief: terminal UI work (Screen A) alongside the accuracy work

Audience: the engineer or coding agent implementing `13-terminal-ui-screen-a-design.md`. Another agent (Claude) is working in the same repository on retrieval accuracy, evaluation and the trust layer at the same time. This brief says what you own, what you must not break, and how to avoid collisions.

Read first: `13-terminal-ui-screen-a-design.md`, then the "Review notes" in section 5 below. The design is not yet approved for implementation as a whole; the stages in section 2 are the ones the reviewer recommends starting now.

## 1. Ground rules (hard)

1. **Never run docket, tests or scripts against `~/.local/share/docket`.** Set `DOCKET_DATA_DIR` to a scratch directory before importing docket. `backend/tests/conftest.py` enforces this for tests; keep it. On 2026-10-03 a test migrated the user's live database. A verified backup is at `~/docket-backup-2026-10-03`; rehearse any migration on a copy of it.
2. **No push, no `pipx`, no changes to `Upgrade/azure-connector-setup-status.md`** (it stays untracked). No Co-Authored-By or attribution lines in commits.
3. **One checkpoint per commit**, each with its tests passing: `cd backend && .venv/bin/python -m pytest -q tests/unit` (currently 1202 passing) and `tests/integration` (6, about 4 minutes, uses local Ollama).
4. **Regression gate for any retrieval, scope, chunking or `QueryService` change** (Upgrade/08 section 4): run the original set and require recall@8 33/33 with 0 wrongful abstentions:
   `cd backend && DOCKET_DATA_DIR=<scratch> .venv/bin/docket eval run --corpus <repo>/Docs --gold <repo>/backend/eval-public/gold.yaml --split all --repeats 1 --out <file>` then `docket eval report --gold ... --runs <file>`. Use absolute paths. Strict accuracy wanders 88-94% run to run; only recall and wrongful abstention are gates.
5. Keep new keyword parameters **optional** so the CLI, `eval/runner.py`, tests and the sidecar keep working.

## 2. Suggested order (each step leaves the CLI better)

1. The section 7 defects of the design: reconnect for a revoked path, correct rechunk guidance (`docket ingest --all --rechunk`), refresh-all including `MISSING` sources, readiness from eligible counts, show resolver locations, refresh caches after indexing and settings changes.
2. Per-file ingestion job results plus cancelled/interrupted outcomes (additive migration).
3. `QueryScope` (all / one source / one file) with enforcement in fast retrieval, agent search, evidence reads and spreadsheet tools.
4. Progress events and cooperative cancellation.
5. The full-screen prompt_toolkit shell, overlays and plain fallback.

## 3. Ownership, to avoid collisions

| Area | Owner | Notes |
| --- | --- | --- |
| `backend/src/docket/interfaces/cli/**` | UI work | |
| `services/sources/**`, readiness/inventory services | UI work | |
| `services/ingestion/**` (progress, cancellation) | UI work | |
| DB models and Alembic migrations | UI work (single owner, linear chain) | tell the accuracy agent before adding one |
| `QueryService.ask()` signature, `RunContext` plumbing (`services/agent/run_context.py`) | UI work | scope, progress callback, cancellation token ride on `RunContext` |
| Inside `_ask_fast` / `_ask_agent` bodies, `services/query/compute.py`, `routing.py`, `trust.py`, prompts | accuracy work | rebase before editing `service.py` |
| `eval/**`, `backend/eval-public/**`, `Upgrade/05-08` | accuracy work | do not edit gold sets or corpora |
| `core/config.py` | shared | additive settings only; rebase often |

Work on a separate branch (or worktree) and rebase onto `main` at each checkpoint.

## 4. Things already in `main` that you should know

- `RunContext` (per-run state) replaced mutable fields on the shared `QueryService`; concurrent asks are isolated (`19155c9`).
- Flags, all default off: `retrieval_pool_k` (`DOCKET_RETRIEVAL_POOL_K`), `compute_stage_enabled`. The answer result may carry `compute` details; show them in Answer Details only when present.
- `services/query/trust.py` defines `AnswerStatus`, `AbstentionReason` (eight codes) and claim types. Reuse these for abstention explanations instead of inventing UI-only reasons. They are not yet produced by `QueryService`.
- `services/query/routing.py` defines `UserMode` (AUTO/FAST/PLAN) and `ExecutionPath` (FAST/INVESTIGATION/CLARIFY). The design's mode picker should use AUTO/FAST/PLAN vocabulary; Plan mode itself is not implemented, so show it as unavailable.
- The agent path is currently less accurate than the fast path (47% against 82% on the extended set). Do not route more traffic to it.

## 5. Review notes on the design (apply while implementing)

1. **Empty scope means unrestricted today.** `services/query/service.py` and `services/agent/tools.py` build the scope filter with `if scope_ids` truthiness, so an empty list is dropped and retrieval searches everything. `hybrid_search` itself returns nothing for `[]`. A user-selected scope with no eligible versions must produce an explicit "nothing searchable in this scope" outcome, never a widened search. Add tests for fast and agent paths.
2. The design's modes (auto/fast/agent) differ from part 06's decided Auto/Fast/Plan; reconcile in the UI copy and command names.
3. Part 07 answer states (verified, partial, conflict, abstained) and separate support and coverage display are not yet in the design's evidence/answer-details section.
4. Cut from the first release: Light theme and density preferences, "Save stored copy", and the stored-data delete flow, unless they are cheap.
5. The design says broader test attempts stalled in a database path and a keyboard-simulation path. Make those tests bounded (timeouts) before depending on them.
6. Opening an overlay must not load Docling or scan folders; counts come from the database.

## 6. Hand back

When a checkpoint is ready, report: files changed, tests run with counts, whether the gold gate was run and its numbers, and any change to a shared file in the table above.
