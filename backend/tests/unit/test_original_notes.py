from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

from docket.infra.evidence.originals import OriginalState, OriginalStatus
from docket.services.query import original_notes as mod


def _ctx(status_by_version, monkeypatch):
    versions = [SimpleNamespace(id=v) for v in status_by_version]

    class Session:
        def execute(self, _q):
            return SimpleNamespace(scalars=lambda: iter(versions))

    @contextmanager
    def factory():
        yield Session()

    monkeypatch.setattr("sqlalchemy.select", lambda *_a, **_k: SimpleNamespace(where=lambda *_x: None))
    return SimpleNamespace(
        resolver=SimpleNamespace(resolve=lambda cid: SimpleNamespace(evidence_version_id={"c1": "v1", "c2": "v2"}[cid])),
        session_factory=factory,
        evidence_manager=SimpleNamespace(original_status=lambda v: status_by_version[v.id]),
    )


def test_only_non_matching_originals_get_a_note(monkeypatch):
    ok = OriginalStatus(OriginalState.UNCHANGED, None, "")
    gone = OriginalStatus(OriginalState.MISSING, None, "The original file is no longer at /x.")
    ctx = _ctx({"v1": ok, "v2": gone}, monkeypatch)
    # EvidenceVersion.id.in_ needs a real column; stub the model.
    monkeypatch.setattr("docket.core.db.models.EvidenceVersion", SimpleNamespace(id=SimpleNamespace(in_=lambda _s: None)))
    assert mod.original_notes(ctx, ["c1", "c2"]) == {"c2": "The original file is no longer at /x."}


def test_failures_are_advisory():
    assert mod.original_notes(SimpleNamespace(), ["c1"]) == {}
