"""R2b-1c: end-to-end evidence API tests (real docketd + REAL worker protocol layer +
real scratch library; only LLM generation and vector retrieval are stubbed, see
``e2e_worker.py``). Run: ``pytest tests/e2e -m integration -p no:cacheprovider``."""

from __future__ import annotations

import pytest

import harness


def pytest_collection_modifyitems(items):
    for item in items:
        if "tests/e2e" in str(item.fspath).replace("\\", "/"):
            item.add_marker(pytest.mark.integration)


@pytest.fixture(autouse=True)
def _need_daemon_binary():
    if not harness.DOCKETD.exists():
        pytest.skip(f"docketd binary not built ({harness.DOCKETD}); run `cargo build --offline` in app/")


@pytest.fixture()
def base():
    b = harness.new_base()
    yield b
    harness.cleanup(b)


@pytest.fixture()
def lib(base):
    lb = harness.E2ELib(base / "data")
    yield lb
    lb.engine.dispose()


@pytest.fixture()
def daemons(base, lib):
    """Factory for daemons on the scratch library; all are stopped (daemon, worker,
    socket) at teardown."""
    made: list[harness.Daemon] = []

    def make(start=True, **kw):
        kw.setdefault("name", f"d{len(made)}")
        d = harness.Daemon(base, data_dir=lib.db.parent, **kw)
        made.append(d)
        if start:
            d.start()
        return d

    yield make
    for d in made:
        d.stop()


@pytest.fixture()
def daemon(daemons):
    return daemons()
