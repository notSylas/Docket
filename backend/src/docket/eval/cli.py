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
from docket.eval.schema import GoldSetError

eval_app = typer.Typer(help="Measure answer accuracy: run a gold set, judge, report, compare, draft gold.")
calibrate_app = typer.Typer(help="Calibrate the automatic judge against your own labels.")
eval_app.add_typer(calibrate_app, name="calibrate")


def _eval_dir() -> Path:
    path = Settings().data_dir / "eval"
    path.mkdir(parents=True, exist_ok=True)
    return path


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
) -> None:
    """Ingest the corpus into a temp dir and record answers for the gold questions."""
    from docket.eval.report import build_report, format_report
    from docket.eval.runner import EvalSetupError, run_eval
    from docket.eval.schema import Split, load_gold_set
    from docket.query.classifier import QueryMode

    if mode not in ("auto", "fast", "agent"):
        raise _fail("--mode must be auto, fast or agent")
    if split not in ("dev", "test", "all"):
        raise _fail("--split must be dev, test or all")
    try:
        gold_set = load_gold_set(gold)
        out_path = out or _eval_dir() / f"runs-{_stamp()}.jsonl"
        out_path.parent.mkdir(parents=True, exist_ok=True)

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
    model: str = typer.Option(None, "--model", help="Judge model (default qwen3:30b)."),
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
        f"({by_source['judge']} judged, {doubts} doubt, {disagreements} cross-check disagreements)"
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
) -> None:
    """Print accuracy (with Wilson intervals), retrieval and failure breakdowns."""
    from docket.eval.judge import load_judged
    from docket.eval.report import build_report, format_report, report_to_dict
    from docket.eval.schema import load_gold_set, load_records

    from docket.eval.benchmark import load_benchmark
    from docket.eval.calibration import score_labels
    try:
        judgments = load_judged(judged) if judged else None
        calibration_judgments = load_judged(calibration_judged) if calibration_judged else judgments
        if labels is not None and calibration_judgments is None:
            raise GoldSetError("--labels requires --judged")
        report = build_report(
            load_gold_set(gold), load_records(runs), judgments,
            benchmark=load_benchmark(manifest) if manifest else None,
            calibration=score_labels(labels, calibration_judgments) if labels else None,
        )
    except GoldSetError as exc:
        raise _fail(str(exc)) from exc
    typer.echo(json.dumps(report_to_dict(report), indent=2) if as_json else format_report(report))
    if require_milestone and not report.milestone["passed"]:
        raise typer.Exit(code=1)


@eval_app.command("compare")
def compare_cmd(
    a: Path = typer.Argument(..., help="Baseline judged (or raw run) JSONL."),
    b: Path = typer.Argument(..., help="New judged (or raw run) JSONL."),
    gold: Path | None = typer.Option(None, "--gold", help="Needed only for raw run files."),
    alpha: float = typer.Option(0.05, "--alpha", help="Significance level for a regression."),
    fail_on: str = typer.Option("significant", "--fail-on", help="significant | any: when to exit non-zero."),
) -> None:
    """Paired flips and McNemar p between two runs; exit 1 if B regressed vs A."""
    from docket.eval.compare import compare_outcomes, format_comparison, load_outcomes
    from docket.eval.schema import load_gold_set

    if fail_on not in ("significant", "any"):
        raise _fail("--fail-on must be significant or any")
    try:
        gold_set = load_gold_set(gold) if gold else None
        comparison = compare_outcomes(load_outcomes(a, gold_set), load_outcomes(b, gold_set))
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
        out_path.parent.mkdir(parents=True, exist_ok=True)
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
    from docket.cli.interactive.reader import CallableReader
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
