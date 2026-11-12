"""Harness adapter interface (baseline v0.2 §7) and the `vibe-cli` MVP adapter.

The adapter layer is deliberately free of any host-specific concepts: each
adapter implements ``prepare`` / ``execute`` / ``teardown`` over a
``RunContext``. A **credential-provider extension point** is reserved on the
interface — the MVP injects per-run env vars through an allowlist provider, and
a host-side broker can replace that later without touching the adapters.

Admission weights (baseline decision 2): API runs weigh 0.25, CLI-agent runs
weigh 1.0.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import subprocess
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

from turbochickens.spec.model import JobSpec

ADAPTER_WEIGHTS: dict[str, float] = {"mistral-api": 0.25, "vibe-cli": 1.0}

VIBE_BIN_ENVVAR = "TURBOCHICKENS_VIBE_BIN"

#: Env keys copied from the daemon environment into the per-run env. Everything
#: else (ambient secrets included) is left out — credentials enter only through
#: the credential provider, as per-run env vars.
BASE_ENV_KEYS = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "TERM",
    "TMPDIR",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_CACHE_HOME",
    "VIBE_HOME",
)

#: Default credential allowlist: names that may be injected per-run from the
#: daemon's environment. The extension point is the CredentialProvider below.
DEFAULT_CREDENTIAL_ALLOWLIST = ("MISTRAL_API_KEY",)


class AdapterNotImplemented(Exception):
    """The spec names an adapter that no implementation is registered for."""


def run_weight(adapter_name: str) -> float:
    """Resource weight of a run by adapter (baseline v0.2, decision 2)."""
    return ADAPTER_WEIGHTS[adapter_name]


def sanitize_log_line(line: str, max_len: int = 4000) -> str:
    """Make a streamed log line safe to store: strip control/format characters
    (anything in Unicode category C*, except tab/newline) and cap the length.
    Logs are data, never instructions."""
    cleaned = "".join(
        ch for ch in line if ch in ("\t", "\n") or not unicodedata.category(ch).startswith("C")
    )
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len] + "…[truncated]"
    return cleaned


@dataclass
class RunContext:
    """Everything a run needs, handed to the adapter. No ambient secrets: the
    per-run env is built fresh in `prepare`."""

    run_id: str
    job_id: str
    spec: JobSpec
    spec_path: Path
    spec_hash: str
    trigger: str
    workspace: Path
    env: dict[str, str] = field(default_factory=dict)
    proc: asyncio.subprocess.Process | None = None


@dataclass
class PreparedRun:
    """Result of `prepare`: the pinned harness identity plus the per-run env."""

    context: RunContext
    harness: dict[str, Any]  # adapter, version, provider, model, temperature


@dataclass
class HarnessResult:
    """Raw, untrusted harness output plus usage telemetry and exit status."""

    exit_code: int | None
    raw_output: str
    usage: dict[str, Any]
    timed_out: bool = False
    error: str | None = None


@dataclass
class TeardownReport:
    process_reaped: bool
    workspace_removed: bool
    env_cleared: bool


class CredentialProvider(Protocol):
    """Extension point: where per-run credentials come from. The MVP injects
    allowlisted env vars; a host-side broker can slot in here later."""

    def credentials(self, run: RunContext) -> dict[str, str]: ...


class EnvAllowlistCredentialProvider:
    """MVP credential provider: inject per-run env vars, but only names on the
    allowlist, read from the daemon's environment. Nothing else leaks through."""

    def __init__(self, allowlist: tuple[str, ...] = DEFAULT_CREDENTIAL_ALLOWLIST) -> None:
        self.allowlist = allowlist

    def credentials(self, run: RunContext) -> dict[str, str]:
        return {k: v for k in self.allowlist if (v := os.environ.get(k)) is not None}


def build_per_run_env(run: RunContext, credentials: dict[str, str]) -> dict[str, str]:
    """Fresh per-run environment: base keys + injected credentials + run vars.
    Deliberately not a copy of os.environ — no ambient secrets."""
    env = {k: os.environ[k] for k in BASE_ENV_KEYS if k in os.environ}
    env.update(credentials)
    env["TURBOCHICKENS_RUN_ID"] = run.run_id
    env["TURBOCHICKENS_JOB_ID"] = run.job_id
    return env


class HarnessAdapter(Protocol):
    """Baseline v0.2 §7: prepare / execute / teardown. `execute` and `teardown`
    are async because harnesses are asyncio subprocesses / HTTP clients."""

    name: str

    def prepare(self, run: RunContext) -> PreparedRun:
        """Pin the harness identity (version, model, provider), build the per-run env."""
        ...

    async def execute(
        self,
        prepared: PreparedRun,
        event_sink: Callable[[str, str, str], Awaitable[None]],
        timeout_sec: int,
    ) -> HarnessResult:
        """Run the job inside the workspace, stream sanitized log lines to the
        supervisor via `event_sink(source, level, line)`, return raw output +
        usage + exit status. Hard-kill on `timeout_sec`."""
        ...

    async def teardown(self, run: RunContext) -> TeardownReport:
        """Confirm process reaped, workspace verified gone, env cleared."""
        ...


def resolve_vibe_bin() -> str:
    """Locate the Vibe CLI: $TURBOCHICKENS_VIBE_BIN override, else PATH lookup."""
    return os.environ.get(VIBE_BIN_ENVVAR) or shutil.which("vibe") or "vibe"


class VibeCLIAdapter:
    """Runs the Vibe CLI headless as a subprocess pinned to the run workspace
    (cwd = workspace, per-run env, stdin closed). stdout is the final message
    (raw output); stdout/stderr are also streamed as sanitized log lines."""

    name = "vibe-cli"

    def __init__(
        self,
        bin_path: str | None = None,
        credential_provider: CredentialProvider | None = None,
    ) -> None:
        self.bin = bin_path or resolve_vibe_bin()
        self.credential_provider: CredentialProvider = (
            credential_provider or EnvAllowlistCredentialProvider()
        )
        self._version: str | None = None

    def _vibe_version(self) -> str:
        if self._version is None:
            try:
                out = subprocess.run(
                    [self.bin, "--version"], capture_output=True, text=True, timeout=15
                )
                text = out.stdout.strip()
                self._version = text.split()[-1] if text else "unknown"
            except (OSError, subprocess.SubprocessError):
                self._version = "unknown"
        return self._version

    def prepare(self, run: RunContext) -> PreparedRun:
        run.env = build_per_run_env(run, self.credential_provider.credentials(run))
        return PreparedRun(
            context=run,
            harness={
                "adapter": self.name,
                "version": self._vibe_version(),
                "provider": "mistral",
                "model": run.spec.harness.model,
                "temperature": run.spec.harness.temperature,
            },
        )

    async def execute(
        self,
        prepared: PreparedRun,
        event_sink: Callable[[str, str, str], Awaitable[None]],
        timeout_sec: int,
    ) -> HarnessResult:
        run = prepared.context
        workspace = run.workspace
        prompt_dst = workspace / "prompt.txt"
        export_dir = workspace / ".tc-export"
        cmd = [
            self.bin,
            "--prompt-file",
            str(prompt_dst),
            "--output",
            "text",
            "--output-dir",
            str(export_dir),
            "--model",
            run.spec.harness.model,
            "--workdir",
            str(workspace),
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(workspace),
                env=run.env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as e:
            return HarnessResult(exit_code=None, raw_output="", usage={}, error=f"cannot spawn {self.bin}: {e}")
        run.proc = proc
        stdout_chunks: list[str] = []

        async def _pump(stream: asyncio.StreamReader, level: str, collect: bool) -> None:
            while True:
                line = await stream.readline()
                if not line:
                    break
                text = line.decode("utf-8", "replace")
                if collect:
                    stdout_chunks.append(text)
                await event_sink("harness", level, sanitize_log_line(text.rstrip("\r\n")))

        pump_out = asyncio.create_task(_pump(proc.stdout, "stdout", True))  # type: ignore[arg-type]
        pump_err = asyncio.create_task(_pump(proc.stderr, "stderr", False))  # type: ignore[arg-type]
        timed_out = False
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout_sec)
        except asyncio.TimeoutError:
            timed_out = True
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
        await asyncio.gather(pump_out, pump_err)
        raw_output = "".join(stdout_chunks).strip()
        usage = self._read_usage(export_dir)
        return HarnessResult(
            exit_code=proc.returncode,
            raw_output=raw_output,
            usage=usage,
            timed_out=timed_out,
        )

    def _read_usage(self, export_dir: Path) -> dict[str, Any]:
        """Read token usage from the harness export the CLI writes into the
        workspace (`--output-dir`). Telemetry only — never a budget."""
        try:
            data = json.loads((export_dir / "export.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            return {"tokens_in": 0, "tokens_out": 0}
        raw = data.get("usage") if isinstance(data, dict) else None
        if not isinstance(raw, dict):
            return {"tokens_in": 0, "tokens_out": 0}
        usage: dict[str, Any] = {
            "tokens_in": raw.get("input_tokens", raw.get("prompt_tokens", 0)),
            "tokens_out": raw.get("output_tokens", raw.get("completion_tokens", 0)),
        }
        if "cost_usd" in raw:
            usage["cost_usd"] = raw["cost_usd"]
        return usage

    async def teardown(self, run: RunContext) -> TeardownReport:
        process_reaped = True
        if run.proc is not None and run.proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                run.proc.kill()
            with contextlib.suppress(Exception):
                await run.proc.wait()
            process_reaped = run.proc.returncode is not None
        workspace_removed = True
        if run.workspace.exists():
            shutil.rmtree(run.workspace, ignore_errors=True)
            workspace_removed = not run.workspace.exists()
        run.env.clear()
        return TeardownReport(
            process_reaped=process_reaped,
            workspace_removed=workspace_removed,
            env_cleared=True,
        )


def make_adapter(spec: JobSpec, credential_provider: CredentialProvider | None = None) -> HarnessAdapter:
    """Instantiate the adapter named by the spec. `mistral-api` lands in spec 05."""
    if spec.harness.adapter == "vibe-cli":
        return VibeCLIAdapter(credential_provider=credential_provider)
    raise AdapterNotImplemented(
        f"adapter {spec.harness.adapter!r} is not implemented yet (mistral-api arrives in spec 05)"
    )
