import pytest

from docket.cli.interactive.commands import (
    CommandRegistry,
    SlashCommand,
    default_registry,
)


@pytest.fixture
def reg():
    return default_registry()


def test_registration_order_stable(reg):
    assert [c.name for c in reg.all()] == [
        "help", "sources", "add", "ingest", "mode", "remove", "show", "retry", "status", "clear", "exit"
    ]


@pytest.mark.parametrize(
    "token,expected",
    [
        ("help", "help"), ("h", "help"), ("?", "help"), ("ls", "sources"),
        ("quit", "exit"), ("q", "exit"), ("sou", "sources"), ("ing", "ingest"),
        ("m", "mode"), ("c", "clear"), ("a", "add"), ("e", "exit"),
        ("HELP", "help"), ("Ing", "ingest"),
    ],
)
def test_resolve(reg, token, expected):
    assert reg.resolve(token).name == expected


def test_unknown_and_empty_resolve_none(reg):
    assert reg.resolve("frobnicate") is None
    assert reg.resolve("") is None


def test_ambiguous_prefix_returns_none():
    r = CommandRegistry()
    r.register(SlashCommand("sources", "s", lambda s, a: None))
    r.register(SlashCommand("status", "t", lambda s, a: None))
    assert r.resolve("s") is None
    assert {c.name for c in r.prefix_matches("s")} == {"sources", "status"}
    assert r.resolve("so").name == "sources"


def test_duplicate_registration_rejected(reg):
    with pytest.raises(ValueError):
        reg.register(SlashCommand("ls", "x", lambda s, a: None))


def test_suggest(reg):
    assert reg.suggest("hlep") == "help"
    assert reg.suggest("zzzz") is None
    assert reg.suggest("HLEP") == "help"


def test_render_help_drift_guard(reg):
    text = reg.render_help("FOOTER TEXT")
    for cmd in reg.all():
        assert f"/{cmd.name}" in text
        assert cmd.summary in text
        for alias in cmd.aliases:
            assert f"/{alias}" in text
    assert text.endswith("FOOTER TEXT")
