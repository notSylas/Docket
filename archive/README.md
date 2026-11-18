# Archive

Frozen history. Nothing here is built, tested or imported by the active code, and none of it is maintained.

| Folder | What it is | Status |
| --- | --- | --- |
| [`spike/`](spike/) | The original validation spike (parsing, retrieval and agent-policy experiments) whose findings in [`spike/RESULTS.md`](spike/RESULTS.md) shaped the current backend. Source comments in `backend/` cite it. | Kept for reference. |
| [`desktop-tauri/`](desktop-tauri/) | The earlier Tauri + React desktop prototype with a Python sidecar. | Paused and superseded by the native egui application in [`../app/`](../app/). It targets an older backend layout (for example `docket.cli` and `docket.inference`) and does not work with the current backend. |

Files were moved here with `git mv`, so their history is preserved (`git log --follow`).
