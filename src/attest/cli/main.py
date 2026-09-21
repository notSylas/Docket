import typer

from attest import __version__

app = typer.Typer(name="attest", help="Local-first, evidence-backed work intelligence assistant.")


@app.callback(invoke_without_command=True)
def main(
    version: bool = typer.Option(False, "--version", help="Show the version and exit."),
) -> None:
    if version:
        typer.echo(f"attest {__version__}")
        raise typer.Exit()


if __name__ == "__main__":
    app()
