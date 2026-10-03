"""Unit tests for `docket.core.config.Settings` -- defaults and environment
variable overrides (`DOCKET_` prefix, via `pydantic_settings`)."""

from __future__ import annotations

from docket.core.config import Settings


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


def test_rewrite_defaults() -> None:
    settings = Settings()
    assert settings.rewrite_enabled is True
    assert settings.rewrite_model is None
    assert settings.rewrite_num_predict == 128


def test_rewrite_env_overrides(monkeypatch) -> None:
    monkeypatch.setenv("DOCKET_REWRITE_ENABLED", "false")
    monkeypatch.setenv("DOCKET_REWRITE_MODEL", "qwen3:8b")
    monkeypatch.setenv("DOCKET_REWRITE_TEMPERATURE", "0.6")
    monkeypatch.setenv("DOCKET_REWRITE_THINK", "true")
    monkeypatch.setenv("DOCKET_REWRITE_NUM_PREDICT", "512")
    settings = Settings()
    assert settings.rewrite_enabled is False
    assert settings.rewrite_model == "qwen3:8b"
    assert settings.rewrite_temperature == 0.6
    assert settings.rewrite_think is True
    assert settings.rewrite_num_predict == 512
