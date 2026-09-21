"""Tests for the InferenceGateway abstraction (CP3)."""

from __future__ import annotations

import ollama
import pytest

from attest.config import settings
from attest.inference.gateway import (
    FakeInferenceGateway,
    InferenceGateway,
    InferenceUnavailableError,
    OllamaGateway,
)


# --- FakeInferenceGateway -----------------------------------------------


def test_fake_gateway_satisfies_protocol_shape() -> None:
    fake = FakeInferenceGateway()
    assert isinstance(fake, InferenceGateway)


def test_fake_gateway_generate_echoes_prompt_by_default() -> None:
    fake = FakeInferenceGateway()
    result = fake.generate(system="be terse", prompt="say hi")
    assert isinstance(result, str)
    assert result  # non-empty
    assert "say hi" in result


def test_fake_gateway_generate_returns_canned_response_when_configured() -> None:
    fake = FakeInferenceGateway(canned_response="a fixed answer")
    assert fake.generate(system="sys", prompt="anything") == "a fixed answer"
    assert fake.generate(system="sys", prompt="something else") == "a fixed answer"


def test_fake_gateway_generate_records_calls() -> None:
    fake = FakeInferenceGateway()
    fake.generate(system="sys1", prompt="p1")
    fake.generate(system="sys2", prompt="p2", temperature=0.5)
    assert fake.generate_calls == [
        {"system": "sys1", "prompt": "p1"},
        {"system": "sys2", "prompt": "p2", "temperature": 0.5},
    ]


def test_fake_gateway_embed_is_deterministic() -> None:
    fake = FakeInferenceGateway()
    vec1 = fake.embed("hello world")
    vec2 = fake.embed("hello world")
    assert vec1 == vec2
    assert len(vec1) > 0
    assert all(isinstance(x, float) for x in vec1)


def test_fake_gateway_embed_differs_for_different_input() -> None:
    fake = FakeInferenceGateway()
    assert fake.embed("hello world") != fake.embed("goodbye world")


def test_fake_gateway_embed_records_calls() -> None:
    fake = FakeInferenceGateway()
    fake.embed("text a")
    fake.embed("text b")
    assert fake.embed_calls == ["text a", "text b"]


# --- OllamaGateway construction ------------------------------------------


def test_ollama_gateway_defaults_to_settings_models() -> None:
    gateway = OllamaGateway()
    assert gateway.gen_model == settings.gen_model
    assert gateway.embed_model == settings.embed_model


def test_ollama_gateway_explicit_args_override_settings() -> None:
    gateway = OllamaGateway(gen_model="custom-gen:1b", embed_model="custom-embed:1b")
    assert gateway.gen_model == "custom-gen:1b"
    assert gateway.embed_model == "custom-embed:1b"


# --- Error translation (mocked, no real Ollama needed) --------------------


def test_generate_wraps_connection_failure(mocker) -> None:
    mocker.patch(
        "attest.inference.gateway._ollama.generate",
        side_effect=ConnectionError("Failed to connect to Ollama."),
    )
    gateway = OllamaGateway()
    with pytest.raises(InferenceUnavailableError):
        gateway.generate(system="sys", prompt="hello")


def test_embed_wraps_connection_failure(mocker) -> None:
    mocker.patch(
        "attest.inference.gateway._ollama.embed",
        side_effect=ConnectionError("Failed to connect to Ollama."),
    )
    gateway = OllamaGateway()
    with pytest.raises(InferenceUnavailableError):
        gateway.embed("hello world")


def test_generate_wraps_model_not_found(mocker) -> None:
    mocker.patch(
        "attest.inference.gateway._ollama.generate",
        side_effect=ollama.ResponseError("model 'nope:1b' not found", 404),
    )
    gateway = OllamaGateway(gen_model="nope:1b")
    from attest.inference.gateway import ModelNotFoundError

    with pytest.raises(ModelNotFoundError):
        gateway.generate(system="sys", prompt="hello")


def test_generate_does_not_leak_raw_ollama_exception(mocker) -> None:
    mocker.patch(
        "attest.inference.gateway._ollama.generate",
        side_effect=ollama.ResponseError("some server error", 500),
    )
    gateway = OllamaGateway()
    with pytest.raises(InferenceUnavailableError) as exc_info:
        gateway.generate(system="sys", prompt="hello")
    assert not isinstance(exc_info.value, ollama.ResponseError)


# --- Integration tests (require a reachable Ollama with models pulled) ----


@pytest.mark.integration
def test_real_ollama_embed_returns_nonempty_vector(ollama_available) -> None:
    gateway = OllamaGateway()
    vector = gateway.embed("hello world")
    assert isinstance(vector, list)
    assert len(vector) > 0
    assert all(isinstance(x, float) for x in vector)


@pytest.mark.integration
def test_real_ollama_generate_returns_nonempty_string(ollama_available) -> None:
    gateway = OllamaGateway()
    result = gateway.generate(
        system="You are terse.",
        prompt="Say the word 'test' and nothing else.",
    )
    assert isinstance(result, str)
    assert len(result.strip()) > 0


@pytest.mark.integration
def test_real_ollama_model_not_found_is_typed(ollama_available) -> None:
    from attest.inference.gateway import ModelNotFoundError

    gateway = OllamaGateway(gen_model="definitely-not-a-real-model:latest")
    with pytest.raises(ModelNotFoundError):
        gateway.generate(system="sys", prompt="hello")
