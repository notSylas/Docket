"""A fake-gateway library run must not silently attempt internet access."""
import socket
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from docket.core.db.models import Chunk
from docket.infra.inference.gateway import FakeInferenceGateway
from docket.interfaces.cli.context import AppContext
from docket.interfaces.cli.interactive.session import _default_factory
from docket.services.query.classifier import QueryMode


@pytest.fixture
def socket_audit(monkeypatch):
    violations, connections = [], []
    original_socket = socket.socket
    original_lookup = socket.getaddrinfo
    allowed_hosts = {"localhost", "127.0.0.1", "::1"}
    monkeypatch.setenv("OLLAMA_HOST", "http://127.0.0.1:11434")
    def check(address):
        host, port = address[:2]
        if host not in allowed_hosts or port != 11434:
            violations.append((host, port))
            raise AssertionError("Offline audit blocked a connection outside local Ollama")
    class AuditedSocket(original_socket):
        def connect(self, address):
            if self.family in (socket.AF_INET, socket.AF_INET6):
                check(address); connections.append(address)
            return super().connect(address)
        def connect_ex(self, address):
            if self.family in (socket.AF_INET, socket.AF_INET6):
                check(address); connections.append(address)
            return super().connect_ex(address)
        def sendto(self, data, *args):
            if self.family in (socket.AF_INET, socket.AF_INET6):
                check(args[-1]); connections.append(args[-1])
            return super().sendto(data, *args)
        def sendmsg(self, buffers, ancdata=(), flags=0, address=None):
            if address is None:
                return super().sendmsg(buffers, ancdata, flags)
            if self.family in (socket.AF_INET, socket.AF_INET6):
                check(address); connections.append(address)
            return super().sendmsg(buffers, ancdata, flags, address)
    def lookup(host, port, *args, **kwargs):
        check((host, port))
        return original_lookup(host, port, *args, **kwargs)
    monkeypatch.setattr(socket, "socket", AuditedSocket)
    monkeypatch.setattr(socket, "getaddrinfo", lookup)
    return SimpleNamespace(check=check, violations=violations, connections=connections)


def test_fake_gateway_ingestion_and_question_open_no_network_connections(tmp_path, socket_audit):
    gateway = FakeInferenceGateway()
    ctx = AppContext.for_testing(data_dir=tmp_path / "data", gateway=gateway,
                                 parser=SimpleNamespace(parser_name="unused", parser_version="1"))
    try:
        folder = tmp_path / "documents"; folder.mkdir()
        (folder / "minutes.txt").write_text("The committee approved the revised budget on Monday. " * 10)
        source = ctx.source_manager.register_source(folder)
        result = ctx.pipeline.run_ingestion_for_source(source.id)
        assert result.files_failed == 0
        with ctx.session_factory() as db:
            cid = db.execute(select(Chunk.id)).scalar_one()
        evidence = ctx.resolver.resolve(cid)
        gateway.canned_response = "The committee approved the revised budget. " + evidence.citation_label
        answer = _default_factory(ctx, ctx.vector_writer.table).ask("What did the committee approve?", mode=QueryMode.FAST)
        assert answer.citations and answer.citations[0].chunk_id == cid
        assert gateway.generate_calls
        # Check attempts too: caught network errors must not hide a violation.
        assert socket_audit.violations == []
        assert socket_audit.connections == []
    finally:
        ctx.engine.dispose()


def test_audit_rejects_external_dns_connections_and_other_loopback_ports(socket_audit):
    for host, port in (("example.com", 443), ("127.0.0.1", 9999)):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
            with pytest.raises(AssertionError, match="Offline audit"):
                client.connect((host, port))
    with pytest.raises(AssertionError, match="Offline audit"):
        socket.getaddrinfo("example.com", 443)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
        with pytest.raises(AssertionError, match="Offline audit"):
            client.sendto(b"no traffic", ("example.com", 443))
        with pytest.raises(AssertionError, match="Offline audit"):
            client.sendmsg([b"no traffic"], [], 0, ("example.com", 443))
    assert len(socket_audit.violations) == 5
    for host in ("localhost", "127.0.0.1", "::1"):
        socket_audit.check((host, 11434))  # validate the allowance without contacting it
