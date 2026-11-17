"""GPU-lane metrics recorder: is the model really running on the GPU?

Samples read-only system tools while an evaluation runs:

* `rocm-smi --showuse --showmemuse --json`  -> GPU utilisation and VRAM use
* `ollama ps`                                -> per-model PROCESSOR split
  ("100% GPU", "48%/52% CPU/GPU", "100% CPU")

All command execution goes through an injectable `Runner`, so unit tests use
fake output and never touch a GPU. Missing tools yield samples with `error`
set rather than exceptions. `GpuMetricsRecorder` runs a daemon sampling thread
only when `start()` is called; `sample_once()` is synchronous.
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field

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


@dataclass
class GpuSample:
    t: float  # seconds since the recorder started
    gpu_use_percent: float | None = None
    vram_percent: float | None = None
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

    def to_dict(self) -> dict:
        return asdict(self)


class GpuMetricsRecorder:
    ROCM_CMD = ("rocm-smi", "--showuse", "--showmemuse", "--json")
    OLLAMA_CMD = ("ollama", "ps")

    def __init__(self, runner: Runner = default_runner, interval_s: float = 2.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
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
        return GpuSummary(
            samples=len(self.samples),
            errors=sum(s.error is not None for s in self.samples),
            max_gpu_use_percent=max(uses, default=None),
            max_vram_percent=max(vrams, default=None),
            min_gpu_share_percent=min(shares, default=None),
            cpu_offload_detected=any(sh < 100.0 for sh in shares),
            models_seen=sorted({p.model for s in self.samples for p in s.processes}),
        )


def assert_no_cpu_fallback(summary: GpuSummary, *, min_samples: int = 1) -> None:
    """Raise when the run silently fell back to CPU or could not be verified."""
    if summary.samples < min_samples or summary.min_gpu_share_percent is None:
        raise RuntimeError("GPU residency could not be verified (no `ollama ps` samples with a processor split)")
    if summary.cpu_offload_detected:
        raise RuntimeError(
            f"model offloaded to CPU: lowest GPU share {summary.min_gpu_share_percent:.0f}% "
            f"(models: {', '.join(summary.models_seen)})"
        )
