"""Swappable inference backend abstraction.

Callers (retrieval, query service, agent, ...) depend on the `InferenceGateway`
Protocol and docket's own exception types below -- never on the `ollama`
package directly. This keeps the rest of the codebase free of any dependency
on how/where generation and embedding actually happen, and lets tests swap in
`FakeInferenceGateway` instead of talking to a real model server.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from typing import Callable, Iterable, Iterator, Protocol, Sequence, runtime_checkable

import ollama as _ollama

from docket.core.config import settings

logger = logging.getLogger(__name__)

# A leading reasoning block some models emit inline in `response` (Ollama
# normally moves it to the separate `thinking` field, so this is a backstop).
_LEADING_THINK_RE = re.compile(r"\A\s*<think>.*?</think>\s*", re.DOTALL)


def strip_think_block(text: str) -> str:
    """`text` without a leading `<think>...</think>` block (if any)."""
    return _LEADING_THINK_RE.sub("", text, count=1)


class ThinkStreamFilter:
    """Incremental `strip_think_block`: feed text pieces, get the visible text.

    Only a *leading* `<think>...</think>` block is removed (same rule as
    `strip_think_block`). Text is held back only while it could still turn out
    to be the start of such a block; once it cannot be, everything flows
    through unchanged. `flush()` releases whatever is still held at the end.
    """

    _OPEN = "<think>"
    _CLOSE = "</think>"

    def __init__(self) -> None:
        self._buf = ""
        self._state = "start"  # start -> (thinking ->) passthrough

    def feed(self, piece: str) -> str:
        if self._state == "passthrough":
            return piece
        if self._state == "trim":
            piece = piece.lstrip()
            if piece:
                self._state = "passthrough"
            return piece
        self._buf += piece
        if self._state == "start":
            stripped = self._buf.lstrip()
            if not stripped:
                return ""
            if stripped.startswith(self._OPEN):
                self._state = "thinking"
                self._buf = stripped[len(self._OPEN):]
            elif self._OPEN.startswith(stripped):
                return ""  # could still become "<think>"
            else:
                self._state = "passthrough"
                out, self._buf = self._buf, ""
                return out
        if self._state == "thinking":
            end = self._buf.find(self._CLOSE)
            if end < 0:
                return ""
            rest = self._buf[end + len(self._CLOSE):]
            self._buf = ""
            self._state = "trim"
            piece = rest
        if self._state == "trim":
            piece = piece.lstrip() if piece else ""
            if piece:
                self._state = "passthrough"
            return piece
        return ""

    def flush(self) -> str:
        """Held text at end of stream. An unterminated think block is dropped,
        a held partial like "<thi" that never completed is released."""
        out = self._buf if self._state == "start" else ""
        self._buf = ""
        self._state = "passthrough"
        return out


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

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Returns one embedding vector per text, in the same order. Must
        return exactly `len(texts)` vectors."""
        ...

    def describe_image(self, image_bytes: bytes, *, prompt: str, model: str, **opts) -> str:
        """Returns a short text description of an image, produced by a
        vision-language model.

        Used only as a retrieval-ranking signal (`docket.infra.index.visual_index`)
        -- the returned description is never citable evidence and must never
        be shown to a user or flow into `validate_citations`/
        `EvidenceResolver`."""
        ...


@runtime_checkable
class StreamingInferenceGateway(Protocol):
    """Optional capability: incremental generation. Callers must check with
    `isinstance(gateway, StreamingInferenceGateway)` and fall back to
    `generate` when absent. Not part of `InferenceGateway` so existing
    implementations stay valid."""

    def generate_stream(self, *, system: str, prompt: str, **opts) -> Iterator[str]:
        """Yields visible text pieces as they are generated (reasoning blocks
        removed). Raises an `InferenceError` -- possibly after some pieces
        were already yielded -- if generation fails."""
        ...


QUERY_INSTRUCTION = "Given a question, retrieve the passages that answer it"


def format_query_for_embedding(query: str) -> str:
    """Query text to embed. With `settings.embed_query_instruct` on, wraps it in
    Qwen3-Embedding's instruct format; documents are never wrapped."""
    if settings.embed_query_instruct:
        return f"Instruct: {QUERY_INSTRUCTION}\nQuery: {query}"
    return query


def embed_texts(
    gateway: InferenceGateway,
    texts: Sequence[str],
    *,
    batch_size: int | None = None,
    max_attempts: int = 3,
    backoff_seconds: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
    between_batches: Callable[[], None] | None = None,
) -> list[list[float]]:
    """Embed `texts` in batches of `batch_size`, preserving order.

    `between_batches`, when given, is called before every batch (the tiered
    indexer uses it to yield to a question being answered).

    Each batch is retried (up to `max_attempts` calls, with a doubling
    backoff) only on `InferenceUnavailableError`; `ModelNotFoundError` and
    anything else propagates immediately. A batch that returns a different
    number of vectors than texts raises rather than letting records and
    vectors drift out of alignment.
    """
    size = batch_size if batch_size is not None else settings.embed_batch_size
    if size < 1:
        raise ValueError(f"batch_size must be >= 1 (got {size})")
    texts = list(texts)
    vectors: list[list[float]] = []
    for start in range(0, len(texts), size):
        if between_batches is not None:
            between_batches()
        batch = texts[start : start + size]
        for attempt in range(1, max_attempts + 1):
            try:
                result = gateway.embed_batch(batch)
                break
            except InferenceUnavailableError:
                if attempt == max_attempts:
                    raise
                sleep(backoff_seconds * 2 ** (attempt - 1))
        if len(result) != len(batch):
            raise InferenceError(
                f"embed_batch returned {len(result)} vectors for {len(batch)} texts"
            )
        vectors.extend(result)
    return vectors


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


def _chain(*iterables: Iterable) -> Iterator:
    for it in iterables:
        yield from it


class OllamaGateway:
    """`InferenceGateway` backed by a local Ollama server."""

    def __init__(self, gen_model: str | None = None, embed_model: str | None = None):
        self.gen_model = gen_model or settings.gen_model
        self.embed_model = embed_model or settings.embed_model
        self._think_unsupported: set[str] = set()

    def _generate_response(self, *, system: str, prompt: str, **opts):
        """Raw `ollama.generate` response (dict-like) with default options
        applied. `think` (if passed) is dropped for good on this gateway for
        a model the server rejects it for (HTTP 400 "does not support
        thinking"), and the call is retried once without it."""
        opts = dict(opts)
        # Per-call model override (e.g. a smaller rewrite model); None or
        # absent means this gateway's own generation model.
        model = opts.pop("model", None) or self.gen_model
        options = dict(opts.get("options") or {})
        # settings.num_ctx/num_predict are defaults, not overrides: a caller
        # that already put num_ctx/num_predict in its own `options` dict
        # (e.g. eval/judge.py's JUDGE_OPTS) wins.
        options.setdefault("num_ctx", settings.num_ctx)
        options.setdefault("num_predict", settings.num_predict)
        opts["options"] = options
        if opts.get("think") is not None and model in self._think_unsupported:
            opts.pop("think")
        try:
            try:
                return _ollama.generate(
                    model=model, system=system, prompt=prompt, **opts
                )
            except _ollama.ResponseError as exc:
                if opts.get("think") is None or "think" not in str(getattr(exc, "error", exc)).lower():
                    raise
                logger.warning(
                    "model %r rejected think=%r (%s); retrying without it",
                    model, opts["think"], exc,
                )
                self._think_unsupported.add(model)
                opts.pop("think")
                return _ollama.generate(
                    model=model, system=system, prompt=prompt, **opts
                )
        except Exception as exc:
            raise _translate_error(exc, model) from exc

    def generate(self, *, system: str, prompt: str, **opts) -> str:
        response = self._generate_response(system=system, prompt=prompt, **opts)
        return strip_think_block(response["response"])

    def generate_stream(self, *, system: str, prompt: str, **opts) -> Iterator[str]:
        """Streaming twin of `generate`: same options and `think` handling,
        yields text pieces. Errors (also mid-stream) become `InferenceError`."""
        opts = dict(opts)
        model = opts.pop("model", None) or self.gen_model
        options = dict(opts.get("options") or {})
        options.setdefault("num_ctx", settings.num_ctx)
        options.setdefault("num_predict", settings.num_predict)
        opts["options"] = options
        if opts.get("think") is not None and model in self._think_unsupported:
            opts.pop("think")
        think_filter = ThinkStreamFilter()

        def open_stream(call_opts: dict):
            it = iter(_ollama.generate(model=model, system=system, prompt=prompt, stream=True, **call_opts))
            # The request is sent lazily; pull the first chunk here so a
            # rejected option surfaces before anything is yielded.
            first = next(it, None)
            return first, it

        try:
            try:
                first, it = open_stream(opts)
            except _ollama.ResponseError as exc:
                if opts.get("think") is None or "think" not in str(getattr(exc, "error", exc)).lower():
                    raise
                logger.warning("model %r rejected think=%r (%s); retrying without it", model, opts["think"], exc)
                self._think_unsupported.add(model)
                opts.pop("think")
                first, it = open_stream(opts)
            chunks: Iterable = ([] if first is None else [first])
            for chunk in _chain(chunks, it):
                piece = think_filter.feed(chunk["response"] or "")
                if piece:
                    yield piece
            tail = think_filter.flush()
            if tail:
                yield tail
        except Exception as exc:
            raise _translate_error(exc, model) from exc

    def embed(self, text: str) -> list[float]:
        try:
            response = _ollama.embed(model=self.embed_model, input=text)
        except Exception as exc:
            raise _translate_error(exc, self.embed_model) from exc
        return response["embeddings"][0]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            response = _ollama.embed(model=self.embed_model, input=list(texts))
        except Exception as exc:
            raise _translate_error(exc, self.embed_model) from exc
        return [list(vector) for vector in response["embeddings"]]

    def describe_image(self, image_bytes: bytes, *, prompt: str, model: str, **opts) -> str:
        opts = dict(opts)
        options = dict(opts.get("options") or {})
        # Same defaulting rationale as `generate` -- a caller-supplied
        # options dict still wins.
        options.setdefault("num_ctx", settings.num_ctx)
        options.setdefault("num_predict", settings.num_predict)
        opts["options"] = options
        try:
            # ollama's `generate` accepts `images` as a list of raw bytes (or
            # base64 strings) -- verified against the installed `ollama`
            # package's actual signature, no base64 encoding needed here.
            response = _ollama.generate(
                model=model, prompt=prompt, images=[image_bytes], **opts
            )
        except Exception as exc:
            raise _translate_error(exc, model) from exc
        return response["response"]


class FakeInferenceGateway:
    """In-memory `InferenceGateway` test double -- no network, no model server.

    - `generate()` returns a configurable canned string (echoing the prompt by
      default).
    - `embed()` returns a deterministic, hash-derived vector: the same input
      text always produces the same vector, which is enough for tests that
      need embeddings to behave consistently (e.g. similarity/ranking checks)
      without a real embedding model.

    Calls are recorded on `generate_calls` / `embed_calls` / `describe_image_calls`
    so tests can assert on what was asked of the gateway.
    """

    def __init__(
        self,
        canned_response: str | None = None,
        embed_dim: int = 32,
        canned_description: str | None = None,
        stream_pieces: Sequence[str] | None = None,
        stream_error: Exception | None = None,
    ):
        self.canned_response = canned_response
        self.embed_dim = embed_dim
        self.canned_description = canned_description
        # Streaming double: `stream_pieces` (default: the canned response in
        # 4-char pieces) are yielded in order; `stream_error`, if set, is
        # raised after them (mid-stream failure).
        self.stream_pieces = list(stream_pieces) if stream_pieces is not None else None
        self.stream_error = stream_error
        self.generate_calls: list[dict] = []
        self.embed_calls: list[str] = []
        self.embed_batch_calls: list[list[str]] = []
        self.describe_image_calls: list[dict] = []

    def generate(self, *, system: str, prompt: str, **opts) -> str:
        self.generate_calls.append({"system": system, "prompt": prompt, **opts})
        if self.canned_response is not None:
            return self.canned_response
        return f"[fake response to: {prompt}]"

    def generate_stream(self, *, system: str, prompt: str, **opts) -> Iterator[str]:
        self.generate_calls.append({"system": system, "prompt": prompt, "stream": True, **opts})
        pieces = self.stream_pieces
        if pieces is None:
            text = self.canned_response if self.canned_response is not None else f"[fake response to: {prompt}]"
            pieces = [text[i : i + 4] for i in range(0, len(text), 4)]
        yield from pieces
        if self.stream_error is not None:
            raise self.stream_error

    def embed(self, text: str) -> list[float]:
        self.embed_calls.append(text)
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        repeated = (digest * ((self.embed_dim // len(digest)) + 1))[: self.embed_dim]
        return [byte / 255.0 for byte in repeated]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        # Records each text on `embed_calls` (as `embed` does) and each call
        # on `embed_batch_calls`, so tests can assert on either.
        self.embed_batch_calls.append(list(texts))
        return [self.embed(text) for text in texts]

    def describe_image(self, image_bytes: bytes, *, prompt: str, model: str, **opts) -> str:
        self.describe_image_calls.append(
            {"image_bytes": image_bytes, "prompt": prompt, "model": model, **opts}
        )
        if self.canned_description is not None:
            return self.canned_description
        return f"[fake description of a {len(image_bytes)}-byte image]"
