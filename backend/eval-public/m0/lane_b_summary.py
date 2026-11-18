"""Lane B summary: accuracy, wrongful abstention (cluster-bootstrap CIs), paired KAG-on vs off, judge agreement,
latency by phase, tokens/s, GPU evidence. Writes summary.json and report.txt (plain text) into --out.

Model-to-model judge agreement is NOT human validation: kappa against human labels stays pending (the
human label sheet is the user's job)."""
from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

import sys
from pathlib import Path as _P

sys.path.insert(0, str(_P(__file__).resolve().parent.parent / "scale"))
from scale_guard import check_docket_checkout  # noqa: E402

check_docket_checkout()

from docket.eval.compare import compare_outcomes, load_repeat_outcomes  # noqa: E402
from docket.eval.judge import load_judged  # noqa: E402
from docket.eval.outputs import write_private_text  # noqa: E402
from docket.eval.report import build_report, percentile  # noqa: E402
from docket.eval.schema import load_gold_set, load_records  # noqa: E402
from docket.eval.scoring import score_run  # noqa: E402
from docket.eval.stats import cohen_kappa, cluster_bootstrap_ci  # noqa: E402

PUB = Path(__file__).resolve().parent.parent
SETS = {
    "orig": PUB / "gold.yaml", "g6": PUB / "gold-g6.yaml",
    "cuad12": PUB / "gold-cuad-scale.yaml", "cuad50": PUB / "gold-cuad-scale.yaml", "cuad200": PUB / "gold-cuad-scale.yaml",
}
CFGS = ("off", "kag")
JUDGES = {"gemma": "gemma3:12b (independent)", "qwen": "qwen3:14b (SELF-judge, comparison only)"}


def abstention_ci(per_question: dict[str, list[bool]], n_boot: int = 5000, seed: int = 0) -> tuple[float, float]:
    ids = [q for q, v in per_question.items() if v]
    if not ids:
        return (0.0, 1.0)
    rng = random.Random(seed)
    samples = []
    for _ in range(n_boot):
        pick = [per_question[ids[rng.randrange(len(ids))]] for _ in ids]
        total = sum(len(v) for v in pick)
        samples.append(sum(sum(v) for v in pick) / total)
    samples.sort()
    return (samples[int(0.025 * n_boot)], samples[int(0.975 * n_boot) - 1])


def jload(path: Path):
    return json.loads(path.read_text()) if path.exists() else None


def pct(values: list[float], q: float):
    v = percentile(values, q)
    return None if v is None else round(v, 2)


def per_set(out: Path, name: str, cfg: str) -> dict | None:
    src = out / "answers" / f"{name}-{cfg}.jsonl"
    if not src.with_name(src.name + ".done").exists():
        return None
    gold = load_gold_set(SETS[name])
    records = load_records(src)
    res: dict = {"records": len(records), "errors": sum(bool(r.error) for r in records), "judges": {}}
    # latency / tokens / phases (ok runs only)
    ok = [r for r in records if not r.error and r.latency_s > 0]
    phases: dict[str, list[float]] = {}
    for r in ok:
        for k, v in (r.latency_by_phase or {}).items():
            phases.setdefault(k, []).append(v)
    gen_rates = [r.eval_count / r.latency_by_phase["generation"] for r in ok
                 if r.eval_count and (r.latency_by_phase or {}).get("generation", 0) > 0]
    res["latency_s"] = {"p50": pct([r.latency_s for r in ok], 0.5), "p90": pct([r.latency_s for r in ok], 0.9), "n": len(ok)}
    res["latency_by_phase_s"] = {k: {"p50": pct(v, 0.5), "p90": pct(v, 0.9)} for k, v in sorted(phases.items())}
    res["answer_tokens_per_s_end_to_end_generation_phase"] = {"p50": pct(gen_rates, 0.5), "n": len(gen_rates)}
    res["prompt_tokens_p50"] = pct([r.prompt_eval_count for r in ok if r.prompt_eval_count], 0.5)
    res["gpu"] = (jload(src.with_name(src.name + ".gpu.json")) or {}).get("summary")
    for tag in JUDGES:
        jp = out / "judged" / f"{name}-{cfg}.{tag}.jsonl"
        if not jp.with_name(jp.name + ".done").exists():
            continue
        judged = load_judged(jp)
        rep = build_report(gold, records, judged)
        score_by_key = {(r.question_id, r.repeat): (q, s) for q, r, s in
                        ((gold.by_id()[r.question_id], r, score_run(gold.by_id()[r.question_id], r)) for r in records if r.question_id in gold.by_id())}
        # wrongful abstention clusters: answerable questions, per-question list of "abstained" per repeat
        per_q: dict[str, list[bool]] = {}
        for (qid, _rep), (q, s) in score_by_key.items():
            if q.answerable:
                per_q.setdefault(qid, []).append(bool(s.abstained))
        lo, hi = abstention_ci(per_q)
        strict = {qid: [] for qid in per_q}
        out_rep = rep.overall
        res["judges"][tag] = {
            "judge_identity": rep.judge_identity, "self_judged": rep.self_judged, "answer_models": rep.answer_models,
            "accuracy": {"value": round(out_rep.strict.value, 4), "passed": out_rep.strict.passed, "n_questions": out_rep.strict.total,
                         "runs": rep.runs, "ci95_cluster_bootstrap": [round(x, 4) for x in (rep.overall_ci or (0, 1))],
                         "wilson95": [round(out_rep.strict.lo, 4), round(out_rep.strict.hi, 4)]},
            "wrongful_abstention": {"value": round(rep.wrongful_abstention.value, 4), "k": rep.wrongful_abstention.passed,
                                    "n_runs": rep.wrongful_abstention.total, "n_questions": len(per_q),
                                    "ci95_cluster_bootstrap": [round(lo, 4), round(hi, 4)]},
            "judged_runs": rep.judged_runs, "gold_binding": rep.gold_binding,
        }
        del strict
    return res


def paired(out: Path, names: list[str], tag: str) -> dict | None:
    a_all: dict[str, list[bool]] = {}
    b_all: dict[str, list[bool]] = {}
    for name in names:
        pa, pb = out / "judged" / f"{name}-off.{tag}.jsonl", out / "judged" / f"{name}-kag.{tag}.jsonl"
        if not (pa.with_name(pa.name + ".done").exists() and pb.with_name(pb.name + ".done").exists()):
            continue
        for q, v in load_repeat_outcomes(pa, None).items():
            a_all[f"{name}:{q}"] = v
        for q, v in load_repeat_outcomes(pb, None).items():
            b_all[f"{name}:{q}"] = v
    if not a_all or not b_all:
        return None
    c = compare_outcomes(a_all, b_all)
    return {"n_questions": c.n, "off_rate": round(c.a_rate, 4), "kag_rate": round(c.b_rate, 4), "both_pass": c.both, "both_fail": c.neither,
            "only_off": c.a_only, "only_kag": c.b_only, "mcnemar_exact_p": round(c.p_value, 4),
            "diff_kag_minus_off": round(c.diff, 4), "diff_ci95": [round(x, 4) for x in c.diff_ci], "verdict": c.label,
            "lost_in_kag": c.a_only_ids, "gained_in_kag": c.b_only_ids}


def judge_agreement(out: Path, names: list[str]) -> dict:
    rows = []
    per = {}
    for name in names:
        for cfg in CFGS:
            pg, pq = out / "judged" / f"{name}-{cfg}.gemma.jsonl", out / "judged" / f"{name}-{cfg}.qwen.jsonl"
            if not (pg.with_name(pg.name + ".done").exists() and pq.with_name(pq.name + ".done").exists()):
                continue
            g = {(r.question_id, r.repeat): r.verdict.value for r in load_judged(pg)}
            q = {(r.question_id, r.repeat): r.verdict.value for r in load_judged(pq)}
            keys = sorted(set(g) & set(q))
            pairs = [(g[k] == "pass", q[k] == "pass") for k in keys]
            per[f"{name}-{cfg}"] = pair_stats(pairs)
            rows += pairs
    return {"pooled": pair_stats(rows), "per_file": per,
            "note": "model-to-model agreement, NOT human validation; kappa vs human labels is pending (labels sheet: ~/docket-eval-results-2026-11-10/labels/)"}


def pair_stats(pairs: list[tuple[bool, bool]]) -> dict:
    n = len(pairs)
    if not n:
        return {"n": 0}
    agree = sum(a == b for a, b in pairs)
    return {"n": n, "raw_agreement": round(agree / n, 4), "disagreements": n - agree,
            "gemma_pass_qwen_fail": sum(a and not b for a, b in pairs), "gemma_fail_qwen_pass": sum(b and not a for a, b in pairs),
            "cohen_kappa_between_judges": round(cohen_kappa([a for a, _ in pairs], [b for _, b in pairs]), 4)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    out = a.out
    summary: dict = {"sets": {}, "paired_kag_on_vs_off": {}, "judge_agreement": None,
                     "bench": jload(out / "bench.json"), "preflight": jload(out / "preflight.json")}
    for name in SETS:
        for cfg in CFGS:
            r = per_set(out, name, cfg)
            if r:
                summary["sets"][f"{name}-{cfg}"] = r
    for tag in JUDGES:
        for label, names in [(n, [n]) for n in SETS] + [("cuad-all-sizes", ["cuad12", "cuad50", "cuad200"]), ("ALL-sets", list(SETS))]:
            p = paired(out, names, tag)
            if p:
                summary["paired_kag_on_vs_off"][f"{label}|judge={tag}"] = p
    summary["judge_agreement"] = judge_agreement(out, list(SETS))
    manifests = sorted((out / "answers").glob("*.manifest.json"))
    summary["manifest_example"] = jload(manifests[0]) if manifests else None
    # GPU evidence across every sampled step
    evidence = {}
    for p in sorted(list((out / "answers").glob("*.gpu.json")) + list((out / "judged").glob("*.gpu.json"))):
        s = (jload(p) or {}).get("summary") or {}
        evidence[p.name.replace(".gpu.json", "")] = {k: s.get(k) for k in (
            "samples", "max_gpu_use_percent", "samples_with_gpu_use", "max_vram_percent", "models_seen",
            "min_gpu_share_percent", "min_gpu_share_by_model", "cpu_offload_detected", "max_rss_mb", "card")}
    summary["gpu_evidence"] = evidence
    write_private_text(out / "summary.json", json.dumps(summary, indent=1) + "\n")
    write_private_text(out / "report.txt", render(summary))


def render(s: dict) -> str:
    L: list[str] = ["M0 PHASE 2b LANE B REPORT (GPU, answers qwen3:14b)", ""]
    L.append("Accuracy = question-level pass (majority of repeats), judge-resolved; wrongful abstention = abstained on answerable runs.")
    L.append("CIs: percentile bootstrap, questions resampled as clusters, 95%.")
    L.append("Judge agreement is model-to-model, NOT human validation; kappa vs human labels is pending (user's labels).")
    L.append("")
    L.append(f"{'set-config':<14}{'judge':<8}{'accuracy [CI]':<34}{'n_q':>4}{'wrongful abst [CI]':<30}")
    for key, v in s["sets"].items():
        for tag, j in v["judges"].items():
            acc, wa = j["accuracy"], j["wrongful_abstention"]
            L.append(f"{key:<14}{tag:<8}{acc['value']:.1%} ({acc['passed']}/{acc['n_questions']}) [{acc['ci95_cluster_bootstrap'][0]:.1%}-{acc['ci95_cluster_bootstrap'][1]:.1%}]".ljust(56)
                     + f"{acc['n_questions']:>4}  {wa['value']:.1%} ({wa['k']}/{wa['n_runs']}) [{wa['ci95_cluster_bootstrap'][0]:.1%}-{wa['ci95_cluster_bootstrap'][1]:.1%}]")
    L += ["", "PAIRED KAG-on vs flags-off (B=KAG minus A=off)"]
    for k, p in s["paired_kag_on_vs_off"].items():
        L.append(f"{k}: n={p['n_questions']} off={p['off_rate']:.1%} kag={p['kag_rate']:.1%} only_off={p['only_off']} only_kag={p['only_kag']} "
                 f"p={p['mcnemar_exact_p']} diff={p['diff_kag_minus_off']:+.1%} CI[{p['diff_ci95'][0]:+.1%},{p['diff_ci95'][1]:+.1%}] -> {p['verdict']}")
    ja = s["judge_agreement"]
    L += ["", "JUDGE AGREEMENT gemma3:12b vs qwen3:14b(self): " + json.dumps(ja["pooled"]), ja["note"]]
    for k, v in ja["per_file"].items():
        L.append(f"  {k}: {json.dumps(v)}")
    L += ["", "LATENCY (s, ok runs) / tokens per s"]
    for key, v in s["sets"].items():
        L.append(f"{key}: e2e p50={v['latency_s']['p50']} p90={v['latency_s']['p90']} n={v['latency_s']['n']}; "
                 f"gen-phase p50={v['latency_by_phase_s'].get('generation', {}).get('p50')} p90={v['latency_by_phase_s'].get('generation', {}).get('p90')}; "
                 f"tok/s p50={v['answer_tokens_per_s_end_to_end_generation_phase']['p50']}; errors={v['errors']}")
    L += ["", "GPU EVIDENCE (per step; sampler every 2 s)"]
    for k, e in s["gpu_evidence"].items():
        L.append(f"{k}: {json.dumps(e)}")
    if s.get("bench"):
        L += ["", "BENCH " + json.dumps(s["bench"], indent=1)[:6000]]
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    main()
