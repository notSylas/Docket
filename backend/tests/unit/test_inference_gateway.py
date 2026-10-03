"""Tests for the InferenceGateway abstraction (CP3)."""

from __future__ import annotations

import ollama
import pytest

from docket.core.config import settings
from docket.infra.inference.gateway import (
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


# --- FakeInferenceGateway.describe_image (visual retrieval CP2) -----------


def test_fake_gateway_describe_image_records_calls() -> None:
    fake = FakeInferenceGateway()
    fake.describe_image(b"png-bytes-1", prompt="describe it", model="vlm:1b")
    fake.describe_image(b"png-bytes-2", prompt="describe it too", model="vlm:1b", extra=1)
    assert fake.describe_image_calls == [
        {"image_bytes": b"png-bytes-1", "prompt": "describe it", "model": "vlm:1b"},
        {
            "image_bytes": b"png-bytes-2",
            "prompt": "describe it too",
            "model": "vlm:1b",
            "extra": 1,
        },
    ]


def test_fake_gateway_describe_image_returns_deterministic_default() -> None:
    fake = FakeInferenceGateway()
    result = fake.describe_image(b"abc", prompt="p", model="m")
    assert isinstance(result, str)
    assert result  # non-empty
    assert result == fake.describe_image(b"abc", prompt="p", model="m")


def test_fake_gateway_describe_image_returns_canned_description_when_configured() -> None:
    fake = FakeInferenceGateway(canned_description="a page about penguins")
    assert fake.describe_image(b"abc", prompt="p", model="m") == "a page about penguins"
    assert fake.describe_image(b"xyz", prompt="p2", model="m") == "a page about penguins"


def test_fake_gateway_describe_image_canned_description_independent_of_canned_response() -> None:
    fake = FakeInferenceGateway(canned_response="gen answer", canned_description="img answer")
    assert fake.generate(system="s", prompt="p") == "gen answer"
    assert fake.describe_image(b"abc", prompt="p", model="m") == "img answer"


# --- OllamaGateway construction ------------------------------------------


def test_ollama_gateway_defaults_to_settings_models() -> None:
    gateway = OllamaGateway()
    assert gateway.gen_model == settings.gen_model
    assert gateway.embed_model == settings.embed_model


def test_ollama_gateway_explicit_args_override_settings() -> None:
    gateway = OllamaGateway(gen_model="custom-gen:1b", embed_model="custom-embed:1b")
    assert gateway.gen_model == "custom-gen:1b"
    assert gateway.embed_model == "custom-embed:1b"


# --- num_ctx/num_predict defaults (M1) -------------------------------------


def test_generate_passes_settings_num_ctx_and_num_predict_by_default(mocker) -> None:
    mock_generate = mocker.patch(
        "docket.infra.inference.gateway._ollama.generate",
        return_value={"response": "ok"},
    )
    gateway = OllamaGateway()
    gateway.generate(system="sys", prompt="hello")

    _, kwargs = mock_generate.call_args
    assert kwargs["options"]["num_ctx"] == settings.num_ctx
    assert kwargs["options"]["num_predict"] == settings.num_predict


def test_generate_merges_num_ctx_into_caller_supplied_options(mocker) -> None:
    mock_generate = mocker.patch(
        "docket.infra.inference.gateway._ollama.generate",
        return_value={"response": "ok"},
    )
    gateway = OllamaGateway()
    gateway.generate(system="sys", prompt="hello", options={"temperature": 0})

    _, kwargs = mock_generate.call_args
    assert kwargs["options"]["temperature"] == 0
    assert kwargs["options"]["num_ctx"] == settings.num_ctx
    assert kwargs["options"]["num_predict"] == settings.num_predict


def test_generate_does_not_override_caller_supplied_num_ctx(mocker) -> None:
    mock_generate = mocker.patch(
        "docket.infra.inference.gateway._ollama.generate",
        return_value={"response": "ok"},
    )
    gateway = OllamaGateway()
    gateway.generate(
        system="sys", prompt="hello", options={"num_ctx": 16384, "num_predict": 2048}
    )

    _, kwargs = mock_generate.call_args
    assert kwargs["options"]["num_ctx"] == 16384
    assert kwargs["options"]["num_predict"] == 2048


def test_generate_passes_through_non_options_kwargs(mocker) -> None:
    mock_generate = mocker.patch(
        "docket.infra.inference.gateway._ollama.generate",
        return_value={"response": "ok"},
    )
    gateway = OllamaGateway()
    gateway.generate(system="sys", prompt="hello", think=False, format="json")

    _, kwargs = mock_generate.call_args
    assert kwargs["think"] is False
    assert kwargs["format"] == "json"
    assert kwargs["options"]["num_ctx"] == settings.num_ctx


# --- OllamaGateway.describe_image (visual retrieval CP2, mocked) ----------


def test_describe_image_calls_ollama_generate_with_images_arg(mocker) -> None:
    mock_generate = mocker.patch(
        "docket.infra.inference.gateway._ollama.generate",
        return_value={"response": "a page showing a chart"},
    )
    gateway = OllamaGateway()
    result = gateway.describe_image(
        b"raw-png-bytes", prompt="describe this page", model="qwen2.5vl:7b"
    )

    assert result == "a page showing a chart"
    _, kwargs = mock_generate.call_args
    assert kwargs["model"] == "qwen2.5vl:7b"
    assert kwargs["prompt"] == "describe this page"
    assert kwargs["images"] == [b"raw-png-bytes"]
    assert kwargs["options"]["num_ctx"] == settings.num_ctx
    assert kwargs["options"]["num_predict"] == settings.num_predict


def test_describe_image_merges_caller_supplied_options(mocker) -> None:
    mock_generate = mocker.patch(
        "docket.infra.inference.gateway._ollama.generate",
        return_value={"response": "ok"},
    )
    gateway = OllamaGateway()
    gateway.describe_image(
        b"bytes", prompt="p", model="m", options={"num_ctx": 4096}
    )

    _, kwargs = mock_generate.call_args
    assert kwargs["options"]["num_ctx"] == 4096
    assert kwargs["options"]["num_predict"] == settings.num_predict


def test_describe_image_wraps_connection_failure(mocker) -> None:
    mocker.patch(
        "docket.infra.inference.gateway._ollama.generate",
        side_effect=ConnectionError("Failed to connect to Ollama."),
    )
    gateway = OllamaGateway()
    with pytest.raises(InferenceUnavailableError):
        gateway.describe_image(b"bytes", prompt="p", model="qwen2.5vl:7b")


def test_describe_image_wraps_model_not_found(mocker) -> None:
    mocker.patch(
        "docket.infra.inference.gateway._ollama.generate",
        side_effect=ollama.ResponseError("model 'nope:1b' not found", 404),
    )
    gateway = OllamaGateway()
    from docket.infra.inference.gateway import ModelNotFoundError

    with pytest.raises(ModelNotFoundError):
        gateway.describe_image(b"bytes", prompt="p", model="nope:1b")


# --- Error translation (mocked, no real Ollama needed) --------------------


def test_generate_wraps_connection_failure(mocker) -> None:
    mocker.patch(
        "docket.infra.inference.gateway._ollama.generate",
        side_effect=ConnectionError("Failed to connect to Ollama."),
    )
    gateway = OllamaGateway()
    with pytest.raises(InferenceUnavailableError):
        gateway.generate(system="sys", prompt="hello")


def test_embed_wraps_connection_failure(mocker) -> None:
    mocker.patch(
        "docket.infra.inference.gateway._ollama.embed",
        side_effect=ConnectionError("Failed to connect to Ollama."),
    )
    gateway = OllamaGateway()
    with pytest.raises(InferenceUnavailableError):
        gateway.embed("hello world")


def test_generate_wraps_model_not_found(mocker) -> None:
    mocker.patch(
        "docket.infra.inference.gateway._ollama.generate",
        side_effect=ollama.ResponseError("model 'nope:1b' not found", 404),
    )
    gateway = OllamaGateway(gen_model="nope:1b")
    from docket.infra.inference.gateway import ModelNotFoundError

    with pytest.raises(ModelNotFoundError):
        gateway.generate(system="sys", prompt="hello")


def test_generate_does_not_leak_raw_ollama_exception(mocker) -> None:
    mocker.patch(
        "docket.infra.inference.gateway._ollama.generate",
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
    from docket.infra.inference.gateway import ModelNotFoundError

    gateway = OllamaGateway(gen_model="definitely-not-a-real-model:latest")
    with pytest.raises(ModelNotFoundError):
        gateway.generate(system="sys", prompt="hello")


# --- answer-path sampling: think / options / <think> stripping -------------


def test_generate_forwards_think_and_caller_options_and_keeps_defaults(mocker) -> None:
    mock_generate = mocker.patch(
        "docket.infra.inference.gateway._ollama.generate", return_value={"response": "ok"}
    )
    OllamaGateway().generate(
        system="s", prompt="p", options={"temperature": 0.0, "num_ctx": 123}, think=False
    )
    _, kwargs = mock_generate.call_args
    assert kwargs["think"] is False
    assert kwargs["options"] == {
        "temperature": 0.0, "num_ctx": 123, "num_predict": settings.num_predict,
    }


def test_generate_without_think_does_not_send_it(mocker) -> None:
    mock_generate = mocker.patch(
        "docket.infra.inference.gateway._ollama.generate", return_value={"response": "ok"}
    )
    OllamaGateway().generate(system="s", prompt="p")
    assert "think" not in mock_generate.call_args.kwargs
    assert "temperature" not in mock_generate.call_args.kwargs["options"]


def test_generate_retries_once_without_think_when_server_rejects_it(mocker) -> None:
    err = ollama.ResponseError('"m" does not support thinking', 400)
    mock_generate = mocker.patch(
        "docket.infra.inference.gateway._ollama.generate",
        side_effect=[err, {"response": "answer"}, {"response": "again"}],
    )
    gateway = OllamaGateway(gen_model="m")
    assert gateway.generate(system="s", prompt="p", think=True) == "answer"
    assert mock_generate.call_count == 2
    assert "think" not in mock_generate.call_args_list[1].kwargs
    # Remembered: the next call does not retry the rejected parameter.
    assert gateway.generate(system="s", prompt="p", think=True) == "again"
    assert mock_generate.call_count == 3
    assert "think" not in mock_generate.call_args_list[2].kwargs


def test_generate_other_errors_with_think_still_raise(mocker) -> None:
    mocker.patch(
        "docket.infra.inference.gateway._ollama.generate",
        side_effect=ollama.ResponseError("boom", 500),
    )
    with pytest.raises(InferenceUnavailableError):
        OllamaGateway().generate(system="s", prompt="p", think=False)


def test_generate_strips_leading_think_block(mocker) -> None:
    mocker.patch(
        "docket.infra.inference.gateway._ollama.generate",
        return_value={"response": "<think>\nhmm <b>\n</think>\n\nThe answer [x #1]."},
    )
    assert OllamaGateway().generate(system="s", prompt="p") == "The answer [x #1]."


def test_generate_keeps_think_tag_that_is_not_leading(mocker) -> None:
    mocker.patch(
        "docket.infra.inference.gateway._ollama.generate",
        return_value={"response": "Use <think>x</think> tags."},
    )
    assert OllamaGateway().generate(system="s", prompt="p") == "Use <think>x</think> tags."


def test_generate_model_override_is_used_and_not_forwarded_as_an_option(mocker) -> None:
    mock_generate = mocker.patch(
        "docket.infra.inference.gateway._ollama.generate", return_value={"response": "ok"}
    )
    gateway = OllamaGateway(gen_model="big")
    gateway.generate(system="s", prompt="p", model="small")
    assert mock_generate.call_args.kwargs["model"] == "small"
    gateway.generate(system="s", prompt="p", model=None)
    assert mock_generate.call_args.kwargs["model"] == "big"
    gateway.generate(system="s", prompt="p")
    assert mock_generate.call_args.kwargs["model"] == "big"


def test_think_rejection_is_tracked_per_model(mocker) -> None:
    err = ollama.ResponseError('"small" does not support thinking', 400)
    mock_generate = mocker.patch(
        "docket.infra.inference.gateway._ollama.generate",
        side_effect=[err, {"response": "a"}, {"response": "b"}],
    )
    gateway = OllamaGateway(gen_model="big")
    gateway.generate(system="s", prompt="p", think=True, model="small")
    # "small" is now known not to think; "big" is unaffected.
    gateway.generate(system="s", prompt="p", think=True)
    assert mock_generate.call_args_list[2].kwargs["model"] == "big"
    assert mock_generate.call_args_list[2].kwargs["think"] is True
