"""Answer your questions with KAG scoping ON and OFF and print ONLY aggregate numbers.

    python run_private_eval.py SCRATCH_DIR QUESTIONS.yaml [--summary-json out.json]

Answers and judging both use qwen3:14b. No document text, answer, question or file name is printed or
written; the optional JSON holds the same aggregate numbers you see on screen.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from kit_common import (  # noqa: E402
    EMBED_MODEL, GEN_MODEL, JUDGE_MODEL, KitError, KitQuestion, Outcome, file_matches,
    guard_scratch, judge_answer, load_questions, summarize,
)

KIT_VERSION = 1


def outcome_for(q: KitQuestion, record, judge_gateway) -> Outcome:
    from docket.eval.scoring import record_abstained

    errored = bool(record.error)
    abstained = (not errored) and record_abstained(record)
    names = [c.source_display_name for c in record.retrieved] + [c.source_display_name for c in record.citations if hasattr(c, "source_display_name")]
    recalled = file_matches(q.expected_file, names) if (q.answerable and q.expected_file) else None
    if errored:
        correct = False
    elif not q.answerable:
        correct = abstained
    elif abstained:
        correct = False
    else:
        correct = judge_answer(judge_gateway, q.question, q.expected_answer, record.answer)
    return Outcome(q.answerable, abstained, correct, recalled, float(record.latency_s or 0.0), errored)


def run(scratch: Path, questions: list[KitQuestion], *, gateway=None, judge_gateway=None, parser=None,
        progress=print) -> dict:
    scratch = guard_scratch(scratch)
    if not (scratch / "docket.sqlite3").exists():
        raise KitError(f"{scratch} has no ingested library; run ingest_folder.py first")
    from docket.eval.runner import EvalRunner, OllamaMetaGateway
    from docket.eval.schema import Question, Setup
    from docket.infra.inference.gateway import OllamaGateway
    from docket.services.documents import kag as kag_graph

    gateway = gateway or OllamaMetaGateway(gen_model=GEN_MODEL, embed_model=EMBED_MODEL)
    judge_gateway = judge_gateway or OllamaGateway(gen_model=JUDGE_MODEL, embed_model=EMBED_MODEL)
    dummy = scratch / "_noingest" / "x"  # EvalRunner wants a corpus folder; nothing is ingested from it
    dummy.mkdir(parents=True, exist_ok=True)
    result: dict = {"kit_version": KIT_VERSION, "gen_model": GEN_MODEL, "judge_model": JUDGE_MODEL, "n_questions": len(questions)}
    with EvalRunner(dummy.parent, gateway=gateway, parser=parser, data_dir=scratch) as runner:
        runner._sources = {}
        runner.ingest()  # query-only: just wires the QueryService to the existing library
        engine = runner._context.engine
        entity = [bool(kag_graph.match_entities(engine, q.question)) for q in questions]
        for label, flag in (("kag_on", "on"), ("kag_off", "off")):
            runner._context.settings.kag_scoping = flag
            outs = []
            for i, q in enumerate(questions, 1):
                # model_construct: skips gold-set validation; run_once only reads question/history/setup
                gq = Question.model_construct(id=f"q{i}", question=q.question, answerable=q.answerable,
                                              gold_spans=[], history=[], setup=Setup())
                outs.append(outcome_for(q, runner.run_once(gq, 0), judge_gateway))
                progress(f"[{label}] {i}/{len(questions)}")
            result[label] = summarize(outs, entity)
    return result


def format_summary(res: dict) -> str:
    rows = [("questions", "questions"), ("answerable", "answerable"), ("accuracy_pct", "accuracy %"),
            ("wrongful_abstention_pct", "wrongful abstention %"),
            ("expected_file_recall_pct", "expected-file recall %"),
            ("entity_match_pct", "entity-match %"), ("median_latency_s", "median latency s"), ("errors", "errors")]
    lines = [f"{'metric':<26}{'KAG on':>10}{'KAG off':>10}"]
    for key, label in rows:
        lines.append(f"{label:<26}{str(res['kag_on'].get(key)):>10}{str(res['kag_off'].get(key)):>10}")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("scratch", type=Path)
    ap.add_argument("questions", type=Path)
    ap.add_argument("--summary-json", type=Path, help="write the aggregate numbers to this small JSON file")
    a = ap.parse_args(argv)
    try:
        qs = load_questions(a.questions)
        res = run(a.scratch, qs)
    except KitError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print()
    print(format_summary(res))
    if a.summary_json:
        a.summary_json.write_text(json.dumps(res, indent=2) + "\n", encoding="utf-8")
        print(f"\nsummary written to {a.summary_json} (aggregate numbers only - safe to send)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
