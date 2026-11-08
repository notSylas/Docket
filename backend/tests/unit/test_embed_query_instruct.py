"""Query-only embedding instruction (flag DOCKET_EMBED_QUERY_INSTRUCT, default off)."""

from __future__ import annotations

from docket.core.config import Settings, settings
from docket.infra.inference.gateway import format_query_for_embedding


def test_flag_default_off_and_env(monkeypatch) -> None:
    monkeypatch.delenv("DOCKET_EMBED_QUERY_INSTRUCT", raising=False)
    assert Settings().embed_query_instruct is False
    monkeypatch.setenv("DOCKET_EMBED_QUERY_INSTRUCT", "1")
    assert Settings().embed_query_instruct is True


def test_format_off_is_identity_and_on_wraps(monkeypatch) -> None:
    monkeypatch.setattr(settings, "embed_query_instruct", False)
    assert format_query_for_embedding("what is X?") == "what is X?"
    monkeypatch.setattr(settings, "embed_query_instruct", True)
    assert format_query_for_embedding("what is X?") == (
        "Instruct: Given a question, retrieve the passages that answer it\nQuery: what is X?"
    )
