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

import httpx

from turbochickens.providers import ProvidersConfig, ProvidersError, load_providers
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


class ProviderConfigError(Exception):
    """The job's model/provider cannot be resolved from providers.toml."""


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
    """Result of `prepare`: the pinned harness identity plus the per-run env.

    `auth` is non-secret auth telemetry (spec 09): ``{"mode": "api-key" |
    "seat" | "none", "token_expires_at": iso|None}`` — recorded in the run
    manifest and audit log; never a token."""

    context: RunContext
    harness: dict[str, Any]  # adapter, version, provider, model, temperature
    auth: dict[str, Any] = field(default_factory=dict)


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

    def auth_telemetry(self, run: RunContext) -> dict[str, Any]:
        """Spec 09: non-secret auth mode for the manifest/audit. An env
        credential present → "api-key"; absent → "none" (a subprocess harness
        like vibe-cli may then use its own profile, e.g. browser auth from
        ~/.vibe/config.toml, which is outside this platform's knowledge)."""
        present = any(os.environ.get(k) is not None for k in self.allowlist)
        return {"mode": "api-key" if present else "none", "token_expires_at": None}


def auth_telemetry_of(provider: CredentialProvider, run: RunContext) -> dict[str, Any]:
    """Best-effort auth telemetry from a credential provider; the default is
    plain api-key env injection."""
    tele = getattr(provider, "auth_telemetry", None)
    if callable(tele):
        try:
            return tele(run) or {"mode": "api-key", "token_expires_at": None}
        except Exception:
            pass
    return {"mode": "api-key", "token_expires_at": None}


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


def vibe_version(bin_path: str) -> str:
    """Best-effort `vibe --version` parse; "unknown" on any failure."""
    try:
        out = subprocess.run([bin_path, "--version"], capture_output=True, text=True, timeout=15)
        text = out.stdout.strip()
        return text.split()[-1] if text else "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


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
            self._version = vibe_version(self.bin)
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
            # spec 09: non-secret auth telemetry (mode + token expiry, never a
            # token). A seat-configured vibe profile is handled by the vibe
            # binary itself via HOME passthrough, hence "none" when no env key.
            auth=auth_telemetry_of(self.credential_provider, run),
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


class MistralAPIAdapter:
    """Spec 05: OpenAI-compatible HTTP adapter against the provider named by the
    job's model (``"provider/model"`` or bare = mistral), configured entirely
    through `providers.toml`.

    Speaks ``POST {base_url}/chat/completions`` with JSON-mode structured
    responses (``response_format: {"type": "json_object"}``) where the model
    profile supports it, so raw output is easy to schema-validate downstream.
    Exponential backoff on provider 429/5xx (and transport errors), honoring
    ``Retry-After`` when present — every retry is streamed to the run logs.
    The credential is the per-run env var named by the provider profile; no
    request is ever sent without it. Token usage is measured telemetry; cost is
    derived from providers.toml pricing, never a declared budget.
    """

    name = "mistral-api"

    def __init__(
        self,
        spec: JobSpec,
        providers: ProvidersConfig | None = None,
        credential_provider: CredentialProvider | None = None,
        client: httpx.AsyncClient | None = None,
        backoff_base_sec: float = 1.0,
        backoff_max_sec: float = 30.0,
        max_retries: int = 5,
    ) -> None:
        self.providers = providers or load_providers()
        self.provider_name, self.model, self.provider, self.model_profile = self.providers.resolve(
            spec.harness.model
        )
        if credential_provider is None:
            # the provider's key env var must be injectable per-run even for
            # non-mistral providers
            allowlist = tuple(dict.fromkeys((*DEFAULT_CREDENTIAL_ALLOWLIST, self.provider.api_key_env)))
            credential_provider = EnvAllowlistCredentialProvider(allowlist)
        self.credential_provider = credential_provider
        self.client = client  # test seam: inject a client (e.g. MockTransport)
        self.backoff_base_sec = backoff_base_sec
        self.backoff_max_sec = backoff_max_sec
        self.max_retries = max_retries

    def prepare(self, run: RunContext) -> PreparedRun:
        run.env = build_per_run_env(run, self.credential_provider.credentials(run))
        return PreparedRun(
            context=run,
            harness={
                "adapter": self.name,
                "version": f"httpx/{httpx.__version__}",
                "provider": self.provider_name,
                "model": self.model,
                "temperature": run.spec.harness.temperature,
            },
            # spec 09: "seat" providers inject the broker's access token under
            # api_key_env (same var), so the bearer header below is unchanged
            auth=auth_telemetry_of(self.credential_provider, run),
        )

    def _messages(self, run: RunContext) -> list[dict[str, str]]:
        """Injected context: prompt + attachments from the workspace, nothing else."""
        prompt = (run.workspace / "prompt.txt").read_text(encoding="utf-8")
        parts = [prompt]
        for att in run.spec.input.attachments:
            p = run.workspace / Path(att).name
            if p.is_file():
                parts.append(f"--- attachment: {att} ---\n{p.read_text(encoding='utf-8')}")
        return [{"role": "user", "content": "\n\n".join(parts)}]

    async def execute(
        self,
        prepared: PreparedRun,
        event_sink: Callable[[str, str, str], Awaitable[None]],
        timeout_sec: int,
    ) -> HarnessResult:
        run = prepared.context
        api_key = run.env.get(self.provider.api_key_env)
        if not api_key:
            return HarnessResult(
                exit_code=None,
                raw_output="",
                usage={},
                error=f"missing credential: ${self.provider.api_key_env} is not set in the per-run env",
            )
        url = self.provider.base_url.rstrip("/") + "/chat/completions"
        body: dict[str, Any] = {
            "model": self.model,
            "messages": self._messages(run),
            "temperature": run.spec.harness.temperature,
        }
        if self.model_profile.json_mode:
            body["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {api_key}"}
        owned = self.client is None
        client = self.client or httpx.AsyncClient(timeout=httpx.Timeout(timeout_sec, connect=10.0))
        try:
            # wall-clock hard budget over the whole call incl. backoff sleeps
            return await asyncio.wait_for(
                self._post_with_backoff(client, url, body, headers, event_sink), timeout=timeout_sec
            )
        except asyncio.TimeoutError:
            return HarnessResult(
                exit_code=None,
                raw_output="",
                usage={},
                timed_out=True,
                error=f"timeout: no completed provider response after {timeout_sec}s",
            )
        finally:
            if owned:
                await client.aclose()

    async def _post_with_backoff(
        self,
        client: httpx.AsyncClient,
        url: str,
        body: dict[str, Any],
        headers: dict[str, str],
        event_sink: Callable[[str, str, str], Awaitable[None]],
    ) -> HarnessResult:
        await event_sink(
            "harness",
            "info",
            f"POST {url} model={self.model} json_mode={str(self.model_profile.json_mode).lower()}",
        )
        delay = self.backoff_base_sec
        attempt = 0
        while True:
            status: int | None = None
            err: str | None = None
            resp: httpx.Response | None = None
            try:
                resp = await client.post(url, json=body, headers=headers)
                status = resp.status_code
            except httpx.TransportError as e:
                err = f"{type(e).__name__}: {e}"
            if resp is not None and status == 200:
                return await self._on_success(resp, event_sink)
            retryable = status is None or status == 429 or status >= 500
            if not retryable:
                detail = self._error_detail(resp)
                return HarnessResult(
                    exit_code=status, raw_output="", usage={},
                    error=f"provider http {status}: {detail} (not retryable)",
                )
            attempt += 1
            if attempt > self.max_retries:
                detail = err or self._error_detail(resp)
                return HarnessResult(
                    exit_code=status, raw_output="", usage={},
                    error=f"provider {'http ' + str(status) if status else 'error'} gave up after {attempt} attempts: {detail}",
                )
            wait = min(delay, self.backoff_max_sec)
            source = "backoff"
            if resp is not None:
                retry_after = self._retry_after(resp)
                if retry_after is not None:
                    wait = min(retry_after, self.backoff_max_sec)
                    source = f"retry-after {retry_after:g}s"
            await event_sink(
                "harness",
                "warn",
                f"provider {'http ' + str(status) if status else 'transport error'}"
                f" — retry {attempt}/{self.max_retries} in {wait:.3f}s ({source})",
            )
            await asyncio.sleep(wait)
            delay *= 2  # exponential

    @staticmethod
    def _retry_after(resp: httpx.Response) -> float | None:
        raw = resp.headers.get("retry-after")
        if raw is None:
            return None
        try:
            return max(0.0, float(raw))
        except ValueError:
            return None

    @staticmethod
    def _error_detail(resp: httpx.Response | None) -> str:
        if resp is None:
            return "no response"
        try:
            data = resp.json()
            msg = data.get("error", {}).get("message") if isinstance(data.get("error"), dict) else None
            if msg:
                return str(msg)
        except ValueError:
            pass
        return resp.text[:200]

    async def _on_success(self, resp: httpx.Response, event_sink: Callable[[str, str, str], Awaitable[None]]) -> HarnessResult:
        try:
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            usage_raw = data.get("usage") or {}
        except (ValueError, KeyError, IndexError, TypeError) as e:
            return HarnessResult(
                exit_code=200, raw_output="", usage={},
                error=f"malformed provider response: {type(e).__name__}: {e}",
            )
        tokens_in = int(usage_raw.get("prompt_tokens", 0) or 0)
        tokens_out = int(usage_raw.get("completion_tokens", 0) or 0)
        usage: dict[str, Any] = {"tokens_in": tokens_in, "tokens_out": tokens_out}
        if self.provider.auth == "seat":
            # spec 09: seat billing is not per-token — report zero cost plus
            # seat-pool telemetry, never a fabricated per-token price
            usage["cost_usd"] = 0.0
            usage["billed_via"] = "seat"
        else:
            cost = self.providers.cost_usd(self.model_profile, tokens_in, tokens_out)
            if cost > 0:
                usage["cost_usd"] = round(cost, 6)
        await event_sink(
            "harness", "info", f"provider http 200: tokens_in={tokens_in} tokens_out={tokens_out}"
        )
        return HarnessResult(exit_code=0, raw_output=content or "", usage=usage)

    async def teardown(self, run: RunContext) -> TeardownReport:
        """No subprocess to reap for a pure HTTP adapter; workspace gone + env
        cleared are still verified, same attestation contract as vibe-cli."""
        workspace_removed = True
        if run.workspace.exists():
            shutil.rmtree(run.workspace, ignore_errors=True)
            workspace_removed = not run.workspace.exists()
        run.env.clear()
        return TeardownReport(
            process_reaped=True,
            workspace_removed=workspace_removed,
            env_cleared=True,
        )


def make_adapter(
    spec: JobSpec,
    credential_provider: CredentialProvider | None = None,
    providers: ProvidersConfig | None = None,
) -> HarnessAdapter:
    """Instantiate the adapter named by the spec. The model/provider must
    resolve from providers.toml — unknown ones fail here, at enqueue time."""
    if spec.harness.adapter == "vibe-cli":
        return VibeCLIAdapter(credential_provider=credential_provider)
    if spec.harness.adapter == "mistral-api":
        try:
            return MistralAPIAdapter(spec, providers=providers, credential_provider=credential_provider)
        except ProvidersError as e:
            raise ProviderConfigError(str(e)) from e
    raise AdapterNotImplemented(f"adapter {spec.harness.adapter!r} is not implemented")
