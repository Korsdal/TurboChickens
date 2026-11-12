"""tc — TurboChickens CLI entry point."""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from turbochickens.client import DaemonUnreachable, request
from turbochickens.daemon import DaemonAlreadyRunning, run_daemon
from turbochickens.settings import SettingsError, load_settings
from turbochickens.spec.loader import SpecValidationError, load_spec, spec_hash

app = typer.Typer(
    name="tc",
    help="TurboChickens — local-first platform for ephemeral, just-in-time AI agent jobs.",
    no_args_is_help=True,
    add_completion=False,
)
jobs_app = typer.Typer(help="Job spec commands.", no_args_is_help=True)
daemon_app = typer.Typer(help="Daemon lifecycle commands.", no_args_is_help=True)
app.add_typer(jobs_app, name="jobs")
app.add_typer(daemon_app, name="daemon")

err_console = Console(stderr=True)
console = Console()


def _settings_opt() -> typer.OptionInfo:
    return typer.Option(
        None,
        "--settings",
        help="Settings TOML path (default: $TURBOCHICKENS_SETTINGS or ~/.turbochickens/settings.toml).",
        envvar="TURBOCHICKENS_SETTINGS",
        dir_okay=False,
    )


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


@daemon_app.command("start")
def daemon_start(
    settings_path: Path | None = _settings_opt(),
) -> None:
    """Start the supervisor daemon in the foreground, binding the Unix socket."""
    try:
        settings = load_settings(settings_path)
    except SettingsError as e:
        err_console.print(f"[bold red]error[/bold red] {e}")
        raise typer.Exit(code=1) from None
    typer.echo(
        f"daemon starting: socket={settings.socket_path} "
        f"db={settings.db_path} audit_log={settings.audit_log_path} "
        f"weight_budget={settings.weight_budget}"
    )
    try:
        asyncio.run(run_daemon(settings))
    except DaemonAlreadyRunning as e:
        err_console.print(f"[bold red]error[/bold red] {e}")
        raise typer.Exit(code=1) from None
    typer.echo("daemon stopped")


@daemon_app.command("stop")
def daemon_stop(
    settings_path: Path | None = _settings_opt(),
) -> None:
    """Ask a running daemon to shut down gracefully over the socket."""
    try:
        settings = load_settings(settings_path)
        resp = request(settings.socket_path, "shutdown", timeout=5.0)
    except (SettingsError, DaemonUnreachable) as e:
        err_console.print(f"[bold red]error[/bold red] {e}")
        raise typer.Exit(code=1) from None
    if not resp.get("ok"):
        err_console.print(f"[bold red]error[/bold red] {resp.get('error', 'unknown error')}")
        raise typer.Exit(code=1)
    typer.echo("daemon stopped")


@app.command("status")
def status(
    settings_path: Path | None = _settings_opt(),
) -> None:
    """Show daemon health, queue depth, and in-flight runs (round-trip over the socket)."""
    try:
        settings = load_settings(settings_path)
        resp = request(settings.socket_path, "status", timeout=5.0)
    except (SettingsError, DaemonUnreachable) as e:
        err_console.print(f"[bold red]error[/bold red] {e}")
        raise typer.Exit(code=1) from None
    if not resp.get("ok"):
        err_console.print(f"[bold red]error[/bold red] {resp.get('error', 'unknown error')}")
        raise typer.Exit(code=1)
    d = resp["daemon"]
    typer.echo(
        f"daemon: up (pid={d['pid']}, version={d['version']}, "
        f"uptime={d['uptime_sec']}s, started_at={d['started_at']})"
    )
    typer.echo(f"socket: {resp['socket']}")
    typer.echo(f"db: {resp['db_path']}")
    typer.echo(f"audit_log: {resp['audit_log']}")
    typer.echo(f"weight_budget: {resp['weight_budget']}")
    typer.echo(f"queue_depth: {resp['queue_depth']}")
    in_flight = resp["in_flight"]
    typer.echo(f"in_flight: {len(in_flight)}")
    if in_flight:
        table = Table(title="In-flight runs")
        table.add_column("run_id")
        table.add_column("weight", justify="right")
        for r in in_flight:
            table.add_row(str(r["run_id"]), str(r["weight"]))
        console.print(table)


@app.callback()
def main() -> None:
    """TurboChickens CLI."""
