"""tc — TurboChickens CLI entry point."""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from turbochickens.client import DaemonUnreachable, request
from turbochickens.daemon import DaemonAlreadyRunning, run_daemon
from turbochickens.models import list_provider_catalogs, model_issues, vibe_catalog
from turbochickens.providers import ProvidersError, load_providers
from turbochickens.runner import TERMINAL_STATES
from turbochickens.seat import SeatBroker, SeatError
from turbochickens.settings import SettingsError, load_settings
from turbochickens.spec.loader import SpecValidationError, load_spec, spec_hash

app = typer.Typer(
    name="tc",
    help="TurboChickens — local-first platform for ephemeral, just-in-time AI agent jobs (eggs hatched into chickens).",
    no_args_is_help=True,
    add_completion=False,
)
jobs_app = typer.Typer(help="Egg (job spec) commands — candling.", no_args_is_help=True)
daemon_app = typer.Typer(help="Coopd lifecycle commands (the supervisor daemon).", no_args_is_help=True)
runs_app = typer.Typer(help="Inspect and steer chickens (runs) — list/show/logs/stop/rerun.", no_args_is_help=True)
results_app = typer.Typer(help="Fetch clutches (validated results) from the nest box.", no_args_is_help=True)
schedule_app = typer.Typer(help="Manage the rooster's crows (cron schedules).", no_args_is_help=True)
stats_app = typer.Typer(help="Feed ledger — per-egg usage rollups (spec 08).", no_args_is_help=True)
auth_app = typer.Typer(help="Perch — seat auth (spec 09); local, no coopd needed.", no_args_is_help=True)
models_app = typer.Typer(help="Feeders — model discovery (spec 10); local, no coopd needed.", no_args_is_help=True)
coop_app = typer.Typer(help="Coop — the TurboChickens GUI served by coopd over HTTP (spec 12).", no_args_is_help=True)
farmhand_app = typer.Typer(
    help="Farmhand — the MCP server over the farm's own socket ops (spec 26): stdio and/or HTTPS.",
    no_args_is_help=True,
)
connectors_app = typer.Typer(
    help="Gates — the connectors registry (connectors.toml); chickens opt in via [harness] connectors (spec 28).",
    no_args_is_help=True,
)
app.add_typer(jobs_app, name="jobs")
app.add_typer(daemon_app, name="daemon")
app.add_typer(runs_app, name="runs")
app.add_typer(results_app, name="results")
app.add_typer(schedule_app, name="schedule")
app.add_typer(stats_app, name="stats")
app.add_typer(auth_app, name="auth")
app.add_typer(models_app, name="models")
app.add_typer(coop_app, name="coop")
app.add_typer(farmhand_app, name="farmhand")
app.add_typer(connectors_app, name="connectors")

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
    settings_path: Path | None = _settings_opt(),
) -> None:
    """Candle an egg (validate a job spec). Prints OK plus the stable spec hash, or field-named errors.

    Spec 10: also checks harness.model against the chosen harness — an unknown
    provider/model errors (exit 1), a mistral-api model without a pricing
    preset in providers.toml only warns (the live catalog may offer it; see
    `tc models list`)."""
    try:
        spec = load_spec(spec_path)
    except SpecValidationError as e:
        for issue in e.issues:
            err_console.print(f"[bold red]error[/bold red] {issue}")
        raise typer.Exit(code=1) from None
    try:
        settings = load_settings(settings_path)
        providers = load_providers(settings.providers_path, settings.data_dir)
    except (SettingsError, ProvidersError) as e:
        err_console.print(f"[bold red]error[/bold red] {e}")
        raise typer.Exit(code=1) from None
    vibe_models = None
    if spec.harness.adapter == "vibe-cli":
        catalog = vibe_catalog()
        if catalog.reachable:
            vibe_models = catalog.models
    errors, warnings = model_issues(spec, providers, vibe_models)
    for w in warnings:
        err_console.print(f"[yellow]warning[/yellow] {w}")
    if errors:
        for e in errors:
            err_console.print(f"[bold red]error[/bold red] {e}")
        raise typer.Exit(code=1) from None
    digest = spec_hash(spec)
    typer.echo(f"OK: egg '{spec.job.name}' candled — spec hash sha256:{digest}")


@models_app.command("list")
def models_list(
    adapter: str = typer.Option(
        "mistral-api",
        "--adapter",
        help="Which catalog to list: 'mistral-api' = every provider in providers.toml via GET {base_url}/models; 'vibe-cli' = the models the vibe binary itself offers.",
    ),
    settings_path: Path | None = _settings_opt(),
) -> None:
    """List usable models from the feeders, on demand (local, no coopd state).

    Per provider: reachable or not, the live model ids from GET /models, and
    which of them have a pricing preset in providers.toml. Credentials match
    spec 05/09 (allowlisted env var, or the seat session token) and are never
    printed. Unreachable providers are marked, not fatal — exit code stays 0.
    """
    if adapter not in ("mistral-api", "vibe-cli"):
        err_console.print(f"[bold red]error[/bold red] --adapter must be 'mistral-api' or 'vibe-cli', got {adapter!r}")
        raise typer.Exit(code=1)
    try:
        settings = load_settings(settings_path)
        providers = load_providers(settings.providers_path, settings.data_dir)
    except (SettingsError, ProvidersError) as e:
        err_console.print(f"[bold red]error[/bold red] {e}")
        raise typer.Exit(code=1) from None
    if adapter == "vibe-cli":
        catalog = vibe_catalog()
        line = f"adapter=vibe-cli  bin={catalog.bin}  version={catalog.version}  reachable={'yes' if catalog.reachable else 'no'}"
        if catalog.reachable:
            line += f"  models={len(catalog.models)}"
        else:
            line += f"  error={catalog.error}"
        typer.echo(line)
        for m in catalog.models:
            typer.echo(f"  {m}")
        return
    broker = SeatBroker(settings, providers)

    def token_for(name: str, provider) -> tuple[str | None, str]:
        if provider.auth == "seat":
            return broker.bearer_token(name), "seat"
        token = os.environ.get(provider.api_key_env)
        return token, "env" if token else "none"

    rows = list_provider_catalogs(providers, token_for)
    for r in rows:
        line = (
            f"provider={r.provider}  auth={r.auth}  cred={r.cred}  base_url={r.base_url}  "
            f"reachable={'yes' if r.reachable else 'no'}"
        )
        if r.reachable:
            line += f"  models={len(r.models)}"
        else:
            line += f"  error={r.error}"
        typer.echo(line)
        for m in r.models:
            typer.echo(f"  {m.id}  [{'priced' if m.priced else 'no-pricing-preset'}]")


@daemon_app.command("start")
def daemon_start(
    settings_path: Path | None = _settings_opt(),
) -> None:
    """Start coopd in the foreground, binding the Unix socket."""
    try:
        settings = load_settings(settings_path)
    except SettingsError as e:
        err_console.print(f"[bold red]error[/bold red] {e}")
        raise typer.Exit(code=1) from None
    typer.echo(
        f"coopd starting: socket={settings.socket_path} "
        f"db={settings.db_path} audit_log={settings.audit_log_path} "
        f"weight_budget={settings.weight_budget}"
    )
    try:
        asyncio.run(run_daemon(settings))
    except DaemonAlreadyRunning as e:
        err_console.print(f"[bold red]error[/bold red] {e}")
        raise typer.Exit(code=1) from None
    typer.echo("coopd stopped")


@coop_app.command("start")
def coop_start(
    settings_path: Path | None = _settings_opt(),
) -> None:
    """Start coopd with the coop GUI enabled — HTTP at coop_host:coop_port serving / and /api/*.

    Identical to `tc daemon start` with coop_enabled = true in settings (the coop
    container's CMD). The bearer token is required on every bind by default; loopback
    with no coop_auth_token is open only with the coop_loopback_open opt-in. When a
    token is required and none configured, an ephemeral one is printed once to stdout."""
    from turbochickens.coop import coop_auth

    try:
        settings = load_settings(settings_path)
    except SettingsError as e:
        err_console.print(f"[bold red]error[/bold red] {e}")
        raise typer.Exit(code=1) from None
    settings = settings.model_copy(update={"coop_enabled": True})
    auth = coop_auth(settings)
    typer.echo(
        f"coopd starting: socket={settings.socket_path} "
        f"db={settings.db_path} audit_log={settings.audit_log_path} "
        f"weight_budget={settings.weight_budget}"
    )
    typer.echo(
        f"coop: gui at {settings.coop_url} auth={'required (bearer token)' if auth.required else 'open (loopback opt-in coop_loopback_open, no token configured)'}"
    )
    try:
        asyncio.run(run_daemon(settings))
    except DaemonAlreadyRunning as e:
        err_console.print(f"[bold red]error[/bold red] {e}")
        raise typer.Exit(code=1) from None
    typer.echo("coopd stopped")


@daemon_app.command("stop")
def daemon_stop(
    settings_path: Path | None = _settings_opt(),
) -> None:
    """Ask a running coopd to shut down gracefully over the socket."""
    try:
        settings = load_settings(settings_path)
        resp = request(settings.socket_path, "shutdown", timeout=5.0)
    except (SettingsError, DaemonUnreachable) as e:
        err_console.print(f"[bold red]error[/bold red] {e}")
        raise typer.Exit(code=1) from None
    if not resp.get("ok"):
        err_console.print(f"[bold red]error[/bold red] {resp.get('error', 'unknown error')}")
        raise typer.Exit(code=1)
    typer.echo("coopd stopped")


@app.command("status")
def status(
    settings_path: Path | None = _settings_opt(),
) -> None:
    """Show coopd health, pecking-order depth, and in-flight chickens (round-trip over the socket)."""
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
        f"coopd: up (pid={d['pid']}, version={d['version']}, "
        f"uptime={d['uptime_sec']}s, started_at={d['started_at']})"
    )
    # spec 32 (G17): the socket payload serves data-dir-relative identifiers
    # only; the local CLI prints the operator's own absolute paths (local-only)
    typer.echo(f"socket: {settings.socket_path}")
    typer.echo(f"db: {settings.db_path}")
    typer.echo(f"audit_log: {settings.audit_log_path}")
    typer.echo(f"weight_budget: {resp['weight_budget']}")
    typer.echo(f"pecking order: {resp['queue_depth']}")
    in_flight = resp["in_flight"]
    typer.echo(f"in_flight: {len(in_flight)}")
    alerts = resp.get("alerts", [])
    if alerts:
        err_console.print(
            f"[bold red]alerts[/bold red]: {len(alerts)} chicken(s) needing attention"
            f" (terminal without an abattoir attestation, or stranded non-terminal): {', '.join(alerts)}"
        )
    else:
        typer.echo("alerts: 0")
    retention = resp.get("retention") or {}
    if retention:
        typer.echo(
            f"retention: audit_log={retention.get('audit_retention')} (the headcount, forever) "
            f"results_retention_months={retention.get('results_retention_months')} (clutches rot after that many months)"
        )
    coop = resp.get("coop") or {}
    if coop.get("enabled"):
        typer.echo(
            f"coop: {coop.get('url')} auth={'required' if coop.get('auth_required') else 'open'}"
        )
    if in_flight:
        table = Table(title="In-flight chickens")
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
    """Queue an egg (a job spec) — the Hatchery hatches it into a chicken — over the socket."""
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
        f"egg queued {run_id} (job '{resp['job_id']}') — pecking order depth {resp['queue_depth']}, weight {resp['weight']}"
    )
    if not wait:
        return
    deadline = time.time() + 3600
    while time.time() < deadline:
        show = _request_or_exit(settings_path, "runs_show", run_id=run_id)
        status = show["run"]["status"]
        if status in TERMINAL_STATES:
            reason = (show["run"].get("manifest") or {}).get("status_reason", "")
            typer.echo(f"chicken {run_id} → {status}" + (f" ({reason})" if reason else ""))
            raise typer.Exit(code=0 if status == "succeeded" else 1)
        time.sleep(0.2)
    err_console.print(f"[bold red]error[/bold red] chicken {run_id} did not finish in time")
    raise typer.Exit(code=1)


@runs_app.command("list")
def runs_list(
    settings_path: Path | None = _settings_opt(),
) -> None:
    """List chickens, most recent first, one line per chicken (round-trip over the socket)."""
    resp = _request_or_exit(settings_path, "runs_list")
    if not resp["runs"]:
        typer.echo("no chickens yet")
        return
    for r in resp["runs"]:
        status = r["status"]
        attested = "yes" if r["attested"] else "no"
        if r["status"] in TERMINAL_STATES and not r["attested"]:
            status += " ALERT:no-attestation"
        line = (
            f"chicken={r['id']}  egg={r['job_name']}  status={status}  trigger={r['trigger']}  "
            f"started={r['started_at'] or '-'}  finished={r['finished_at'] or '-'}  attested={attested}"
        )
        if r.get("status_reason"):
            line += f"  reason={r['status_reason']}"
        typer.echo(line)


@runs_app.command("show")
def runs_show(
    run_id: str = typer.Argument(..., help="Chicken ID, e.g. run-0123abcd4567."),
    settings_path: Path | None = _settings_opt(),
) -> None:
    """Show one chicken: lifecycle fields, manifest, abattoir attestation."""
    resp = _request_or_exit(settings_path, "runs_show", run_id=run_id)
    r = resp["run"]
    typer.echo(f"chicken: {r['id']}")
    typer.echo(f"egg: {r['job_name']} ({r['job_id']})")
    typer.echo(f"status: {r['status']}")
    typer.echo(f"trigger: {r['trigger']}")
    typer.echo(f"started_at: {r['started_at']}")
    typer.echo(f"finished_at: {r['finished_at']}")
    if r["manifest"]:
        typer.echo("manifest:")
        typer.echo(json.dumps(r["manifest"], indent=2, sort_keys=True))
    if r["attestation"]:
        typer.echo(
            "abattoir attestation: process_reaped={process_reaped} workspace_removed={workspace_removed} "
            "env_cleared={env_cleared} verified_at={verified_at}".format(**r["attestation"])
        )
    elif r["status"] in TERMINAL_STATES:
        err_console.print("[bold red]ALERT: terminal chicken without an abattoir attestation[/bold red]")
    if r.get("result"):
        res = r["result"]
        # spec 32 (G17): the served payload carries no artifacts_dir — the
        # local CLI derives the operator's own path from its local settings
        local_settings = load_settings(settings_path)
        if res["valid"]:
            typer.echo(f"clutch: valid (nest box: {local_settings.results_dir / run_id})")
        else:
            typer.echo(
                f"clutch: QUARANTINED — failed candling, schema errors below"
                f" (quarantine: {local_settings.quarantine_dir / run_id})"
            )
            for e in res["schema_errors"] or []:
                typer.echo(f"  schema error: {e}")


@runs_app.command("logs")
def runs_logs(
    run_id: str = typer.Argument(..., help="Chicken ID, e.g. run-0123abcd4567."),
    settings_path: Path | None = _settings_opt(),
) -> None:
    """Print the chicken's streamed events (sanitized log lines + state transitions).

    The log is the bounded tail (spec 32: the per-chicken event log is capped);
    a capped or cut-off tail is noted on stderr."""
    resp = _request_or_exit(settings_path, "runs_logs", run_id=run_id)
    for e in resp["events"]:
        typer.echo(f"{e['seq']:>4} {e['ts']} [{e['level']}] {e['source']}: {e['line']}")
    if resp.get("capped"):
        typer.echo(
            f"the fence capped this chicken's log at {resp['total']} events — only the tail is kept",
            err=True,
        )
    elif resp.get("truncated"):
        typer.echo(
            f"showing the last {len(resp['events'])} of {resp['total']} events (tail)",
            err=True,
        )


@runs_app.command("stop")
def runs_stop(
    run_id: str = typer.Argument(..., help="Chicken ID, e.g. run-0123abcd4567."),
    settings_path: Path | None = _settings_opt(),
) -> None:
    """Stop a chicken deliberately (spec 22): a queued egg is cancelled (it never hatches);
    a running chicken gets SIGTERM, then a hard kill, with the abattoir's attested teardown.

    Talks to the same `run_stop` socket op the GUI/Farmhand will use; waits for
    the terminal state and exits 0, or exits 1 for an unknown or already-terminal chicken."""
    resp = _request_or_exit(settings_path, "run_stop", timeout=60.0, run_id=run_id)
    line = f"chicken {resp['run_id']} → {resp['status']}"
    if resp.get("reason"):
        line += f" ({resp['reason']})"
    typer.echo(line)


@runs_app.command("rerun")
def runs_rerun(
    run_id: str = typer.Argument(..., help="Chicken ID to re-run, e.g. run-0123abcd4567."),
    settings_path: Path | None = _settings_opt(),
) -> None:
    """Re-queue the same egg as a fresh chicken (spec 22): new run ID, lineage recorded
    (`rerun_of` in the manifest and the Headcount), through the normal queue/admission/pipeline."""
    resp = _request_or_exit(settings_path, "run_rerun", timeout=10.0, run_id=run_id)
    typer.echo(
        f"egg re-queued {resp['run_id']} (job '{resp['job_id']}') — re-run of {run_id}, "
        f"pecking order depth {resp['queue_depth']}, weight {resp['weight']}"
    )


@results_app.command("get")
def results_get(
    run_id: str = typer.Argument(..., help="Chicken ID whose clutch to fetch from the nest box."),
    settings_path: Path | None = _settings_opt(),
) -> None:
    """Print the chicken's candled clutch (schema-validated result) as JSON.

    Only valid clutches are ever returned. A quarantined chicken (its clutch
    failed candling) exits 1 with the recorded schema errors — the clutch is
    kept in quarantine for inspection, never served as a result.
    """
    resp = _daemon_request(settings_path, "results_get", run_id=run_id)
    if not resp.get("ok"):
        err_console.print(f"[bold red]error[/bold red] {resp.get('error', 'unknown error')}")
        if resp.get("quarantined"):
            for e in resp.get("schema_errors", []):
                err_console.print(f"  schema error: {e}")
            # spec 32 (G17): the served payload carries no artifacts_dir — the
            # local CLI derives the operator's own quarantine path from settings
            local_settings = load_settings(settings_path)
            err_console.print(f"  quarantined clutch kept at: {local_settings.quarantine_dir / run_id}")
        if resp.get("expired"):
            err_console.print("  the clutch was composted under the retention TTL; the append-only index row remains")
        raise typer.Exit(code=1) from None
    typer.echo(json.dumps(resp["output"], indent=2, sort_keys=True, ensure_ascii=False))


@schedule_app.command("add")
def schedule_add(
    spec_path: Path = typer.Argument(
        ...,
        help="Path to the job spec TOML file (its [trigger] must be cron with a schedule).",
        exists=True,
        dir_okay=False,
        readable=True,
    ),
    settings_path: Path | None = _settings_opt(),
) -> None:
    """Register an egg's cron trigger as a crow with the rooster (coopd's scheduler)."""
    try:
        spec = load_spec(spec_path)
    except SpecValidationError as e:
        for issue in e.issues:
            err_console.print(f"[bold red]error[/bold red] {issue}")
        raise typer.Exit(code=1) from None
    if spec.trigger.type != "cron" or not spec.trigger.schedule:
        err_console.print(
            f"[bold red]error[/bold red] egg '{spec.job.name}' has trigger.type="
            f"'{spec.trigger.type}'; only cron-triggered eggs carry a crow"
        )
        raise typer.Exit(code=1)
    digest = spec_hash(spec)
    resp = _request_or_exit(
        settings_path,
        "schedule_add",
        timeout=10.0,
        spec_path=str(spec_path.resolve()),
        spec_hash=digest,
    )
    typer.echo(
        f"crow scheduled for egg '{resp['job_id']}' cron='{resp['schedule']}' "
        f"spec=sha256:{resp['spec_hash']} next_run={resp.get('next_run') or '-'}"
    )


@schedule_app.command("list")
def schedule_list(
    settings_path: Path | None = _settings_opt(),
) -> None:
    """List the rooster's registered crows with their next crow time."""
    resp = _request_or_exit(settings_path, "schedule_list")
    if not resp["schedules"]:
        typer.echo("no crows yet")
        return
    for s in resp["schedules"]:
        typer.echo(
            f"egg={s['job_id']}  cron='{s['schedule']}'  next_run={s.get('next_run') or '-'}  "
            f"added_at={s['added_at']}  spec=sha256:{s['spec_hash'][:12]}…"
        )


@schedule_app.command("rm")
def schedule_rm(
    job_id: str = typer.Argument(..., help="Egg name (id) whose crow to remove."),
    settings_path: Path | None = _settings_opt(),
) -> None:
    """Remove an egg's crow from the rooster."""
    resp = _request_or_exit(settings_path, "schedule_rm", job_id=job_id)
    typer.echo(f"removed crow for egg '{resp['job_id']}'")


def _fmt_stat(v) -> str:
    return "-" if v is None else f"{v:.6g}"


def _stats_line(s: dict) -> str:
    return (
        f"egg={s['job_name']}  n={s['n_runs']}  "
        f"tokens_in mean={_fmt_stat(s['tokens_in_mean'])} p50={_fmt_stat(s['tokens_in_p50'])}"
        f" p95={_fmt_stat(s['tokens_in_p95'])}  "
        f"tokens_out mean={_fmt_stat(s['tokens_out_mean'])} p50={_fmt_stat(s['tokens_out_p50'])}"
        f" p95={_fmt_stat(s['tokens_out_p95'])}  tokens_total_p95={_fmt_stat(s['tokens_p95'])}  "
        f"expected_cost_usd={_fmt_stat(s['expected_cost_usd'])}  updated_at={s['updated_at']}"
    )


@stats_app.command("list")
def stats_list(
    settings_path: Path | None = _settings_opt(),
) -> None:
    """List the feed ledger: per-egg rollups (n, mean/p50/p95 tokens in+out, expected feed per chicken)."""
    resp = _request_or_exit(settings_path, "stats_list")
    if not resp["stats"]:
        typer.echo("no feed recorded yet")
        return
    for s in resp["stats"]:
        typer.echo(_stats_line(s))


@stats_app.command("show")
def stats_show(
    job_id: str = typer.Argument(..., help="Egg name (id) whose feed ledger to show."),
    settings_path: Path | None = _settings_opt(),
) -> None:
    """Show one egg's feed ledger in detail: n, mean/p50/p95 tokens in+out, expected feed."""
    resp = _request_or_exit(settings_path, "stats_show", job_id=job_id)
    s = resp["stats"]
    typer.echo(f"egg: {s['job_name']} ({s['job_id']})")
    typer.echo(f"n_runs: {s['n_runs']}")
    typer.echo(
        f"tokens_in:  mean={_fmt_stat(s['tokens_in_mean'])} p50={_fmt_stat(s['tokens_in_p50'])}"
        f" p95={_fmt_stat(s['tokens_in_p95'])}"
    )
    typer.echo(
        f"tokens_out: mean={_fmt_stat(s['tokens_out_mean'])} p50={_fmt_stat(s['tokens_out_p50'])}"
        f" p95={_fmt_stat(s['tokens_out_p95'])}"
    )
    typer.echo(f"tokens_total_p95: {_fmt_stat(s['tokens_p95'])}")
    typer.echo(f"expected_cost_usd_per_run: {_fmt_stat(s['expected_cost_usd'])}")
    typer.echo(f"updated_at: {s['updated_at']}")


def _seat_broker(settings_path: Path | None) -> tuple[SeatBroker, object]:
    """Load settings + providers and build the seat broker (local, no daemon)."""
    settings = load_settings(settings_path)
    providers = load_providers(settings.providers_path, settings.data_dir)
    return SeatBroker(settings, providers), settings


@auth_app.command("login")
def auth_login(
    provider: str = typer.Argument(..., help="Provider name from providers.toml with auth = \"seat\"."),
    settings_path: Path | None = _settings_opt(),
) -> None:
    """Claim a perch seat: run the OAuth device flow for a seat provider and store the session under the data dir."""
    try:
        broker, _ = _seat_broker(settings_path)
        session = broker.login(provider, notify=typer.echo)
    except (SettingsError, ProvidersError, SeatError) as e:
        err_console.print(f"[bold red]error[/bold red] {e}")
        raise typer.Exit(code=1) from None
    from turbochickens.seat import _iso

    typer.echo(
        f"perch seat claimed for provider '{provider}' "
        f"(token expires { _iso(session.expires_at) }) — tokens are never printed or logged"
    )


@auth_app.command("status")
def auth_status(
    settings_path: Path | None = _settings_opt(),
) -> None:
    """Show each feeder's auth mode and stored perch seat expiry (no tokens)."""
    try:
        broker, _ = _seat_broker(settings_path)
        rows = broker.status()
    except (SettingsError, ProvidersError, SeatError) as e:
        err_console.print(f"[bold red]error[/bold red] {e}")
        raise typer.Exit(code=1) from None
    if not rows:
        typer.echo("no providers configured")
        return
    for r in rows:
        line = f"provider={r['provider']}  auth={r['mode']}"
        if r.get("session"):
            s = r["session"]
            line += f"  perch seat: expires_at={s['expires_at']} expired={s['expired']}"
        elif r.get("session_error"):
            line += f"  perch seat: UNREADABLE ({r['session_error']})"
        elif r["mode"] == "seat":
            line += "  perch seat: none (run `tc auth login " + r["provider"] + "`)"
        typer.echo(line)


@auth_app.command("logout")
def auth_logout(
    provider: str = typer.Argument(..., help="Provider whose stored seat session to delete."),
    settings_path: Path | None = _settings_opt(),
) -> None:
    """Release a provider's perch seat (tokens are removed, not revoked)."""
    try:
        broker, _ = _seat_broker(settings_path)
        broker.logout(provider)
    except (SettingsError, ProvidersError, SeatError) as e:
        err_console.print(f"[bold red]error[/bold red] {e}")
        raise typer.Exit(code=1) from None
    typer.echo(f"released perch seat for provider '{provider}'")


@farmhand_app.command("serve")
def farmhand_serve(
    transport: str | None = typer.Option(
        None,
        "--transport",
        help="Which transport(s) to serve: stdio, https, or both (default: the farmhand_transport setting, itself default 'both').",
    ),
    settings_path: Path | None = _settings_opt(),
) -> None:
    """Serve the Farmhand (spec 26): an MCP server whose tools are thin wrappers over coopd's socket ops.

    stdio speaks MCP on stdin/stdout for same-machine agents (the 0600 daemon socket gates access).
    https serves streamable-HTTP at farmhand_https_path on the coop listener (coop_host:coop_port),
    behind the same bearer rule as /api/* — the bearer is required on every bind by default; loopback
    with no token configured is open only with the coop_loopback_open opt-in. When a token is required
    and none is configured, an ephemeral one is printed once.
    both runs the two simultaneously. A coopd started with coop_enabled serves the https side itself
    on its existing listener when farmhand_transport includes 'https'."""
    from turbochickens.farmhand import TRANSPORTS, serve_transport

    try:
        settings = load_settings(settings_path)
    except SettingsError as e:
        err_console.print(f"[bold red]error[/bold red] {e}")
        raise typer.Exit(code=1) from None
    chosen = transport or settings.farmhand_transport
    if chosen not in TRANSPORTS:
        err_console.print(
            f"[bold red]error[/bold red] --transport must be one of {', '.join(TRANSPORTS)}, got {chosen!r}"
        )
        raise typer.Exit(code=1) from None
    # stdio is a protocol channel: anything informational goes to stderr only
    https_line = (
        f"https={settings.coop_url.rstrip('/')}{settings.farmhand_https_path}"
        if chosen in ("https", "both")
        else "https=-"
    )
    err_console.print(f"farmhand serving transport={chosen} socket={settings.socket_path} {https_line}")
    try:
        asyncio.run(serve_transport(settings, chosen))
    except KeyboardInterrupt:
        pass
    except OSError as e:
        err_console.print(f"[bold red]error[/bold red] cannot serve the farmhand https listener: {e}")
        raise typer.Exit(code=1) from None


def _parse_kv_pairs(pairs: list[str] | None, flag: str) -> dict[str, str]:
    """Parse repeatable `--env KEY=VALUE` / `--header NAME=REF` options."""
    out: dict[str, str] = {}
    for p in pairs or []:
        if "=" not in p:
            err_console.print(f"[bold red]error[/bold red] {flag} entries must be KEY=VALUE, got {p!r}")
            raise typer.Exit(code=1)
        k, v = p.split("=", 1)
        if not k or not v:
            err_console.print(f"[bold red]error[/bold red] {flag} entries need a non-empty key and value, got {p!r}")
            raise typer.Exit(code=1)
        out[k] = v
    return out


@connectors_app.command("list")
def connectors_list(
    settings_path: Path | None = _settings_opt(),
) -> None:
    """List the registered gates (connectors) with non-secret summaries — env key
    names and header refs only, never literal values."""
    resp = _request_or_exit(settings_path, "connectors_list")
    if not resp["connectors"]:
        typer.echo("no gates registered")
        return
    for c in resp["connectors"]:
        if c["transport"] == "stdio":
            typer.echo(
                f"gate={c['name']}  transport=stdio  command={c['command']}  args={c['args']}"
                f"  env_keys={c['env_keys']}"
            )
        else:
            typer.echo(f"gate={c['name']}  transport=https  url={c['url']}  headers={c['headers']}")


@connectors_app.command("add")
def connectors_add(
    name: str = typer.Argument(..., help="Gate name, e.g. 'toolx'."),
    transport: str = typer.Option(..., "--transport", help="'stdio' (command/args/env) or 'https' (url/headers)."),
    command: str | None = typer.Option(None, "--command", help="stdio: the command to spawn."),
    args: list[str] | None = typer.Option(None, "--arg", help="stdio: one command argument (repeatable)."),
    env: list[str] | None = typer.Option(None, "--env", help="stdio: KEY=VALUE (repeatable); values may be $ENV refs."),
    url: str | None = typer.Option(None, "--url", help="https: the MCP endpoint URL."),
    headers: list[str] | None = typer.Option(
        None, "--header", help="https: NAME=$REF (repeatable), e.g. --header 'Authorization=Bearer $TOKEN' — literal secrets are refused."
    ),
    settings_path: Path | None = _settings_opt(),
) -> None:
    """Register (or replace) a gate in connectors.toml. Env values may be literals
    or $ENV refs (only key names are ever served); https header values must be
    $ENV refs — a literal header secret is refused with a field-named error."""
    payload: dict = {"transport": transport}
    if command is not None:
        payload["command"] = command
    if args:
        payload["args"] = list(args)
    kv_env = _parse_kv_pairs(env, "--env")
    if kv_env:
        payload["env"] = kv_env
    if url is not None:
        payload["url"] = url
    kv_headers = _parse_kv_pairs(headers, "--header")
    if kv_headers:
        payload["headers"] = kv_headers
    resp = _daemon_request(settings_path, "connectors_add", timeout=10.0, name=name, **payload)
    if not resp.get("ok"):
        if resp.get("errors"):
            for e in resp["errors"]:
                err_console.print(f"[bold red]error[/bold red] {e['field']}: {e['message']}")
        else:
            err_console.print(f"[bold red]error[/bold red] {resp.get('error', 'unknown error')}")
        raise typer.Exit(code=1)
    typer.echo(f"registered gate '{resp['name']}' (transport={resp['transport']}) — registry: {resp['source']}")


@connectors_app.command("rm")
def connectors_rm(
    name: str = typer.Argument(..., help="Gate name to remove."),
    settings_path: Path | None = _settings_opt(),
) -> None:
    """Remove a gate from connectors.toml."""
    resp = _request_or_exit(settings_path, "connectors_rm", name=name)
    typer.echo(f"removed gate '{resp['name']}' (transport={resp['transport']})")


@app.callback()
def main() -> None:
    """TurboChickens CLI."""
