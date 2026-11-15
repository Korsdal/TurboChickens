"""Tests for spec 05: the `mistral-api` adapter + `providers.toml` + backoff.

Seams:
- pure: `load_providers` / `ProvidersConfig.resolve` on TOML files and defaults.
- contract: `MistralAPIAdapter` driven directly with an `httpx.MockTransport`
  (JSON-mode request shape, backoff on 429/5xx, no retry on 4xx, timeout,
  missing credential) — the MVP spec sanctions contract-shaped tests for the
  real adapters.
- e2e: the real daemon on a temp socket (thread, plus one subprocess boot)
  running `mistral-api` jobs over loopback HTTP against a hermetic fake
  OpenAI-compatible server — no real credentials, no external network.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from test_daemon import DaemonThread
from turbochickens.adapter import MistralAPIAdapter, RunContext
from turbochickens.audit import AuditLog
from turbochickens.cli.main import app
from turbochickens.client import DaemonUnreachable, request
from turbochickens.providers import ProvidersConfig, ProvidersError, load_providers
from turbochickens.settings import load_settings
from turbochickens.spec.model import JobSpec

runner = CliRunner()

SCHEMA = {
    "type": "object",
    "required": ["message"],
    "additionalProperties": False,
    "properties": {"message": {"type": "string"}, "echo": {"type": "string"}},
}


def combined_output(result) -> str:
    parts = [result.output or ""]
    try:
        parts.append(result.stderr or "")
    except ValueError:
        pass
    return "".join(parts)


def run_id_of(result) -> str:
    m = re.search(r"egg queued (run-[0-9a-f]+)", result.output)
    assert m, f"no run id in output: {result.output}"
    return m.group(1)


def wait_terminal(socket_path, run_id: str, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = request(socket_path, "runs_show", run_id=run_id, timeout=2.0)
        if resp.get("ok") and resp["run"]["status"] in ("succeeded", "failed"):
            return resp["run"]
        time.sleep(0.1)
    raise AssertionError(f"run {run_id} not terminal after {timeout}s")


def db_row(settings, sql: str, params=()) -> tuple | None:
    conn = sqlite3.connect(str(settings.db_path))
    try:
        return conn.execute(sql, params).fetchone()
    finally:
        conn.close()


# --- providers.toml: the pure seam -------------------------------------------


def test_providers_defaults_load_and_resolve_mistral(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("TURBOCHICKENS_PROVIDERS", raising=False)
    cfg = load_providers(data_dir=tmp_path)  # empty dir -> built-in defaults
    assert "mistral" in cfg.providers
    name, model, provider, profile = cfg.resolve("mistral-large-latest")
    assert (name, model) == ("mistral", "mistral-large-latest")
    assert provider.base_url == "https://api.mistral.ai/v1"
    assert profile.context_window > 0 and profile.input_usd_per_mtok > 0
    # pricing feeds expected cost: 1M input tokens at the input price
    assert cfg.cost_usd(profile, 1_000_000, 0) == pytest.approx(profile.input_usd_per_mtok)


def test_providers_file_overlays_per_provider_and_adds_new_ones(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("TURBOCHICKENS_PROVIDERS", raising=False)
    f = tmp_path / "providers.toml"
    f.write_text(
        """
[providers.tensorx]
base_url = "https://api.tensorx.example/v1"
api_key_env = "TENSORX_API_KEY"

[providers.tensorx.models.example-model]
context_window = 128000
input_usd_per_mtok = 1.0
output_usd_per_mtok = 3.0
json_mode = false
"""
    )
    cfg = load_providers(f, data_dir=tmp_path)
    assert set(cfg.providers) == {"mistral", "tensorx"}  # defaults kept, new one added
    name, model, provider, profile = cfg.resolve("tensorx/example-model")
    assert (name, model, provider.api_key_env, profile.json_mode) == (
        "tensorx",
        "example-model",
        "TENSORX_API_KEY",
        False,
    )
    # a bare model still resolves against the home provider
    assert cfg.resolve("mistral-small-latest")[0] == "mistral"


def test_providers_file_replaces_a_provider_wholesale(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("TURBOCHICKENS_PROVIDERS", raising=False)
    f = tmp_path / "providers.toml"
    f.write_text(
        """
[providers.mistral]
base_url = "http://127.0.0.1:9/v1"
api_key_env = "MISTRAL_API_KEY"

[providers.mistral.models.only-model]
context_window = 8192
input_usd_per_mtok = 1.0
output_usd_per_mtok = 2.0
"""
    )
    cfg = load_providers(f, data_dir=tmp_path)
    assert cfg.providers["mistral"].base_url == "http://127.0.0.1:9/v1"
    assert list(cfg.providers["mistral"].models) == ["only-model"]
    with pytest.raises(ProvidersError, match="not found in providers.toml"):
        cfg.resolve("mistral-large-latest")  # default model list was replaced


def test_providers_invalid_file_is_a_named_error(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("TURBOCHICKENS_PROVIDERS", raising=False)
    f = tmp_path / "providers.toml"
    f.write_text('[providers.mistral]\nbase_url = "http://x/v1"\nbogus_key = 1\nmodels = {}\n')
    with pytest.raises(ProvidersError, match="providers.mistral.bogus_key"):
        load_providers(f, data_dir=tmp_path)
    with pytest.raises(ProvidersError, match="providers file not found"):
        load_providers(tmp_path / "missing.toml")


def test_providers_env_var_resolution(tmp_path, monkeypatch) -> None:
    f = tmp_path / "p.toml"
    f.write_text(
        '[providers.acme]\nbase_url = "http://acme.test/v1"\napi_key_env = "ACME_KEY"\n'
        '[providers.acme.models.m]\ncontext_window = 1000\ninput_usd_per_mtok = 0.1\noutput_usd_per_mtok = 0.2\n'
    )
    monkeypatch.setenv("TURBOCHICKENS_PROVIDERS", str(f))
    cfg = load_providers(data_dir=tmp_path / "empty")  # env var wins over data_dir
    assert "acme" in cfg.providers and "mistral" in cfg.providers
    with pytest.raises(ProvidersError, match="unknown provider"):
        cfg.resolve("nope/thing")


# --- mistral-api adapter: contract seam over httpx.MockTransport --------------


def make_spec(model: str = "fake-model", temperature: float = 0.2) -> JobSpec:
    return JobSpec.model_validate(
        {
            "job": {"name": "contract-job"},
            "trigger": {"type": "manual"},
            "harness": {"adapter": "mistral-api", "model": model, "temperature": temperature},
            "resources": {"timeout_sec": 30, "max_context_mb": 2, "network": "none"},
            "input": {"prompt_file": "prompts/p.txt", "attachments": []},
            "output": {"format": "json", "schema_file": "schemas/o.json"},
        }
    )


def make_providers(json_mode: bool = True, base_url: str = "http://provider.test/v1") -> ProvidersConfig:
    return ProvidersConfig.model_validate(
        {
            "providers": {
                "mistral": {
                    "base_url": base_url,
                    "api_key_env": "MISTRAL_API_KEY",
                    "models": {
                        "fake-model": {
                            "context_window": 8192,
                            "input_usd_per_mtok": 1.0,
                            "output_usd_per_mtok": 2.0,
                            "json_mode": json_mode,
                        }
                    },
                }
            }
        }
    )


def make_ctx(tmp_path: Path, prompt: str = "Say hello") -> RunContext:
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    (ws / "prompt.txt").write_text(prompt)
    return RunContext(
        run_id="run-contract",
        job_id="contract-job",
        spec=make_spec(),
        spec_path=tmp_path / "job.toml",
        spec_hash="abc",
        trigger="manual",
        workspace=ws,
    )


def ok_payload(content: str) -> dict:
    return {
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 40, "total_tokens": 140},
    }


def drive(adapter: MistralAPIAdapter, ctx: RunContext, handler, timeout_sec: int = 10):
    """Run prepare → execute → teardown against a MockTransport, capturing the
    streamed events and the requests the adapter actually sent."""
    events: list[tuple[str, str, str]] = []
    seen: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    async def go():
        async def sink(source, level, line):
            events.append((source, level, line))

        client = httpx.AsyncClient(transport=httpx.MockTransport(recording))
        adapter.client = client
        prepared = adapter.prepare(ctx)
        env_keys = sorted(ctx.env.keys())  # snapshot before teardown clears it
        result = await adapter.execute(prepared, sink, timeout_sec)
        report = await adapter.teardown(ctx)
        await client.aclose()
        return prepared, result, report, env_keys

    prepared, result, report, env_keys = asyncio.run(go())
    return prepared, result, events, report, seen, env_keys


def test_contract_json_mode_request_shape_and_success(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MISTRAL_API_KEY", "sk-contract")
    content = json.dumps({"message": "hi"})

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=ok_payload(content))

    ctx = make_ctx(tmp_path, prompt="Say hello")
    adapter = MistralAPIAdapter(make_spec(), providers=make_providers(), backoff_base_sec=0.01)
    prepared, result, events, report, seen, env_keys = drive(adapter, ctx, handler)

    # harness identity pinned in prepare
    h = prepared.harness
    assert h["adapter"] == "mistral-api" and h["provider"] == "mistral"
    assert h["model"] == "fake-model" and h["temperature"] == 0.2
    assert h["version"].startswith("httpx/")
    assert "MISTRAL_API_KEY" in env_keys  # credential injected per-run, key name only

    # result: raw output + measured usage + cost from providers.toml pricing
    assert result.exit_code == 0 and result.raw_output == content
    assert result.usage["tokens_in"] == 100 and result.usage["tokens_out"] == 40
    assert result.usage["cost_usd"] == pytest.approx(100 * 1.0 / 1e6 + 40 * 2.0 / 1e6, abs=1e-9)

    # the one request: OpenAI-compatible shape, JSON mode, bearer auth
    assert len(seen) == 1
    req = seen[0]
    assert req.method == "POST" and req.url.path == "/v1/chat/completions"
    assert req.headers["authorization"] == "Bearer sk-contract"
    body = json.loads(req.content)
    assert body["model"] == "fake-model" and body["temperature"] == 0.2
    assert body["response_format"] == {"type": "json_object"}
    assert "Say hello" in body["messages"][0]["content"]

    # streamed log line for the request + success; teardown verified
    lines = [line for _, _, line in events]
    assert any("POST" in line and "fake-model" in line for line in lines)
    assert any("http 200" in line for line in lines)
    assert report.process_reaped and report.workspace_removed and report.env_cleared
    assert not ctx.workspace.exists() and ctx.env == {}


def test_contract_no_response_format_when_model_disables_json_mode(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MISTRAL_API_KEY", "sk-contract")
    adapter = MistralAPIAdapter(make_spec(), providers=make_providers(json_mode=False), backoff_base_sec=0.01)
    _, result, _, _, seen, _ = drive(
        adapter, make_ctx(tmp_path), lambda req: httpx.Response(200, json=ok_payload("{}"))
    )
    assert result.exit_code == 0
    assert "response_format" not in json.loads(seen[0].content)


def test_contract_exponential_backoff_on_5xx(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MISTRAL_API_KEY", "sk-contract")
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] <= 2:
            return httpx.Response(500, json={"error": {"message": "boom"}})
        return httpx.Response(200, json=ok_payload('{"message": "ok after retries"}'))

    adapter = MistralAPIAdapter(make_spec(), providers=make_providers(), backoff_base_sec=0.01, max_retries=5)
    _, result, events, _, seen, _ = drive(adapter, make_ctx(tmp_path), handler)
    assert result.exit_code == 0 and "ok after retries" in result.raw_output
    assert len(seen) == 3
    waits = [float(m.group(1)) for _, _, line in events if (m := re.search(r"retry \d+/\d+ in ([\d.]+)s", line))]
    assert len(waits) == 2
    assert waits[1] == pytest.approx(waits[0] * 2)  # exponential
    assert any("http 500" in line for _, level, line in events if level == "warn")


def test_contract_retry_after_header_is_honored(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MISTRAL_API_KEY", "sk-contract")
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={"error": {"message": "slow down"}}, headers={"Retry-After": "0.5"})
        return httpx.Response(200, json=ok_payload('{"message": "ok"}'))

    adapter = MistralAPIAdapter(make_spec(), providers=make_providers(), backoff_base_sec=10.0, max_retries=3)
    _, result, events, _, _, _ = drive(adapter, make_ctx(tmp_path), handler)
    assert result.exit_code == 0
    retry_lines = [line for _, level, line in events if level == "warn" and "retry" in line]
    assert len(retry_lines) == 1
    assert "429" in retry_lines[0] and "0.500s" in retry_lines[0] and "retry-after" in retry_lines[0]


def test_contract_429_exhaustion_gives_up_with_error(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MISTRAL_API_KEY", "sk-contract")
    adapter = MistralAPIAdapter(make_spec(), providers=make_providers(), backoff_base_sec=0.01, max_retries=2)
    _, result, events, _, seen, _ = drive(
        adapter, make_ctx(tmp_path), lambda req: httpx.Response(429, json={"error": {"message": "rate limited"}})
    )
    assert result.exit_code == 429 and result.error and "429" in result.error and "gave up" in result.error
    assert len(seen) == 3  # initial + 2 retries
    assert len([1 for _, level, line in events if level == "warn" and "retry" in line]) == 2


def test_contract_4xx_other_than_429_is_not_retried(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MISTRAL_API_KEY", "sk-contract")
    adapter = MistralAPIAdapter(make_spec(), providers=make_providers(), backoff_base_sec=0.01, max_retries=5)
    _, result, events, _, seen, _ = drive(
        adapter, make_ctx(tmp_path), lambda req: httpx.Response(400, json={"error": {"message": "bad request"}})
    )
    assert result.exit_code == 400 and "not retryable" in result.error
    assert len(seen) == 1  # no retry on a client error
    assert not any("retry" in line for _, _, line in events)


def test_contract_transport_error_is_retried(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MISTRAL_API_KEY", "sk-contract")
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(200, json=ok_payload('{"message": "ok"}'))

    adapter = MistralAPIAdapter(make_spec(), providers=make_providers(), backoff_base_sec=0.01, max_retries=3)
    _, result, events, _, seen, _ = drive(adapter, make_ctx(tmp_path), handler)
    assert result.exit_code == 0
    assert any("transport error" in line for _, level, line in events if level == "warn")
    assert len(seen) == 2


def test_contract_missing_credential_sends_no_request(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    adapter = MistralAPIAdapter(make_spec(), providers=make_providers(), backoff_base_sec=0.01)
    _, result, _, _, seen, _ = drive(
        adapter, make_ctx(tmp_path), lambda req: httpx.Response(200, json=ok_payload("{}"))
    )
    assert result.error and "MISTRAL_API_KEY" in result.error
    assert seen == []  # fail closed: no request without a credential


def test_contract_timeout_hard_stops(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MISTRAL_API_KEY", "sk-contract")

    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, json=ok_payload("{}"))

    adapter = MistralAPIAdapter(make_spec(), providers=make_providers(), backoff_base_sec=0.01)
    t0 = time.time()
    _, result, _, _, _, _ = drive(adapter, make_ctx(tmp_path), handler, timeout_sec=1)
    assert time.time() - t0 < 4
    assert result.timed_out is True and "timeout" in result.error


def test_contract_malformed_200_response_is_an_error(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MISTRAL_API_KEY", "sk-contract")
    adapter = MistralAPIAdapter(make_spec(), providers=make_providers(), backoff_base_sec=0.01)
    _, result, _, _, _, _ = drive(
        adapter, make_ctx(tmp_path), lambda req: httpx.Response(200, json={"nope": True})
    )
    assert result.error and "malformed provider response" in result.error


# --- e2e: daemon on a temp socket + hermetic fake provider over loopback ------


class FakeMistralHandler(BaseHTTPRequestHandler):
    """A hermetic OpenAI-compatible endpoint. Behavior is scripted by a
    `SCENARIO: <name>` marker in the prompt; every request is recorded."""

    def log_message(self, *args) -> None:  # quiet
        pass

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        self.server.requests.append(
            {"path": self.path, "authorization": self.headers.get("Authorization"), "body": body}
        )
        prompt = ""
        try:
            prompt = body["messages"][0]["content"]
        except (KeyError, IndexError, TypeError):
            pass
        scenario = next(
            (s for s in ("bad-json", "schema-violation", "retry429", "fail400", "slow") if f"SCENARIO: {s}" in prompt),
            "ok",
        )
        if scenario == "fail400":
            self._send(400, {"error": {"message": "bad request from fake provider"}})
        elif scenario == "retry429":
            count = self.server.counters.get("retry429", 0) + 1
            self.server.counters["retry429"] = count
            if count <= 2:
                self._send(429, {"error": {"message": "rate limited"}}, {"Retry-After": "0.05"})
            else:
                self._send(200, self._ok(prompt))
        elif scenario == "bad-json":
            self._send(200, self._ok_raw("this is not json"))
        elif scenario == "schema-violation":
            self._send(200, self._ok_raw('{"wrong_field": 1}'))
        elif scenario == "slow":
            time.sleep(3)
            self._send(200, self._ok(prompt))
        else:
            self._send(200, self._ok(prompt))

    def _ok(self, prompt: str) -> dict:
        return self._ok_raw(json.dumps({"message": "hello from fake api", "echo": prompt.strip()[:30]}))

    @staticmethod
    def _ok_raw(content: str) -> dict:
        return {
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 40, "total_tokens": 140},
        }

    def _send(self, code: int, payload: dict, extra_headers: dict | None = None) -> None:
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def fake_api():
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeMistralHandler)
    server.requests = []
    server.counters = {}
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield server
    server.shutdown()
    server.server_close()
    t.join(timeout=5)


@pytest.fixture
def providers_file(tmp_path, monkeypatch, fake_api) -> Path:
    port = fake_api.server_address[1]
    f = tmp_path / "providers.toml"
    f.write_text(
        f"""
[providers.mistral]
base_url = "http://127.0.0.1:{port}/v1"
api_key_env = "MISTRAL_API_KEY"

[providers.mistral.models.fake-model]
context_window = 8192
input_usd_per_mtok = 1.0
output_usd_per_mtok = 2.0
json_mode = true
"""
    )
    monkeypatch.setenv("TURBOCHICKENS_PROVIDERS", str(f))
    return f


@pytest.fixture
def api_daemon(tmp_path, monkeypatch, providers_file):
    monkeypatch.setenv("MISTRAL_API_KEY", "tc-test-key")
    f = tmp_path / "settings.toml"
    f.write_text(f'data_dir = "{tmp_path / "data"}"\nweight_budget_override = 1\n')
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(f))
    settings = load_settings(f)
    dt = DaemonThread(settings)
    dt.start()
    yield dt
    dt.stop()


def write_api_job(tmp_path: Path, name: str = "api-job", prompt: str = "Say hello",
                  model: str = "fake-model", timeout: int = 30, schema: dict | None = None) -> Path:
    d = tmp_path / name
    (d / "prompts").mkdir(parents=True, exist_ok=True)
    (d / "schemas").mkdir(parents=True, exist_ok=True)
    (d / "prompts" / "prompt.txt").write_text(prompt)
    (d / "schemas" / "out.schema.json").write_text(json.dumps(schema or SCHEMA))
    (d / "job.toml").write_text(
        f"""
[job]
name = "{name}"
description = "test api job"

[trigger]
type = "manual"

[harness]
adapter = "mistral-api"
model = "{model}"
temperature = 0.0

[resources]
timeout_sec = {timeout}
max_context_mb = 2
network = "none"

[input]
prompt_file = "prompts/prompt.txt"
attachments = []

[output]
format = "json"
schema_file = "schemas/out.schema.json"
"""
    )
    return d / "job.toml"


def test_api_run_over_http_lands_schema_validated_result(api_daemon, fake_api, tmp_path) -> None:
    spec_path = write_api_job(tmp_path)
    result = runner.invoke(app, ["run", str(spec_path), "--wait"])
    assert result.exit_code == 0, combined_output(result)
    assert "succeeded" in result.output
    assert "weight 0.25" in result.output  # API chickens weigh 0.25 in admission control
    run_id = run_id_of(result)
    settings = api_daemon.settings

    run = wait_terminal(settings.socket_path, run_id)
    assert run["status"] == "succeeded"
    m = run["manifest"]
    assert m["harness"]["adapter"] == "mistral-api"
    assert m["harness"]["provider"] == "mistral"
    assert m["harness"]["model"] == "fake-model"
    assert m["usage"]["tokens_in"] == 100 and m["usage"]["tokens_out"] == 40
    assert m["usage"]["cost_usd"] == pytest.approx(0.00018)
    assert "MISTRAL_API_KEY" in m["env_keys"]
    assert "tc-test-key" not in json.dumps(m)  # env keys only, never secret values

    # the provider saw exactly one OpenAI-compatible, JSON-mode, authenticated call
    reqs = [r for r in fake_api.requests if r["path"] == "/v1/chat/completions"]
    assert len(reqs) == 1
    assert reqs[0]["authorization"] == "Bearer tc-test-key"
    assert reqs[0]["body"]["model"] == "fake-model"
    assert reqs[0]["body"]["response_format"] == {"type": "json_object"}
    assert "Say hello" in reqs[0]["body"]["messages"][0]["content"]

    # validated output in the result store + served by `tc results get`
    res = runner.invoke(app, ["results", "get", run_id])
    assert res.exit_code == 0, combined_output(res)
    assert json.loads(res.output)["message"] == "hello from fake api"
    row = db_row(settings, "SELECT valid FROM results WHERE run_id = ?", (run_id,))
    assert row == (1,)
    assert (settings.results_dir / run_id / "output.json").exists()

    # teardown attested, workspace gone, no alert
    att = db_row(settings, "SELECT process_reaped, workspace_removed, env_cleared FROM teardown_attestations WHERE run_id = ?", (run_id,))
    assert att == (1, 1, 1)
    assert not (settings.data_dir / "workspaces" / run_id).exists()
    assert run_id not in request(settings.socket_path, "status", timeout=2.0)["alerts"]
    events = [r["event"] for r in AuditLog(settings.audit_log_path).read_all()]
    assert "clutch.stored" in events and "abattoir.attested" in events and "chicken.finished" in events


def test_429_backoff_is_visible_in_run_logs(api_daemon, fake_api, tmp_path) -> None:
    spec_path = write_api_job(tmp_path, name="retry-job", prompt="SCENARIO: retry429 please summarize")
    result = runner.invoke(app, ["run", str(spec_path), "--wait"])
    assert result.exit_code == 0, combined_output(result)
    run_id = run_id_of(result)
    assert wait_terminal(api_daemon.settings.socket_path, run_id)["status"] == "succeeded"

    logs = request(api_daemon.settings.socket_path, "runs_logs", run_id=run_id, timeout=2.0)["events"]
    retry_lines = [e for e in logs if e["level"] == "warn" and "retry" in e["line"] and "429" in e["line"]]
    assert len(retry_lines) == 2
    assert all(e["source"] == "harness" for e in retry_lines)
    # two 429s then the 200 — the run recovers
    assert len([r for r in fake_api.requests if r["path"] == "/v1/chat/completions"]) == 3
    res = runner.invoke(app, ["results", "get", run_id])
    assert res.exit_code == 0 and json.loads(res.output)["message"] == "hello from fake api"


def test_schema_invalid_api_output_is_quarantined_never_valid(api_daemon, fake_api, tmp_path) -> None:
    spec_path = write_api_job(tmp_path, name="bad-json-job", prompt="SCENARIO: bad-json give me output")
    result = runner.invoke(app, ["run", str(spec_path), "--wait"])
    assert result.exit_code == 1
    assert "failed" in result.output
    run_id = run_id_of(result)
    settings = api_daemon.settings
    run = wait_terminal(settings.socket_path, run_id)
    assert run["status"] == "failed"
    assert "schema validation" in run["manifest"]["status_reason"]
    assert run["result"]["valid"] is False and run["result"]["schema_errors"]
    row = db_row(settings, "SELECT valid, artifacts_dir, schema_errors FROM results WHERE run_id = ?", (run_id,))
    assert row[0] == 0 and row[2]
    qdir = settings.quarantine_dir / run_id
    assert (qdir / "raw_output.txt").read_text() == "this is not json"
    assert json.loads((qdir / "schema_errors.json").read_text())

    # quarantined output is never returned as a valid result
    res = runner.invoke(app, ["results", "get", run_id])
    assert res.exit_code == 1
    assert "quarantined" in combined_output(res)
    assert "not valid JSON" in combined_output(res)
    # teardown still attested
    att = db_row(settings, "SELECT process_reaped, workspace_removed, env_cleared FROM teardown_attestations WHERE run_id = ?", (run_id,))
    assert att == (1, 1, 1)


def test_provider_400_fails_without_retry(api_daemon, fake_api, tmp_path) -> None:
    spec_path = write_api_job(tmp_path, name="fail400-job", prompt="SCENARIO: fail400 go")
    result = runner.invoke(app, ["run", str(spec_path), "--wait"])
    assert result.exit_code == 1
    run_id = run_id_of(result)
    run = wait_terminal(api_daemon.settings.socket_path, run_id)
    assert run["status"] == "failed"
    assert "http 400" in run["manifest"]["status_reason"]
    logs = request(api_daemon.settings.socket_path, "runs_logs", run_id=run_id, timeout=2.0)["events"]
    assert not any(re.search(r"retry \d+/\d+", e["line"]) for e in logs)
    assert len([r for r in fake_api.requests if r["path"] == "/v1/chat/completions"]) == 1


def test_unknown_model_is_rejected_at_enqueue(api_daemon, tmp_path) -> None:
    spec_path = write_api_job(tmp_path, name="unknown-model-job", model="no-such-model")
    result = runner.invoke(app, ["run", str(spec_path)])
    assert result.exit_code == 1
    assert "not found in providers.toml" in combined_output(result)
    st = request(api_daemon.settings.socket_path, "status", timeout=2.0)
    assert st["queue_depth"] == 0


def test_api_runs_weigh_0_25_and_share_the_weight_budget(api_daemon, fake_api, tmp_path) -> None:
    # budget override is 1 in this fixture: four 0.25-weight API runs fit, the fifth backlogs
    spec_path = write_api_job(tmp_path, name="slow-api-job", prompt="SCENARIO: slow work")
    ids = []
    for _ in range(5):
        r = runner.invoke(app, ["run", str(spec_path)])
        assert r.exit_code == 0, combined_output(r)
        assert "weight 0.25" in r.output
        ids.append(run_id_of(r))
    settings = api_daemon.settings
    max_in_flight = 0
    saw_backlog = False
    deadline = time.time() + 2.5
    while time.time() < deadline:
        st = request(settings.socket_path, "status", timeout=2.0)
        max_in_flight = max(max_in_flight, len(st["in_flight"]))
        if len(st["in_flight"]) == 4 and st["queue_depth"] >= 1:
            saw_backlog = True
            assert all(w["weight"] == 0.25 for w in st["in_flight"])
            break
        time.sleep(0.05)
    assert max_in_flight == 4 and saw_backlog  # backlog, never rejection
    for run_id in ids:
        assert wait_terminal(settings.socket_path, run_id, timeout=30)["status"] == "succeeded"


def test_e2e_subprocess_daemon_api_run(tmp_path, monkeypatch, providers_file, fake_api) -> None:
    monkeypatch.setenv("MISTRAL_API_KEY", "tc-test-key")
    f = tmp_path / "settings.toml"
    f.write_text(f'data_dir = "{tmp_path / "data"}"\nweight_budget_override = 4\n')
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(f))
    settings = load_settings(f)
    spec_path = write_api_job(tmp_path, name="e2e-api-job")
    env = dict(os.environ)  # includes TURBOCHICKENS_SETTINGS, TURBOCHICKENS_PROVIDERS, MISTRAL_API_KEY
    proc = subprocess.Popen(
        [sys.executable, "-m", "turbochickens", "daemon", "start"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.time() + 15
        while time.time() < deadline:
            if settings.socket_path.exists():
                try:
                    if request(settings.socket_path, "ping", timeout=1.0).get("ok"):
                        break
                except DaemonUnreachable:
                    pass
            if proc.poll() is not None:
                raise AssertionError(f"daemon exited early: {proc.stdout.read() if proc.stdout else ''}")
            time.sleep(0.05)
        else:
            raise AssertionError("daemon subprocess did not start answering")

        result = runner.invoke(app, ["run", str(spec_path), "--wait"])
        assert result.exit_code == 0, combined_output(result)
        assert "weight 0.25" in result.output and "succeeded" in result.output
        run_id = run_id_of(result)

        res = runner.invoke(app, ["results", "get", run_id])
        assert res.exit_code == 0, combined_output(res)
        assert json.loads(res.output)["message"] == "hello from fake api"

        logs = runner.invoke(app, ["runs", "logs", run_id])
        assert logs.exit_code == 0 and "POST" in logs.output and "fake-model" in logs.output

        result = runner.invoke(app, ["daemon", "stop"])
        assert result.exit_code == 0, combined_output(result)
        proc.wait(timeout=15)
        assert proc.returncode == 0

        att = db_row(settings, "SELECT process_reaped, workspace_removed, env_cleared FROM teardown_attestations WHERE run_id = ?", (run_id,))
        assert att == (1, 1, 1)
        events = [r["event"] for r in AuditLog(settings.audit_log_path).read_all()]
        for expected in ("coopd.started", "egg.queued", "clutch.stored", "abattoir.attested", "chicken.finished", "coopd.stopped"):
            assert expected in events
        assert not (settings.data_dir / "workspaces" / run_id).exists()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


# --- reviewer finding: transport errors / log lines never carry the provider
# --- URL's userinfo (a base_url may embed user:pass@host) ----------------------


def test_contract_transport_error_strips_provider_url_userinfo(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MISTRAL_API_KEY", "sk-contract")
    secret_url = "http://feeduser:feedsecret@provider.test/v1"

    def handler(request: httpx.Request) -> httpx.Response:
        # a transport error whose text embeds the full request URL (userinfo!)
        raise httpx.ConnectError(f"cannot reach {request.url}", request=request)

    adapter = MistralAPIAdapter(
        make_spec(), providers=make_providers(base_url=secret_url), backoff_base_sec=0.01, max_retries=0
    )
    _, result, events, _, seen, _ = drive(adapter, make_ctx(tmp_path), handler)
    # negative: neither the served error nor any streamed log line carries the
    # URL or its userinfo
    assert result.error and "ConnectError" in result.error
    assert "provider.test" not in result.error and "feedsecret" not in result.error, result.error
    blob = "\n".join(line for _, _, line in events)
    assert "feedsecret" not in blob and "feeduser:feedsecret" not in blob, blob
    assert "<url withheld by the fox>" in result.error
    # positive: the request really went to the userinfo URL (the scrub is
    # display-only), and the POST log line kept a scrubbed URL
    assert seen and str(seen[0].url).startswith(secret_url)
    post_lines = [line for _, _, line in events if "POST" in line and "fake-model" in line]
    assert post_lines and "provider.test/v1/chat/completions" in post_lines[0]
    assert "feeduser" not in post_lines[0] and "feedsecret" not in post_lines[0]


def test_contract_byte_cap_counts_encoded_bytes_not_characters(tmp_path, monkeypatch) -> None:
    # reviewer finding: OUTPUT_MAX_BYTES is a byte cap — a multibyte-heavy
    # provider response whose CHARACTER count is under the cap must still be
    # truncated at the encoded byte cap
    from turbochickens.adapter import _url_scrubbed  # noqa: F401  (import smoke)
    from turbochickens.settings import OUTPUT_MAX_BYTES
    from turbochickens.util import cap_text_bytes

    # positive seam: the helper itself
    text = "é" * (OUTPUT_MAX_BYTES // 2 + 10)  # 2 bytes/char → over the byte cap, chars under it
    assert len(text) < OUTPUT_MAX_BYTES < len(text.encode("utf-8"))
    capped, truncated = cap_text_bytes(text, OUTPUT_MAX_BYTES)
    assert truncated is True and len(capped.encode("utf-8")) <= OUTPUT_MAX_BYTES
    plain = "a" * (OUTPUT_MAX_BYTES + 5)
    capped2, truncated2 = cap_text_bytes(plain, OUTPUT_MAX_BYTES)
    assert truncated2 is True and len(capped2) == OUTPUT_MAX_BYTES
    ok, trunc3 = cap_text_bytes("short", OUTPUT_MAX_BYTES)
    assert trunc3 is False and ok == "short"

    # contract seam: the adapter caps the provider content on encoded bytes
    monkeypatch.setenv("MISTRAL_API_KEY", "sk-contract")
    big = "é" * (OUTPUT_MAX_BYTES // 2 + 10)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=ok_payload(json.dumps({"message": big})))

    adapter = MistralAPIAdapter(make_spec(), providers=make_providers(), backoff_base_sec=0.01, max_retries=0)
    _, result, _, _, _, _ = drive(adapter, make_ctx(tmp_path), handler)
    assert result.truncated is True
    assert len(result.raw_output.encode("utf-8")) <= OUTPUT_MAX_BYTES
