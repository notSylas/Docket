"""Standalone verification harness for the sidecar's real dispatch logic
(`server.py` + `main.py`), run against a REAL `python main.py` subprocess --
no Tauri, no PyInstaller, no mocked backend. This is the checkpoint's most
important verification: it proves the entire real `docket` backend is
reachable and correct through the exact JSON-line wire protocol the Rust
bridge speaks, standalone, before any Rust/PyInstaller complexity is
layered on top.

Requires the backend venv active (`docket` importable via `sys.executable`)
and, for the `query.ask` steps, a reachable Ollama with `qwen3:14b` +
`qwen3-embedding:0.6b` pulled -- same requirement as `backend/tests
/integration/test_cli_e2e.py`, which this harness's ingest+query flow
mirrors closely (same sample doc, same known-good question). If Ollama
isn't reachable, the `query.ask` steps are skipped with an explicit,
printed reason -- never silently.

Run directly (recommended -- prints every request/response line):

    cd desktop/sidecar
    source ../../backend/.venv/bin/activate
    python _test_server.py

Also collectible by pytest (`pytest desktop/sidecar/_test_server.py -v -s`)
since it's one `test_`-prefixed function using `tmp_path`, but the `-s`
flag is required to see the request/response transcript -- run directly
for that transcript by default.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

SIDECAR_DIR = Path(__file__).resolve().parent
REPO_ROOT = SIDECAR_DIR.parents[1]
SAMPLE_DOCX = REPO_ROOT / "Docs" / "01_Work_Intelligence_PRD_v1.0.docx"


def _ollama_reachable() -> bool:
    try:
        urllib.request.urlopen("http://localhost:11434/api/tags", timeout=2)
        return True
    except Exception:
        return False


class Sidecar:
    """Thin wrapper around a `python main.py` subprocess speaking the
    JSON-line request/response protocol."""

    def __init__(self, data_dir: Path) -> None:
        env = {**os.environ, "DOCKET_DATA_DIR": str(data_dir)}
        self.proc = subprocess.Popen(
            [sys.executable, "main.py"],
            cwd=str(SIDECAR_DIR),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=True,
            bufsize=1,
        )

    def send_raw(self, line: str) -> None:
        assert self.proc.stdin is not None
        print(f">>> {line}")
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()

    def read_response(self) -> dict:
        assert self.proc.stdout is not None
        line = self.proc.stdout.readline()
        if not line:
            stderr = self.proc.stderr.read() if self.proc.stderr else ""
            raise RuntimeError(
                f"sidecar produced no output -- process likely exited. stderr:\n{stderr}"
            )
        print(f"<<< {line.rstrip()}")
        return json.loads(line)

    def request(self, req_id: str, op: str, params: dict | None = None) -> dict:
        self.send_raw(json.dumps({"id": req_id, "op": op, "params": params or {}}))
        return self.read_response()

    def close(self) -> str:
        assert self.proc.stdin is not None
        try:
            self.proc.stdin.close()
        except BrokenPipeError:
            pass
        try:
            self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)
        return self.proc.stderr.read() if self.proc.stderr else ""


def test_sidecar_end_to_end(tmp_path: Path | None = None) -> None:
    tmp_root = Path(tempfile.mkdtemp(prefix="docket_sidecar_test_")) if tmp_path is None else tmp_path
    data_dir = tmp_root / "docket_data"
    source_folder = tmp_root / "source_docs"
    source_folder.mkdir(parents=True)

    sidecar = Sidecar(data_dir)
    try:
        # 1. Fresh data dir -> empty source list.
        resp = sidecar.request("req_1", "sources.list")
        assert resp["ok"] is True
        assert resp["result"]["sources"] == []

        # 2. sources.add with a bogus/nonexistent path -> InvalidSourcePath.
        resp = sidecar.request(
            "req_2", "sources.add", {"path": str(tmp_root / "does_not_exist")}
        )
        assert resp["ok"] is False
        assert resp["error"]["type"] == "InvalidSourcePath"
        assert resp["error"]["op"] == "sources.add"

        # 3. sources.add with a real temp folder -> real source dict back.
        resp = sidecar.request("req_3", "sources.add", {"path": str(source_folder)})
        assert resp["ok"] is True
        source = resp["result"]["source"]
        assert source["id"]
        assert source["status"] == "active"
        assert source["path"] == str(source_folder)
        source_id = source["id"]

        # 4. sources.ingest on a source pointing at a folder with a real
        #    ingestable .docx (copied in, not pointed at Docs/ directly).
        assert SAMPLE_DOCX.exists(), f"sample docx missing: {SAMPLE_DOCX}"
        shutil.copy(SAMPLE_DOCX, source_folder / SAMPLE_DOCX.name)

        resp = sidecar.request("req_4", "sources.ingest", {"source_id": source_id})
        assert resp["ok"] is True, resp
        job = resp["result"]
        assert job["files_processed"] >= 1
        assert job["files_failed"] == 0
        assert job["status"] == "succeeded"

        # 5. query.ask against the freshly-ingested content -- needs Ollama.
        if not _ollama_reachable():
            print(
                "\n*** Ollama not reachable at http://localhost:11434 -- "
                "SKIPPING query.ask verification steps. This is a real gap "
                "in this run's coverage, not a silent pass. ***\n"
            )
        else:
            question = "What is Local-First Work Intelligence System?"
            resp = sidecar.request("req_5", "query.ask", {"question": question})
            assert resp["ok"] is True, resp
            result = resp["result"]
            assert result["mode"] == "fast"
            assert "evidence" in result["answer"].lower()
            assert SAMPLE_DOCX.name in json.dumps(result["citations"])
            assert len(result["citations"]) >= 1

            # 6. query.ask with explicit mode="agent" -> response mode
            #    reflects the forced mode, not the classifier's own guess
            #    (this plain-lookup question would normally classify FAST).
            resp = sidecar.request(
                "req_6", "query.ask", {"question": question, "mode": "agent"}
            )
            assert resp["ok"] is True, resp
            assert resp["result"]["mode"] == "agent"

        # 7. sources.revoke, then confirm sources.list reflects it.
        resp = sidecar.request("req_7", "sources.revoke", {"source_id": source_id})
        assert resp["ok"] is True
        assert resp["result"] == {}

        resp = sidecar.request("req_8", "sources.list")
        assert resp["ok"] is True
        revoked = next(s for s in resp["result"]["sources"] if s["id"] == source_id)
        assert revoked["status"] == "revoked"

        # 8. Unknown op -> UnknownOp error.
        resp = sidecar.request("req_9", "totally.bogus.op")
        assert resp["ok"] is False
        assert resp["error"]["type"] == "UnknownOp"

        # 9. Malformed line -> no crash, process keeps running and answers
        #    the next valid line correctly.
        sidecar.send_raw("{not valid json,,,")
        resp = sidecar.request("req_10", "sources.list")
        assert resp["ok"] is True
        assert len(resp["result"]["sources"]) == 1  # the revoked one, still listed

        print("\nALL SIDECAR VERIFICATION STEPS PASSED\n")
    finally:
        stderr = sidecar.close()
        if stderr.strip():
            print("---- sidecar stderr (dev diagnostics / any InternalError tracebacks) ----")
            print(stderr)
            print("---- end sidecar stderr ----")


if __name__ == "__main__":
    test_sidecar_end_to_end()
