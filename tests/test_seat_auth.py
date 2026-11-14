"""Tests for spec 09: seat/subscription auth (OAuth device flow) via the
credential broker behind the existing `CredentialProvider` seam.

Seams:
- pure: `ProviderProfile.auth` + `oauth_*` fields in providers.toml (seat
  requires the OAuth endpoints, api-key rejects them, default unchanged).
- store: `SeatStore` round-trip, 0600/0700 permissions, corrupt-file
  fail-closed, delete.
- contract: `SeatBroker` over `httpx.MockTransport` (valid session, expired
  token → refresh, revoked → fail closed, no session → fail closed, api-key
  passthrough unchanged) and `MistralAPIAdapter` driven with the broker (seat
  bearer token sent; no session → no request, clear error).
- device flow: `SeatBroker.login` against a hermetic loopback fake OAuth
  server (auto-approve, authorization_pending-then-success, api-key provider
  rejected) + `tc auth login/status/logout` CLI.
- e2e: the real daemon on a temp socket running `mistral-api` jobs against a
  hermetic loopback fake provider + fake OAuth server, with NO *_API_KEY in
  the environment — seat run succeeds, expired token is refreshed before the
  run, revoked seat fails closed without any API request, and a vibe-cli run
  without an env key still works (vibe's own profile via HOME passthrough).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import stat
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from typer.testing import CliRunner

from test_daemon import DaemonThread
from turbochickens.adapter import MistralAPIAdapter, RunContext
from turbochickens.audit import AuditLog
from turbochickens.cli.main import app
from turbochickens.client import request
from turbochickens.providers import ProvidersConfig, ProvidersError, load_providers
from turbochickens.seat import SeatBroker, SeatError, SeatSession, SeatStore
from turbochickens.settings import Settings, load_settings
from turbochickens.spec.model import JobSpec

runner = CliRunner()

SCHEMA = {
    "type": "object",
    "required": ["message"],
    "additionalProperties": False,
    "properties": {"message": {"type": "string"}},
}

DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"


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

SEAT_PROVIDERS_TOML = """
[providers.seatco]
base_url = "http://127.0.0.1:{api_port}/v1"
api_key_env = "SEATCO_TOKEN"
auth = "seat"
oauth_device_url = "http://127.0.0.1:{oauth_port}/device"
oauth_token_url = "http://127.0.0.1:{oauth_port}/token"
oauth_client_id = "tc-test-client"
oauth_scope = "openid profile"

[providers.seatco.models.seat-model]
context_window = 8192
input_usd_per_mtok = 1.0
output_usd_per_mtok = 2.0
json_mode = true
"""


def test_builtin_defaults_stay_api_key_and_seat_profile_parses(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("TURBOCHICKENS_PROVIDERS", raising=False)
    cfg = load_providers(data_dir=tmp_path)
    assert cfg.providers["mistral"].auth == "api-key"  # default, unchanged behavior
    assert cfg.providers["mistral"].oauth_token_url is None

    f = tmp_path / "providers.toml"
    f.write_text(SEAT_PROVIDERS_TOML.format(api_port=1, oauth_port=2))
    cfg2 = load_providers(f, data_dir=tmp_path)
    p = cfg2.providers["seatco"]
    assert p.auth == "seat"
    assert p.oauth_device_url == "http://127.0.0.1:2/device"
    assert p.oauth_token_url == "http://127.0.0.1:2/token"
    assert p.oauth_client_id == "tc-test-client"
    assert p.api_key_env == "SEATCO_TOKEN"
    name, model, provider, profile = cfg2.resolve("seatco/seat-model")
    assert (name, model) == ("seatco", "seat-model") and provider.auth == "seat"
    assert profile.context_window == 8192


def test_seat_provider_requires_oauth_endpoints(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("TURBOCHICKENS_PROVIDERS", raising=False)
    f = tmp_path / "providers.toml"
    f.write_text(
        '[providers.seatco]\nbase_url = "http://x/v1"\nauth = "seat"\n'
        'oauth_device_url = "http://x/device"\noauth_token_url = "http://x/token"\n'
        '[providers.seatco.models.m]\ncontext_window = 1000\ninput_usd_per_mtok = 0.1\noutput_usd_per_mtok = 0.2\n'
    )
    with pytest.raises(ProvidersError, match="oauth_client_id"):
        load_providers(f, data_dir=tmp_path)


def test_api_key_provider_rejects_oauth_fields(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("TURBOCHICKENS_PROVIDERS", raising=False)
    f = tmp_path / "providers.toml"
    f.write_text(
        '[providers.x]\nbase_url = "http://x/v1"\napi_key_env = "X_KEY"\n'
        'oauth_token_url = "http://x/token"\n'
        '[providers.x.models.m]\ncontext_window = 1000\ninput_usd_per_mtok = 0.1\noutput_usd_per_mtok = 0.2\n'
    )
    with pytest.raises(ProvidersError, match="only valid with auth='seat'"):
        load_providers(f, data_dir=tmp_path)


def test_unknown_auth_literal_is_rejected(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("TURBOCHICKENS_PROVIDERS", raising=False)
    f = tmp_path / "providers.toml"
    f.write_text(
        '[providers.x]\nbase_url = "http://x/v1"\nauth = "kerberos"\n'
        '[providers.x.models.m]\ncontext_window = 1000\ninput_usd_per_mtok = 0.1\noutput_usd_per_mtok = 0.2\n'
    )
    with pytest.raises(ProvidersError, match="auth"):
        load_providers(f, data_dir=tmp_path)


# --- SeatStore ----------------------------------------------------------------


def test_seat_store_roundtrip_permissions_delete(tmp_path) -> None:
    store = SeatStore(tmp_path / "data")
    sess = SeatSession(
        provider="seatco", access_token="tok-1", refresh_token="ref-1", expires_at=time.time() + 500
    )
    store.save(sess)
    f = store.dir / "seatco.json"
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.dir.stat().st_mode) == 0o700
    loaded = store.load("seatco")
    assert loaded is not None
    assert loaded.access_token == "tok-1" and loaded.refresh_token == "ref-1"
    assert loaded.expires_at == sess.expires_at and loaded.expired is False
    assert store.load("missing") is None
    assert store.delete("seatco") is True and store.load("seatco") is None
    assert store.delete("seatco") is False


def test_seat_store_corrupt_file_fails_closed(tmp_path) -> None:
    store = SeatStore(tmp_path / "data")
    store.save(SeatSession("seatco", "t", "r", time.time() + 100))
    (store.dir / "seatco.json").write_text("{not json")
    with pytest.raises(SeatError, match="unreadable"):
        store.load("seatco")


# --- broker contract over httpx.MockTransport ----------------------------------


def seat_providers_config() -> ProvidersConfig:
    return ProvidersConfig.model_validate(
        {
            "providers": {
                "seatco": {
                    "base_url": "http://provider.test/v1",
                    "api_key_env": "SEATCO_TOKEN",
                    "auth": "seat",
                    "oauth_device_url": "http://oauth.test/device",
                    "oauth_token_url": "http://oauth.test/token",
                    "oauth_client_id": "tc-test-client",
                    "oauth_scope": "openid",
                    "models": {
                        "seat-model": {
                            "context_window": 8192,
                            "input_usd_per_mtok": 1.0,
                            "output_usd_per_mtok": 2.0,
                            "json_mode": True,
                        }
                    },
                },
                "apikeyco": {
                    "base_url": "http://apikey.test/v1",
                    "api_key_env": "APIKEYCO_KEY",
                    "models": {
                        "m": {
                            "context_window": 8192,
                            "input_usd_per_mtok": 1.0,
                            "output_usd_per_mtok": 2.0,
                            "json_mode": True,
                        }
                    },
                },
            }
        }
    )


def make_seat_spec(model: str = "seatco/seat-model") -> JobSpec:
    return JobSpec.model_validate(
        {
            "job": {"name": "seat-job"},
            "trigger": {"type": "manual"},
            "harness": {"adapter": "mistral-api", "model": model, "temperature": 0.0},
            "resources": {"timeout_sec": 30, "max_context_mb": 2, "network": "none"},
            "input": {"prompt_file": "prompts/p.txt", "attachments": []},
            "output": {"format": "json", "schema_file": "schemas/o.json"},
        }
    )


def make_ctx(tmp_path: Path, spec: JobSpec) -> RunContext:
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    (ws / "prompt.txt").write_text("Say hello")
    return RunContext(
        run_id="run-seat-contract",
        job_id=spec.job.name,
        spec=spec,
        spec_path=tmp_path / "job.toml",
        spec_hash="abc",
        trigger="manual",
        workspace=ws,
    )


def seed(tmp_path: Path, providers: ProvidersConfig, access="seat-access-1", refresh="seat-refresh-1",
         expires_in: float = 3600) -> SeatBroker:
    settings = Settings(data_dir=tmp_path / "data")
    broker = SeatBroker(settings, providers)
    broker.store.save(
        SeatSession(provider="seatco", access_token=access, refresh_token=refresh,
                    expires_at=time.time() + expires_in)
    )
    return broker


def test_broker_seat_credentials_and_telemetry_without_any_env_key(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("SEATCO_TOKEN", raising=False)
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    providers = seat_providers_config()
    broker = seed(tmp_path, providers)
    ctx = make_ctx(tmp_path, make_seat_spec())
    creds = broker.credentials(ctx)
    assert creds == {"SEATCO_TOKEN": "seat-access-1"}  # the only injected var
    tele = broker.auth_telemetry(ctx)
    assert tele["mode"] == "seat"
    assert tele["token_expires_at"] and tele["token_expires_at"].endswith("Z")
    assert "seat-access-1" not in json.dumps(tele)  # expiry only, never the token


def test_broker_refreshes_expired_token(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("SEATCO_TOKEN", raising=False)
    providers = seat_providers_config()
    settings = Settings(data_dir=tmp_path / "data")
    seen: list[httpx.Request] = []

    def oauth_handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.url.path == "/token"
        return httpx.Response(
            200,
            json={
                "access_token": "seat-access-fresh",
                "refresh_token": "seat-refresh-2",
                "token_type": "Bearer",
                "expires_in": 3600,
            },
        )

    broker = SeatBroker(settings, providers, client=httpx.Client(transport=httpx.MockTransport(oauth_handler)))
    broker.store.save(
        SeatSession("seatco", "seat-access-old", "seat-refresh-1", expires_at=time.time() - 5)
    )
    ctx = make_ctx(tmp_path, make_seat_spec())
    creds = broker.credentials(ctx)
    assert creds == {"SEATCO_TOKEN": "seat-access-fresh"}
    body = urllib.parse.parse_qs(seen[0].content.decode())
    assert body["grant_type"] == ["refresh_token"] and body["refresh_token"] == ["seat-refresh-1"]
    assert body["client_id"] == ["tc-test-client"]
    # the refreshed session is persisted (rotated refresh token, future expiry)
    stored = broker.store.load("seatco")
    assert stored is not None and stored.access_token == "seat-access-fresh"
    assert stored.refresh_token == "seat-refresh-2" and not stored.expired
    tele = broker.auth_telemetry(ctx)
    assert tele["mode"] == "seat" and tele["token_expires_at"] is not None


def test_broker_revoked_seat_fails_closed_and_deletes_session(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("SEATCO_TOKEN", raising=False)
    providers = seat_providers_config()
    settings = Settings(data_dir=tmp_path / "data")

    def oauth_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant", "error_description": "revoked"})

    broker = SeatBroker(settings, providers, client=httpx.Client(transport=httpx.MockTransport(oauth_handler)))
    broker.store.save(SeatSession("seatco", "old", "ref-1", expires_at=time.time() - 5))
    ctx = make_ctx(tmp_path, make_seat_spec())
    with pytest.raises(SeatError, match="revoked or expired"):
        broker.credentials(ctx)
    with pytest.raises(SeatError, match="tc auth login"):
        broker.credentials(ctx)  # session was deleted — still fail closed
    assert broker.store.load("seatco") is None


def test_broker_without_session_fails_closed_with_login_hint(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("SEATCO_TOKEN", raising=False)
    providers = seat_providers_config()
    broker = SeatBroker(Settings(data_dir=tmp_path / "data"), providers)
    ctx = make_ctx(tmp_path, make_seat_spec())
    with pytest.raises(SeatError, match="tc auth login seatco"):
        broker.credentials(ctx)


def test_broker_api_key_provider_keeps_env_allowlist_behavior(tmp_path, monkeypatch) -> None:
    providers = seat_providers_config()
    broker = SeatBroker(Settings(data_dir=tmp_path / "data"), providers)
    ctx = make_ctx(tmp_path, make_seat_spec(model="apikeyco/m"))
    monkeypatch.delenv("APIKEYCO_KEY", raising=False)
    assert broker.credentials(ctx) == {}  # nothing leaks; the adapter fails closed downstream
    assert broker.auth_telemetry(ctx)["mode"] == "api-key"
    monkeypatch.setenv("APIKEYCO_KEY", "sk-env")
    assert broker.credentials(ctx) == {"APIKEYCO_KEY": "sk-env"}
    assert broker.auth_telemetry(ctx) == {"mode": "api-key", "token_expires_at": None}


def _drive_adapter(adapter, ctx, api_handler, timeout_sec: int = 10):
    events: list[tuple[str, str, str]] = []
    seen: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return api_handler(request)

    async def go():
        async def sink(source, level, line):
            events.append((source, level, line))

        client = httpx.AsyncClient(transport=httpx.MockTransport(recording))
        adapter.client = client
        prepared = adapter.prepare(ctx)
        env_keys = sorted(ctx.env.keys())  # snapshot before teardown clears it
        result = await adapter.execute(prepared, sink, timeout_sec)
        await adapter.teardown(ctx)
        await client.aclose()
        return prepared, result, env_keys

    prepared, result, env_keys = asyncio.run(go())
    return prepared, result, events, seen, env_keys


def ok_payload() -> dict:
    return {
        "choices": [{"index": 0, "message": {"role": "assistant", "content": '{"message": "hi"}'}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 40, "total_tokens": 140},
    }


def test_contract_adapter_sends_seat_bearer_token_and_zero_cost(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("SEATCO_TOKEN", raising=False)
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    providers = seat_providers_config()
    broker = seed(tmp_path, providers, access="seat-access-xyz")
    adapter = MistralAPIAdapter(make_seat_spec(), providers=providers, credential_provider=broker, backoff_base_sec=0.01)
    ctx = make_ctx(tmp_path, make_seat_spec())
    prepared, result, events, seen, env_keys = _drive_adapter(
        adapter, ctx, lambda req: httpx.Response(200, json=ok_payload())
    )
    assert len(seen) == 1
    assert seen[0].headers["authorization"] == "Bearer seat-access-xyz"
    assert result.exit_code == 0 and result.error is None
    # seat billing: zero per-token cost + seat telemetry, never a fabricated price
    assert result.usage["cost_usd"] == 0.0 and result.usage["billed_via"] == "seat"
    assert result.usage["tokens_in"] == 100
    # manifest telemetry: mode + expiry, no token; per-run env carried only the allowlisted var
    assert prepared.auth["mode"] == "seat" and prepared.auth["token_expires_at"]
    assert "seat-access-xyz" not in json.dumps(prepared.auth)
    assert "SEATCO_TOKEN" in env_keys
    assert "seat-access-xyz" not in json.dumps(env_keys)  # keys only, never values, in telemetry


def test_contract_adapter_without_seat_session_sends_no_request(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("SEATCO_TOKEN", raising=False)
    providers = seat_providers_config()
    broker = SeatBroker(Settings(data_dir=tmp_path / "data"), providers)
    adapter = MistralAPIAdapter(make_seat_spec(), providers=providers, credential_provider=broker, backoff_base_sec=0.01)
    ctx = make_ctx(tmp_path, make_seat_spec())
    with pytest.raises(SeatError, match="tc auth login"):
        adapter.prepare(ctx)  # fail closed at prepare, before any HTTP
    # and even if prepare somehow passed, execute sends nothing without the var
    ctx.env = {"TURBOCHICKENS_RUN_ID": ctx.run_id}
    prepared = type("P", (), {"context": ctx})()
    result = asyncio.run(adapter.execute(prepared, _noop_sink, 5))
    assert result.error and "SEATCO_TOKEN" in result.error


async def _noop_sink(source, level, line):
    return None


# --- hermetic loopback fake OAuth + API servers ---------------------------------


class FakeOAuthHandler(BaseHTTPRequestHandler):
    """Scripted OAuth device-flow server. `server.mode` ∈ auto | pending_once |
    refresh_ok | revoked."""

    def log_message(self, *args) -> None:
        pass

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        params = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"))
        params = {k: v[0] for k, v in params.items()}
        self.server.oauth_requests.append({"path": self.path, "params": params})
        if self.path == "/device":
            port = self.server.server_address[1]
            self._send(
                200,
                {
                    "device_code": "dev-123",
                    "user_code": "ABCD-EFGH",
                    "verification_uri": f"http://127.0.0.1:{port}/verify",
                    "interval": 0.05,
                    "expires_in": 300,
                },
            )
        elif self.path == "/token":
            gt = params.get("grant_type", "")
            if gt == DEVICE_GRANT:
                n = self.server.counters.get("device_polls", 0) + 1
                self.server.counters["device_polls"] = n
                if self.server.mode == "pending_once" and n == 1:
                    self._send(400, {"error": "authorization_pending"})
                else:
                    self._send(
                        200,
                        {
                            "access_token": "seat-access-1",
                            "refresh_token": "seat-refresh-1",
                            "token_type": "Bearer",
                            "expires_in": 3600,
                            "scope": "openid",
                        },
                    )
            elif gt == "refresh_token":
                if self.server.mode == "revoked":
                    self._send(400, {"error": "invalid_grant", "error_description": "refresh token revoked"})
                else:
                    n = self.server.counters.get("refreshes", 0) + 1
                    self.server.counters["refreshes"] = n
                    self._send(
                        200,
                        {
                            "access_token": f"seat-access-refreshed-{n}",
                            "refresh_token": f"seat-refresh-{n + 1}",
                            "token_type": "Bearer",
                            "expires_in": 3600,
                        },
                    )
            else:
                self._send(400, {"error": "unsupported_grant_type"})
        else:
            self._send(404, {"error": "not found"})

    def _send(self, code: int, payload: dict) -> None:
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class FakeAPIHandler(BaseHTTPRequestHandler):
    """Hermetic OpenAI-compatible endpoint; records the Authorization header."""

    def log_message(self, *args) -> None:
        pass

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        self.server.requests.append(
            {"path": self.path, "authorization": self.headers.get("Authorization"), "body": body}
        )
        self._send(200, ok_payload())

    def _send(self, code: int, payload: dict) -> None:
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def _serve(server) -> threading.Thread:
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return t


@pytest.fixture
def seat_env(tmp_path, monkeypatch):
    api = ThreadingHTTPServer(("127.0.0.1", 0), FakeAPIHandler)
    api.requests = []
    oauth = ThreadingHTTPServer(("127.0.0.1", 0), FakeOAuthHandler)
    oauth.oauth_requests = []
    oauth.counters = {}
    oauth.mode = "auto"
    t1 = _serve(api)
    t2 = _serve(oauth)
    # no API key anywhere in the environment — seat auth must carry the run
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    monkeypatch.delenv("SEATCO_TOKEN", raising=False)
    providers_file = tmp_path / "providers.toml"
    providers_file.write_text(
        SEAT_PROVIDERS_TOML.format(api_port=api.server_address[1], oauth_port=oauth.server_address[1])
    )
    monkeypatch.setenv("TURBOCHICKENS_PROVIDERS", str(providers_file))
    settings_file = tmp_path / "settings.toml"
    settings_file.write_text(f'data_dir = "{tmp_path / "data"}"\nweight_budget_override = 1\n')
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(settings_file))
    yield SimpleNamespace(
        tmp_path=tmp_path, api=api, oauth=oauth, providers_file=providers_file, settings_file=settings_file
    )
    api.shutdown(); api.server_close(); t1.join(timeout=5)
    oauth.shutdown(); oauth.server_close(); t2.join(timeout=5)


@pytest.fixture
def seat_daemon(seat_env):
    settings = load_settings(seat_env.settings_file)
    dt = DaemonThread(settings)
    dt.start()
    yield dt
    dt.stop()


def write_seat_job(tmp_path: Path, name: str = "seat-job", model: str = "seatco/seat-model",
                   adapter: str = "mistral-api") -> Path:
    d = tmp_path / name
    (d / "prompts").mkdir(parents=True, exist_ok=True)
    (d / "schemas").mkdir(parents=True, exist_ok=True)
    (d / "prompts" / "prompt.txt").write_text("Say hello")
    (d / "schemas" / "out.schema.json").write_text(json.dumps(SCHEMA))
    (d / "job.toml").write_text(
        f"""
[job]
name = "{name}"

[trigger]
type = "manual"

[harness]
adapter = "{adapter}"
model = "{model}"
temperature = 0.0

[resources]
timeout_sec = 30
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


def broker_for(seat_env) -> SeatBroker:
    settings = load_settings(seat_env.settings_file)
    return SeatBroker(settings, load_providers(settings.providers_path, settings.data_dir))


def seed_session(seat_env, access="seat-access-1", refresh="seat-refresh-1", expires_in: float = 3600) -> None:
    broker = broker_for(seat_env)
    broker.store.save(
        SeatSession("seatco", access, refresh, expires_at=time.time() + expires_in)
    )


# --- device flow + CLI ----------------------------------------------------------


def test_device_flow_login_auto_approve_stores_session(seat_env) -> None:
    broker = broker_for(seat_env)
    notes: list[str] = []
    session = broker.login("seatco", notify=notes.append)
    assert session.access_token == "seat-access-1" and session.refresh_token == "seat-refresh-1"
    assert not session.expired
    assert any("ABCD-EFGH" in n and "http" in n for n in notes)  # verification uri + code shown
    stored = broker.store.load("seatco")
    assert stored is not None and stored.access_token == "seat-access-1"
    # the device grant went to the device endpoint, then the token endpoint
    paths = [r["path"] for r in seat_env.oauth.oauth_requests]
    assert paths == ["/device", "/token"]
    assert seat_env.oauth.oauth_requests[1]["params"]["grant_type"] == DEVICE_GRANT


def test_device_flow_authorization_pending_then_success(seat_env) -> None:
    seat_env.oauth.mode = "pending_once"
    broker = broker_for(seat_env)
    session = broker.login("seatco", notify=lambda _n: None)
    assert session.access_token == "seat-access-1"
    assert seat_env.oauth.counters["device_polls"] == 2  # one pending, one success


def test_login_rejects_api_key_provider(seat_env) -> None:
    providers = seat_providers_config()  # has an api-key provider "apikeyco"
    broker = SeatBroker(load_settings(seat_env.settings_file), providers)
    with pytest.raises(SeatError, match="auth='api-key'"):
        broker.login("apikeyco")
    with pytest.raises(SeatError, match="unknown provider"):
        broker.login("no-such-provider")


def test_cli_auth_login_status_logout_never_print_tokens(seat_env) -> None:
    r = runner.invoke(app, ["auth", "login", "seatco"])
    assert r.exit_code == 0, combined_output(r)
    assert "perch seat claimed for provider 'seatco'" in r.output
    assert "seat-access-1" not in r.output and "seat-refresh-1" not in r.output
    assert any("ABCD-EFGH" in line for line in r.output.splitlines())

    r = runner.invoke(app, ["auth", "status"])
    assert r.exit_code == 0, combined_output(r)
    assert "provider=seatco" in r.output and "auth=seat" in r.output
    assert "expires_at=" in r.output and "expired=False" in r.output
    assert "seat-access-1" not in r.output

    r = runner.invoke(app, ["auth", "logout", "seatco"])
    assert r.exit_code == 0, combined_output(r)
    r = runner.invoke(app, ["auth", "status"])
    assert "none (run `tc auth login seatco`)" in r.output
    r = runner.invoke(app, ["auth", "logout", "seatco"])
    assert r.exit_code == 1 and "no perch seat" in combined_output(r)


# --- e2e through the daemon ------------------------------------------------------


def test_e2e_seat_run_succeeds_with_no_api_key_in_env(seat_env, seat_daemon, tmp_path) -> None:
    # establish the seat session via the device flow against the fake OAuth server
    broker = broker_for(seat_env)
    session = broker.login("seatco", notify=lambda _n: None)
    assert session.access_token == "seat-access-1"

    spec_path = write_seat_job(tmp_path)
    result = runner.invoke(app, ["run", str(spec_path), "--wait"])
    assert result.exit_code == 0, combined_output(result)
    run_id = run_id_of(result)
    settings = seat_daemon.settings
    run = wait_terminal(settings.socket_path, run_id)
    assert run["status"] == "succeeded"

    m = run["manifest"]
    # manifest records auth mode + token expiry — never the token
    assert m["auth"]["mode"] == "seat" and m["auth"]["token_expires_at"]
    assert "SEATCO_TOKEN" in m["env_keys"]
    assert "seat-access-1" not in json.dumps(m)
    # seat usage: zero per-token cost + seat telemetry
    assert m["usage"]["cost_usd"] == 0.0 and m["usage"]["billed_via"] == "seat"
    assert m["usage"]["tokens_in"] == 100

    # the provider saw exactly one authenticated call with the seat-derived bearer token
    reqs = [r for r in seat_env.api.requests if r["path"] == "/v1/chat/completions"]
    assert len(reqs) == 1
    assert reqs[0]["authorization"] == "Bearer seat-access-1"
    assert reqs[0]["body"]["model"] == "seat-model"

    # audit records the mode per run; no token anywhere in the audit log
    events = AuditLog(settings.audit_log_path).read_all()
    auth_rows = [e for e in events if e["event"] == "perch.resolved" and e.get("run_id") == run_id]
    assert len(auth_rows) == 1 and auth_rows[0]["mode"] == "seat" and auth_rows[0]["token_expires_at"]
    fin = [e for e in events if e["event"] == "chicken.finished" and e.get("run_id") == run_id]
    assert fin and fin[0]["auth_mode"] == "seat"
    assert "seat-access-1" not in json.dumps(events)

    # validated result served; stats report zero expected cost (seat, not per-token)
    res = runner.invoke(app, ["results", "get", run_id])
    assert res.exit_code == 0, combined_output(res)
    assert json.loads(res.output)["message"] == "hi"
    stats = request(settings.socket_path, "stats_show", job_id="seat-job", timeout=2.0)["stats"]
    assert stats["n_runs"] == 1 and stats["expected_cost_usd"] == 0.0

    # teardown attested; the session file is 0600 and holds the token only at rest
    att = db_row(settings, "SELECT process_reaped, workspace_removed, env_cleared FROM teardown_attestations WHERE run_id = ?", (run_id,))
    assert att == (1, 1, 1)
    sess_file = settings.seats_dir / "seatco.json"
    assert sess_file.exists() and stat.S_IMODE(sess_file.stat().st_mode) == 0o600
    assert "seat-access-1" in sess_file.read_text()  # at rest only — never in specs/manifest/audit


def test_e2e_expired_seat_token_is_refreshed_before_the_run(seat_env, seat_daemon, tmp_path) -> None:
    seed_session(seat_env, access="seat-access-old", refresh="seat-refresh-1", expires_in=-10)
    seat_env.oauth.mode = "refresh_ok"

    spec_path = write_seat_job(tmp_path, name="seat-refresh-job")
    result = runner.invoke(app, ["run", str(spec_path), "--wait"])
    assert result.exit_code == 0, combined_output(result)
    run_id = run_id_of(result)
    run = wait_terminal(seat_daemon.settings.socket_path, run_id)
    assert run["status"] == "succeeded"

    # a refresh grant hit the token endpoint, and the API saw the NEW token
    refresh_reqs = [
        r for r in seat_env.oauth.oauth_requests if r["params"].get("grant_type") == "refresh_token"
    ]
    assert len(refresh_reqs) == 1 and refresh_reqs[0]["params"]["refresh_token"] == "seat-refresh-1"
    api_reqs = [r for r in seat_env.api.requests if r["path"] == "/v1/chat/completions"]
    assert len(api_reqs) == 1
    assert api_reqs[0]["authorization"] == "Bearer seat-access-refreshed-1"
    assert run["manifest"]["auth"]["mode"] == "seat"

    # the stored session was rotated
    stored = SeatStore(seat_daemon.settings.data_dir).load("seatco")
    assert stored is not None and stored.access_token == "seat-access-refreshed-1" and not stored.expired


def test_e2e_revoked_seat_fails_closed_without_any_api_request(seat_env, seat_daemon, tmp_path) -> None:
    seed_session(seat_env, expires_in=-10)  # expired → forces a refresh attempt
    seat_env.oauth.mode = "revoked"

    spec_path = write_seat_job(tmp_path, name="seat-revoked-job")
    result = runner.invoke(app, ["run", str(spec_path), "--wait"])
    assert result.exit_code == 1
    run_id = run_id_of(result)
    settings = seat_daemon.settings
    run = wait_terminal(settings.socket_path, run_id)
    assert run["status"] == "failed"
    reason = run["manifest"]["status_reason"]
    assert "SeatError" in reason and "tc auth login seatco" in reason
    assert run["manifest"]["auth"]["mode"] == "seat"  # mode still recorded
    assert seat_env.api.requests == []  # fail closed: no request without a valid credential
    assert not (settings.seats_dir / "seatco.json").exists()  # revoked session deleted
    # teardown attestation still happened on the failed run
    att = db_row(settings, "SELECT process_reaped, workspace_removed, env_cleared FROM teardown_attestations WHERE run_id = ?", (run_id,))
    assert att == (1, 1, 1)


# --- vibe-cli: seat-configured vibe profile via HOME passthrough -----------------

FAKE_VIBE_ENVFACT = '''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
if "--version" in args:
    print("vibe 9.9.9-fake")
    sys.exit(0)
prompt = ""
if "--prompt-file" in args:
    with open(args[args.index("--prompt-file") + 1]) as f:
        prompt = f.read()
out_dir = args[args.index("--output-dir") + 1] if "--output-dir" in args else None
if out_dir:
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "export.json"), "w") as f:
        json.dump({"usage": {"input_tokens": 10, "output_tokens": 5}}, f)
# report which credential-relevant env the harness actually saw
print(
    "ENVFACT home=%s mistral_api_key=%s seatco_token=%s"
    % (
        "set" if os.environ.get("HOME") else "unset",
        "set" if os.environ.get("MISTRAL_API_KEY") else "unset",
        "set" if os.environ.get("SEATCO_TOKEN") else "unset",
    ),
    file=sys.stderr,
)
print(json.dumps({"message": "hello from fake vibe"}))
'''


def test_e2e_vibe_cli_without_env_key_uses_its_own_profile(seat_env, tmp_path, monkeypatch) -> None:
    """vibe-cli needs no API key: HOME/XDG_* pass through BASE_ENV_KEYS, so a
    browser-auth provider registered in ~/.vibe/config.toml (vibe's own
    ProviderConfig browser_auth_* fields) is picked up by the vibe binary
    itself. TurboChickens injects no credential and records mode "none"."""
    bin_path = tmp_path / "fake-vibe"
    bin_path.write_text(FAKE_VIBE_ENVFACT)
    bin_path.chmod(0o755)
    monkeypatch.setenv("TURBOCHICKENS_VIBE_BIN", str(bin_path))
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)

    settings = load_settings(seat_env.settings_file)
    dt = DaemonThread(settings)
    dt.start()
    try:
        spec_path = write_seat_job(tmp_path, name="vibe-seat-job", adapter="vibe-cli", model="mistral-medium-3.5")
        result = runner.invoke(app, ["run", str(spec_path), "--wait"])
        assert result.exit_code == 0, combined_output(result)
        run_id = run_id_of(result)
        run = wait_terminal(settings.socket_path, run_id)
        assert run["status"] == "succeeded"
        # no env credential was injected; the harness used its own profile
        assert run["manifest"]["auth"]["mode"] == "none"
        assert "MISTRAL_API_KEY" not in run["manifest"]["env_keys"]
        logs = request(settings.socket_path, "runs_logs", run_id=run_id, timeout=2.0)["events"]
        envfacts = [e["line"] for e in logs if "ENVFACT" in e["line"]]
        assert envfacts, f"fake vibe env report missing from logs: {[e['line'] for e in logs]}"
        assert "home=set" in envfacts[0]  # HOME passthrough → ~/.vibe/config.toml reachable
        assert "mistral_api_key=unset" in envfacts[0]
        assert "seatco_token=unset" in envfacts[0]
    finally:
        dt.stop()
