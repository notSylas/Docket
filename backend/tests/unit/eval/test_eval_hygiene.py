"""Hygiene guards: scale-driver import path, one source per document,
gold binding, and judge identity in every report."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

from docket.eval.hygiene import (
    LibraryHygieneError,
    library_stats,
    require_one_source_per_document,
    servable_source_duplicates,
    unbound_records,
    verify_gold_binding,
)
from docket.eval.judge import ClaimJudge, JudgeVerdict, is_self_judged, judge_runs, load_judged
from docket.eval.report import build_report, format_report, report_to_dict
from docket.eval.schema import GoldSet, GoldSetError, Question, fingerprint

BACKEND = Path(__file__).resolve().parents[3]
SCALE = BACKEND.parent / "evals" / "suites" / "scale"


def _load_guard():
    spec = importlib.util.spec_from_file_location("scale_guard_under_test", SCALE / "scale_guard.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# -- (a) import-path guard ---------------------------------------------------

def test_guard_accepts_docket_inside_expected_src(tmp_path):
    g = _load_guard()
    src = tmp_path / "wt" / "backend" / "src"
    (src / "docket").mkdir(parents=True)
    assert g.check_docket_checkout(src, module_file=str(src / "docket" / "__init__.py"), isolated=False) == src / "docket"


def test_guard_rejects_other_checkout_and_mentions_dash_I(tmp_path):
    g = _load_guard()
    expected = tmp_path / "wt" / "backend" / "src"
    other = tmp_path / "main" / "backend" / "src" / "docket"
    other.mkdir(parents=True)
    expected.mkdir(parents=True)
    with pytest.raises(SystemExit, match="expected under"):
        g.check_docket_checkout(expected, module_file=str(other / "__init__.py"), isolated=False)
    with pytest.raises(SystemExit, match="python -I"):
        g.check_docket_checkout(expected, module_file=str(other / "__init__.py"), isolated=True)


def test_guard_default_expectation_is_this_checkouts_src_and_env_override(monkeypatch, tmp_path):
    g = _load_guard()
    monkeypatch.delenv("DOCKET_EXPECTED_SRC", raising=False)
    assert g.expected_src(SCALE / "scale_guard.py") == (BACKEND / "src").resolve()
    monkeypatch.setenv("DOCKET_EXPECTED_SRC", str(tmp_path))
    assert g.expected_src() == tmp_path.resolve()


def test_guard_passes_for_the_docket_this_test_imports():
    """The suite runs with PYTHONPATH (or an editable install) pointing at this checkout."""
    import docket

    g = _load_guard()
    expected = Path(docket.__file__).resolve().parent.parent
    assert g.check_docket_checkout(expected) == Path(docket.__file__).resolve().parent


def test_drivers_use_the_guards():
    for name in ("cuad_scale_curve.py", "cuad_query_clean.py"):
        src = (SCALE / name).read_text()
        assert "check_docket_checkout()" in src, name
        assert "require_one_source_per_document" in src, name
        assert not any(ln.lstrip().startswith(("subprocess", "os.system")) and "-I" in ln for ln in src.splitlines())
        # the guard must run before any docket import at module level
        first_docket_import = src.find("from docket")
        if name == "cuad_scale_curve.py":
            assert src.find("check_docket_checkout()") < first_docket_import


# -- (b) one source per document ---------------------------------------------

def _db(tmp_path, rows):
    path = tmp_path / "docket.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE sources(id TEXT PRIMARY KEY, status TEXT);"
        "CREATE TABLE evidence_versions(id TEXT PRIMARY KEY, source_id TEXT, content_hash TEXT, status TEXT);"
    )
    for i, (source, s_status, h, v_status) in enumerate(rows):
        conn.execute("INSERT OR IGNORE INTO sources VALUES (?,?)", (source, s_status))
        conn.execute("INSERT INTO evidence_versions VALUES (?,?,?,?)", (f"ev{i}", source, h, v_status))
    conn.commit()
    conn.close()
    return path


def test_clean_library_has_one_source_per_contract(tmp_path):
    db = _db(tmp_path, [("s1", "ACTIVE", "h1", "READY"), ("s1", "ACTIVE", "h2", "READY"), ("s2", "ACTIVE", "h3", "READY")])
    stats = require_one_source_per_document(db)
    assert stats["servable_documents"] == 3 and stats["sources_per_document_max"] == 1
    assert stats["documents_in_multiple_sources"] == 0 and stats["sources_total"] == 2


def test_duplicate_contract_across_active_sources_fails(tmp_path):
    db = _db(tmp_path, [("s1", "ACTIVE", "h1", "READY"), ("s2", "ACTIVE", "h1", "READY"), ("s3", "ACTIVE", "h2", "READY")])
    with pytest.raises(LibraryHygieneError, match="1 contract"):
        require_one_source_per_document(db)
    conn = sqlite3.connect(db)
    (dup,) = servable_source_duplicates(conn)
    assert dup.source_ids == ("s1", "s2") and library_stats(conn)["documents_in_multiple_sources"] == 1
    conn.close()


def test_revoked_or_superseded_copies_do_not_count(tmp_path):
    db = _db(tmp_path, [
        ("s1", "ACTIVE", "h1", "READY"),
        ("s2", "REVOKED", "h1", "READY"),     # revoked copy
        ("s3", "ACTIVE", "h1", "SUPERSEDED"),  # superseded copy
        ("s4", "deleted", "h1", "READY"),     # case-insensitive status
    ])
    assert require_one_source_per_document(db)["servable_documents"] == 1


def test_check_is_read_only(tmp_path):
    db = _db(tmp_path, [("s1", "ACTIVE", "h1", "READY")])
    before = db.read_bytes()
    require_one_source_per_document(db)
    assert db.read_bytes() == before


def test_real_migrated_schema_is_compatible(migrated_sqlite_engine, tmp_path):
    path = Path(migrated_sqlite_engine.url.database)
    assert require_one_source_per_document(path)["servable_documents"] == 0


# -- (c) gold binding --------------------------------------------------------

def _gold(extra=""):
    return GoldSet(questions=[Question(id="u", type="out_of_corpus", question="Q?" + extra, answerable=False)])


def test_gold_binding_detects_changed_gold(make_record):
    gold = _gold()
    ok = make_record(question_id="u", gold_fingerprint=fingerprint(gold))
    verify_gold_binding(gold, [ok])
    with pytest.raises(GoldSetError, match="different gold set"):
        verify_gold_binding(_gold(" changed"), [ok])
    unbound = make_record(question_id="u")
    verify_gold_binding(gold, [unbound])
    assert unbound_records([unbound, ok]) == 1


def test_benchmark_binding(make_record):
    gold = _gold()
    r = make_record(question_id="u", benchmark_fingerprint="aaa")
    verify_gold_binding(gold, [r], benchmark_fingerprint="aaa")
    with pytest.raises(GoldSetError, match="frozen benchmark"):
        verify_gold_binding(gold, [r], benchmark_fingerprint="bbb")


def test_report_flags_gold_mismatch_without_raising(make_record):
    gold = _gold()
    rec = make_record(question_id="u", answer="x", gold_fingerprint="stale")
    report = build_report(gold, [rec])
    assert report.gold_binding.startswith("MISMATCH")
    assert "MISMATCH" in format_report(report)


# -- (d) every report states judge identity ----------------------------------

class _Gw:
    def __init__(self):
        self.n = 0

    def generate(self, *, system, prompt, **opts):
        return json.dumps({"supported": True, "reason": "ok"})

    def embed(self, text):
        return [0.0]


def _paraphrase_setup(make_record, gen_model="qwen3:14b"):
    chunk = "Employees receive 25 days of paid vacation per year."
    q = Question(id="p", type="single_fact", question="Q?", answerable=True, must_contain=["twenty-five"],
                 gold_spans=["25 days of paid vacation per year"], split="dev")
    rec = make_record(question_id="p", answer="25 days [f #c0]", chunks=[chunk], cited=[0],
                      prompt=f"Context:\n[f #c0]\n{chunk}\n\nQuestion: q\n\nAnswer:", spans_indexed=[True],
                      configuration={"gen_model": gen_model, "top_k": 8})
    return GoldSet(questions=[q]), [rec]


def test_unjudged_report_states_no_judge_n_and_ci_method(make_record):
    gold, records = _paraphrase_setup(make_record)
    text = format_report(build_report(gold, records))
    assert "Judge: none (deterministic checks only)" in text
    assert "n = 1 questions, 1 runs" in text and "CI method:" in text and "Evaluation configuration:" in text
    assert "answering model: qwen3:14b" in text


def test_judged_report_states_judges_and_warns_when_self_judged(make_record):
    gold, records = _paraphrase_setup(make_record)
    judged = judge_runs(gold, records, ClaimJudge(_Gw(), "qwen3:14b"))
    assert judged[0].self_judged and judged[0].answer_model == "qwen3:14b"
    text = format_report(build_report(gold, records, judged))
    assert "Judge: qwen3:14b" in text and "WARNING: self-judged" in text


def test_independent_judge_has_no_warning_and_is_named(make_record):
    gold, records = _paraphrase_setup(make_record)
    judged = judge_runs(gold, records, ClaimJudge(_Gw(), "gemma3:12b"))
    report = build_report(gold, records, judged)
    text = format_report(report)
    assert "Judge: gemma3:12b" in text and "self-judged" not in text
    assert report.judge_models == ["gemma3:12b"] and report.self_judged is False
    assert report_to_dict(report)["judge_identity"] == "gemma3:12b"


def test_old_judged_files_without_identity_fields_still_load(make_record, tmp_path):
    gold, records = _paraphrase_setup(make_record)
    judged = judge_runs(gold, records, ClaimJudge(_Gw(), "gemma3:12b"), out_path=tmp_path / "j.jsonl")
    line = json.loads((tmp_path / "j.jsonl").read_text())
    line.pop("self_judged"), line.pop("answer_model")
    (tmp_path / "old.jsonl").write_text(json.dumps(line) + "\n")
    (old,) = load_judged(tmp_path / "old.jsonl")
    assert old.self_judged is False and old.answer_model is None and old.verdict is JudgeVerdict.PASS
    assert "Judge: gemma3:12b" in format_report(build_report(gold, records, [old]))


def test_is_self_judged():
    assert is_self_judged("a", ["a", "b"]) and not is_self_judged("a", ["b"]) and not is_self_judged(None, ["a"])
