from __future__ import annotations

import subprocess
import sys

from typer.testing import CliRunner

from docket.cli.main import app

runner = CliRunner()


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "docket" in result.stdout


def test_eval_subcommand_still_reachable_without_module_level_import() -> None:
    """`app`'s `eval` subcommand is mounted lazily (`_LazyEvalGroup` in
    `cli/main.py`) instead of via `app.add_typer(eval_app, ...)`, so it must
    still work end to end through the normal Typer/Click dispatch path."""
    result = runner.invoke(app, ["eval", "--help"])
    assert result.exit_code == 0
    assert "Measure answer accuracy" in result.output


def test_importing_cli_main_does_not_import_eval_cli() -> None:
    """`docket.cli.main`'s own top-level imports must not pull in
    `docket.eval.cli` -- only actually running the CLI (any subcommand, or
    `--help`) does, via `cli/main.py`'s `_LazyEvalGroup`. Regression guard
    for Phase 5 of the restructuring plan: `eval` and `cli` used to import
    each other at module top, which only avoided a hard circular-import
    failure by accident of `cli/main.py`'s own internal import order."""
    result = subprocess.run(
        [sys.executable, "-c", "import sys; import docket.cli.main; "
         "assert 'docket.eval.cli' not in sys.modules, sys.modules.keys()"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


def test_eval_runner_importable_before_cli_main_in_a_fresh_interpreter() -> None:
    """`eval/runner.py` used to subclass `docket.cli.context.AppContext` and
    reach into its private `_ensure_schema`, which only worked today because
    `cli/main.py` happened to import `docket.cli.context` before
    `docket.eval.cli` -- fragile import-order luck, not a real fix. Prove
    the fragility is gone by importing `docket.eval.runner` first, in a
    fresh interpreter that has never touched `docket.cli.main`, then
    importing `docket.cli.main` afterwards -- the order that specifically
    would not have been protected by `cli/main.py`'s own internal ordering."""
    result = subprocess.run(
        [sys.executable, "-c",
         "import docket.eval.runner; import docket.cli.main; print('ok')"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout
