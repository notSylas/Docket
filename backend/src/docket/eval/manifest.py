"""Baseline manifest: everything needed to say what a measurement measured.

Written next to every eval run (`<runs>.manifest.json`), copied into each
`RunRecord.manifest`, and printed in reports. Purely descriptive: it changes no
behaviour. Probing (git, rocm-smi, ollama --version) goes through an injectable
runner and tolerates missing tools.
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from importlib import metadata
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from docket.eval.schema import GoldSet, GoldSetError, fingerprint

MANIFEST_VERSION = 1
LANES = ("cpu", "gpu")

# Settings that change what the pipeline does (read via getattr; absent = skipped).
FLAG_SETTINGS = (
    "doc_routing", "field_lookup", "family_latest", "kag_scoping", "documents_build",
    "compute_stage_enabled", "retrieval_pool_k", "rewrite_enabled", "query_scope_enabled",
    "answer_think", "num_ctx", "reference_mode",
)
PACKAGES = ("pydantic", "sqlalchemy", "lancedb", "ollama", "typer", "pyyaml", "docling", "numpy", "pytest")
_SECRET_WORDS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")

Run = Callable[[Sequence[str]], str]


class BaselineManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = MANIFEST_VERSION
    lane: str = "cpu"
    created_at: str
    code_commit: str | None = None
    git_dirty: bool | None = None
    git_branch: str | None = None
    flags: dict[str, str] = Field(default_factory=dict)
    env_flags: dict[str, str] = Field(default_factory=dict)  # DOCKET_* variables set at run time
    models: dict[str, str] = Field(default_factory=dict)  # gen, embed, rewrite, vision
    judges: list[str] = Field(default_factory=list)
    self_judged: bool = False
    gold_path: str | None = None
    gold_fingerprint: str | None = None
    corpus_hashes_fingerprint: str | None = None
    corpus_files: int | None = None
    library: dict[str, Any] | None = None  # sizes and sources-per-document stats
    python_version: str = ""
    packages: dict[str, str] = Field(default_factory=dict)
    hardware: dict[str, str] = Field(default_factory=dict)  # GPU name, VRAM, ROCm, Ollama versions

    def fingerprint(self) -> str:
        """Digest of the identity-bearing fields (excludes timestamp)."""
        payload = self.model_dump(mode="json")
        payload.pop("created_at", None)
        return fingerprint(payload)


def _default_run(cmd: Sequence[str]) -> str:
    done = subprocess.run(list(cmd), capture_output=True, text=True, timeout=10, check=False)
    if done.returncode != 0:
        raise RuntimeError(f"{cmd[0]} exited {done.returncode}")
    return done.stdout


def _try(run: Run, cmd: Sequence[str]) -> str | None:
    try:
        return run(cmd).strip()
    except (OSError, RuntimeError, subprocess.SubprocessError):
        return None


def git_info(repo: Path, run: Run = _default_run) -> tuple[str | None, bool | None, str | None]:
    base = ["git", "-C", str(repo)]
    commit = _try(run, [*base, "rev-parse", "HEAD"])
    if not commit:
        return None, None, None
    status = _try(run, [*base, "status", "--porcelain"])
    branch = _try(run, [*base, "rev-parse", "--abbrev-ref", "HEAD"])
    return commit, (bool(status) if status is not None else None), branch


def hardware_info(run: Run = _default_run) -> dict[str, str]:
    """GPU name, VRAM, ROCm and Ollama versions; any that cannot be read are omitted."""
    info: dict[str, str] = {}
    out = _try(run, ["ollama", "--version"])
    if out:
        info["ollama_version"] = out.splitlines()[-1].strip()
    name = _try(run, ["rocm-smi", "--showproductname"])
    if name:
        for line in name.splitlines():
            if "Card series" in line or "Card Series" in line:
                info["gpu_name"] = line.split(":", 2)[-1].strip()
                break
    vram = _try(run, ["rocm-smi", "--showmeminfo", "vram"])
    if vram:
        for line in vram.splitlines():
            if "Total Memory" in line:
                digits = line.rsplit(":", 1)[-1].strip()
                if digits.isdigit():
                    info["vram_total_bytes"] = digits
                    break
    version = _try(run, ["rocm-smi", "--showdriverversion"])
    if version:
        info["rocm_driver"] = version.splitlines()[-1].strip() if version else ""
    for path in ("/opt/rocm/.info/version", "/opt/rocm/.info/version-dev"):
        try:
            info["rocm_version"] = Path(path).read_text().strip()
            break
        except OSError:
            continue
    return info


def package_versions(names: Sequence[str] = PACKAGES) -> dict[str, str]:
    out: dict[str, str] = {}
    for name in names:
        try:
            out[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            continue
    return out


def build_manifest(
    settings: Any,
    gold: GoldSet | None = None,
    *,
    lane: str = "cpu",
    judges: Sequence[str] = (),
    gold_path: str | Path | None = None,
    corpus: dict[str, str] | None = None,
    library: dict[str, Any] | None = None,
    repo: Path | None = None,
    run: Run = _default_run,
    env: dict[str, str] | None = None,
    probe_hardware: bool | None = None,
) -> BaselineManifest:
    if lane not in LANES:
        raise ValueError(f"lane must be one of {LANES}")
    repo = repo or Path(__file__).resolve().parents[3]
    commit, dirty, branch = git_info(repo, run)
    environ = os.environ if env is None else env
    gen = getattr(settings, "gen_model", None)
    models = {
        k: str(v)
        for k, v in {
            "gen": gen,
            "embed": getattr(settings, "embed_model", None),
            "rewrite": getattr(settings, "rewrite_model", None) or gen,
            "vision": getattr(settings, "vision_model", None),
        }.items()
        if v
    }
    flags = {n: str(getattr(settings, n)) for n in FLAG_SETTINGS if hasattr(settings, n)}
    env_flags = {
        k: v for k, v in sorted(environ.items())
        if k.startswith("DOCKET_") and not any(w in k for w in _SECRET_WORDS)
    }
    if probe_hardware is None:
        probe_hardware = lane == "gpu"
    return BaselineManifest(
        lane=lane,
        created_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        code_commit=commit, git_dirty=dirty, git_branch=branch,
        flags=flags, env_flags=env_flags, models=models,
        judges=list(judges), self_judged=bool(gen) and gen in judges,
        gold_path=str(gold_path) if gold_path else None,
        gold_fingerprint=fingerprint(gold) if gold is not None else None,
        corpus_hashes_fingerprint=fingerprint(corpus) if corpus is not None else None,
        corpus_files=len(corpus) if corpus is not None else None,
        library=library,
        python_version=f"{platform.python_implementation()} {sys.version.split()[0]}",
        packages=package_versions(),
        hardware=hardware_info(run) if probe_hardware else {},
    )


def write_manifest(manifest: BaselineManifest, path: Path, *, overwrite: bool = False) -> None:
    """Create-only by default (refuses to overwrite), mode 0600."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if overwrite:
        path.unlink(missing_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(manifest.model_dump_json(indent=2) + "\n")


def load_manifest(path: Path) -> BaselineManifest:
    try:
        return BaselineManifest.model_validate_json(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValidationError) as exc:
        raise GoldSetError(f"cannot read baseline manifest {path}: {exc}") from exc


def verify_manifest_gold(manifest: BaselineManifest, gold: GoldSet) -> None:
    if manifest.gold_fingerprint is not None and manifest.gold_fingerprint != fingerprint(gold):
        raise GoldSetError("gold set changed since this baseline manifest was written")


def manifest_lines(m: BaselineManifest | dict | None) -> list[str]:
    if m is None:
        return ["Run manifest: not recorded (run predates the baseline manifest)"]
    if isinstance(m, dict):
        m = BaselineManifest.model_validate(m)
    commit = (m.code_commit or "unknown")[:12] + (" (dirty)" if m.git_dirty else "")
    lines = [
        f"Run manifest: lane={m.lane}  commit={commit}  branch={m.git_branch or 'unknown'}  at {m.created_at}",
        "  models: " + (", ".join(f"{k}={v}" for k, v in m.models.items()) or "not recorded"),
        "  flags: " + (", ".join(f"{k}={v}" for k, v in m.flags.items()) or "not recorded"),
        f"  gold: {m.gold_path or 'n/a'} sha256:{(m.gold_fingerprint or 'n/a')[:12]}",
    ]
    if m.library:
        lines.append("  library: " + ", ".join(f"{k}={v}" for k, v in m.library.items()))
    if m.hardware:
        lines.append("  hardware: " + ", ".join(f"{k}={v}" for k, v in m.hardware.items()))
    lines.append(f"  python {m.python_version}")
    return lines
