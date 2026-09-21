from typer.testing import CliRunner

from attest.cli.main import app

runner = CliRunner()


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "attest" in result.stdout
