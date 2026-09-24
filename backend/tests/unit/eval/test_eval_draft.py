"""Gold drafting (verbatim-quote guard, dedupe) and the interactive review loop."""

from __future__ import annotations

import json
import random
from dataclasses import dataclass

import pytest
from test_eval_judge import QueueGateway

from docket.eval.draft import (
    build_draft,
    chunk_weight,
    draft_questions,
    is_near_duplicate,
    load_gold_set,
    merge_into_file,
    parse_draft_output,
    review_gold,
    save_gold_file,
    weighted_order,
)
from docket.eval.schema import GoldSetError

TABLE = (
    "| Journey | Name | Owner |\n| --- | --- | --- |\n"
    "| J-01 | Upload a contract | Alice |\n| J-02 | Search the archive | Bob |\n"
)
PROSE = "Employees receive 25 days of paid vacation per year. Unused days expire on March 31 of the next year."


@dataclass
class Chunk:
    chunk_id: str
    source_name: str
    heading: str | None
    text: str


def _draft_json(**over):
    base = {
        "question": "How many days of paid vacation do employees receive?",
        "quote": "25 days of paid vacation per year",
        "facts": ["25"],
        "type": "single_fact",
    }
    base.update(over)
    return json.dumps(base)


def _chunk(text=PROSE, cid="c0"):
    return Chunk(cid, "handbook", "Vacation", text)


def test_valid_draft_becomes_unreviewed_question():
    q, reason = build_draft(_chunk(), json.loads(_draft_json()))
    assert reason == "ok" and q is not None
    assert q.reviewed is False and q.answerable and q.gold_spans == ["25 days of paid vacation per year"]
    assert q.must_contain == ["25"] and q.origin == "handbook > Vacation" and q.id.startswith("draft-")


@pytest.mark.parametrize(
    "over,reason",
    [
        ({"quote": "30 days of paid vacation each year"}, "quote_not_verbatim"),
        ({"quote": "vacation"}, "quote_too_short"),
        ({"question": "What does the passage say about vacation?"}, "context_dependent"),
        ({"facts": ["99"]}, "facts_not_in_passage"),
        ({"facts": []}, "no_facts"),
        ({"skip": True}, "skipped"),
    ],
)
def test_bad_drafts_rejected(over, reason):
    q, got = build_draft(_chunk(), json.loads(_draft_json(**over)))
    assert q is None and got == reason


def test_quote_verbatim_check_is_normalized():
    raw = json.loads(_draft_json(quote="25  DAYS of paid\nvacation per year"))
    q, reason = build_draft(_chunk(), raw)
    assert reason == "ok"


def test_long_quote_trimmed_to_word_boundary_and_enumeration_needs_two_items():
    raw = json.loads(_draft_json(quote=PROSE, facts=["25", "March 31"], type="enumeration"))
    q, reason = build_draft(_chunk(), raw)
    assert reason == "ok" and 20 <= len(q.gold_spans[0]) <= 60 and PROSE.startswith(q.gold_spans[0])
    assert q.enumeration == ["25", "March 31"] and q.must_contain == []
    single, _ = build_draft(_chunk(), json.loads(_draft_json(facts=["25"], type="enumeration")))
    assert single.type.value == "single_fact"


def test_parse_draft_output_tolerates_fences_and_junk():
    assert parse_draft_output("```json\n" + _draft_json() + "\n```")["facts"] == ["25"]
    assert parse_draft_output("no json here") is None


def test_dedupe():
    assert is_near_duplicate("How many vacation days do employees get?", ["How many vacation days do employees get"])
    assert is_near_duplicate("What is the marketing budget for 2025?", ["What is the 2025 marketing budget?"])
    assert not is_near_duplicate("Who owns J-02?", ["How many vacation days do employees get?"])


def test_chunk_weight_prefers_tables_lists_numbers():
    plain = "Some general prose about the company culture and values. " * 3
    lst = "- one thing\n- another thing\n- third thing\n" + plain
    assert chunk_weight(TABLE) > chunk_weight(lst) > chunk_weight(plain)
    wins = 0
    chunks = [_chunk(plain + str(i), f"p{i}") for i in range(20)] + [_chunk(TABLE * 2, "t")]
    for seed in range(40):
        wins += weighted_order(chunks, random.Random(seed))[0].chunk_id == "t"
    assert wins > 5  # ~9 expected; uniform would give ~2
    assert weighted_order([_chunk("short")], random.Random(0)) == []  # too small to draft from


def test_draft_questions_rejects_hallucinated_dedupes_and_counts():
    chunks = [_chunk(PROSE, "a"), _chunk(TABLE, "b"), _chunk(PROSE + " Extra sentence here.", "c")]
    replies = [
        _draft_json(),
        _draft_json(quote="a made-up quote not in the text"),  # hallucinated
        _draft_json(question="How many days of paid vacation do employees receive"),  # duplicate
    ]
    gateway = QueueGateway(replies)
    result = draft_questions(gateway, chunks, count=5, existing_questions=[])
    assert len(result.questions) == 1 and result.attempts == 3
    assert result.rejected["quote_not_verbatim"] + result.rejected["duplicate"] == 2
    assert gateway.calls[0]["think"] is False and gateway.calls[0]["options"] == {"temperature": 0}


def test_draft_questions_stops_at_count_and_respects_existing():
    gateway = QueueGateway([_draft_json()])
    result = draft_questions(gateway, [_chunk()], count=3, existing_questions=["How many days of paid vacation do employees receive?"])
    assert result.questions == [] and result.rejected["duplicate"] == 1


def test_unparseable_model_output_is_counted():
    result = draft_questions(QueueGateway(["blah"]), [_chunk()], count=1)
    assert result.rejected["unparseable"] == 1


# -- files & review ------------------------------------------------------------


class ScriptedReader:
    def __init__(self, answers):
        self.answers = list(answers)
        self.prompts: list[str] = []

    def read(self, prompt):
        self.prompts.append(prompt)
        if not self.answers:
            raise EOFError
        return self.answers.pop(0)


def _draft_file(tmp_path, n=4):
    questions = []
    for i in range(n):
        q, _ = build_draft(
            _chunk(PROSE, f"c{i}"),
            json.loads(_draft_json(question=f"Question number {i} about vacation rules?")),
        )
        questions.append(q)
    path = tmp_path / "draft.yaml"
    save_gold_file(path, questions)
    return path


def test_save_and_reload_roundtrip(tmp_path):
    path = _draft_file(tmp_path, 2)
    loaded = load_gold_set(path).questions
    assert [q.reviewed for q in loaded] == [False, False]
    assert "reviewed: false" in path.read_text()
    merged = merge_into_file(path, [])
    assert len(merged) == 2


def test_review_accept_reject_skip_and_resume(tmp_path):
    path = _draft_file(tmp_path, 4)
    out: list[str] = []
    reader = ScriptedReader(["a", "r", "s", "q"])
    summary = review_gold(path, reader, out.append)
    assert (summary.accepted, summary.rejected, summary.skipped, summary.quit_early) == (1, 1, 1, True)
    saved = load_gold_set(path).questions
    assert len(saved) == 3 and [q.reviewed for q in saved] == [True, False, False]
    assert any("[1/4]" in line for line in out) and any("[3/3]" in line for line in out)

    # Resume: reviewed entries are not shown again.
    out.clear()
    summary = review_gold(path, ScriptedReader(["a", "a"]), out.append)
    assert summary.accepted == 2 and summary.remaining == 0 and not summary.quit_early
    assert all(q.reviewed for q in load_gold_set(path).questions)
    assert sum("---" in line for line in out) == 2
    assert review_gold(path, ScriptedReader([]), out.append).remaining == 0


def test_review_saves_after_every_decision_and_eof_quits(tmp_path):
    path = _draft_file(tmp_path, 3)
    review_gold(path, ScriptedReader(["a", "a"]), lambda _: None)  # EOF after two decisions
    assert [q.reviewed for q in load_gold_set(path).questions] == [True, True, False]


def test_review_edit(tmp_path):
    path = _draft_file(tmp_path, 1)
    out: list[str] = []
    reader = ScriptedReader(["e", "Edited question?", "25 | 26", "25 days of paid vacation per year", "enumeration"])
    summary = review_gold(path, reader, out.append)
    (q,) = load_gold_set(path).questions
    assert summary.edited == 1 and q.reviewed and q.question == "Edited question?"
    assert q.enumeration == ["25", "26"] and q.type.value == "enumeration"


def test_review_edit_invalid_then_retry(tmp_path):
    path = _draft_file(tmp_path, 1)
    out: list[str] = []
    reader = ScriptedReader(
        ["e", "", "", "too short", "", "y", "", "", "25 days of paid vacation per year", "", "q"]
    )
    summary = review_gold(path, reader, out.append)
    assert any(line.startswith("invalid:") for line in out)
    assert summary.edited == 1 and load_gold_set(path).questions[0].reviewed


def test_review_invalid_choice_reprompts_and_bad_file(tmp_path):
    path = _draft_file(tmp_path, 1)
    out: list[str] = []
    review_gold(path, ScriptedReader(["x", "a"]), out.append)
    assert "please answer a, e, r, s or q" in out
    bad = tmp_path / "bad.yaml"
    bad.write_text("questions: [{id: a}]")
    with pytest.raises(GoldSetError):
        review_gold(bad, ScriptedReader([]), out.append)
