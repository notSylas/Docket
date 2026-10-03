import os

import pytest

# The unit suite must never download the embedding tokenizer. Set before
# `docket.core.config.settings` is first imported.
os.environ.setdefault("DOCKET_EMBED_TOKENIZER", "heuristic")


class WordCounter:
    """Deterministic stand-in for the embedding tokenizer: one token per word."""

    name = "test:words"

    def count(self, text: str) -> int:
        return len(text.split())


@pytest.fixture(scope="session", autouse=True)
def _offline_token_counter():
    # Chunker default counter -> whitespace words, so no test touches the
    # network and the 512-token default cap never alters word-window tests.
    from docket.infra.parsing import tokens

    patch = pytest.MonkeyPatch()
    patch.setattr(tokens, "load_token_counter", lambda model: WordCounter())
    tokens.get_token_counter.cache_clear()
    yield
    patch.undo()
    tokens.get_token_counter.cache_clear()


def _ollama_available() -> bool:
    try:
        import ollama

        ollama.list()
        return True
    except Exception:
        return False


@pytest.fixture(scope="session")
def ollama_available() -> bool:
    return _ollama_available()


def pytest_collection_modifyitems(config, items):
    if _ollama_available():
        return
    skip_integration = pytest.mark.skip(reason="Ollama not reachable")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip_integration)
