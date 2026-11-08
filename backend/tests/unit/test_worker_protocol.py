from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from types import SimpleNamespace

from docket.interfaces.worker import build_methods, serve


class FakeService:
    def __init__(self, abstain: bool = False) -> None:
        self.abstain = abstain
        self.calls: list[tuple[str, dict]] = []

    def ask(self, question, **kwargs):
        self.calls.append((question, kwargs))
        kwargs["on_progress"]("search")
        kwargs["on_progress"]("search")
        kwargs["on_progress"]("resolve_evidence")
        kwargs["on_progress"]("generation")
        kwargs["on_progress"]("citation_validation")
        cites = [] if self.abstain else [
            SimpleNamespace(citation_label="[a.txt#1]", chunk_id="c1", source_display_name="a.txt")
        ]
        return SimpleNamespace(
            answer="I don't know." if self.abstain else "It is 2,650 [a.txt#1].",
            abstained=self.abstain,
            citations=cites,
            latency_by_phase={"generation": 1.23456},
        )


def make_context(table="T"):
    src = SimpleNamespace(id="s1", path="/x/docs", status=SimpleNamespace(value="active"),
                          name=None, display_name=None)
    return SimpleNamespace(
        settings=SimpleNamespace(gen_model="qwen3:14b", embed_model="emb"),
        readiness=SimpleNamespace(snapshot=lambda: SimpleNamespace(chunks=7), files_by_source=lambda: {"s1": 3}),
        source_manager=SimpleNamespace(list_sources=lambda: [src]),
        vector_writer=SimpleNamespace(table=table),
        resolver=SimpleNamespace(resolve=lambda cid: SimpleNamespace(text="Crane   day rate\n2,650")),
    )


def run(lines, service=None, ctx=None, keep_progress=False):
    service = service or FakeService()
    methods = build_methods(ctx or make_context(), lambda c, t: service)
    out = io.StringIO()
    serve(methods, io.StringIO("\n".join(lines) + "\n"), out)
    msgs = [json.loads(x) for x in out.getvalue().splitlines()]
    if not keep_progress:
        msgs = [m for m in msgs if m.get("method") != "progress"]
    return msgs, service


def req(i, method, params=None):
    return json.dumps({"jsonrpc": "2.0", "id": i, "method": method, **({"params": params} if params is not None else {})})


def test_ping_and_status():
    msgs, _ = run([req(1, "ping"), req(2, "status")])
    assert msgs[0]["id"] == 1 and msgs[0]["result"]["pong"] is True
    assert msgs[1]["result"]["model"] == "qwen3:14b" and msgs[1]["result"]["chunks"] == 7


def test_sources_list():
    msgs, _ = run([req(1, "sources.list")])
    (s,) = msgs[0]["result"]["sources"]
    assert s["id"] == "s1" and s["status"] == "active" and s["files"] == 3 and s["display_name"]


def test_ask_streams_phases_then_result():
    msgs, svc = run([req(5, "query.ask", {"question": "rate?"})], keep_progress=True)
    phases = [m["params"]["phase"] for m in msgs if m.get("method") == "progress"]
    assert phases == ["retrieving", "reading", "answering"]
    final = msgs[-1]
    assert final["id"] == 5
    r = final["result"]
    assert r["abstained"] is False and "2,650" in r["answer"]
    assert r["citations"] == [{"label": "[a.txt#1]", "source_display_name": "a.txt",
                               "chunk_id": "c1", "excerpt": "Crane day rate 2,650"}]
    assert r["timings"]["by_phase"]["generation"] == 1.235
    assert "scope" not in svc.calls[0][1]


def test_ask_abstained_and_scope():
    msgs, svc = run([req(1, "query.ask", {"question": "q", "scope": "s1"})], FakeService(abstain=True))
    assert msgs[-1]["result"]["abstained"] is True and msgs[-1]["result"]["citations"] == []
    assert svc.calls[0][1]["scope"].source_id == "s1"


def test_ask_validation_and_no_index():
    msgs, _ = run([req(1, "query.ask", {"question": "  "}), req(2, "query.ask", {"question": "q"})],
                  ctx=make_context(table=None))
    assert msgs[0]["error"]["code"] == -32602
    assert msgs[1]["error"]["data"]["kind"] == "NoContentIndexed"


def test_protocol_errors_and_survival():
    msgs, _ = run(["{not json", "[1]", json.dumps({"jsonrpc": "2.0", "id": 3, "method": "nope"}),
                   req(4, "ping", ), json.dumps({"jsonrpc": "2.0", "method": "ping"}), req(6, "ping")])
    assert msgs[0]["error"]["code"] == -32700 and msgs[0]["id"] is None
    assert msgs[1]["error"]["code"] == -32600
    assert msgs[2]["error"]["code"] == -32601 and msgs[2]["id"] == 3
    assert msgs[3]["id"] == 4 and msgs[3]["result"]["pong"]
    assert msgs[4]["id"] == 6  # notification (no id) produced no reply


def test_handler_exception_is_redacted_error():
    class Boom:
        def ask(self, q, **k):
            raise RuntimeError("failed token=abc123secret")

    msgs, _ = run([req(1, "query.ask", {"question": "q"}), req(2, "ping")], Boom())
    assert msgs[0]["error"]["data"]["kind"] == "RuntimeError"
    assert "abc123secret" not in msgs[0]["error"]["message"]
    assert msgs[1]["result"]["pong"]


def test_worker_command_registered():
    from typer.testing import CliRunner
    from docket.interfaces.cli.main import app

    result = CliRunner().invoke(app, ["worker", "--help"])
    assert result.exit_code == 0 and "JSON-RPC" in result.output


def test_real_worker_stdout_is_only_protocol(tmp_path):
    env = {**os.environ, "DOCKET_DATA_DIR": str(tmp_path / "data")}
    proc = subprocess.run(
        [sys.executable, "-c", "from docket.interfaces.cli.main import app; app()", "worker"],
        input=req(1, "ping") + "\n" + req(2, "sources.list") + "\n",
        capture_output=True, text=True, env=env, timeout=120,
    )
    lines = proc.stdout.splitlines()
    assert [json.loads(x)["id"] for x in lines] == [1, 2], proc.stderr
    assert json.loads(lines[1])["result"] == {"sources": []}
