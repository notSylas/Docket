"""Unit tests for identifier follow-up retrieval (`infra.retrieval.id_followup`)."""

from __future__ import annotations

import docket.infra.retrieval.id_followup as mod
from docket.core.config import Settings
from docket.infra.retrieval.id_followup import extract_identifiers, followup_chunk_ids


def test_extract_identifiers_patterns_and_order() -> None:
    q = "Per Clause 4.2 and Section 3, does BL-102 match Approval AM-07? clause 4.2 again"
    assert extract_identifiers(q) == ["Clause 4.2", "Section 3", "BL-102", "AM-07"]


def test_extract_ignores_plain_words() -> None:
    assert extract_identifiers("what is the total revenue in 2024?") == []


def test_followup_disabled_by_flag_default_on(monkeypatch) -> None:
    monkeypatch.delenv("DOCKET_ID_FOLLOWUP", raising=False)
    assert Settings().id_followup is True
    monkeypatch.setenv("DOCKET_ID_FOLLOWUP", "0")
    assert Settings().id_followup is False


def _patch(monkeypatch, table: dict[str, list[str]]) -> list[str]:
    calls: list[str] = []

    def fake(engine, phrase, limit, scope_version_ids=None):
        calls.append(phrase)
        return table.get(phrase, [])

    monkeypatch.setattr(mod, "phrase_search", fake)
    return calls


def test_question_id_missing_triggers_lookup(monkeypatch) -> None:
    calls = _patch(monkeypatch, {"BL-102": ["c9", "c1", "c8"]})
    out = followup_chunk_ids(None, "What is BL-102 owner?", [("c1", "row here")])
    assert calls == ["BL-102"]
    assert out == ["c9", "c8"]  # c1 already retrieved; capped at 2 per id


def test_question_id_present_no_lookup(monkeypatch) -> None:
    calls = _patch(monkeypatch, {})
    out = followup_chunk_ids(None, "BL-102?", [("c1", "| BL-102 | owner |")])
    assert out == [] and calls == []


def test_chunk_reference_mentioned_once_triggers_lookup_but_defined_does_not(monkeypatch) -> None:
    calls = _patch(monkeypatch, {"BL-105": ["c5"]})
    retrieved = [
        ("c1", "Blocked by BL-105 and BL-107"),
        ("c2", "BL-107 row: shipped"),
        ("c3", "BL-107 also"),
    ]
    out = followup_chunk_ids(None, "what blocks it", retrieved)
    assert calls == ["BL-105"]
    assert out == ["c5"]


def test_total_cap(monkeypatch) -> None:
    ids = [f"BL-{i}" for i in range(10, 20)]
    _patch(monkeypatch, {i: [f"x{i}a", f"x{i}b"] for i in ids})
    out = followup_chunk_ids(None, " ".join(ids), [("c1", "nothing")], max_total=6)
    assert len(out) == 6


def test_phrase_search_real_fts(migrated_sqlite_engine) -> None:
    from sqlalchemy import text
    with migrated_sqlite_engine.connect() as conn:
        n = conn.execute(text("SELECT count(*) FROM fts_chunks")).scalar()
    assert n == 0
    assert mod.phrase_search(migrated_sqlite_engine, "BL-102", 3) == []
    assert mod.phrase_search(migrated_sqlite_engine, "---", 3) == []
