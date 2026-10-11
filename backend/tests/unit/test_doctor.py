from types import SimpleNamespace
import select
import subprocess
import sys

import pytest
from typer.testing import CliRunner
from docket.core.config import Settings
from docket.core.writer_lock import LOCK_FILENAME
from docket.infra.index.manifest import new_manifest, write_manifest
from docket.interfaces.cli.context import AppContext
from docket.services import doctor


def tags(settings):
    return {"models": [{"name": settings.gen_model}, {"name": settings.embed_model}]}


def run(settings, **kwargs):
    return doctor.checks(settings, tags_fetcher=lambda url: tags(settings), environment={}, terminal=False, **kwargs)


def test_fresh_install_checks_never_initialize_data(tmp_path):
    settings = Settings(data_dir=tmp_path / "absent")
    seen = []
    results = doctor.checks(settings, tags_fetcher=lambda url: seen.append(url) or tags(settings), environment={})
    assert seen == ["http://127.0.0.1:11434/api/tags"]
    assert not any(c.status == "BLOCK" for c in results)
    assert not settings.data_dir.exists()
    assert all(c.fix for c in results if c.status != "OK")


def test_existing_database_and_lock_owner_bytes_unchanged(tmp_path):
    ctx = AppContext.for_testing(data_dir=tmp_path / "data")
    ctx.engine.dispose()
    path = ctx.settings.data_dir / LOCK_FILENAME
    path.write_text('{"pid":999999,"operation":"stale owner"}')
    before = {p.relative_to(ctx.settings.data_dir): p.read_bytes() for p in ctx.settings.data_dir.rglob("*") if p.is_file()}
    results = run(ctx.settings)
    after = {p.relative_to(ctx.settings.data_dir): p.read_bytes() for p in ctx.settings.data_dir.rglob("*") if p.is_file()}
    assert before == after
    assert next(c for c in results if c.name == "Migration revision").status == "OK"
    assert next(c for c in results if c.name == "Writer lock").status == "OK"


@pytest.mark.parametrize("host", ["http://example.com:11434", "http://127.0.0.1/other", "http://localhost@outside.test", "https://localhost:11434"])
def test_remote_or_unsafe_ollama_hosts_are_never_contacted(tmp_path, host):
    def forbidden(url):
        pytest.fail("unsafe host was contacted")
    checks = doctor.checks(Settings(data_dir=tmp_path), tags_fetcher=forbidden,
                           environment={"OLLAMA_HOST": host})
    assert next(c for c in checks if c.name == "Ollama").status == "BLOCK"


def test_missing_models_manifest_and_old_revision_are_blocking(tmp_path):
    ctx = AppContext.for_testing(data_dir=tmp_path / "data")
    settings = ctx.settings
    write_manifest(settings.index_manifest_path, new_manifest("different-model", 8))
    results = doctor.checks(settings, tags_fetcher=lambda url: {"models": []}, environment={})
    assert next(c for c in results if c.name == "Required models").status == "BLOCK"
    assert next(c for c in results if c.name == "Index manifest").status == "BLOCK"
    assert settings.gen_model in next(c for c in results if c.name == "Required models").fix
    with ctx.engine.begin() as db:
        from sqlalchemy import text
        db.execute(text("UPDATE alembic_version SET version_num='b8e1f3a45c92'"))
    assert next(c for c in run(settings) if c.name == "Migration revision").status == "BLOCK"
    ctx.engine.dispose()


def test_unreachable_ollama_and_permission_or_space_problems_are_plain(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor.os, "access", lambda *args: False)
    monkeypatch.setattr(doctor.shutil, "disk_usage", lambda *args: SimpleNamespace(free=1))
    def unavailable(url):
        raise OSError("SECRET exception contents")
    checks = doctor.checks(Settings(data_dir=tmp_path), tags_fetcher=unavailable, environment={})
    assert {c.name for c in checks if c.status == "BLOCK"} >= {"Data folder", "Disk space", "Ollama"}
    assert "SECRET" not in str(checks)
    assert all(c.fix for c in checks if c.status != "OK")


def test_busy_writer_is_reported_without_waiting_or_changing_owner(tmp_path):
    code = "from pathlib import Path; import sys; from docket.core.writer_lock import WriterLock\nwith WriterLock(Path(sys.argv[1])).hold('updating test documents'):\n print('ready',flush=True)\n sys.stdin.read()"
    proc = subprocess.Popen([sys.executable, "-c", code, str(tmp_path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert select.select([proc.stdout], [], [], 5)[0] and proc.stdout.readline().strip() == "ready"
        before = (tmp_path / LOCK_FILENAME).read_bytes()
        result = doctor.lock_check(tmp_path)
        assert result.status == "WARN" and str(proc.pid) in result.detail and "started" in result.detail
        assert (tmp_path / LOCK_FILENAME).read_bytes() == before
    finally:
        proc.terminate(); proc.wait(timeout=5)


def test_doctor_cli_warnings_exit_zero_blockers_exit_one(tmp_path, monkeypatch):
    from docket.interfaces.cli import main
    monkeypatch.setenv("DOCKET_DATA_DIR", str(tmp_path / "absent"))
    monkeypatch.setattr(main, "build_context", lambda: pytest.fail("doctor initialized the application"))
    monkeypatch.setattr(doctor, "fetch_tags", lambda url: tags(Settings()))
    ok = CliRunner().invoke(main.app, ["doctor"])
    assert ok.exit_code == 0, ok.output
    assert "WARN" in ok.output and "Fix:" in ok.output
    monkeypatch.setattr(doctor, "fetch_tags", lambda url: {"models": []})
    blocked = CliRunner().invoke(main.app, ["doctor"])
    assert blocked.exit_code == 1 and "ollama pull" in blocked.output and "Traceback" not in blocked.output
    assert not (tmp_path / "absent").exists()


def test_tags_transport_uses_only_get_with_no_proxy_and_no_redirect(monkeypatch):
    seen = {}
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, size): return b'{"models": []}'
    class Opener:
        def open(self, request, timeout):
            seen.update(url=request.full_url, method=request.get_method(), timeout=timeout)
            return Response()
    def build(*handlers):
        assert any(isinstance(h, doctor._NoRedirect) for h in handlers)
        assert handlers[0].proxies == {}
        return Opener()
    monkeypatch.setattr(doctor.urllib.request, "build_opener", build)
    assert doctor.fetch_tags(doctor.local_tags_url("localhost:11434")) == {"models": []}
    assert seen == {"url": "http://localhost:11434/api/tags", "method": "GET", "timeout": 1.5}
    with pytest.raises(ValueError):
        doctor._NoRedirect().redirect_request(None, None, 302, "", {}, "http://outside.test")
