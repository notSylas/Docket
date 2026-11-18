"""Lane B orchestrator: preflight, benchmarks, answers (flags off / KAG on), judging (gemma3:12b, then qwen3:14b), summary.

Every step is a subprocess run ONE AT A TIME (nothing runs on the GPU in parallel). Idempotent: a step with
`<output>.done` is skipped. A failed step is retried at most once, except OOM/ROCm errors and CPU-fallback
failures, which stop the whole run (evidence is written to <out>/FAILURE.json) -- never a retry loop.
Called by run_lane_b.sh (which sets PYTHONPATH, the venv python, the memory cap and the single parse worker).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import sys
from pathlib import Path as _P

sys.path.insert(0, str(_P(__file__).resolve().parent.parent / "scale"))
from scale_guard import check_docket_checkout  # noqa: E402  # sibling module; fails loudly under `python -I`

check_docket_checkout()

from docket.eval.gpu_metrics import foreign_gpu_processes, parse_kfd_pids, parse_ollama_ps  # noqa: E402
from docket.eval.outputs import ensure_private_dir, open_private, write_private_text  # noqa: E402

HERE = Path(__file__).resolve().parent
EVAL_PUBLIC = HERE.parent
REPO = EVAL_PUBLIC.parent.parent
HOME = Path.home()
DEFAULT_OUT = HOME / "docket-eval-results-2026-11-10" / "m0-lane-b"
SCRATCH = Path(os.environ.get("DOCKET_LANE_B_SCRATCH", HOME / "docket-scale" / "m0"))
SRC_DATA = HOME / "docket-scale" / "data-kag"  # existing library with the KAG tables; only ever COPIED
CUAD_CORPUS = HOME / "docket-scale" / "out" / "curve-kag" / "corpus_growing"
CUAD12_PDFS = HOME / "docket-scale" / "libs" / "lib12" / "contracts"
KEEP = {  # source ids of the clean libraries inside data-kag (as in ~/docket-scale/out/chain-clean2.sh)
    12: ["src_11690e44710644bfaeb2184d0d4f3a9c"],
    50: ["src_9faf3c670a014a2db8b025b6105b2313", "src_590ed2f176e0422797481b83dbc88f05"],
    200: ["src_9f5861931fd94d47ae729aaadea2bc63", "src_0ef722ea355a49f3b0bb107f8bdabc8a"],
}
CUAD_REPEATS = {12: 2, 50: 1, 200: 1}
ANSWER_MODEL = "qwen3:14b"
JUDGES = [("gemma", "gemma3:12b", False), ("qwen", "qwen3:14b", True)]
CONFIGS = {"off": {}, "kag": {"DOCKET_KAG_SCOPING": "on"}}
FATAL = re.compile(r"out of memory|OOM|hipError|HIP error|ROCm error|rocBLAS|GPU hang|amdgpu.*(reset|fault)|llama runner process has terminated", re.I)
FALLBACK = re.compile(r"offloaded to CPU|residency could not be verified|no GPU utilisation", re.I)

CLI = [sys.executable, "-m", "docket.interfaces.cli.main"]


class StopRun(Exception):
    pass


def sets() -> list[dict]:
    pub = EVAL_PUBLIC
    out = [
        {"name": "orig", "gold": pub / "gold.yaml", "kind": "cli", "corpus": pub / "corpus-design-docs", "repeats": 3},
        {"name": "g6", "gold": pub / "gold-g6.yaml", "kind": "cli", "corpus": pub / "corpus-synthetic-g6", "repeats": 3},
    ]
    for size in (12, 50, 200):
        out.append({"name": f"cuad{size}", "gold": pub / "gold-cuad-scale.yaml", "kind": "clean", "size": size,
                    "repeats": CUAD_REPEATS[size]})
    return out


def sh(cmd: list[str], timeout: float = 20) -> str:
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        return done.stdout + done.stderr
    except (OSError, subprocess.SubprocessError) as exc:
        return f"<{cmd[0]} failed: {exc}>"


def unload_models(log) -> None:
    for entry in parse_ollama_ps(sh(["ollama", "ps"])):
        sh(["ollama", "stop", entry.model], 60)
        log(f"unloaded {entry.model}")
    deadline = time.time() + 30
    while parse_ollama_ps(sh(["ollama", "ps"])) and time.time() < deadline:
        time.sleep(1)


def preflight(out: Path, log) -> dict:
    pids = sh(["rocm-smi", "--showpids"])
    ps = sh(["ollama", "ps"])
    info = {"showpids": pids, "ollama_ps": ps, "time": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    foreign = foreign_gpu_processes(pids)
    held = parse_kfd_pids(pids)
    if foreign:
        raise StopRun(f"foreign GPU processes hold the GPU: {foreign}; wait or stop them yourself (never killed here)")
    loaded = parse_ollama_ps(ps)
    if loaded or held:
        # Ollama's own runners from an earlier session: only unload through ollama, never kill.
        log(f"preflight: Ollama has models loaded ({[p.model for p in loaded]}); unloading via `ollama stop`")
        unload_models(log)
        pids, ps = sh(["rocm-smi", "--showpids"]), sh(["ollama", "ps"])
        if parse_kfd_pids(pids) or parse_ollama_ps(ps):
            raise StopRun("GPU still held after `ollama stop` (a dead PID may hold VRAM: a reboot is needed); stopping")
    info["clean"] = True
    write_private_text(out / "preflight.json", json.dumps(info, indent=1) + "\n")
    return info


class Runner:
    def __init__(self, out: Path, allow_fallback: bool) -> None:
        self.out = out
        self.allow = allow_fallback
        self.logdir = ensure_private_dir(out / "logs")
        self.logf = open_private(out / "lane_b.log", overwrite=False) if not (out / "lane_b.log").exists() else open(out / "lane_b.log", "a")
        self.child: subprocess.Popen | None = None

    def log(self, msg: str) -> None:
        line = f"{time.strftime('%H:%M:%S')} {msg}"
        print(line, flush=True)
        self.logf.write(line + "\n")
        self.logf.flush()

    def step(self, name: str, cmd: list[str], output: Path, env_extra: dict[str, str] | None = None) -> None:
        marker = output.with_name(output.name + ".done")
        if marker.exists():
            self.log(f"skip {name} (done)")
            return
        for attempt in (1, 2):
            for stale in (output, output.with_name(output.name + ".manifest.json"), output.with_name(output.name + ".gpu.json")):
                stale.unlink(missing_ok=True)
            env = {**os.environ, **(env_extra or {})}
            self.log(f"run {name} attempt {attempt}: {' '.join(cmd[2:6])} ... env={env_extra or {}}")
            t0 = time.time()
            logpath = self.logdir / f"{name}.log"
            with open_private(logpath, overwrite=True) as lf:
                self.child = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT, env=env, cwd=str(REPO / "backend"))
                rc = self.child.wait()
                self.child = None
            text = logpath.read_text(errors="replace")
            wall = round(time.time() - t0, 1)
            if rc == 0:
                write_private_text(marker, json.dumps({"wall_s": wall, "finished": time.strftime("%Y-%m-%dT%H:%M:%S")}) + "\n")
                self.log(f"done {name} in {wall}s")
                return
            fatal = FATAL.search(text)
            fallback = FALLBACK.search(text)
            self.log(f"FAILED {name} rc={rc} after {wall}s fatal={bool(fatal)} fallback={bool(fallback)}")
            if fatal or fallback or attempt == 2:
                self.failure(name, rc, text, "fatal GPU error" if fatal else "CPU fallback" if fallback else "failed twice")
                raise StopRun(f"{name} failed ({'GPU error' if fatal else 'CPU fallback' if fallback else 'twice'}); see FAILURE.json")
            unload_models(self.log)

    def failure(self, name: str, rc: int, text: str, why: str) -> None:
        dmesg = sh(["dmesg"]).splitlines()[-40:] if True else []
        write_private_text(self.out / "FAILURE.json", json.dumps({
            "step": name, "returncode": rc, "why": why, "log_tail": text.splitlines()[-40:],
            "dmesg_tail": dmesg, "rocm_smi_pids": sh(["rocm-smi", "--showpids"]), "ollama_ps": sh(["ollama", "ps"]),
        }, indent=1) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=Path(os.environ.get("DOCKET_LANE_B_OUT", DEFAULT_OUT)))
    ap.add_argument("--only", default="", help="comma list of: bench, answers, judge-gemma, judge-qwen, summary")
    ap.add_argument("--allow-cpu-fallback", action="store_true", help="escape hatch: record, do not fail on CPU offload")
    ap.add_argument("--skip-preflight", action="store_true")
    a = ap.parse_args()
    only = {s for s in a.only.split(",") if s}
    want = lambda s: not only or s in only  # noqa: E731
    out = ensure_private_dir(a.out)
    ensure_private_dir(SCRATCH)
    assert "/.local/share/docket" not in str(SCRATCH.resolve()) and "/.local/share/docket" not in str(out.resolve())
    r = Runner(out, a.allow_cpu_fallback)
    fb = ["--allow-cpu-fallback"] if a.allow_cpu_fallback else []

    def on_term(signum, frame):  # never leave a child holding VRAM
        if r.child is not None:
            r.child.terminate()
        raise StopRun(f"signal {signum}")

    signal.signal(signal.SIGTERM, on_term)
    code = 0
    try:
        if not a.skip_preflight:
            preflight(out, r.log)
        answers = ensure_private_dir(out / "answers")
        judged = ensure_private_dir(out / "judged")
        if want("bench"):
            r.step("bench", [sys.executable, str(HERE / "bench_lane_b.py"), "--out", str(out / "bench.json"),
                             "--scratch", str(SCRATCH), *fb], out / "bench.json")
            unload_models(r.log)
        if want("answers"):
            for s in sets():
                for cfg, env in CONFIGS.items():
                    outp = answers / f"{s['name']}-{cfg}.jsonl"
                    if s["kind"] == "cli":
                        cmd = CLI + ["eval", "run", "--corpus", str(s["corpus"]), "--gold", str(s["gold"]), "--split", "all",
                                     "--repeats", str(s["repeats"]), "--lane", "gpu", "--model", ANSWER_MODEL, "--out", str(outp), *fb]
                        r.step(f"answers-{s['name']}-{cfg}", cmd, outp, env)
                    else:
                        work = SCRATCH / "work" / f"clean{s['size']}-{cfg}"
                        cmd = [sys.executable, str(HERE.parent / "scale" / "cuad_query_clean.py"), str(SRC_DATA), str(work), str(outp),
                               "--keep", *KEEP[s["size"]], "--repeats", str(s["repeats"]), "--corpus", str(CUAD_CORPUS), *fb]
                        try:
                            r.step(f"answers-{s['name']}-{cfg}", cmd, outp, env)
                        finally:
                            shutil.rmtree(work, ignore_errors=True)  # a COPY; the original data dir is never mutated
            unload_models(r.log)
        for tag, model, selfjudge in JUDGES:
            if not want(f"judge-{tag}"):
                continue
            for s in sets():
                for cfg in CONFIGS:
                    src = answers / f"{s['name']}-{cfg}.jsonl"
                    if not (src.with_name(src.name + ".done")).exists():
                        r.log(f"no finished answers for {src.name}; skipping judge {tag}")
                        continue
                    outp = judged / f"{s['name']}-{cfg}.{tag}.jsonl"
                    cmd = CLI + ["eval", "judge", "--gold", str(s["gold"]), "--runs", str(src), "--out", str(outp),
                                 "--model", model, "--lane", "gpu", *(["--allow-self-judge"] if selfjudge else []), *fb]
                    r.step(f"judge-{tag}-{s['name']}-{cfg}", cmd, outp)
            unload_models(r.log)
        if want("summary"):
            r.step("summary", [sys.executable, str(HERE / "lane_b_summary.py"), "--out", str(out)], out / "summary.json")
    except StopRun as exc:
        r.log(f"STOP: {exc}")
        code = 3
    except BaseException as exc:  # noqa: BLE001
        r.log(f"ERROR: {type(exc).__name__}: {exc}")
        code = 4
    finally:
        if r.child is not None:
            r.child.terminate()
        unload_models(r.log)
        r.logf.close()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
