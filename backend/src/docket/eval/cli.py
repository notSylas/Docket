"""`docket eval ...` Typer sub-app: run, judge, report, compare, draft, review, calibrate.

Outputs default to `<data_dir>/eval/` (outside git). Heavy imports (ollama,
docling) are deferred into the commands so `docket --help` stays fast.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import typer

from docket.core.config import Settings
from docket.eval.outputs import ensure_private_dir
from docket.eval.schema import GoldSetError

eval_app = typer.Typer(help="Measure answer accuracy: run a gold set, judge, report, compare, draft gold.")
calibrate_app = typer.Typer(help="Calibrate the automatic judge against your own labels.")
eval_app.add_typer(calibrate_app, name="calibrate")
kag_app = typer.Typer(
    help="Lane A (CPU-only, no model) KAG evaluation: silver gold, scratch library, metrics, review sheets."
)
eval_app.add_typer(kag_app, name="kag")


def _eval_dir() -> Path:
    return ensure_private_dir(Settings().data_dir / "eval")


def _stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def _fail(message: str) -> typer.Exit:
    typer.echo(f"Error: {message}", err=True)
    return typer.Exit(code=1)


# Seams the tests replace so no Ollama or Docling is needed.
def _make_gateway(model: str) -> Any:
    from docket.eval.runner import OllamaMetaGateway

    return OllamaMetaGateway(gen_model=model)


def _make_parser() -> Any | None:
    return None  # None = the default DoclingParser


@eval_app.command("run")
def run_cmd(
    corpus: Path = typer.Option(..., "--corpus", help="Folder to ingest (sub-folders = sources)."),
    gold: Path = typer.Option(..., "--gold", help="Gold-set YAML."),
    repeats: int = typer.Option(3, "--repeats", min=1, help="Runs per question."),
    split: str = typer.Option("dev", "--split", help="dev | test | all (test is for milestone acceptance only)."),
    out: Path | None = typer.Option(None, "--out", help="Output JSONL (default <data_dir>/eval/runs-<time>.jsonl)."),
    model: str = typer.Option(None, "--model", help="Generation model (default: settings gen_model)."),
    ids: list[str] = typer.Option(None, "--id", help="Only these question ids (repeatable)."),
    mode: str = typer.Option("auto", "--mode", help="auto | fast | agent; use forced modes for paired routing comparisons."),
    manifest: Path | None = typer.Option(None, "--manifest", help="Frozen benchmark manifest; verify inputs before running."),
    lane: str = typer.Option("cpu", "--lane", help="cpu | gpu: recorded in the run manifest."),
) -> None:
    """Ingest the corpus into a temp dir and record answers for the gold questions."""
    from docket.eval.report import build_report, format_report
    from docket.eval.runner import EvalSetupError, run_eval
    from docket.eval.schema import Split, load_gold_set
    from docket.services.query.classifier import QueryMode

    if mode not in ("auto", "fast", "agent"):
        raise _fail("--mode must be auto, fast or agent")
    if split not in ("dev", "test", "all"):
        raise _fail("--split must be dev, test or all")
    if lane not in ("cpu", "gpu"):
        raise _fail("--lane must be cpu or gpu")
    try:
        gold_set = load_gold_set(gold)
        out_path = out or _eval_dir() / f"runs-{_stamp()}.jsonl"
        ensure_private_dir(out_path.parent)

        def progress(done: int, total: int, record: Any) -> None:
            flag = f" ERROR {record.error}" if record.error else ""
            typer.echo(f"[{done}/{total}] {record.question_id}#{record.repeat} {record.latency_s:.1f}s{flag}")

        records = run_eval(
            gold_set,
            corpus,
            out_path,
            gateway=_make_gateway(model or Settings().gen_model),
            parser=_make_parser(),
            repeats=repeats,
            split=None if split == "all" else Split(split),
            ids=ids or None,
            mode=None if mode == "auto" else QueryMode(mode),
            manifest_path=manifest,
            progress=progress,
            lane=lane,
            gold_path=gold,
        )
    except (GoldSetError, EvalSetupError) as exc:
        raise _fail(str(exc)) from exc
    typer.echo(f"\nWrote {len(records)} runs to {out_path}\n")
    typer.echo(format_report(build_report(gold_set, records)))


@eval_app.command("judge")
def judge_cmd(
    gold: Path = typer.Option(..., "--gold"),
    runs: Path = typer.Option(..., "--runs", help="JSONL from `docket eval run`."),
    out: Path | None = typer.Option(None, "--out", help="Judged JSONL (default: <runs>.judged.jsonl)."),
    model: str = typer.Option(
        None, "--model",
        help="Judge model (default qwen3:30b). Use gemma3:12b as the independent judge for qwen3 answers.",
    ),
    allow_self_judge: bool = typer.Option(
        False, "--allow-self-judge",
        help="Permit a judge that is also the answering model (comparison only; refused by default).",
    ),
    cross_check: bool = typer.Option(False, "--cross-check", help="Also judge with a second model and record disagreements."),
    cross_model: str = typer.Option(None, "--cross-model", help="Cross-check model (default gemma3:12b)."),
) -> None:
    """Judge paraphrased facts and every candidate answer's citation support."""
    from docket.eval.judge import (
        DEFAULT_CROSS_CHECK_MODEL,
        DEFAULT_JUDGE_MODEL,
        ClaimJudge,
        judge_runs,
    )
    from docket.eval.schema import load_gold_set, load_records

    try:
        gold_set = load_gold_set(gold)
        records = load_records(runs)
    except GoldSetError as exc:
        raise _fail(str(exc)) from exc
    primary_name = model or DEFAULT_JUDGE_MODEL
    primary = ClaimJudge(_make_gateway(primary_name), primary_name)
    cross = None
    if cross_check or cross_model:
        cross_name = cross_model or DEFAULT_CROSS_CHECK_MODEL
        cross = ClaimJudge(_make_gateway(cross_name), cross_name)
    out_path = out or runs.with_name(runs.stem + ".judged.jsonl")
    from docket.eval.hygiene import verify_gold_binding
    from docket.eval.judge import SelfJudgeError, require_independent_judge

    try:
        verify_gold_binding(gold_set, records)
    except GoldSetError as exc:
        raise _fail(str(exc)) from exc
    answer_models = {str(r.configuration["gen_model"]) for r in records if r.configuration.get("gen_model")}
    judge_names = [primary_name] + ([cross_name] if cross is not None else [])
    typer.echo(f"Judge identity: {', '.join(judge_names)} (answering model: {', '.join(sorted(answer_models)) or 'not recorded'})")
    try:
        clash = require_independent_judge(answer_models, judge_names, allow_self_judge=allow_self_judge)
    except SelfJudgeError as exc:
        raise _fail(str(exc)) from exc
    if clash:
        typer.echo("WARNING: self-judged (--allow-self-judge) -- a judge model is also the answering model.", err=True)

    def progress(name: str, done: int, total: int) -> None:
        typer.echo(f"[{name}] claim {done}/{total}")

    judged = judge_runs(gold_set, records, primary, cross, out_path=out_path, progress=progress)
    by_source = {"judge": 0, "deterministic": 0}
    for run in judged:
        by_source[run.source] += 1
    doubts = sum(r.verdict.value == "judge_doubt" for r in judged)
    disagreements = sum(r.disagreement for r in judged)
    typer.echo(
        f"Wrote {len(judged)} verdicts to {out_path} "
        f"({by_source['judge']} judged, {doubts} doubt, {disagreements} cross-check disagreements: model-to-model agreement, not human validation)"
    )


@eval_app.command("report")
def report_cmd(
    gold: Path = typer.Option(..., "--gold"),
    runs: Path = typer.Option(..., "--runs"),
    judged: Path | None = typer.Option(None, "--judged", help="Judged JSONL; replaces the strict/optimistic bracket."),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
    manifest: Path | None = typer.Option(None, "--manifest", help="Frozen input manifest for milestone acceptance."),
    labels: Path | None = typer.Option(None, "--labels", help="Human calibration labels."),
    calibration_judged: Path | None = typer.Option(None, "--calibration-judged", help="Judged dev runs used for calibration (defaults to --judged)."),
    require_milestone: bool = typer.Option(False, "--require-milestone", help="Exit 1 unless the accuracy milestone passes."),
    redact: bool = typer.Option(False, "--redact", help="With --json: strip question/answer/passage text from the output."),
) -> None:
    """Print accuracy (with Wilson intervals), retrieval and failure breakdowns."""
    from docket.eval.judge import load_judged
    from docket.eval.report import build_report, format_report, report_to_dict
    from docket.eval.schema import fingerprint, load_gold_set, load_records

    from docket.eval.benchmark import load_benchmark
    from docket.eval.calibration import score_labels
    from docket.eval.hygiene import verify_gold_binding

    try:
        judgments = load_judged(judged) if judged else None
        calibration_judgments = load_judged(calibration_judged) if calibration_judged else judgments
        if labels is not None and calibration_judgments is None:
            raise GoldSetError("--labels requires --judged")
        gold_set, run_records = load_gold_set(gold), load_records(runs)
        bench = load_benchmark(manifest) if manifest else None
        verify_gold_binding(
            gold_set, run_records, benchmark_fingerprint=fingerprint(bench) if bench else None,
        )
        report = build_report(
            gold_set, run_records, judgments,
            benchmark=bench,
            calibration=score_labels(labels, calibration_judgments) if labels else None,
        )
    except GoldSetError as exc:
        raise _fail(str(exc)) from exc
    from docket.eval.outputs import redact_dict

    payload = report_to_dict(report)
    typer.echo(json.dumps(redact_dict(payload) if redact else payload, indent=2) if as_json else format_report(report))
    if require_milestone and not report.milestone["passed"]:
        raise typer.Exit(code=1)


@eval_app.command("export")
def export_cmd(
    runs: Path = typer.Option(..., "--runs", help="JSONL from `docket eval run`."),
    out: Path = typer.Option(..., "--out", help="New file (created 0600; refuses to overwrite)."),
    judged: Path | None = typer.Option(None, "--judged", help="Judged JSONL to export instead of the runs."),
    redact: bool = typer.Option(False, "--redact", help="Strip prompts, answers, passage text and judge reasons."),
) -> None:
    """Copy runs (or judged verdicts) to a new private file, optionally redacted for sharing."""
    from docket.eval.judge import load_judged
    from docket.eval.outputs import open_private, redact_dict, redact_record
    from docket.eval.schema import load_records

    try:
        with open_private(out, overwrite=False) as fh:
            count = 0
            if judged is not None:
                for run in load_judged(judged):
                    data = run.model_dump(mode="json")
                    fh.write(json.dumps(redact_dict(data) if redact else data) + "\n")
                    count += 1
            else:
                for record in load_records(runs):
                    fh.write((redact_record(record) if redact else record).model_dump_json() + "\n")
                    count += 1
    except (GoldSetError, OSError) as exc:
        raise _fail(str(exc)) from exc
    typer.echo(f"Wrote {count} {'redacted ' if redact else ''}lines to {out}")


@eval_app.command("compare")
def compare_cmd(
    a: Path = typer.Argument(..., help="Baseline judged (or raw run) JSONL."),
    b: Path = typer.Argument(..., help="New judged (or raw run) JSONL."),
    gold: Path | None = typer.Option(None, "--gold", help="Needed only for raw run files."),
    alpha: float = typer.Option(0.05, "--alpha", help="Significance level for a regression."),
    fail_on: str = typer.Option("significant", "--fail-on", help="significant | any: when to exit non-zero."),
) -> None:
    """Paired flips and McNemar p between two runs; exit 1 if B regressed vs A."""
    from docket.eval.compare import compare_outcomes, describe_judging, format_comparison, load_repeat_outcomes
    from docket.eval.schema import load_gold_set

    if fail_on not in ("significant", "any"):
        raise _fail("--fail-on must be significant or any")
    try:
        gold_set = load_gold_set(gold) if gold else None
        comparison = compare_outcomes(load_repeat_outcomes(a, gold_set), load_repeat_outcomes(b, gold_set))
        comparison.judging_a, comparison.judging_b = describe_judging(a), describe_judging(b)
    except (GoldSetError, OSError) as exc:
        raise _fail(str(exc)) from exc
    typer.echo(format_comparison(comparison, a.name, b.name))
    if comparison.regressed(alpha, fail_on):
        typer.echo("REGRESSION: B lost more questions than it gained.", err=True)
        raise typer.Exit(code=1)


@eval_app.command("draft")
def draft_cmd(
    corpus: Path = typer.Option(..., "--corpus", help="Any folder of documents."),
    out: Path | None = typer.Option(None, "--out", help="Draft YAML (appended to if it exists)."),
    count: int = typer.Option(30, "--count", min=1, help="How many questions to draft."),
    model: str = typer.Option(None, "--model", help="Drafting model (default: settings gen_model; run without thinking)."),
    seed: int = typer.Option(0, "--seed"),
) -> None:
    """Draft gold questions from corpus chunks; unverifiable quotes are rejected."""
    from docket.eval.draft import draft_questions, merge_into_file
    from docket.eval.runner import EvalRunner, EvalSetupError
    from docket.eval.schema import load_gold_set

    out_path = out or _eval_dir() / f"draft-{_stamp()}.yaml"
    try:
        existing = [q.question for q in load_gold_set(out_path).questions] if out_path.exists() else []
        gateway = _make_gateway(model or Settings().gen_model)
        typer.echo("Ingesting the corpus into a temp workspace...")
        with EvalRunner(corpus, gateway=gateway, parser=_make_parser()) as runner:
            runner.ingest()
            chunks = runner.corpus_chunks()
        typer.echo(f"{len(chunks)} chunks; drafting up to {count} questions...")

        def progress(accepted: int, attempts: int, reason: str) -> None:
            typer.echo(f"  attempt {attempts}: {reason} ({accepted}/{count} accepted)")

        result = draft_questions(
            gateway, chunks, count=count, seed=seed, existing_questions=existing, progress=progress
        )
        ensure_private_dir(out_path.parent)
        merge_into_file(out_path, result.questions)
    except (GoldSetError, EvalSetupError) as exc:
        raise _fail(str(exc)) from exc
    rejected = ", ".join(f"{k}={v}" for k, v in result.rejected.most_common()) or "none"
    typer.echo(f"\n{len(result.questions)} drafts written to {out_path} (rejected: {rejected})")
    typer.echo(f"Next: docket eval review {out_path}")


@eval_app.command("review")
def review_cmd(
    file: Path = typer.Argument(..., help="Draft/gold YAML to review (progress is saved after every decision)."),
) -> None:
    """Accept / edit / reject drafted questions one at a time; resumable."""
    from docket.interfaces.cli.interactive.reader import CallableReader
    from docket.eval.draft import format_summary, review_gold

    try:
        summary = review_gold(file, CallableReader(input), typer.echo)
    except GoldSetError as exc:
        raise _fail(str(exc)) from exc
    typer.echo(format_summary(summary))


@calibrate_app.command("export")
def calibrate_export_cmd(
    gold: Path = typer.Option(..., "--gold"),
    runs: Path = typer.Option(..., "--runs"),
    judged: Path = typer.Option(..., "--judged"),
    out: Path | None = typer.Option(None, "--out", help="Labels YAML to fill in."),
    n: int = typer.Option(30, "--n", min=1),
    seed: int = typer.Option(0, "--seed"),
) -> None:
    """Export ~30 sampled answers to a labels YAML for you to mark correct/incorrect."""
    from docket.eval.calibration import export_labels
    from docket.eval.judge import load_judged
    from docket.eval.schema import load_gold_set, load_records

    out_path = out or _eval_dir() / f"labels-{_stamp()}.yaml"
    try:
        count = export_labels(
            load_gold_set(gold), load_records(runs), load_judged(judged), out_path, n=n, seed=seed
        )
    except GoldSetError as exc:
        raise _fail(str(exc)) from exc
    typer.echo(f"Exported {count} items to {out_path}. Fill in `correct: true/false`, then run:")
    typer.echo(f"  docket eval calibrate score --labels {out_path} --judged {judged}")


@calibrate_app.command("score")
def calibrate_score_cmd(
    labels: Path = typer.Option(..., "--labels", help="The filled-in labels YAML."),
    judged: Path = typer.Option(..., "--judged"),
) -> None:
    """Cohen's kappa between the automatic verdicts and your labels."""
    from docket.eval.calibration import format_calibration, score_labels
    from docket.eval.judge import load_judged

    try:
        result = score_labels(labels, load_judged(judged))
    except GoldSetError as exc:
        raise _fail(str(exc)) from exc
    typer.echo(format_calibration(result))


@calibrate_app.command("sheet-export")
def calibrate_sheet_export_cmd(
    results_dir: Path = typer.Option(..., "--results-dir", help="Folder of saved runs (*.jsonl) and judged files (*.j14.jsonl)."),
    out_dir: Path = typer.Option(None, "--out-dir", help="Default: <results-dir>/labels (created 0700, files 0600)."),
    gold_dir: Path = typer.Option(None, "--gold-dir", help="Folder of gold*.yaml (default backend/eval-public)."),
    n: int = typer.Option(80, "--n", min=2, help="Total items including seeded ones."),
    seeded: int = typer.Option(12, "--seeded", min=0, help="Seeded mutated answers (wrong number/entity, unsupported sentence)."),
    seed: int = typer.Option(0, "--seed"),
) -> None:
    """Export a blind, stratified human-label sheet (CSV+JSONL), instructions and a separate key."""
    from docket.eval.calibration_sheet import discover_sources, export_label_sheet

    gold_dir = gold_dir or Path(__file__).resolve().parents[3] / "eval-public"
    try:
        sources = discover_sources(results_dir, gold_dir)
        summary = export_label_sheet(sources, out_dir or results_dir / "labels", n=n, seeded=seeded, seed=seed)
    except GoldSetError as exc:
        raise _fail(str(exc)) from exc
    typer.echo(f"Exported {summary['items']} items ({summary['real']} real, {summary['seeded']} seeded) from {summary['sources']} sources")
    typer.echo(f"Strata: {summary['strata']}")
    for name, path in summary["paths"].items():
        typer.echo(f"  {name}: {path}")
    typer.echo("Give labelers ONLY the sheet and the instructions; keep labels_key.jsonl private.")


@calibrate_app.command("sheet-score")
def calibrate_sheet_score_cmd(
    labels: Path = typer.Option(..., "--labels", help="The returned CSV/JSONL sheet."),
    key: Path = typer.Option(..., "--key", help="labels_key.jsonl written at export."),
    n_boot: int = typer.Option(5000, "--n-boot", min=100),
) -> None:
    """Kappa (bootstrap CI), agreement and confusion matrix of each judge vs the human labels."""
    from docket.eval.calibration_sheet import format_sheet_score, load_key, load_returned_labels, score_label_sheet

    try:
        result = score_label_sheet(load_returned_labels(labels), load_key(key), n_boot=n_boot)
    except GoldSetError as exc:
        raise _fail(str(exc)) from exc
    typer.echo(format_sheet_score(result))


@eval_app.command("freeze")
def freeze_cmd(
    corpus: Path = typer.Option(..., "--corpus"),
    gold: Path = typer.Option(..., "--gold"),
    out: Path = typer.Option(..., "--out", help="New manifest path; never overwrites an existing freeze."),
) -> None:
    """Freeze reviewed gold questions and the corpus bytes for acceptance runs."""
    from docket.eval.benchmark import freeze_benchmark
    from docket.eval.schema import load_gold_set
    try:
        manifest = freeze_benchmark(load_gold_set(gold), corpus, out)
    except (GoldSetError, OSError) as exc:
        raise _fail(str(exc)) from exc
    typer.echo(f"Froze {len(manifest.files)} documents to {out}")


def _default_kag_gold() -> Path:
    return Path(__file__).resolve().parents[3] / "eval-public" / "kag" / "kag-gold-cuad12.silver.yaml"


@kag_app.command("run")
def kag_run_cmd(
    db: Path = typer.Option(..., "--db", help="Scratch library docket.sqlite3 with the KAG tables built (never the real data dir)."),
    gold: Path = typer.Option(None, "--gold", help="KAG gold YAML (default eval-public/kag/kag-gold-cuad12.silver.yaml)."),
    ks: str = typer.Option("3,6,12", "--ks", help="Comma-separated k values for clause recall@k."),
    human_only: bool = typer.Option(False, "--human-only", help="Only count human_reviewed items (silver excluded)."),
    n_boot: int = typer.Option(2000, "--n-boot", min=100, help="Bootstrap resamples per metric."),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
    redact: bool = typer.Option(False, "--redact", help="With --json: drop paths and document file names."),
    out: Path | None = typer.Option(None, "--out", help="Also write the report here (created 0600)."),
) -> None:
    """Run the KAG Lane A evaluation (entity P/R, resolution, roles, clause recall on/off, provenance, lifecycle)."""
    from docket.eval.kag.graph import RealDataDirError
    from docket.eval.kag.run import format_kag_report, report_to_json, run_kag_eval
    from docket.eval.kag.schema import load_kag_gold
    from docket.eval.outputs import write_private_text

    try:
        gold_path = gold or _default_kag_gold()
        report = run_kag_eval(
            load_kag_gold(gold_path), db, gold_path=str(gold_path),
            ks=tuple(int(k) for k in ks.split(",")), human_only=human_only, n_boot=n_boot,
        )
    except (GoldSetError, RealDataDirError, ValueError, OSError) as exc:
        raise _fail(str(exc)) from exc
    text = report_to_json(report, redact=redact) if as_json else format_kag_report(report)
    typer.echo(text)
    if out is not None:
        write_private_text(out, text + "\n")


@kag_app.command("silver")
def kag_silver_cmd(
    gold: list[Path] = typer.Option(None, "--gold", help="gold-cuad*.yaml to derive from (repeatable; default gold-cuad.yaml + gold-cuad-scale.yaml)."),
    out: Path = typer.Option(None, "--out", help="Output YAML (default eval-public/kag/kag-gold-cuad12.silver.yaml)."),
) -> None:
    """Derive SILVER (unreviewed) KAG labels mechanically from existing gold; never marks anything human-reviewed."""
    from docket.eval.kag.silver import build_silver, write_gold_yaml

    base = Path(__file__).resolve().parents[3] / "eval-public"
    paths = gold or [base / "gold-cuad.yaml", base / "gold-cuad-scale.yaml"]
    try:
        built = build_silver(paths, corpus="CUAD v1 12-contract slice")
        write_gold_yaml(built, out or _default_kag_gold())
    except (GoldSetError, OSError) as exc:
        raise _fail(str(exc)) from exc
    typer.echo(
        f"Wrote silver gold to {out or _default_kag_gold()}: {len(built.entities)} entity docs, "
        f"{len(built.resolution_pairs)} resolution pairs, {len(built.roles)} roles, {len(built.clauses)} clauses (all silver_unreviewed)"
    )


@kag_app.command("library")
def kag_library_cmd(
    source_db: Path = typer.Option(..., "--source-db", help="Existing scratch library whose stored chunks are reused."),
    gold: Path = typer.Option(None, "--gold", help="KAG gold; its documents are copied (default silver gold)."),
    dest: Path = typer.Option(..., "--dest", help="NEW scratch directory (never the real data dir)."),
) -> None:
    """Build a scratch library (no model, no embeddings): copy stored chunks, run the deterministic KAG builder."""
    from docket.eval.kag.graph import RealDataDirError
    from docket.eval.kag.schema import load_kag_gold
    from docket.eval.kag.scratch import ScratchError, build_scratch_library

    try:
        files = sorted(load_kag_gold(gold or _default_kag_gold()).files())
        info = build_scratch_library(source_db, files, dest)
    except (GoldSetError, ScratchError, RealDataDirError, OSError) as exc:
        raise _fail(str(exc)) from exc
    typer.echo(f"Built {info['db']}: {info['documents']} documents, {info['chunks']} chunks, {info['built']} document rows built")


@kag_app.command("review")
def kag_review_cmd(
    db: Path = typer.Option(..., "--db", help="Scratch library with the KAG tables built."),
    out_dir: Path = typer.Option(..., "--out-dir", help="Review sheet directory (created 0700, files 0600; contains document text)."),
    max_pairs: int = typer.Option(60, "--max-pairs", min=1),
) -> None:
    """Export CSV review sheets (entities, resolution pairs, relations) + instructions for a human."""
    from docket.eval.kag.graph import RealDataDirError, connect_ro, load_snapshot
    from docket.eval.kag.review_sheet import export_review_sheets

    try:
        conn = connect_ro(db)
        try:
            summary = export_review_sheets(conn, load_snapshot(conn), out_dir, max_pairs=max_pairs)
        finally:
            conn.close()
    except (RealDataDirError, OSError) as exc:
        raise _fail(str(exc)) from exc
    typer.echo(f"Wrote review sheets: {summary['entity_rows']} entity rows, {summary['resolution_rows']} resolution pairs, "
               f"{summary['relation_rows']} relation pairs")
    for name, path in summary["paths"].items():
        typer.echo(f"  {name}: {path}")
