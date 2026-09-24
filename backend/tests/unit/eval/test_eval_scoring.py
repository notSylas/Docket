"""Every deterministic scoring function, plus the per-question pass rule."""

from __future__ import annotations

from docket.eval.schema import Question
from docket.eval.scoring import (
    Verdict,
    check_citations,
    check_must_contain,
    check_must_not_contain,
    contains_refusal,
    enumeration_completeness,
    extract_context,
    fact_in_context,
    is_abstention,
    looks_truncated,
    matches,
    normalize_text,
    retrieval_hit,
    score_run,
    span_hits,
    strip_citations,
    undecidable_by_regex,
)
from docket.query.prompts import ABSTENTION_PHRASE

SPAN = "25 days of paid vacation per year"
CHUNK = "Employees receive 25 days of paid vacation per year. Unused days lapse."


def _q(**kw) -> Question:
    base = dict(
        id="q", type="single_fact", question="Q?", answerable=True,
        must_contain=["25"], gold_spans=[SPAN],
    )
    base.update(kw)
    return Question(**base)


# -- normalization -----------------------------------------------------------


def test_normalize_case_whitespace_dashes_quotes():
    assert normalize_text("  Hello  \n WORLD\t") == "hello world"
    assert normalize_text("J‑01 – J—06 − x") == "j-01 - j-06 - x"
    assert normalize_text("“It’s”") == "\"it's\""


def test_normalize_thousands_separators():
    assert normalize_text("$1,200,000") == "$1200000"
    assert normalize_text("1,2") == "1,2"  # not a thousands group
    assert normalize_text("a, b, 1,000.50") == "a, b, 1000.50"
    assert normalize_text("1,2345") == "1,2345"


def test_strip_citations_so_tags_cannot_match():
    assert "report" not in normalize_text(strip_citations("Yes. [report.pdf #a1b2c3]"))
    assert strip_citations("see [aside] no hash").count("[aside]") == 1


def test_matches_plain_and_regex():
    text = normalize_text("The budget is $1,200,000 (about 1.2 million).")
    assert matches("1,200,000", text)
    assert matches("1200000", text)
    assert matches("re:1\\.2 million", text)
    assert matches("re:^the budget", text)
    assert not matches("re:^budget", text)
    assert not matches("3 million", text)


# -- must_contain / must_not_contain ------------------------------------------


def test_must_contain_reports_found_and_missing():
    result = check_must_contain("Six journeys: J-01 to J-06 [a #1]", ["six", "j-06", "seven"])
    assert result.found == ["six", "j-06"]
    assert result.missing == ["seven"]
    assert not result.ok
    assert check_must_contain("x", []).ok


def test_must_contain_ignores_citation_tag_text():
    assert not check_must_contain("Answer. [budget.pdf #abc]", ["budget"]).ok


def test_must_not_contain():
    assert check_must_not_contain("It is 30 days", ["30 days", "re:forty"]) == ["30 days"]
    assert check_must_not_contain("It is 25 days", ["30 days"]) == []


# -- abstention ---------------------------------------------------------------


def test_is_abstention_exact_only():
    assert is_abstention(ABSTENTION_PHRASE)
    assert is_abstention("  " + ABSTENTION_PHRASE.upper() + "  ")
    assert is_abstention(ABSTENTION_PHRASE + " [a #1]")
    assert not is_abstention(ABSTENTION_PHRASE + " But the budget might be 5.")
    assert not is_abstention("I don't know.")
    assert not is_abstention("")


def test_contains_refusal_detects_hedged_partial():
    assert contains_refusal("Well. " + ABSTENTION_PHRASE + " Maybe 5.")
    assert not contains_refusal("The budget is 5.")


# -- citations ----------------------------------------------------------------


def test_check_citations():
    ok = check_citations(["c1"], ["c1", "c2"], [])
    assert ok.ok
    assert not check_citations([], ["c1"], []).ok
    bad = check_citations(["c9"], ["c1"], [])
    assert bad.invalid_ids == ["c9"] and not bad.ok
    warned = check_citations(["c1"], ["c1"], ["answer references an unknown citation: [x #y]"])
    assert warned.unknown_tags and not warned.ok
    assert not check_citations(["c1"], ["c1"], ["answer contains no citations"]).unknown_tags


# -- enumeration --------------------------------------------------------------


def test_enumeration_completeness():
    result = enumeration_completeness("Email, and PHONE.", ["email", "phone", "chat"])
    assert result.found == ["email", "phone"]
    assert result.missing == ["chat"]
    assert result.total == 3
    assert abs(result.completeness - 2 / 3) < 1e-9
    assert enumeration_completeness("anything", []).completeness == 1.0


# -- retrieval hit / fact in context ------------------------------------------


def test_span_hits_and_retrieval_hit_survive_formatting():
    chunks = ["Employees receive 25  days\nof paid VACATION per year.", "unrelated"]
    assert span_hits([SPAN, "not present anywhere"], chunks) == [True, False]
    assert retrieval_hit([SPAN], chunks)
    assert not retrieval_hit(["not present anywhere"], chunks)
    assert not retrieval_hit([SPAN], [])
    assert not retrieval_hit([], chunks)


def test_extract_context_excludes_history_and_question():
    prompt = (
        "Conversation so far:\nUser: q\nAssistant: 25 days of paid vacation per year\n\n"
        f"Context:\n[f #1]\n{CHUNK}\n\n[g #2]\nmore\n\nQuestion: How many?\n\nAnswer:"
    )
    context = extract_context(prompt)
    assert context.startswith("[f #1]") and context.endswith("more")
    assert "Conversation" not in context and "Question" not in context


def test_fact_in_context_ignores_history_leak_and_handles_none():
    prompt = (
        "Conversation so far:\nAssistant: 25 days of paid vacation per year\n\n"
        "Context:\n[f #1]\nnothing relevant\n\nQuestion: q\n\nAnswer:"
    )
    assert fact_in_context([SPAN], prompt) == [False]
    assert fact_in_context([SPAN], f"Context:\n{CHUNK}\n\nQuestion: q\n\nAnswer:") == [True]
    assert fact_in_context([SPAN], None) == [False]


def test_looks_truncated():
    prompt = "x" * 4000  # ~1000 tokens
    assert looks_truncated("", prompt, 300)
    assert not looks_truncated("", prompt, 900)
    assert not looks_truncated("", prompt, None)
    assert not looks_truncated("", None, 100)


def test_undecidable_by_regex_hook():
    assert undecidable_by_regex("six")
    assert not undecidable_by_regex("25")
    assert not undecidable_by_regex("re:six|6")
    assert not undecidable_by_regex("j-06")


# -- pass rule ----------------------------------------------------------------


def test_pass_answerable(make_record):
    record = make_record(answer="They get 25 days [f #c0]", chunks=[CHUNK], cited=[0])
    score = score_run(_q(), record)
    assert score.verdict is Verdict.PASS and score.passed and score.citation_ok


def test_fail_missing_numeric_fact(make_record):
    record = make_record(answer="They get 30 days [f #c0]", chunks=[CHUNK], cited=[0])
    score = score_run(_q(), record)
    assert score.verdict is Verdict.FAIL
    assert score.missing_facts == ["25"]


def test_fail_forbidden_content(make_record):
    record = make_record(answer="25 days, or 30 days [f #c0]", chunks=[CHUNK], cited=[0])
    score = score_run(_q(must_not_contain=["30 days"]), record)
    assert score.verdict is Verdict.FAIL and score.forbidden_found == ["30 days"]


def test_fail_no_citation_and_bad_citation(make_record):
    assert score_run(_q(), make_record(answer="25 days", chunks=[CHUNK])).verdict is Verdict.FAIL
    record = make_record(
        answer="25 days", chunks=[CHUNK], cited=[0], validation_warnings=["answer references an unknown citation: [x #y]"]
    )
    assert score_run(_q(), record).verdict is Verdict.FAIL


def test_fail_when_cited_chunk_not_retrieved(make_record):
    record = make_record(answer="25 days", chunks=[CHUNK])
    record.citations = make_record(cited=[5]).citations
    score = score_run(_q(), record)
    assert score.verdict is Verdict.FAIL
    assert any("not in retrieved" in r for r in score.reasons)


def test_fail_abstained_on_answerable(make_record):
    score = score_run(_q(), make_record(answer=ABSTENTION_PHRASE, chunks=[CHUNK]))
    assert score.verdict is Verdict.FAIL and score.abstained
    hedged = score_run(_q(), make_record(answer="25 days. " + ABSTENTION_PHRASE, chunks=[CHUNK], cited=[0]))
    assert hedged.verdict is Verdict.FAIL


def test_needs_judge_for_paraphrasable_fact(make_record):
    question = _q(must_contain=["twenty-five"])
    record = make_record(answer="They get 25 days [f #c0]", chunks=[CHUNK], cited=[0])
    score = score_run(question, record)
    assert score.verdict is Verdict.NEEDS_JUDGE


def test_needs_judge_when_cited_chunk_lacks_gold_span(make_record):
    record = make_record(answer="25 days [f #c1]", chunks=[CHUNK, "other text"], cited=[1])
    assert score_run(_q(), record).verdict is Verdict.NEEDS_JUDGE


def test_decisive_miss_beats_judge_hook(make_record):
    question = _q(must_contain=["twenty-five", "25"])
    record = make_record(answer="thirty [f #c0]", chunks=[CHUNK], cited=[0])
    assert score_run(question, record).verdict is Verdict.FAIL


def test_enumeration_pass_and_partial_fail(make_record):
    question = Question(
        id="e", type="enumeration", question="Q?", answerable=True,
        enumeration=["email", "phone", "re:chat|im"], gold_spans=["three channels: email, phone, and chat"],
    )
    chunk = "Support is available through three channels: email, phone, and chat."
    full = score_run(question, make_record(answer="Email, phone, chat [f #c0]", chunks=[chunk], cited=[0]))
    assert full.verdict is Verdict.PASS and (full.enumeration_found, full.enumeration_total) == (3, 3)
    partial = score_run(question, make_record(answer="Email, phone [f #c0]", chunks=[chunk], cited=[0]))
    assert partial.verdict is Verdict.FAIL and partial.enumeration_found == 2


def test_unanswerable_passes_only_on_exact_abstention(make_record):
    question = Question(id="o", type="out_of_corpus", question="Q?", answerable=False)
    assert score_run(question, make_record(answer=ABSTENTION_PHRASE)).verdict is Verdict.PASS
    assert score_run(question, make_record(answer="It is Bob [f #c0]")).verdict is Verdict.FAIL
    assert score_run(question, make_record(answer=ABSTENTION_PHRASE + " Maybe Bob.")).verdict is Verdict.FAIL


def test_revoked_leak_flag(make_record):
    question = Question(
        id="r", type="revoked", question="Q?", answerable=False,
        setup={"revoke": ["finance"]}, gold_spans=[SPAN],
    )
    leaked = score_run(question, make_record(answer=ABSTENTION_PHRASE, chunks=[CHUNK]))
    assert leaked.verdict is Verdict.PASS and leaked.leaked
    clean = score_run(question, make_record(answer=ABSTENTION_PHRASE, chunks=["unrelated"]))
    assert not clean.leaked


def test_run_error_fails(make_record):
    score = score_run(_q(), make_record(error="InferenceUnavailableError: down"))
    assert score.verdict is Verdict.FAIL and "run error" in score.reasons[0]
