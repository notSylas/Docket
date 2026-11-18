"""Startup guard for the scale drivers (import-path hygiene).

Failure it prevents: running a driver with `python -I` (or from another venv)
silently imports the EDITABLE install of `docket` -- usually the main checkout
-- instead of this checkout, so a "worktree" measurement actually measures other
code. `check_docket_checkout()` runs BEFORE `docket` is imported by the caller's
own code and fails loudly (SystemExit) unless the importable `docket` package
lives under the expected `src/` directory.

Expected directory: env DOCKET_EXPECTED_SRC if set, else `<backend>/src` of the
checkout this file lives in. Honour PYTHONPATH by pointing it at that src.
"""
import importlib.util
import os
import sys
from pathlib import Path


def expected_src(here: Path | None = None) -> Path:
    override = os.environ.get("DOCKET_EXPECTED_SRC")
    if override:
        return Path(override).resolve()
    return ((here or Path(__file__)).resolve().parents[3] / "backend" / "src").resolve()


def check_docket_checkout(expected: Path | None = None, *, isolated: bool | None = None,
                          module_file: str | None = None) -> Path:
    """Return the verified docket package directory or raise SystemExit."""
    expected = (expected or expected_src()).resolve()
    isolated = sys.flags.isolated if isolated is None else isolated
    if module_file is None:
        spec = importlib.util.find_spec("docket")
        module_file = spec.origin if spec and spec.origin else None
    if module_file is None:
        raise SystemExit("scale guard: package 'docket' is not importable; set PYTHONPATH=%s" % expected)
    package_dir = Path(module_file).resolve().parent
    if expected not in package_dir.parents:
        hint = " (python -I ignores PYTHONPATH; do not use -I with an editable install)" if isolated else ""
        raise SystemExit(
            f"scale guard: 'docket' is imported from {package_dir}, expected under {expected}{hint}. "
            f"Run with PYTHONPATH={expected} and without -I."
        )
    return package_dir
