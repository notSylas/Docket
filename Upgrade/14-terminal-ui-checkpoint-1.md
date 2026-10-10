# Screen A checkpoint 1: truthful CLI state and source recovery

Date: 10 October 2026. Branch: `ui/screen-a`. Worktree: `/tmp/docket-screen-a`.

This checkpoint improves the existing terminal workflow before constructing the full-screen application. It follows the [handoff](13-codex-handoff.md) and updates the [Screen A specification](13-terminal-ui-screen-a-design.md).

## Behavior delivered

- `/add` on a disconnected path points to `/reconnect` instead of claiming it is already usable. Quoted paths are accepted.
- `/reconnect <source-id>` validates a user-disconnected local folder, retains its source/authorization identity, and starts indexing. Connector access revocation and deletion states are refused. Repeated `/remove` cannot relabel an access revocation as a user disconnect.
- Interactive `/ingest all` and one-shot `docket ingest --all` include temporarily missing sources so the existing pipeline can recover them. Disconnected/deleted sources stay excluded. Completion reflects the same eligible targets.
- Stale-chunk notices give an executable `docket ingest --all --rechunk` command. An unavailable root is distinguished from an empty supported-file inventory.
- A database-only readiness service counts chunks under ACTIVE sources and READY versions, and counts only versions with chunks as eligible files. Session readiness additionally requires the vector table. `/status` exposes eligible counts and reports readiness-check failures separately from an empty index.
- `/show` displays the resolver's stored location or an explicit unavailable label.
- Retry targets the latest attempted question. Failed retries preserve successful history and citation state; a successful retry replaces its prior turn only after completion. Clear resets retry state.
- Indexing, reconnect, and disconnect invalidate the cached query service. Future editable-settings integration remains part of the later settings checkpoint.

## Files changed

| Area | Files |
| --- | --- |
| Dependency wiring | `backend/src/docket/interfaces/cli/context.py` |
| One-shot ingestion | `backend/src/docket/interfaces/cli/main.py` |
| Interactive commands/state | `backend/src/docket/interfaces/cli/interactive/{commands,completer,ingestion_ui,query_flow,session,source_commands,state}.py` |
| Source services | `backend/src/docket/services/sources/manager.py`, new `readiness.py` |
| Tests | `backend/tests/unit/{test_commands,test_completer,test_interactive,test_sources_manager}.py`, new `test_readiness.py`, `backend/tests/unit/ingestion/test_rechunk.py` |
| Design and handoff notes | `Upgrade/10-interfaces-and-user-experience.md`, `11-recommended-architecture-and-roadmap.md`, `13-terminal-ui-screen-a-design.md`, this file |

The design now reconciles Auto/Fast/Plan and trust capability limits with parts 06/07, names RunContext as the future per-run carrier, adds visual tokens/state rules/wireframes/copy-scroll behavior, defines context reset on scope changes, and adds a prototype review gate. Optional appearance and deletion/export actions are explicitly deferred.

## Validation

- Bounded baseline outside the sandbox: **21 passed** (CLI context and keyboard reader).
- Initial focused regression suite: **217 passed**.
- Final full unit suite: **1,254 passed**, 64.51 seconds.
- Full integration suite: **5 passed, 1 skipped**, 464.98 seconds. The skipped real-PDF fixture was ignored by Git and initially absent from the new worktree.
- Copied that fixture from the main checkout; the PDF integration check then **passed**, 86.20 seconds (four dependency deprecation/runtime warnings). All six integration scenarios passed across these two runs.
- Documentation local links/code fences and `git diff --check` pass.
- Original gold gate: **not run**. This checkpoint changes no retrieval implementation, explicit scope plumbing, chunking, QueryService, model prompts, or routing. Run the original/extended gates when the scope checkpoint changes those paths.

All test processes set `DOCKET_DATA_DIR` to scratch space before importing Docket. The existing test safety fixture remains intact. The shared installed interpreter is used with `PYTHONPATH=/tmp/docket-screen-a/backend/src`, so it imports this worktree's code without modifying the main checkout's installation.

Runs use an outer `timeout --signal=INT --kill-after=10s` bound and pytest's `faulthandler_timeout`. LanceDB's sandbox-only stall was reproduced and traced; the same baseline passed outside the sandbox. Integration's long wait was in unchanged local-Ollama agent inference, and that suite completed successfully.

## Ownership and remaining work

No shared `core/config.py`, QueryService/RunContext, DB model/migration, accuracy service, gold corpus, or evaluation file changed. No push, package installation, live-data migration, or Azure-status-document change was performed. Main remains a separate checkout; this checkpoint is kept on the UI branch.

Eligibility counts are not a full index-consistency or model-readiness audit. Full inventories, model/settings controls, persisted per-file jobs, cancellation, explicit QueryScope, automatic indexing for a newly added folder, and the Screen A layout remain subsequent checkpoints. Plan/claim-verification screens activate only when the accuracy backend provides those capabilities.

Before adding the job-details migration, coordinate migration ownership with the accuracy work and rehearse on a copy of the verified backup, as the handoff requires. Review the navigable visual prototype before integrating it with backend operations.
