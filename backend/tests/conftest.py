import os
import tempfile
from pathlib import Path

import pytest

# The unit suite must never download the embedding tokenizer. Set before
# `docket.core.config.settings` is first imported.
os.environ.setdefault("DOCKET_EMBED_TOKENIZER", "heuristic")

# SAFETY NET: no test may ever resolve the real user data directory. Opening
# the app against it runs database migrations on the user's live data (a test
# bug once did exactly that). Point every test process at a throwaway dir
# BEFORE `docket.core.config.settings` is imported; individual tests still
# override DOCKET_DATA_DIR with their own tmp_path via monkeypatch.
_REAL_DATA_DIR = Path.home() / ".local" / "share" / "docket"
os.environ["DOCKET_DATA_DIR"] = tempfile.mkdtemp(prefix="docket-tests-data-")


@pytest.fixture(autouse=True)
def _never_touch_real_data_dir():
    """Fail loudly (before any code runs) if a test's data dir is the real one."""
    configured = os.environ.get("DOCKET_DATA_DIR")
    if configured and Path(configured).expanduser().resolve() == _REAL_DATA_DIR.resolve():
        pytest.fail(f"test would use the real data dir {_REAL_DATA_DIR}", pytrace=False)
    yield


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
