"""tc — TurboChickens CLI entry point."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from turbochickens.client import DaemonUnreachable, request
from turbochickens.daemon import DaemonAlreadyRunning, run_daemon
from turbochickens.runner import TERMINAL_STATES
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
runs_app = typer.Typer(help="Inspect runs.", no_args_is_help=True)
results_app = typer.Typer(help="Fetch validated results.", no_args_is_help=True)
app.add_typer(jobs_app, name="jobs")
app.add_typer(daemon_app, name="daemon")
app.add_typer(runs_app, name="runs")
app.add_typer(results_app, name="results")

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
    alerts = resp.get("alerts", [])
    if alerts:
        err_console.print(f"[bold red]alerts[/bold red]: {len(alerts)} terminal run(s) without a valid teardown attestation: {', '.join(alerts)}")
    else:
        typer.echo("alerts: 0")
    if in_flight:
        table = Table(title="In-flight runs")
        table.add_column("run_id")
        table.add_column("weight", justify="right")
        for r in in_flight:
            table.add_row(str(r["run_id"]), str(r["weight"]))
        console.print(table)


def _daemon_request(settings_path: Path | None, op: str, timeout: float = 5.0, **payload) -> dict:
    """Load settings and round-trip one request to the daemon; exit 1 on failure."""
    try:
        settings = load_settings(settings_path)
        return request(settings.socket_path, op, timeout=timeout, **payload)
    except (SettingsError, DaemonUnreachable) as e:
        err_console.print(f"[bold red]error[/bold red] {e}")
        raise typer.Exit(code=1) from None


def _request_or_exit(settings_path: Path | None, op: str, timeout: float = 5.0, **payload) -> dict:
    resp = _daemon_request(settings_path, op, timeout=timeout, **payload)
    if not resp.get("ok"):
        err_console.print(f"[bold red]error[/bold red] {resp.get('error', 'unknown error')}")
        raise typer.Exit(code=1)
    return resp


@app.command("run")
def run_cmd(
    spec_path: Path = typer.Argument(
        ...,
        help="Path to the job spec TOML file.",
        exists=True,
        dir_okay=False,
        readable=True,
    ),
    wait: bool = typer.Option(False, "--wait", help="Wait until the run reaches a terminal state."),
    settings_path: Path | None = _settings_opt(),
) -> None:
    """Submit a manual run of a job to the daemon queue (over the socket)."""
    try:
        spec = load_spec(spec_path)
    except SpecValidationError as e:
        for issue in e.issues:
            err_console.print(f"[bold red]error[/bold red] {issue}")
        raise typer.Exit(code=1) from None
    digest = spec_hash(spec)
    resp = _request_or_exit(
        settings_path, "enqueue", timeout=10.0, spec_path=str(spec_path.resolve()), spec_hash=digest, trigger="manual"
    )
    run_id = resp["run_id"]
    typer.echo(
        f"queued run {run_id} job '{resp['job_id']}' status=queued "
        f"weight={resp['weight']} queue_depth={resp['queue_depth']}"
    )
    if not wait:
        return
    deadline = time.time() + 3600
    while time.time() < deadline:
        show = _request_or_exit(settings_path, "runs_show", run_id=run_id)
        status = show["run"]["status"]
        if status in TERMINAL_STATES:
            reason = (show["run"].get("manifest") or {}).get("status_reason", "")
            typer.echo(f"run {run_id} → {status}" + (f" ({reason})" if reason else ""))
            raise typer.Exit(code=0 if status == "succeeded" else 1)
        time.sleep(0.2)
    err_console.print(f"[bold red]error[/bold red] run {run_id} did not finish in time")
    raise typer.Exit(code=1)


@runs_app.command("list")
def runs_list(
    settings_path: Path | None = _settings_opt(),
) -> None:
    """List runs, most recent first, one line per run (round-trip over the socket)."""
    resp = _request_or_exit(settings_path, "runs_list")
    if not resp["runs"]:
        typer.echo("no runs")
        return
    for r in resp["runs"]:
        status = r["status"]
        attested = "yes" if r["attested"] else "no"
        if r["status"] in TERMINAL_STATES and not r["attested"]:
            status += " ALERT:no-attestation"
        typer.echo(
            f"{r['id']}  job={r['job_name']}  status={status}  trigger={r['trigger']}  "
            f"started={r['started_at'] or '-'}  finished={r['finished_at'] or '-'}  attested={attested}"
        )


@runs_app.command("show")
def runs_show(
    run_id: str = typer.Argument(..., help="Run ID, e.g. run-0123abcd4567."),
    settings_path: Path | None = _settings_opt(),
) -> None:
    """Show one run: lifecycle fields, manifest, attestation."""
    resp = _request_or_exit(settings_path, "runs_show", run_id=run_id)
    r = resp["run"]
    typer.echo(f"run: {r['id']}")
    typer.echo(f"job: {r['job_name']} ({r['job_id']})")
    typer.echo(f"status: {r['status']}")
    typer.echo(f"trigger: {r['trigger']}")
    typer.echo(f"started_at: {r['started_at']}")
    typer.echo(f"finished_at: {r['finished_at']}")
    if r["manifest"]:
        typer.echo("manifest:")
        typer.echo(json.dumps(r["manifest"], indent=2, sort_keys=True))
    if r["attestation"]:
        typer.echo(
            "attestation: process_reaped={process_reaped} workspace_removed={workspace_removed} "
            "env_cleared={env_cleared} verified_at={verified_at}".format(**r["attestation"])
        )
    elif r["status"] in TERMINAL_STATES:
        err_console.print("[bold red]ALERT: terminal run without a teardown attestation[/bold red]")
    if r.get("result"):
        res = r["result"]
        if res["valid"]:
            typer.echo(f"result: valid (artifacts: {res['artifacts_dir']})")
        else:
            typer.echo(f"result: QUARANTINED — output failed schema validation (artifacts: {res['artifacts_dir']})")
            for e in res["schema_errors"] or []:
                typer.echo(f"  schema error: {e}")


@runs_app.command("logs")
def runs_logs(
    run_id: str = typer.Argument(..., help="Run ID, e.g. run-0123abcd4567."),
    settings_path: Path | None = _settings_opt(),
) -> None:
    """Print the run's streamed events (sanitized log lines + state transitions)."""
    resp = _request_or_exit(settings_path, "runs_logs", run_id=run_id)
    for e in resp["events"]:
        typer.echo(f"{e['seq']:>4} {e['ts']} [{e['level']}] {e['source']}: {e['line']}")


@results_app.command("get")
def results_get(
    run_id: str = typer.Argument(..., help="Run ID whose validated result to fetch."),
    settings_path: Path | None = _settings_opt(),
) -> None:
    """Print the schema-validated result of a run as JSON.

    Only valid results are ever returned. A quarantined run (output failed
    schema validation) exits 1 with the recorded schema errors — its output is
    kept in quarantine for inspection, never served as a result.
    """
    resp = _daemon_request(settings_path, "results_get", run_id=run_id)
    if not resp.get("ok"):
        err_console.print(f"[bold red]error[/bold red] {resp.get('error', 'unknown error')}")
        if resp.get("quarantined"):
            for e in resp.get("schema_errors", []):
                err_console.print(f"  schema error: {e}")
            if resp.get("artifacts_dir"):
                err_console.print(f"  quarantined artifacts: {resp['artifacts_dir']}")
        raise typer.Exit(code=1) from None
    typer.echo(json.dumps(resp["output"], indent=2, sort_keys=True, ensure_ascii=False))


@app.callback()
def main() -> None:
    """TurboChickens CLI."""
