"""Tests for docket.infra.parsing.tokens and the manifest `tokenizer` field."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from docket.infra.index import manifest as manifest_module
from docket.infra.index.manifest import IndexManifestGuard, read_manifest
from docket.infra.parsing import tokens
from docket.infra.parsing.tokens import (
    HEURISTIC_NAME,
    HFTokenCounter,
    HeuristicTokenCounter,
    load_token_counter,
    part_locator,
    split_to_fit,
)


class _WordCounter:
    name = "test:words"

    def count(self, text: str) -> int:
        return len(text.split())


class _FakeHFTokenizer:
    def encode(self, text, add_special_tokens=True):
        assert add_special_tokens is False
        return list(text)[::2]


def test_heuristic_is_conservative_and_deterministic() -> None:
    counter = HeuristicTokenCounter()
    assert counter.count("") == 0
    assert counter.count("abcd") == 2  # ceil(4 / 3)
    assert counter.name == HEURISTIC_NAME


def test_load_uses_hf_tokenizer_when_available(monkeypatch) -> None:
    monkeypatch.setattr(
        tokens, "_load_hf", lambda model: HFTokenCounter(_FakeHFTokenizer(), model)
    )
    counter = load_token_counter("some/model")
    assert counter.name == "hf:some/model"
    assert counter.count("abcdef") == 3


def test_load_falls_back_to_heuristic_with_one_warning(monkeypatch, caplog) -> None:
    def boom(model):
        raise OSError("offline")

    monkeypatch.setattr(tokens, "_load_hf", boom)
    # Alembic's fileConfig (run by other tests) can disable existing loggers.
    monkeypatch.setattr(tokens.logger, "disabled", False)
    with caplog.at_level(logging.WARNING, logger=tokens.logger.name):
        counter = load_token_counter("some/model")
        counter.count("x")
        counter.count("y")
    assert counter.name == HEURISTIC_NAME
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


def test_heuristic_setting_skips_hf_without_warning(monkeypatch, caplog) -> None:
    monkeypatch.setattr(tokens, "_load_hf", lambda model: pytest.fail("must not load"))
    with caplog.at_level(logging.WARNING, logger=tokens.logger.name):
        assert load_token_counter("heuristic").name == HEURISTIC_NAME
    assert not caplog.records


def test_split_to_fit_unchanged_when_it_fits() -> None:
    assert split_to_fit("a b c", _WordCounter(), 10, "hdr") == ["a b c"]


def test_split_to_fit_cuts_lines_then_words_and_respects_prefix() -> None:
    counter = _WordCounter()
    text = "one two three\nfour five six\n" + " ".join(f"w{i}" for i in range(20))
    parts = split_to_fit(text, counter, 6, "hdr")
    assert all(counter.count("hdr\n" + p) <= 6 for p in parts)
    # Nothing lost: same words in the same order.
    assert " ".join(parts).split() == text.split()
    # Line boundaries are preferred: the first two lines stay whole.
    assert "one two three" in parts[0]


def test_split_to_fit_halves_a_single_unbreakable_token() -> None:
    parts = split_to_fit("x" * 100, HeuristicTokenCounter(), 10)
    assert "".join(parts) == "x" * 100
    assert all(HeuristicTokenCounter().count(p) <= 10 for p in parts)


def test_part_locator_only_marks_split_units() -> None:
    assert part_locator({"a": 1}, 0, 1) == {"a": 1}
    assert part_locator({"a": 1}, 1, 3) == {"a": 1, "part": 2, "of": 3}


def test_manifest_records_tokenizer_on_create_and_updates_without_error(tmp_path: Path) -> None:
    path = tmp_path / "index_manifest.json"
    name = {"value": "hf:model-a"}
    guard = IndexManifestGuard(path, "m1", tokenizer_name=lambda: name["value"])
    guard.check_write(32)
    first = read_manifest(path)
    assert first is not None and first.tokenizer == "hf:model-a"

    # A different tokenizer is informational: recorded, never a mismatch.
    name["value"] = HEURISTIC_NAME
    guard.check_write(32)
    second = read_manifest(path)
    assert second is not None and second.tokenizer == HEURISTIC_NAME
    assert second.created_at == first.created_at
    guard.check_query(32)


def test_manifest_without_tokenizer_name_stays_null(tmp_path: Path) -> None:
    path = tmp_path / "index_manifest.json"
    IndexManifestGuard(path, "m1").check_write(8)
    assert read_manifest(path).tokenizer is None
    assert manifest_module.SCHEMA_VERSION == 1
