"""Token counting and token-bounded splitting shared by every chunker.

The cap (`Settings.chunk_max_tokens`) exists so no chunk exceeds the embedding
model's context. The counter is the embedding model's own Hugging Face
tokenizer (`Settings.embed_tokenizer`) when it can be loaded; otherwise a
conservative character heuristic is used and one warning is logged, so an
offline install still chunks safely (it over-counts, so chunks only get
smaller, never larger than the cap).

The counter's `name` is recorded in the index manifest's `tokenizer` field
for information only. It is not a compatibility key: the tokenizer decides
where chunks are cut, not what vector space they are embedded into, so a
different tokenizer never invalidates an index.

Chunk functions take an optional counter; `get_token_counter()` is the
default. Setting `DOCKET_EMBED_TOKENIZER=heuristic` selects the heuristic
without a warning (the unit-test suite does this so it never downloads).
"""

from __future__ import annotations

import logging
import math
import re
from functools import lru_cache
from typing import Protocol

from docket.core.config import settings

logger = logging.getLogger(__name__)

HEURISTIC_NAME = "heuristic:chars/3"


class TokenCounter(Protocol):
    name: str

    def count(self, text: str) -> int: ...


class HeuristicTokenCounter:
    """~3 characters per token: deliberately above the ~4 chars/token typical
    of English, so it over-counts rather than lets a chunk run past the cap."""

    name = HEURISTIC_NAME

    def count(self, text: str) -> int:
        return math.ceil(len(text) / 3)


class HFTokenCounter:
    def __init__(self, tokenizer, model: str):
        self._tokenizer = tokenizer
        self.name = f"hf:{model}"

    def count(self, text: str) -> int:
        return len(self._tokenizer.encode(text, add_special_tokens=False))


def _load_hf(model: str) -> HFTokenCounter:
    from transformers import AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(model)
    # Chunks are counted, never fed to the model here; silence the
    # "sequence longer than max length" warning for long inputs.
    tokenizer.model_max_length = 10**9
    return HFTokenCounter(tokenizer, model)


def load_token_counter(model: str) -> TokenCounter:
    """The HF tokenizer for `model`, or the heuristic if it cannot be loaded."""
    if model == "heuristic":
        return HeuristicTokenCounter()
    try:
        return _load_hf(model)
    except Exception as exc:  # offline and not cached, missing library, bad name
        logger.warning(
            "could not load tokenizer %r (%s); falling back to a conservative "
            "character-count heuristic for chunk size limits",
            model,
            exc,
        )
        return HeuristicTokenCounter()


@lru_cache(maxsize=1)
def get_token_counter() -> TokenCounter:
    """Process-wide default counter, loaded lazily on first use."""
    return load_token_counter(settings.embed_tokenizer)


# -- token-bounded splitting ------------------------------------------------


def _joined(prefix: str, body: str) -> str:
    return f"{prefix}\n{body}" if prefix else body


def pack_parts(
    parts: list[str], sep: str, counter: TokenCounter, cap: int, prefix: str = ""
) -> list[list[str]]:
    """Greedily group `parts` so each group, joined by `sep` and preceded by
    `prefix` and a newline, is within `cap`. A single part that does not fit
    on its own still gets its own group; the caller refines it."""
    groups: list[list[str]] = []
    current: list[str] = []
    for part in parts:
        candidate = current + [part]
        if current and counter.count(_joined(prefix, sep.join(candidate))) > cap:
            groups.append(current)
            current = [part]
        else:
            current = candidate
    if current:
        groups.append(current)
    return groups


_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")
# Coarse to fine: lines/paragraphs, sentences, words. Characters are the
# last resort (`_halve_chars`) for text with no whitespace to cut on.
_LEVELS = (
    ("\n", lambda t: t.split("\n")),
    (" ", lambda t: _SENTENCE_RE.split(t)),
    (" ", lambda t: t.split()),
)


def _halve_chars(text: str, counter: TokenCounter, cap: int, prefix: str) -> list[str]:
    if len(text) < 2 or counter.count(_joined(prefix, text)) <= cap:
        return [text]
    mid = len(text) // 2
    return _halve_chars(text[:mid], counter, cap, prefix) + _halve_chars(
        text[mid:], counter, cap, prefix
    )


def _split_level(text: str, level: int, counter: TokenCounter, cap: int, prefix: str) -> list[str]:
    if level >= len(_LEVELS):
        return _halve_chars(text, counter, cap, prefix)
    sep, splitter = _LEVELS[level]
    parts = [p for p in splitter(text) if p.strip()]
    pieces: list[str] = []
    for group in pack_parts(parts, sep, counter, cap, prefix):
        body = sep.join(group)
        if len(group) == 1 and counter.count(_joined(prefix, body)) > cap:
            pieces.extend(_split_level(body, level + 1, counter, cap, prefix))
        else:
            pieces.append(body)
    return pieces


def split_to_fit(text: str, counter: TokenCounter, cap: int, prefix: str = "") -> list[str]:
    """Split `text` into bodies so that `prefix + "\\n" + body` is within `cap`
    tokens, cutting on line, then sentence, then word, then character
    boundaries. Returns `[text]` unchanged when it already fits (or when
    `prefix` alone fills the cap, where splitting could not help)."""
    if counter.count(_joined(prefix, text)) <= cap:
        return [text]
    if prefix and counter.count(prefix) >= cap:
        return [text]
    return _split_level(text, 0, counter, cap, prefix)


def part_locator(locator: dict, index: int, total: int) -> dict:
    """`locator` plus a `part` indicator, only when a unit was split."""
    if total == 1:
        return locator
    return {**locator, "part": index + 1, "of": total}
