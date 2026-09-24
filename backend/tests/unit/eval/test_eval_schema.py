"""Gold-set schema validation and loader errors."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from docket.eval.schema import GoldSetError, Split, default_split, load_gold_set, load_records


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "gold.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def _one(extra: str = "", *, type_="single_fact", answerable="true", spans=True) -> str:
    span_line = '    gold_spans: ["this is a verbatim quote span"]\n' if spans else ""
    return (
        "questions:\n"
        "  - id: q1\n"
        f"    type: {type_}\n"
        "    question: Q?\n"
        f"    answerable: {answerable}\n"
        + span_line
        + extra
    )


def test_fixture_gold_set_loads(fixtures_dir):
    gold = load_gold_set(fixtures_dir / "gold.yaml")
    assert len(gold.questions) == 8
    assert {q.type.value for q in gold.questions} == {
        "single_fact", "enumeration", "numeric", "multi_doc",
        "follow_up", "out_of_corpus", "revoked",
    }
    assert gold.by_id()["revoked-engineering"].setup.revoke == ["finance"]


def test_bare_list_is_accepted(tmp_path):
    body = _one("    must_contain: [x]\n").replace("questions:\n", "")
    # dedent the list items into a top-level list
    body = "\n".join(line[2:] if line.startswith("  ") else line for line in body.splitlines())
    assert len(load_gold_set(_write(tmp_path, body)).questions) == 1


def test_split_default_is_deterministic_and_roughly_70_30():
    assert default_split("abc") == default_split("abc")
    splits = [default_split(f"q{i}") for i in range(1000)]
    dev = sum(s is Split.DEV for s in splits) / 1000
    assert 0.65 < dev < 0.75


def test_explicit_split_is_kept(tmp_path):
    gold = load_gold_set(_write(tmp_path, _one("    must_contain: [x]\n    split: test\n")))
    assert gold.questions[0].split is Split.TEST
    assert gold.questions[0].reviewed is False


def test_missing_file(tmp_path):
    with pytest.raises(GoldSetError, match="cannot read"):
        load_gold_set(tmp_path / "nope.yaml")


def test_bad_yaml(tmp_path):
    with pytest.raises(GoldSetError, match="not valid YAML"):
        load_gold_set(_write(tmp_path, "questions: [unclosed"))


def test_top_level_must_be_mapping(tmp_path):
    with pytest.raises(GoldSetError, match="must be a mapping"):
        load_gold_set(_write(tmp_path, "just a string"))


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (_one("    must_contain: [x]\n    bogus: 1\n"), "bogus"),
        (_one("    must_contain: [x]\n", type_="nonsense"), "type"),
        (_one(spans=False, extra="    must_contain: [x]\n"), "gold_spans"),
        (_one(extra="", spans=True), "must_contain or enumeration"),
        (_one("    must_contain: ['re:(']\n"), "invalid regex"),
        (_one("    must_contain: ['']\n"), "empty entry"),
        (_one("    must_contain: [x]\n    gold_spans: [short]\n").replace(
            '    gold_spans: ["this is a verbatim quote span"]\n', ""), "20-60"),
        (_one("    must_contain: [x]\n", type_="out_of_corpus"), "answerable: false"),
        (_one(type_="revoked", answerable="false", spans=False), "setup.revoke"),
        (_one("    must_contain: [x]\n", type_="enumeration"), "enumeration list"),
        (_one("    must_contain: [x]\n", type_="follow_up"), "history"),
        (_one("    must_contain: [x]\n", type_="multi_doc"), "two gold_spans"),
        (_one("    must_contain: [x]\n", answerable="false", type_="out_of_corpus"),
         "must not set"),
    ],
)
def test_invalid_questions_rejected_with_context(tmp_path, body, message):
    with pytest.raises(GoldSetError, match=message) as exc:
        load_gold_set(_write(tmp_path, body))
    assert "question #1 (q1)" in str(exc.value) or "bogus" in str(exc.value)


def test_long_gold_span_rejected(tmp_path):
    long_span = "x" * 61
    body = _one("    must_contain: [x]\n").replace("this is a verbatim quote span", long_span)
    with pytest.raises(GoldSetError, match="got 61"):
        load_gold_set(_write(tmp_path, body))


def test_duplicate_ids_rejected(tmp_path):
    q = _one("    must_contain: [x]\n").replace("questions:\n", "")
    body = "questions:\n" + q + q
    with pytest.raises(GoldSetError, match="duplicate question id"):
        load_gold_set(_write(tmp_path, body))


def test_bad_id_rejected(tmp_path):
    body = _one("    must_contain: [x]\n").replace("id: q1", "id: 'has space'")
    with pytest.raises(GoldSetError, match="id"):
        load_gold_set(_write(tmp_path, body))


def test_load_records_reports_bad_line(tmp_path):
    path = tmp_path / "r.jsonl"
    path.write_text('{"question_id": "a", "repeat": 0}\n\nnot json\n', encoding="utf-8")
    with pytest.raises(GoldSetError, match=r"r\.jsonl:3"):
        load_records(path)
