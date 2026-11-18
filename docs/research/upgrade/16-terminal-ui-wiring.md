# Screen A on the real services (`docket ui`)

Status: wired behind a new opt-in command. `docket`, `docket chat` and `docket tui-demo` are unchanged. The decision to make this the default is the user's, later.

Design context: `13-terminal-ui-screen-a-design.md` (sections 5, 6, 9), `13-codex-handoff.md`, and the approved look in `15-terminal-ui-prototype-review.md`.

## 1. How to run

```
docket ui                 # needs an interactive terminal; reads the normal data dir
docket ui --ascii         # ASCII borders (also DOCKET_ASCII=1)
docket ui --reduced-motion
```

It uses the same data directory, database and models as `docket chat`. To try it without touching your real data, set `DOCKET_DATA_DIR` to a scratch folder first:

```
DOCKET_DATA_DIR=/tmp/docket-scratch docket ui
```

Ollama must be running with the answer and embedding models for indexing and answering; the welcome panel says what is missing. The key map is the one in doc 15 section 3.

## 2. Structure

| Piece | File | Role |
| --- | --- | --- |
| `TuiBackend` protocol, view records, `FakeBackend` | `interfaces/cli/tui/backend.py` | The only thing the controller and overlays know about |
| `RealBackend` | `interfaces/cli/tui/real_backend.py` | Built from `AppContext`; reads SQLite, runs the pipeline and `QueryService` |
| `TuiApp` (controller), `DemoUI` (demo wiring) | `interfaces/cli/tui/app.py` | Overlay stack, commands, workers, drafts |
| Views | `interfaces/cli/tui/overlays.py` + the `*_fragments` in `app.py` | Unchanged apart from labels and a few "not available here" states |

The view records are still the dataclasses in `fake_data` (`FakeSource`, `FakeFile`, `FakeCitation`, `FakeAnswer`), re-exported from `backend.py` as `SourceView`, `FileView`, `CitationView`, `AnswerView`. The header shows `DEMO DATA` only when `backend.demo` is true.

Threading: `run_index`, `ask` and `readiness` are blocking calls that the controller runs on a daemon worker thread. A worker never touches the UI; it publishes immutable snapshots (`IndexProgress`, `IndexOutcome`, `AskOutcome`, `ReadinessReport`) through `loop.call_soon_threadsafe`. Every backend method opens its own sessions from `session_factory`. One expensive operation (indexing or answering) at a time; sending is refused with a message and the draft is kept. Quick local actions (register, disconnect, reconnect, folder checks, source/job listings) run on the UI thread because they are single small SQLite or `stat` calls.

## 3. What is real now

| Area | Source of truth |
| --- | --- |
| Welcome / readiness | `infra/inference/health.check_ollama` (worker thread), configured model names, `ReadinessService.snapshot()` file and chunk counts. "Check again" re-runs it |
| Sources panel | `Source` rows. Status labels: ACTIVE = Ready, MISSING = Failed (folder unreachable), REVOKED = Disconnected, tombstoned or pending delete = Unavailable. Per-file rows from the non-superseded `EvidenceVersion` rows with chunk counts (Ready, Failed, Pending, "Processed, no searchable text"). Last indexed from the newest succeeded or partial job |
| Add folder | Path validation (exists, is a folder, readable, not already registered, disconnected sources point to Reconnect), broad-location guard, real directory suggestions, `SourceManager.register_source`, then indexing starts |
| Reconnect / Disconnect | `SourceManager.reconnect_source` (then indexing), `deactivate_source(reason="user_disconnected")` after a confirmation. No purge or delete actions |
| Jobs | Aggregate `IngestionJob` rows (state, time, processed/failed/chunks). Abandoned RUNNING rows read as Interrupted. Per-file detail is not stored, so the list says so |
| Indexing | `IngestionPipeline.run_ingestion_for_source` with its per-file `ProgressEvent` callback on a worker. Overlay: file X of N, current file, elapsed, indexed / unchanged / failed counts, concise failure lines (`quiet.condense_error`, de-duplicated, relative to the source folder), `quiet_ingest()` around the run, ignore-rule and stale-recipe notices after it |
| Chat | `QueryService.ask` on a worker with `mode` (auto, fast, agent) and the in-memory history. `/clear` resets it. `/retry` replaces the retried turn like `query_flow` |
| Answer text and Sources | `render.number_citations`, so `[1]`, `[2]` match `docket chat`. File names are shown; the same relative path under two sources gets the source name added |
| Evidence | `EvidenceResolver.resolve_many` (verbatim text, `location`) plus the evidence-version row (indexed time, version n of m, source). A chunk that no longer resolves becomes an "unavailable" citation with a generic reason |
| Answer details | `QueryResult`: mode actually used, elapsed, named files (`scoped_to`), follow-up rewrite, period ambiguity, compute record (used or fallback reason), validation warnings, abstention |
| Activity labels | Only two states are known: "Searching and writing answer" while `ask` runs, then "Checking citations" while citations are resolved. Elapsed seconds are shown |

### Stop and cancel semantics (never immediate)

* **Indexing, Stop**: cooperative, "stop after the current file". The progress callback raises a private `_StopRequested` between files. It derives from `BaseException` on purpose: `IngestionPipeline.run_ingestion_for_source` swallows every `Exception` from its progress callback, so an ordinary exception would be ignored. This needed no change in `services/ingestion`. Consequences, all identical to a Ctrl-C today: the job row is finalized as FAILED with error `interrupted` (Jobs shows "Interrupted"); files already finished are kept; the end-of-run whole-source reconcile pass is skipped and runs at the next Refresh. The UI shows "Stopping after the current file finishes" until the worker returns, and a large file can take a while.
* **Exit while indexing**: the exit confirmation offers "Stop and exit". The terminal is restored, then `docket ui` waits for the current file and prints that it is doing so (Ctrl+C leaves immediately).
* **Answering, Ctrl+C**: the model call cannot be interrupted. The UI says so, stays busy until the call returns, then discards the answer and tells the user nothing was added.

## 4. What is still fake, unconnected or deferred

* **Scope**: `QueryScope` does not exist, so the Scope picker (all / source / file) cannot be enforced. Choosing one shows "not connected to search yet" and leaves the scope on "All ready sources". The only restriction is the service's own file-name detection, shown in Answer details as "Named in the question". Per doc 13-codex-handoff section 5.1, an enforced scope must also have an explicit "nothing searchable in this scope" outcome before it is turned on.
* **Per-file job persistence**: aggregate rows only. Failure reasons are shown live during a run and in that run's summary; later, a failed file shows "reason not stored".
* **Settings persistence**: nothing is saved. Appearance and answering mode apply to the session. The answer model list shows only the configured model; thinking level is not connected. Embedding model is read-only.
* **Maintenance**: `/rechunk` and `/reindex` are listed as unavailable and point to `docket ingest --all --rechunk` and `docket reindex`.
* **Open original**: disabled (needs original-file verification against the stored version).
* **Plan mode**: unavailable, as before. Agent investigation is offered as an explicit choice because `/mode agent` exists in chat; the doc 13-codex-handoff section 4 caveat (agent path is less accurate than fast) is repeated in its description.
* **Passages searched**: the query service does not report it, so Answer details omits "N searched".
* **Spreadsheet cell grids** in Evidence: real passages are shown as verbatim text (no `table` data is produced yet).
* **History**: input history and Tab completion are still not in the composer (unchanged from the prototype).

## 5. Tests

* `backend/tests/unit/tui/test_tui_real.py` (29): backend wiring on a temp data dir with a fake pipeline, fake `QueryService` and real SQLite rows (readiness numbers, world and status mapping, folder validation, source actions, job history, run_index progress / failures / stop / unreachable, ask success, abstain, error, history and mode mapping, citation resolution and disambiguation); headless UI scenarios on a controllable stub backend with real worker threads (async readiness, busy gating with draft kept, stages, evidence and details, error then `/retry`, cancel then discard, `/clear`, scope refusal, indexing progress / hide / stop / exit confirm, worker exceptions); one full path (UI to `RealBackend` to fake pipeline and service on a real DB); `docket ui` refusing without a TTY.
* The demo tests are unchanged and pass on `FakeBackend`.
* No test needs Ollama, a model, Docling, the network or the user's data directory.

## 6. Not verified

* Never run against a live Ollama, a real Docling parse or a real index. All of the above is exercised through test doubles; the real pipeline's per-file events and the real `QueryResult` shapes are taken from reading the code.
* No pseudo-terminal smoke run of `docket ui` was done (the tests drive the application through prompt_toolkit's pipe input and a dummy output).
* Behaviour on very large libraries (tens of thousands of files) is untested; the Sources snapshot is rebuilt on the UI thread after each operation and could be slow there.
* The stop path relies on `BaseException` passing through the pipeline's `emit`; a test double mirrors that contract, so a future change to `emit` would need a matching test against the real class.
