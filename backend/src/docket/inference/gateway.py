"""Swappable inference backend abstraction.

Callers (retrieval, query service, agent, ...) depend on the `InferenceGateway`
Protocol and docket's own exception types below -- never on the `ollama`
package directly. This keeps the rest of the codebase free of any dependency
on how/where generation and embedding actually happen, and lets tests swap in
`FakeInferenceGateway` instead of talking to a real model server.
"""

from __future__ import annotations

import hashlib
from typing import Protocol, runtime_checkable

import ollama as _ollama

from docket.config import settings


class InferenceError(Exception):
    """Base class for errors raised by an `InferenceGateway`.

    Raised instead of letting a raw exception from the underlying inference
    library (e.g. `ollama`) leak out of the gateway.
    """


class InferenceUnavailableError(InferenceError):
    """The inference backend could not be reached or failed unexpectedly.

    Covers connection failures (e.g. Ollama isn't running) as well as other
    backend-side failures that aren't a "model not found" case.
    """


class ModelNotFoundError(InferenceError):
    """The requested model is not available on the inference backend.

    For Ollama this typically means the model hasn't been pulled yet
    (`ollama pull <model>`).
    """


@runtime_checkable
class InferenceGateway(Protocol):
    """Interface for a backend capable of text generation and embedding."""

    def generate(self, *, system: str, prompt: str, **opts) -> str:
        """Returns the generated text response."""
        ...

    def embed(self, text: str) -> list[float]:
        """Returns the embedding vector for a single piece of text."""
        ...


def _translate_error(exc: Exception, model: str) -> InferenceError:
    """Map an exception raised by the `ollama` package to an `InferenceError`."""
    # ollama-python raises the builtin ConnectionError when the underlying
    # httpx request can't connect at all (e.g. Ollama isn't running).
    if isinstance(exc, ConnectionError):
        return InferenceUnavailableError(
            f"Could not reach Ollama (model={model!r}): {exc}"
        )

    if isinstance(exc, _ollama.ResponseError):
        message = str(getattr(exc, "error", exc))
        status_code = getattr(exc, "status_code", None)
        # Ollama returns HTTP 404 with an "... not found ..." message when a
        # model hasn't been pulled. Key off the status code primarily, with a
        # text fallback in case that ever changes.
        if status_code == 404 or "not found" in message.lower():
            return ModelNotFoundError(
                f"Model {model!r} is not available on Ollama "
                f"(try `ollama pull {model}`): {message}"
            )
        return InferenceUnavailableError(
            f"Ollama request failed (status={status_code}): {message}"
        )

    return InferenceUnavailableError(
        f"Unexpected error communicating with Ollama (model={model!r}): {exc}"
    )


class OllamaGateway:
    """`InferenceGateway` backed by a local Ollama server."""

    def __init__(self, gen_model: str | None = None, embed_model: str | None = None):
        self.gen_model = gen_model or settings.gen_model
        self.embed_model = embed_model or settings.embed_model

    def generate(self, *, system: str, prompt: str, **opts) -> str:
        try:
            response = _ollama.generate(
                model=self.gen_model, system=system, prompt=prompt, **opts
            )
        except Exception as exc:
            raise _translate_error(exc, self.gen_model) from exc
        return response["response"]

    def embed(self, text: str) -> list[float]:
        try:
            response = _ollama.embed(model=self.embed_model, input=text)
        except Exception as exc:
            raise _translate_error(exc, self.embed_model) from exc
        return response["embeddings"][0]


class FakeInferenceGateway:
    """In-memory `InferenceGateway` test double -- no network, no model server.

    - `generate()` returns a configurable canned string (echoing the prompt by
      default).
    - `embed()` returns a deterministic, hash-derived vector: the same input
      text always produces the same vector, which is enough for tests that
      need embeddings to behave consistently (e.g. similarity/ranking checks)
      without a real embedding model.

    Calls are recorded on `generate_calls` / `embed_calls` so tests can assert
    on what was asked of the gateway.
    """

    def __init__(self, canned_response: str | None = None, embed_dim: int = 32):
        self.canned_response = canned_response
        self.embed_dim = embed_dim
        self.generate_calls: list[dict] = []
        self.embed_calls: list[str] = []

    def generate(self, *, system: str, prompt: str, **opts) -> str:
        self.generate_calls.append({"system": system, "prompt": prompt, **opts})
        if self.canned_response is not None:
            return self.canned_response
        return f"[fake response to: {prompt}]"

    def embed(self, text: str) -> list[float]:
        self.embed_calls.append(text)
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        repeated = (digest * ((self.embed_dim // len(digest)) + 1))[: self.embed_dim]
        return [byte / 255.0 for byte in repeated]
