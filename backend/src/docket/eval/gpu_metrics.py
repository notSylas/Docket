"""GPU-lane metrics recorder: is the model really running on the GPU?

Samples read-only system tools while an evaluation runs:

* `rocm-smi --showuse --showmemuse --json`  -> GPU utilisation and VRAM use
* `ollama ps`                                -> per-model PROCESSOR split
  ("100% GPU", "48%/52% CPU/GPU", "100% CPU")

All command execution goes through an injectable `Runner`, so unit tests use
fake output and never touch a GPU. Missing tools yield samples with `error`
set rather than exceptions. `GpuMetricsRecorder` runs a daemon sampling thread
only when `start()` is called; `sample_once()` is synchronous.

Verified against real output on Ubuntu 24.04 / ROCm 7.2.1 / Ollama (fixtures in
tests/unit/eval/fixtures). The machine has TWO ROCm cards (card0 = the discrete GPU, card1 = the
CPU's tiny iGPU), so utilisation is read for one chosen card (`card=`, env `DOCKET_GPU_CARD`,
default: the maximum over all cards).

CPU-fallback policy: a GPU-lane run (`gpu_lane_guard`) fails LOUDLY (`CpuFallbackError`) when the
model was not fully on the GPU, residency could not be verified, or no GPU activity was seen.
Documented escape hatch: `allow_cpu_fallback=True` (CLI `--allow-cpu-fallback`, or env
`DOCKET_ALLOW_CPU_FALLBACK=1`); the summary is still written and records `cpu_fallback_allowed`.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

Runner = Callable[[Sequence[str]], str]  # returns stdout; raises OSError/RuntimeError on failure


def default_runner(cmd: Sequence[str], timeout: float = 10.0) -> str:
    done = subprocess.run(list(cmd), capture_output=True, text=True, timeout=timeout, check=False)
    if done.returncode != 0:
        raise RuntimeError(f"{cmd[0]} exited {done.returncode}: {done.stderr.strip()[:200]}")
    return done.stdout


@dataclass(frozen=True)
class OllamaProcess:
    model: str
    size: str
    processor: str  # raw text, e.g. "100% GPU"
    gpu_percent: float | None
    cpu_percent: float | None


_PROC_SPLIT = re.compile(r"(\d+)%/(\d+)%\s+CPU/GPU")
_PROC_ONE = re.compile(r"(\d+)%\s+(GPU|CPU)")


def parse_processor(text: str) -> tuple[float | None, float | None]:
    """(gpu_percent, cpu_percent) from an `ollama ps` PROCESSOR cell."""
    m = _PROC_SPLIT.search(text)
    if m:
        return float(m.group(2)), float(m.group(1))
    m = _PROC_ONE.search(text)
    if m:
        pct = float(m.group(1))
        return (pct, 100.0 - pct) if m.group(2) == "GPU" else (100.0 - pct, pct)
    return None, None


def parse_ollama_ps(output: str) -> list[OllamaProcess]:
    """Parse the table printed by `ollama ps`. NAME ID SIZE PROCESSOR [CONTEXT] UNTIL."""
    lines = [ln for ln in output.splitlines() if ln.strip()]
    if len(lines) < 2:
        return []
    out: list[OllamaProcess] = []
    for line in lines[1:]:
        cols = re.split(r"\s{2,}", line.strip())
        if len(cols) < 4:
            continue
        gpu, cpu = parse_processor(cols[3])
        out.append(OllamaProcess(model=cols[0], size=cols[2], processor=cols[3], gpu_percent=gpu, cpu_percent=cpu))
    return out


def parse_rocm_smi(output: str) -> dict[str, dict[str, float | None]]:
    """{card: {"gpu_use_percent", "vram_percent"}} from `rocm-smi --json` output."""
    data = json.loads(output)
    cards: dict[str, dict[str, float | None]] = {}
    for card, fields in data.items():
        if not card.startswith("card") or not isinstance(fields, dict):
            continue

        def pick(*needles: str) -> float | None:
            for key, value in fields.items():
                low = key.lower()
                if all(n in low for n in needles):
                    try:
                        return float(str(value).strip().rstrip("%"))
                    except ValueError:
                        return None
            return None

        cards[card] = {
            "gpu_use_percent": pick("gpu use"),
            "vram_percent": pick("memory allocated") if pick("memory allocated") is not None else pick("vram"),
        }
    return cards


class CpuFallbackError(RuntimeError):
    """A GPU-lane run was not (verifiably) fully on the GPU."""


def parse_kfd_pids(output: str) -> list[tuple[int, str]]:
    """(pid, process name) rows from `rocm-smi --showpids`; empty when "No KFD PIDs currently running"."""
    rows: list[tuple[int, str]] = []
    for line in output.splitlines():
        cols = line.split()
        if len(cols) >= 3 and cols[0].isdigit() and not cols[1].isdigit():
            rows.append((int(cols[0]), cols[1]))
    return rows


def foreign_gpu_processes(output: str, allowed: Sequence[str] = ("llama-server", "ollama")) -> list[tuple[int, str]]:
    """KFD processes that are not Ollama's runner (pre-flight: the GPU must be ours alone)."""
    return [(pid, name) for pid, name in parse_kfd_pids(output) if not any(name.startswith(a) for a in allowed)]


def process_tree_rss_mb(pid: int | None = None) -> float | None:
    """Resident memory (MB) of `pid` (default: this process) plus all its descendants, via /proc.
    None when /proc is unavailable."""
    root = os.getpid() if pid is None else pid
    try:
        parents: dict[int, int] = {}
        rss: dict[int, int] = {}
        page = os.sysconf("SC_PAGE_SIZE")
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                stat = Path(f"/proc/{entry}/stat").read_text()
            except OSError:
                continue
            tail = stat[stat.rindex(")") + 2:].split()  # fields after "(comm)": state ppid ... rss at index 21
            parents[int(entry)] = int(tail[1])
            rss[int(entry)] = int(tail[21]) * page
    except (OSError, ValueError, IndexError):
        return None
    members, grew = {root}, True
    while grew:
        grew = False
        for child, parent in parents.items():
            if parent in members and child not in members:
                members.add(child)
                grew = True
    return sum(rss.get(p, 0) for p in members) / (1024 * 1024)


@dataclass
class GpuSample:
    t: float  # seconds since the recorder started
    gpu_use_percent: float | None = None
    vram_percent: float | None = None
    rss_mb: float | None = None  # process tree of the eval process
    processes: list[OllamaProcess] = field(default_factory=list)
    error: str | None = None


@dataclass
class GpuSummary:
    samples: int
    errors: int
    max_gpu_use_percent: float | None
    max_vram_percent: float | None
    min_gpu_share_percent: float | None  # lowest GPU share of any loaded model
    cpu_offload_detected: bool
    models_seen: list[str]
    samples_with_gpu_use: int = 0
    max_rss_mb: float | None = None
    card: str | None = None
    min_gpu_share_by_model: dict[str, float] = field(default_factory=dict)
    cpu_fallback_allowed: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


class GpuMetricsRecorder:
    ROCM_CMD = ("rocm-smi", "--showuse", "--showmemuse", "--json")
    OLLAMA_CMD = ("ollama", "ps")

    def __init__(self, runner: Runner = default_runner, interval_s: float = 2.0,
                 clock: Callable[[], float] = time.monotonic, card: str | None = None,
                 rss_pid: int | None = None) -> None:
        self._card = card or os.environ.get("DOCKET_GPU_CARD") or None
        self._rss_pid = rss_pid
        self._runner = runner
        self._interval = interval_s
        self._clock = clock
        self._t0 = clock()
        self.samples: list[GpuSample] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def sample_once(self) -> GpuSample:
        sample = GpuSample(t=self._clock() - self._t0)
        errors: list[str] = []
        try:
            cards = parse_rocm_smi(self._runner(self.ROCM_CMD))
            if self._card is not None:
                if self._card not in cards:
                    raise ValueError(f"card {self._card!r} not in rocm-smi output ({sorted(cards)})")
                cards = {self._card: cards[self._card]}
            uses = [c["gpu_use_percent"] for c in cards.values() if c["gpu_use_percent"] is not None]
            vrams = [c["vram_percent"] for c in cards.values() if c["vram_percent"] is not None]
            sample.gpu_use_percent = max(uses) if uses else None
            sample.vram_percent = max(vrams) if vrams else None
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
            errors.append(f"rocm-smi: {exc}")
        try:
            sample.processes = parse_ollama_ps(self._runner(self.OLLAMA_CMD))
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            errors.append(f"ollama ps: {exc}")
        sample.rss_mb = process_tree_rss_mb(self._rss_pid)
        sample.error = "; ".join(errors) or None
        self.samples.append(sample)
        return sample

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()

        def loop() -> None:
            while not self._stop.is_set():
                self.sample_once()
                self._stop.wait(self._interval)

        self._thread = threading.Thread(target=loop, daemon=True, name="gpu-metrics")
        self._thread.start()

    def stop(self) -> GpuSummary:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval + 5)
            self._thread = None
        return self.summary()

    def summary(self) -> GpuSummary:
        uses = [s.gpu_use_percent for s in self.samples if s.gpu_use_percent is not None]
        vrams = [s.vram_percent for s in self.samples if s.vram_percent is not None]
        shares = [p.gpu_percent for s in self.samples for p in s.processes if p.gpu_percent is not None]
        rss = [s.rss_mb for s in self.samples if s.rss_mb is not None]
        by_model: dict[str, float] = {}
        for s in self.samples:
            for p in s.processes:
                if p.gpu_percent is not None:
                    by_model[p.model] = min(p.gpu_percent, by_model.get(p.model, 100.0))
        return GpuSummary(
            samples_with_gpu_use=sum(u > 0 for u in uses),
            max_rss_mb=max(rss, default=None),
            card=self._card,
            min_gpu_share_by_model=dict(sorted(by_model.items())),
            samples=len(self.samples),
            errors=sum(s.error is not None for s in self.samples),
            max_gpu_use_percent=max(uses, default=None),
            max_vram_percent=max(vrams, default=None),
            min_gpu_share_percent=min(shares, default=None),
            cpu_offload_detected=any(sh < 100.0 for sh in shares),
            models_seen=sorted({p.model for s in self.samples for p in s.processes}),
        )


def assert_no_cpu_fallback(summary: GpuSummary, *, min_samples: int = 1, require_activity_after: int = 3) -> None:
    """Raise `CpuFallbackError` when the run silently fell back to CPU or could not be verified.
    Also raises when >= `require_activity_after` samples were taken but the GPU never showed use > 0."""
    if summary.samples < min_samples or summary.min_gpu_share_percent is None:
        raise CpuFallbackError("GPU residency could not be verified (no `ollama ps` samples with a processor split)")
    if summary.cpu_offload_detected:
        raise CpuFallbackError(
            f"model offloaded to CPU: lowest GPU share {summary.min_gpu_share_percent:.0f}% "
            f"(models: {', '.join(summary.models_seen)})"
        )
    if summary.samples >= require_activity_after and summary.samples_with_gpu_use == 0 and summary.max_gpu_use_percent is not None:
        raise CpuFallbackError("no GPU utilisation above 0% was observed during the run")


def allow_cpu_fallback_from_env(env: dict[str, str] | None = None) -> bool:
    return (os.environ if env is None else env).get("DOCKET_ALLOW_CPU_FALLBACK", "") in ("1", "true", "yes", "on")


@contextlib.contextmanager
def gpu_lane_guard(
    lane: str,
    summary_path: Path | None = None,
    *,
    allow_cpu_fallback: bool = False,
    recorder: GpuMetricsRecorder | None = None,
    interval_s: float = 2.0,
    on_summary: Callable[[GpuSummary], None] | None = None,
) -> Iterator[GpuMetricsRecorder | None]:
    """Record GPU metrics around a block when `lane == "gpu"`; no-op (yields None) for the CPU lane.

    On exit: stop the recorder, write `{summary, samples}` to `summary_path` (0600), call `on_summary`,
    then raise `CpuFallbackError` unless `allow_cpu_fallback` (or env DOCKET_ALLOW_CPU_FALLBACK=1).
    A pending exception from the block is never masked by the fallback check."""
    if lane != "gpu":
        yield None
        return
    allow = allow_cpu_fallback or allow_cpu_fallback_from_env()
    rec = recorder or GpuMetricsRecorder(interval_s=interval_s)
    rec.sample_once()
    rec.start()
    failed = False
    try:
        yield rec
    except BaseException:
        failed = True
        raise
    finally:
        summary = rec.stop()
        rec.sample_once()  # one final reading after the block
        summary = rec.summary()
        summary.cpu_fallback_allowed = allow
        if summary_path is not None:
            from docket.eval.outputs import write_private_text

            write_private_text(
                summary_path,
                json.dumps({"summary": summary.to_dict(), "samples": [asdict(s) for s in rec.samples]}) + "\n",
            )
        if on_summary is not None:
            on_summary(summary)
        if not failed and not allow:
            assert_no_cpu_fallback(summary)
