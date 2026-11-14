"""Tests for spec-32 ticket 13 (review finding G10): Feeder/OAuth response
bytes never reach errors, run logs, or served payloads.

Two legs, both asserted at the highest existing seams:

- **secret echo**: a hostile Feeder/OAuth endpoint echoing the posted
  credential (a token endpoint reflecting the refresh/device token) in its
  raw error body must never appear in errors, run events, the manifest, the
  Headcount, served socket payloads, CLI output, or any file under the data
  dir. Errors carry the status code + the parsed `error` field only.
- **terminal injection**: a provider error carrying ANSI/OSC escape sequences
  is sanitized before it can reach `tc runs logs` (and `tc runs list` /
  `tc run --wait`) raw — supervisor-authored log lines included.

Seams: the shared `http_error_detail` pure seam; `SeatBroker` over
`httpx.MockTransport`; the daemon socket (`auth_login_poll`, `runs_show`,
`runs_list`, `runs_logs`); e2e through the real daemon (in a thread) against
hermetic loopback fake Feeder + fake OAuth servers, driven by the `tc` CLI.
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import stat
import threading
import time
import unicodedata
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
from turbochickens.models import fetch_models
from turbochickens.providers import ProvidersConfig, load_providers
from turbochickens.seat import SeatBroker, SeatError, SeatSession
from turbochickens.settings import Settings, load_settings
from turbochickens.spec.model import JobSpec
from turbochickens.util import WITHHELD_NON_JSON_BODY, http_error_detail

runner = CliRunner()

SCHEMA = {
    "type": "object",
    "required": ["message"],
    "additionalProperties": False,
    "properties": {"message": {"type": "string"}},
}

DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"

#: planted secret markers — they stand in for real token material
REFRESH_MARKER = "PLANTED-REFRESH-TOKEN-9f8e7d6c"
DEVICE_MARKER = "PLANTED-DEVICE-CODE-abc123"
RAW_MARKER = "PLANTED-RAW-BODY-MARKER-456"
#: ANSI/OSC probe bytes a hostile Feeder puts in a parsed error message
OSC = "\x1b]8;;https://evil.example\x07"
ANSI_RED = "\x1b[31m"


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


def has_control_chars(s: str) -> bool:
    """Any Unicode C* char (ANSI/OSC escapes live here) except tab/newline."""
    return any(
        unicodedata.category(ch).startswith("C") and ch not in ("\t", "\n") for ch in s
    )


def db_rows(settings, sql: str, params=()) -> list[tuple]:
    conn = sqlite3.connect(str(settings.db_path))
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def sweep_data_dir(settings, *markers: str) -> list[str]:
    """Return the data-dir files whose bytes contain any planted marker."""
    hits: list[str] = []
    for p in sorted(settings.data_dir.rglob("*")):
        if p.is_file() and not stat.S_ISSOCK(p.stat().st_mode):
            data = p.read_bytes()
            for marker in markers:
                if marker.encode() in data:
                    hits.append(f"{p.relative_to(settings.data_dir)} :: {marker}")
    return hits


# --- pure seam: http_error_detail --------------------------------------------


def test_http_error_detail_parsed_error_field_only_never_raw_body() -> None:
    # non-JSON body echoing a credential → fixed withheld marker, no body bytes
    r = http_error_detail(httpx.Response(400, content=f"invalid_grant refresh_token={REFRESH_MARKER}".encode()))
    assert r == WITHHELD_NON_JSON_BODY
    assert REFRESH_MARKER not in r
    # JSON body with the marker in a non-error field → parsed error only
    r = http_error_detail(httpx.Response(400, json={"error": "invalid_grant", "echo": REFRESH_MARKER}))
    assert r == "invalid_grant"
    # dict-style error message is kept (positive), sanitized of escapes
    r = http_error_detail(httpx.Response(400, json={"error": {"message": f"bad {OSC}{ANSI_RED}red"}}))
    assert "bad" in r and "red" in r
    assert not has_control_chars(r)
    # JSON without any usable error field → withheld marker, body bytes dropped
    r = http_error_detail(httpx.Response(400, json={"status": "nope", "echo": RAW_MARKER}))
    assert "withheld by the fox" in r and RAW_MARKER not in r
    # positive: plain parsed message survives verbatim
    r = http_error_detail(httpx.Response(429, json={"error": {"message": "rate limited"}}))
    assert r == "rate limited"


# --- contract seam: SeatBroker over MockTransport ------------------------------


def _seat_providers() -> ProvidersConfig:
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
                    "models": {"seat-model": {"context_window": 8192, "input_usd_per_mtok": 1.0, "output_usd_per_mtok": 2.0, "json_mode": True}},
                }
            }
        }
    )


def _seat_ctx(tmp_path: Path) -> RunContext:
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    (ws / "prompt.txt").write_text("hi")
    spec = JobSpec.model_validate(
        {
            "job": {"name": "seat-job"},
            "trigger": {"type": "manual"},
            "harness": {"adapter": "mistral-api", "model": "seatco/seat-model", "temperature": 0.0},
            "resources": {"timeout_sec": 30, "max_context_mb": 2, "network": "none"},
            "input": {"prompt_file": "prompts/p.txt", "attachments": []},
            "output": {"format": "json", "schema_file": "schemas/o.json"},
        }
    )
    return RunContext(
        run_id="run-hygiene",
        job_id="seat-job",
        spec=spec,
        spec_path=tmp_path / "job.toml",
        spec_hash="abc",
        trigger="manual",
        workspace=ws,
    )


def test_refresh_token_endpoint_echo_never_reaches_seat_error(tmp_path, monkeypatch) -> None:
    """Negative: a token endpoint whose raw error body reflects the posted
    refresh token must not leak into the SeatError (which lands in run data).
    Positive: the status code + parsed error code still surface."""
    monkeypatch.delenv("SEATCO_TOKEN", raising=False)
    providers = _seat_providers()
    settings = Settings(data_dir=tmp_path / "data")

    def echo_handler(request: httpx.Request) -> httpx.Response:
        # hostile echo: non-JSON body repeating the posted refresh_token
        return httpx.Response(400, content=f"invalid_grant: refresh_token={REFRESH_MARKER}".encode())

    broker = SeatBroker(settings, providers, client=httpx.Client(transport=httpx.MockTransport(echo_handler)))
    broker.store.save(SeatSession("seatco", "old-access", REFRESH_MARKER, expires_at=time.time() - 5))
    with pytest.raises(SeatError) as excinfo:
        broker.credentials(_seat_ctx(tmp_path))
    msg = str(excinfo.value)
    assert REFRESH_MARKER not in msg  # the echo never reaches the error
    assert "http 400" in msg  # status code still surfaces
    assert "tc auth login seatco" in msg  # actionable hint intact
    assert broker.store.load("seatco") is None  # fail closed, session deleted


def test_refresh_token_echo_in_json_non_error_field_never_reaches_seat_error(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("SEATCO_TOKEN", raising=False)
    providers = _seat_providers()
    settings = Settings(data_dir=tmp_path / "data")

    def echo_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": "invalid_grant", "error_description": f"refresh token revoked: {REFRESH_MARKER}"},
        )

    broker = SeatBroker(settings, providers, client=httpx.Client(transport=httpx.MockTransport(echo_handler)))
    broker.store.save(SeatSession("seatco", "old-access", REFRESH_MARKER, expires_at=time.time() - 5))
    with pytest.raises(SeatError) as excinfo:
        broker.credentials(_seat_ctx(tmp_path))
    msg = str(excinfo.value)
    assert REFRESH_MARKER not in msg
    assert "http 400" in msg and "invalid_grant" in msg  # parsed error field survives (positive)


def test_device_poll_echo_never_reaches_seat_error(tmp_path, monkeypatch) -> None:
    """The device-flow poll must not echo the posted device_code either."""
    monkeypatch.delenv("SEATCO_TOKEN", raising=False)
    providers = _seat_providers()
    settings = Settings(data_dir=tmp_path / "data")
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        if request.url.path == "/device":
            return httpx.Response(
                200,
                json={
                    "device_code": DEVICE_MARKER,
                    "user_code": "ABCD-EFGH",
                    "verification_uri": "http://oauth.test/verify",
                    "interval": 0.05,
                    "expires_in": 300,
                },
            )
        # /token: hostile echo of the posted device_code in a raw body
        return httpx.Response(400, content=f"access_denied: device_code={DEVICE_MARKER}".encode())

    broker = SeatBroker(settings, providers, client=httpx.Client(transport=httpx.MockTransport(handler)))
    grant = broker.start_device_flow("seatco")
    assert grant.device_code == DEVICE_MARKER
    with pytest.raises(SeatError) as excinfo:
        broker.poll_device_flow(grant)
    msg = str(excinfo.value)
    assert DEVICE_MARKER not in msg
    assert "http 400" in msg  # status code still surfaces (positive)


def test_seat_errors_still_carry_parsed_error_positive(tmp_path, monkeypatch) -> None:
    """Positive: a legitimate OAuth error (JSON, parsed `error` code) surfaces
    the status code + the code — the useful message is not lost."""
    monkeypatch.delenv("SEATCO_TOKEN", raising=False)
    providers = _seat_providers()
    settings = Settings(data_dir=tmp_path / "data")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant", "error_description": "revoked"})

    broker = SeatBroker(settings, providers, client=httpx.Client(transport=httpx.MockTransport(handler)))
    broker.store.save(SeatSession("seatco", "old", "ref-1", expires_at=time.time() - 5))
    with pytest.raises(SeatError) as excinfo:
        broker.credentials(_seat_ctx(tmp_path))
    msg = str(excinfo.value)
    assert "http 400" in msg and "invalid_grant" in msg


def test_adapter_provider_error_carries_status_and_parsed_message(tmp_path, monkeypatch) -> None:
    """Positive at the adapter seam: a real provider error still surfaces the
    status code + parsed error message in the run's failure reason."""
    monkeypatch.setenv("FEEDERCO_KEY", "sk-feeder-test")
    providers = ProvidersConfig.model_validate(
        {
            "providers": {
                "feederco": {
                    "base_url": "http://feeder.test/v1",
                    "api_key_env": "FEEDERCO_KEY",
                    "models": {"feeder-model": {"context_window": 8192, "input_usd_per_mtok": 1.0, "output_usd_per_mtok": 2.0, "json_mode": True}},
                }
            }
        }
    )
    spec = JobSpec.model_validate(
        {
            "job": {"name": "api-job"},
            "trigger": {"type": "manual"},
            "harness": {"adapter": "mistral-api", "model": "feederco/feeder-model", "temperature": 0.0},
            "resources": {"timeout_sec": 30, "max_context_mb": 2, "network": "none"},
            "input": {"prompt_file": "prompts/p.txt", "attachments": []},
            "output": {"format": "json", "schema_file": "schemas/o.json"},
        }
    )
    adapter = MistralAPIAdapter(spec, providers=providers, backoff_base_sec=0.01, max_retries=0)

    async def go():
        async def sink(source, level, line):
            return None

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda req: httpx.Response(400, json={"error": {"message": "bad request"}})
            )
        )
        adapter.client = client
        ctx = _seat_ctx(tmp_path)
        prepared = adapter.prepare(ctx)
        result = await adapter.execute(prepared, sink, 5)
        await client.aclose()
        return result

    result = asyncio.run(go())
    assert result.exit_code == 400
    assert "provider http 400" in result.error and "bad request" in result.error and "not retryable" in result.error


def test_models_error_carries_status_and_parsed_message_only() -> None:
    """`tc models list` error column: parsed error field kept, raw body never."""
    provider = ProvidersConfig.model_validate(
        {
            "providers": {
                "feederco": {
                    "base_url": "http://feeder.test/v1",
                    "api_key_env": "FEEDERCO_KEY",
                    "models": {},
                }
            }
        }
    ).providers["feederco"]

    def handler_json(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "no models for you"}})

    ids, err = fetch_models(provider, "sk-x", client=httpx.Client(transport=httpx.MockTransport(handler_json)))
    assert ids is None and "http 400" in err and "no models for you" in err  # positive

    def handler_raw(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, content=f"upstream exploded bearer={RAW_MARKER} {ANSI_RED}".encode())

    ids, err = fetch_models(provider, "sk-x", client=httpx.Client(transport=httpx.MockTransport(handler_raw)))
    assert ids is None and "http 400" in err
    assert RAW_MARKER not in err and not has_control_chars(err)  # negative: no body bytes, no escapes


# --- hermetic loopback fake Feeder + fake OAuth servers -------------------------


class FakeOAuthHandler(BaseHTTPRequestHandler):
    """Scripted OAuth server. mode ∈ auto | echo_refresh | echo_poll."""

    def log_message(self, *args) -> None:
        pass

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        params = {k: v[0] for k, v in urllib.parse.parse_qs(self.rfile.read(length).decode()).items()}
        self.server.oauth_requests.append({"path": self.path, "params": params})
        if self.path == "/device":
            self._send(
                200,
                {
                    "device_code": DEVICE_MARKER,
                    "user_code": "ABCD-EFGH",
                    "verification_uri": "http://127.0.0.1/verify",
                    "interval": 0.05,
                    "expires_in": 300,
                },
            )
        elif self.path == "/token":
            gt = params.get("grant_type", "")
            if self.server.mode == "echo_refresh" and gt == "refresh_token":
                # hostile echo of the posted refresh token in a raw body
                self._send_raw(400, f"invalid_grant: refresh_token={params.get('refresh_token', '')}")
            elif self.server.mode == "echo_poll" and gt == DEVICE_GRANT:
                self._send_raw(400, f"access_denied: device_code={params.get('device_code', '')}")
            elif gt == DEVICE_GRANT:
                self._send(200, {"access_token": "seat-access-1", "refresh_token": "seat-refresh-1",
                                 "token_type": "Bearer", "expires_in": 3600})
            else:
                self._send(400, {"error": "invalid_grant"})
        else:
            self._send(404, {"error": "not found"})

    def _send(self, code: int, payload: dict) -> None:
        self._send_raw(code, json.dumps(payload), "application/json")

    def _send_raw(self, code: int, body: str, ctype: str = "text/plain") -> None:
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class FakeFeederHandler(BaseHTTPRequestHandler):
    """Hermetic OpenAI-compatible Feeder. mode ∈ ok | json_ansi_error | raw_echo_error."""

    def log_message(self, *args) -> None:
        pass

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        self.server.feeder_requests.append({"path": self.path, "authorization": self.headers.get("Authorization")})
        mode = self.server.mode
        if mode == "ok":
            self._send(200, {
                "choices": [{"index": 0, "message": {"role": "assistant", "content": '{"message": "hi"}'}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            })
        elif mode == "json_ansi_error":
            # parsed error message carrying ANSI/OSC escapes
            self._send(400, {"error": {"message": f"bad request {OSC}OSC-INJECT {ANSI_RED}red\x1b[0m tail"}})
        elif mode == "raw_echo_error":
            # non-JSON body with a planted marker + escapes
            self._send_raw(400, f"upstream exploded bearer={RAW_MARKER} {ANSI_RED}red")
        else:
            self._send(400, {"error": {"message": "unknown mode"}})

    def do_GET(self) -> None:
        self._send(200, {"data": [{"id": "feeder-model"}]})

    def _send(self, code: int, payload: dict) -> None:
        self._send_raw(code, json.dumps(payload), "application/json")

    def _send_raw(self, code: int, body: str, ctype: str = "text/plain") -> None:
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def _serve(server) -> threading.Thread:
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return t


PROVIDERS_TOML = """
[providers.feederco]
base_url = "http://127.0.0.1:{api_port}/v1"
api_key_env = "FEEDERCO_KEY"

[providers.feederco.models.feeder-model]
context_window = 8192
input_usd_per_mtok = 1.0
output_usd_per_mtok = 2.0
json_mode = true

[providers.seatco]
base_url = "http://127.0.0.1:{api_port}/v1"
api_key_env = "SEATCO_TOKEN"
auth = "seat"
oauth_device_url = "http://127.0.0.1:{oauth_port}/device"
oauth_token_url = "http://127.0.0.1:{oauth_port}/token"
oauth_client_id = "tc-test-client"

[providers.seatco.models.seat-model]
context_window = 8192
input_usd_per_mtok = 1.0
output_usd_per_mtok = 2.0
json_mode = true
"""


@pytest.fixture
def feeder_env(tmp_path, monkeypatch):
    api = ThreadingHTTPServer(("127.0.0.1", 0), FakeFeederHandler)
    api.feeder_requests = []
    api.mode = "ok"
    oauth = ThreadingHTTPServer(("127.0.0.1", 0), FakeOAuthHandler)
    oauth.oauth_requests = []
    oauth.mode = "auto"
    t1 = _serve(api)
    t2 = _serve(oauth)
    monkeypatch.setenv("FEEDERCO_KEY", "sk-feeder-test")
    monkeypatch.delenv("SEATCO_TOKEN", raising=False)
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    providers_file = tmp_path / "providers.toml"
    providers_file.write_text(PROVIDERS_TOML.format(api_port=api.server_address[1], oauth_port=oauth.server_address[1]))
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
def feeder_daemon(feeder_env):
    settings = load_settings(feeder_env.settings_file)
    dt = DaemonThread(settings)
    dt.start()
    yield dt
    dt.stop()


def write_job(tmp_path: Path, name: str, model: str) -> Path:
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
adapter = "mistral-api"
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


def _sweep_run_data(settings, socket_path, run_id: str, *markers: str) -> dict:
    """Collect every operator-visible run surface and check the markers."""
    surfaces = {}
    show = request(socket_path, "runs_show", run_id=run_id, timeout=2.0)
    lst = request(socket_path, "runs_list", timeout=2.0)
    logs = request(socket_path, "runs_logs", run_id=run_id, timeout=2.0)
    surfaces["socket runs_show"] = json.dumps(show)
    surfaces["socket runs_list"] = json.dumps(lst)
    surfaces["socket runs_logs"] = json.dumps(logs)
    surfaces["run_events rows"] = "\n".join(
        r[0] for r in db_rows(settings, "SELECT line FROM run_events WHERE run_id = ?", (run_id,))
    )
    surfaces["manifest_json"] = (db_rows(settings, "SELECT manifest_json FROM runs WHERE id = ?", (run_id,)) or [("",)])[0][0]
    surfaces["results rows"] = "\n".join(
        str(r) for r in db_rows(settings, "SELECT output_json, schema_errors FROM results WHERE run_id = ?", (run_id,))
    )
    surfaces["audit.jsonl"] = json.dumps(AuditLog(settings.audit_log_path).read_all())
    surfaces["data_dir files"] = "\n".join(sweep_data_dir(settings, *markers))
    for name, blob in surfaces.items():
        for marker in markers:
            assert marker not in blob, f"marker {marker!r} leaked into {name}"
    return surfaces


# --- socket seam: auth_login_poll served payload --------------------------------


def test_auth_login_poll_echo_never_reaches_served_payload(feeder_env, feeder_daemon) -> None:
    """Negative: the device-flow poll error echoed by a hostile token endpoint
    never reaches the `auth_login_poll` socket response (served by the Coop at
    POST /api/auth/login/status). Positive: status code still in the error."""
    feeder_env.oauth.mode = "echo_poll"
    sp = feeder_daemon.settings.socket_path
    start = request(sp, "auth_login_start", provider="seatco", timeout=5.0)
    assert start.get("ok") and start["user_code"] == "ABCD-EFGH"
    assert DEVICE_MARKER not in json.dumps(start)  # device_code never in the start response either
    poll = request(sp, "auth_login_poll", provider="seatco", timeout=5.0)
    assert poll.get("ok") is False
    err = poll.get("error", "")
    assert DEVICE_MARKER not in err  # the echoed device_code is withheld
    assert "http 400" in err  # positive: status code surfaces
    # and nothing echo-ish landed in the Headcount
    audit_blob = json.dumps(AuditLog(feeder_daemon.settings.audit_log_path).read_all())
    assert DEVICE_MARKER not in audit_blob


# --- e2e: token-endpoint echo vs run data + Headcount ----------------------------


def test_e2e_token_endpoint_echo_never_reaches_run_data_or_headcount(feeder_env, feeder_daemon, tmp_path) -> None:
    """A seat whose refresh is revoked by an endpoint echoing the posted
    refresh token: the marker must appear in no error, run event, manifest,
    audit row, served payload, CLI output, or data-dir file — while the run
    still fails closed with the status code + login hint."""
    settings = feeder_daemon.settings
    # seed an expired session whose refresh token is the planted marker
    broker = SeatBroker(settings, load_providers(settings.providers_path, settings.data_dir))
    broker.store.save(SeatSession("seatco", "old-access", REFRESH_MARKER, expires_at=time.time() - 10))
    feeder_env.oauth.mode = "echo_refresh"

    spec_path = write_job(tmp_path, "seat-echo-job", model="seatco/seat-model")
    result = runner.invoke(app, ["run", str(spec_path), "--wait"])
    assert result.exit_code == 1
    out = combined_output(result)
    run_id = run_id_of(result)
    assert REFRESH_MARKER not in out  # CLI run output clean
    assert "http 400" in out and "tc auth login seatco" in out  # positive: status + hint survive

    run = wait_terminal(settings.socket_path, run_id)
    assert run["status"] == "failed"
    reason = run["manifest"]["status_reason"]
    assert REFRESH_MARKER not in reason
    assert "SeatError" in reason and "http 400" in reason and "tc auth login seatco" in reason

    # marker sweep over every run-data + Headcount + served surface
    surfaces = _sweep_run_data(settings, settings.socket_path, run_id, REFRESH_MARKER)
    assert "withheld by the fox" in surfaces["manifest_json"]  # body withheld, not echoed

    # CLI surfaces: runs list (prints status_reason raw), runs logs (prints event lines raw)
    r_list = runner.invoke(app, ["runs", "list"])
    assert REFRESH_MARKER not in combined_output(r_list)
    r_logs = runner.invoke(app, ["runs", "logs", run_id])
    assert REFRESH_MARKER not in combined_output(r_logs)
    r_show = runner.invoke(app, ["runs", "show", run_id])
    assert REFRESH_MARKER not in combined_output(r_show)
    # fail closed: no API request went out, revoked session deleted, teardown attested
    assert [r for r in feeder_env.api.feeder_requests if r["path"] == "/v1/chat/completions"] == []
    assert not (settings.seats_dir / "seatco.json").exists()
    att = db_rows(settings, "SELECT process_reaped, workspace_removed, env_cleared FROM teardown_attestations WHERE run_id = ?", (run_id,))
    assert att == [(1, 1, 1)]


# --- e2e: ANSI/OSC probe vs tc runs logs + marker sweep --------------------------


def test_e2e_provider_ansi_error_sanitized_before_runs_logs(feeder_env, feeder_daemon, tmp_path) -> None:
    """Negative: a provider error message containing ANSI/OSC escapes never
    reaches `tc runs logs` (nor runs list / run --wait / manifest / audit) raw.
    Positive: the status code + the parsed (escape-stripped) message survive."""
    feeder_env.api.mode = "json_ansi_error"
    settings = feeder_daemon.settings
    spec_path = write_job(tmp_path, "ansi-job", model="feederco/feeder-model")
    result = runner.invoke(app, ["run", str(spec_path), "--wait"])
    assert result.exit_code == 1
    out = combined_output(result)
    run_id = run_id_of(result)
    assert not has_control_chars(out)  # the --wait failure line is escape-free
    assert "http 400" in out and "bad request" in out  # positive: useful message kept

    run = wait_terminal(settings.socket_path, run_id)
    assert run["status"] == "failed"
    reason = run["manifest"]["status_reason"]
    assert "provider http 400" in reason and "bad request" in reason and "OSC-INJECT" in reason
    assert not has_control_chars(reason)

    # runs_logs payload (what the Coop serves and the CLI prints): every event
    # line is control-char free; the sanitized text is there
    logs = request(settings.socket_path, "runs_logs", run_id=run_id, timeout=2.0)["events"]
    assert logs, "expected run events"
    for e in logs:
        assert not has_control_chars(e["line"]), f"escape bytes in event line: {e['line']!r}"
    assert any("provider http 400" in e["line"] and "OSC-INJECT" in e["line"] for e in logs)

    # CLI surfaces print stored lines/reasons raw — they must be clean
    r_logs = runner.invoke(app, ["runs", "logs", run_id])
    assert not has_control_chars(combined_output(r_logs))
    assert "OSC-INJECT" in combined_output(r_logs)  # sanitized text still readable
    r_list = runner.invoke(app, ["runs", "list"])
    assert not has_control_chars(combined_output(r_list))
    assert REFRESH_MARKER not in combined_output(r_list)

    # manifest + Headcount rows are escape-free too
    surfaces = _sweep_run_data(settings, settings.socket_path, run_id, REFRESH_MARKER, RAW_MARKER)
    assert not has_control_chars(surfaces["manifest_json"])
    assert not has_control_chars(surfaces["run_events rows"])
    assert not has_control_chars(surfaces["audit.jsonl"])


def test_e2e_provider_raw_body_marker_swept_from_run_data_and_headcount(feeder_env, feeder_daemon, tmp_path) -> None:
    """Negative: a non-JSON provider error body carrying a planted secret marker
    + escapes yields only the withheld marker — swept over run data, Headcount,
    served payloads, CLI output, and every data-dir file. Positive: status
    code + withheld note in the reason."""
    feeder_env.api.mode = "raw_echo_error"
    settings = feeder_daemon.settings
    spec_path = write_job(tmp_path, "raw-echo-job", model="feederco/feeder-model")
    result = runner.invoke(app, ["run", str(spec_path), "--wait"])
    assert result.exit_code == 1
    out = combined_output(result)
    run_id = run_id_of(result)
    assert RAW_MARKER not in out and not has_control_chars(out)
    assert "provider http 400" in out and "withheld by the fox" in out  # positive

    run = wait_terminal(settings.socket_path, run_id)
    assert run["status"] == "failed"
    reason = run["manifest"]["status_reason"]
    assert RAW_MARKER not in reason and not has_control_chars(reason)
    assert "provider http 400" in reason and "withheld by the fox" in reason

    _sweep_run_data(settings, settings.socket_path, run_id, RAW_MARKER, REFRESH_MARKER, DEVICE_MARKER)
    for cli_args in (["runs", "list"], ["runs", "logs", run_id], ["runs", "show", run_id]):
        r = runner.invoke(app, cli_args)
        blob = combined_output(r)
        for marker in (RAW_MARKER, REFRESH_MARKER, DEVICE_MARKER):
            assert marker not in blob
        assert not has_control_chars(blob)


def test_e2e_legitimate_provider_run_still_works(feeder_env, feeder_daemon, tmp_path) -> None:
    """Positive end-to-end: an ordinary feeder run still succeeds and its logs
    are unaffected by the hygiene changes."""
    feeder_env.api.mode = "ok"
    settings = feeder_daemon.settings
    spec_path = write_job(tmp_path, "ok-job", model="feederco/feeder-model")
    result = runner.invoke(app, ["run", str(spec_path), "--wait"])
    assert result.exit_code == 0, combined_output(result)
    run_id = run_id_of(result)
    run = wait_terminal(settings.socket_path, run_id)
    assert run["status"] == "succeeded"
    res = runner.invoke(app, ["results", "get", run_id])
    assert res.exit_code == 0 and json.loads(res.output)["message"] == "hi"
    logs = request(settings.socket_path, "runs_logs", run_id=run_id, timeout=2.0)["events"]
    assert any("provider http 200" in e["line"] for e in logs)
