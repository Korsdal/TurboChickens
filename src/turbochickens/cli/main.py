"""tc — TurboChickens CLI entry point."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

from turbochickens.spec.loader import SpecValidationError, load_spec, spec_hash

app = typer.Typer(
    name="tc",
    help="TurboChickens — local-first platform for ephemeral, just-in-time AI agent jobs.",
    no_args_is_help=True,
    add_completion=False,
)
jobs_app = typer.Typer(help="Job spec commands.", no_args_is_help=True)
app.add_typer(jobs_app, name="jobs")

err_console = Console(stderr=True)


@jobs_app.command("validate")
def jobs_validate(
    spec_path: Path = typer.Argument(
        ...,
        help="Path to the job spec TOML file.",
        exists=True,
        dir_okay=False,
        readable=True,
    ),
) -> None:
    """Validate a job spec. Prints OK plus the stable spec hash, or field-named errors."""
    try:
        spec = load_spec(spec_path)
    except SpecValidationError as e:
        for issue in e.issues:
            err_console.print(f"[bold red]error[/bold red] {issue}")
        raise typer.Exit(code=1) from None
    digest = spec_hash(spec)
    typer.echo(f"OK: job '{spec.job.name}' — spec hash sha256:{digest}")


@app.callback()
def main() -> None:
    """TurboChickens CLI."""
