"""Unit tests for `docket.config.Settings` -- defaults and environment
variable overrides (`DOCKET_` prefix, via `pydantic_settings`)."""

from __future__ import annotations

from docket.config import Settings


def test_num_ctx_and_num_predict_defaults() -> None:
    settings = Settings()
    assert settings.num_ctx == 8192
    assert settings.num_predict == 4096


def test_num_ctx_env_override(monkeypatch) -> None:
    monkeypatch.setenv("DOCKET_NUM_CTX", "16384")
    settings = Settings()
    assert settings.num_ctx == 16384


def test_num_predict_env_override(monkeypatch) -> None:
    monkeypatch.setenv("DOCKET_NUM_PREDICT", "2048")
    settings = Settings()
    assert settings.num_predict == 2048
