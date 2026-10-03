"""Batch embedding with bounded retry (`embed_texts`) and the gateway batch methods."""

from __future__ import annotations

import pytest

from docket.infra.inference import gateway as gateway_module
from docket.infra.inference.gateway import (
    FakeInferenceGateway,
    InferenceError,
    InferenceUnavailableError,
    ModelNotFoundError,
    OllamaGateway,
    embed_texts,
)


class _Scripted:
    """Gateway whose `embed_batch` raises scripted errors, then delegates."""

    def __init__(self, errors: list[Exception], drop: int = 0):
        self.errors = list(errors)
        self.drop = drop
        self.calls = 0
        self.inner = FakeInferenceGateway()

    def embed_batch(self, texts):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        out = self.inner.embed_batch(texts)
        return out[: len(out) - self.drop]


def test_order_and_batching() -> None:
    gateway = FakeInferenceGateway()
    texts = [f"t{i}" for i in range(7)]
    vectors = embed_texts(gateway, texts, batch_size=3)
    assert vectors == [gateway.embed(t) for t in texts]
    assert [len(c) for c in gateway.embed_batch_calls] == [3, 3, 1]


def test_empty_input_makes_no_calls() -> None:
    gateway = FakeInferenceGateway()
    assert embed_texts(gateway, []) == []
    assert gateway.embed_batch_calls == []


def test_count_mismatch_fails_loudly() -> None:
    with pytest.raises(InferenceError, match="1 vectors for 3 texts"):
        embed_texts(_Scripted([], drop=2), ["a", "b", "c"])


def test_transient_error_is_retried_with_backoff() -> None:
    sleeps: list[float] = []
    scripted = _Scripted([InferenceUnavailableError("x"), InferenceUnavailableError("y")])
    vectors = embed_texts(scripted, ["a"], sleep=sleeps.append, backoff_seconds=0.1)
    assert len(vectors) == 1
    assert scripted.calls == 3
    assert sleeps == [0.1, 0.2]


def test_retry_is_bounded() -> None:
    scripted = _Scripted([InferenceUnavailableError("x")] * 10)
    with pytest.raises(InferenceUnavailableError):
        embed_texts(scripted, ["a"], max_attempts=3, sleep=lambda s: None)
    assert scripted.calls == 3


def test_model_not_found_is_not_retried() -> None:
    scripted = _Scripted([ModelNotFoundError("nope")])
    with pytest.raises(ModelNotFoundError):
        embed_texts(scripted, ["a"], sleep=lambda s: None)
    assert scripted.calls == 1


def test_ollama_embed_batch_passes_list(monkeypatch) -> None:
    seen = {}

    def fake_embed(model, input):
        seen["input"] = input
        return {"embeddings": [[1.0], [2.0]]}

    monkeypatch.setattr(gateway_module._ollama, "embed", fake_embed)
    assert OllamaGateway(embed_model="m").embed_batch(["a", "b"]) == [[1.0], [2.0]]
    assert seen["input"] == ["a", "b"]
