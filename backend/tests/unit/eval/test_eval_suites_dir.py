"""`docket eval` default location of the eval suites: checkout, $DOCKET_EVAL_SUITES, working directory; never a path that
only exists in a source checkout when the package is installed elsewhere."""
from __future__ import annotations

from pathlib import Path

import pytest
import typer

from docket.eval import cli

REPO = Path(__file__).resolve().parents[4]


def test_source_checkout_default_is_evals_suites(monkeypatch):
    monkeypatch.delenv("DOCKET_EVAL_SUITES", raising=False)
    assert cli._suites_dir() == REPO / "evals" / "suites"
    assert (cli._suites_dir() / "gold.yaml").is_file()


def test_env_override_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("DOCKET_EVAL_SUITES", str(tmp_path))
    assert cli._suites_dir() == tmp_path.resolve()


def test_installed_package_finds_suites_from_the_working_directory(monkeypatch, tmp_path):
    monkeypatch.delenv("DOCKET_EVAL_SUITES", raising=False)
    # as if docket were installed in site-packages, far from any checkout
    monkeypatch.setattr(cli, "__file__", str(tmp_path / "venv" / "lib" / "python3.12" / "site-packages" / "docket" / "eval" / "cli.py"))
    work = tmp_path / "checkout" / "sub"
    (tmp_path / "checkout" / "evals" / "suites").mkdir(parents=True)
    work.mkdir(parents=True)
    monkeypatch.chdir(work)
    assert cli._suites_dir() == (tmp_path / "checkout" / "evals" / "suites").resolve()


def test_installed_package_outside_a_checkout_fails_clearly(monkeypatch, tmp_path):
    monkeypatch.delenv("DOCKET_EVAL_SUITES", raising=False)
    monkeypatch.setattr(cli, "__file__", str(tmp_path / "venv" / "lib" / "python3.12" / "site-packages" / "docket" / "eval" / "cli.py"))
    empty = tmp_path / "nowhere"
    empty.mkdir()
    monkeypatch.chdir(empty)
    with pytest.raises(typer.Exit):
        cli._suites_dir()
