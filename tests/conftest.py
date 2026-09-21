import pytest


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
