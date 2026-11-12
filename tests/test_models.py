"""Tests for spec 10: model discovery (`tc models list`) + `tc jobs validate`
model checks.

Seams:
- contract: `fetch_models` over `httpx.MockTransport` (id parsing, bearer sent
  only when a token exists, non-200/malformed/transport errors, and the token
  never appearing in any error string).
- pure: `model_issues` (unknown provider → error, model without pricing
  preset → warning, vibe-cli model not in the probed catalog → error, catalog
  unknown → no check).
- e2e: `tc models list` against a hermetic loopback fake OpenAI-compatible
  server serving GET /models — live ids with priced/unpriced marks, an
  unreachable provider row (rc 0), credential handling (env key sent, no key
  → unauthenticated, 401 → unreachable row, key never printed), and a seat
  provider authenticated with the stored session token (spec 09).
- vibe-cli: a fake vibe binary answering `--version` and the unknown-model
  probe (`Available: ...`), plus a broken binary.
- CLI: `tc jobs validate` warns on unpriced mistral-api models and errors on
  models unknown to the chosen harness.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from typer.testing import CliRunner

from turbochickens.cli.main import app
from turbochickens.models import (
    PROBE_MODEL,
    fetch_models,
    list_provider_catalogs,
    model_issues,
    vibe_catalog,
)
from turbochickens.providers import ProvidersConfig, load_providers
from turbochickens.seat import SeatSession, SeatStore
from turbochickens.spec.loader import load_spec

runner = CliRunner()


def combined_output(result) -> str:
    parts = [result.output or ""]
    try:
        parts.append(result.stderr or "")
    except ValueError:
        pass
    return "".join(parts)


def make_providers(base_url: str = "http://provider.test/v1", auth: str = "api-key") -> ProvidersConfig:
    p: dict = {
        "base_url": base_url,
        "api_key_env": "FAKE_API_KEY",
        "models": {
            "mistral-large-latest": {
                "context_window": 128000,
                "input_usd_per_mtok": 2.0,
                "output_usd_per_mtok": 6.0,
            }
        },
    }
    if auth == "seat":
        p["auth"] = "seat"
        p["oauth_device_url"] = "http://127.0.0.1:9/device"
        p["oauth_token_url"] = "http://127.0.0.1:9/token"
        p["oauth_client_id"] = "tc-test-client"
    return ProvidersConfig.model_validate({"providers": {"fake": p}})


# --- contract seam: fetch_models over httpx.MockTransport ---------------------


def _models_payload(ids: list[str]) -> dict:
    return {"object": "list", "data": [{"id": i, "object": "model", "created": 0} for i in ids]}


def test_fetch_models_parses_ids_and_sends_bearer() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_models_payload(["b-model", "a-model", "a-model"]))

    provider = make_providers().providers["fake"]
    client = httpx.Client(transport=httpx.MockTransport(handler))
    ids, err = fetch_models(provider, "sk-secret-token", client=client)
    assert err is None
    assert ids == ["a-model", "b-model"]  # sorted, deduped
    assert len(seen) == 1
    assert seen[0].method == "GET" and seen[0].url.path == "/v1/models"
    assert seen[0].headers["authorization"] == "Bearer sk-secret-token"


def test_fetch_models_without_token_sends_no_auth_header() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_models_payload(["m1"]))

    provider = make_providers().providers["fake"]
    ids, err = fetch_models(provider, None, client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert err is None and ids == ["m1"]
    assert "authorization" not in seen[0].headers


def test_fetch_models_errors_never_contain_the_token() -> None:
    provider = make_providers().providers["fake"]
    token = "sk-secret-token"

    def h_http(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"message": "boom"}})

    def h_malformed(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"nope": True})

    def h_transport(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    for h in (h_http, h_malformed, h_transport):
        ids, err = fetch_models(provider, token, client=httpx.Client(transport=httpx.MockTransport(h)))
        assert ids is None and err
        assert token not in err
    _, err500 = fetch_models(provider, token, client=httpx.Client(transport=httpx.MockTransport(h_http)))
    assert "http 500" in err500 and "boom" in err500


def test_list_provider_catalogs_marks_priced_and_survives_seat_errors(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("TURBOCHICKENS_PROVIDERS", raising=False)
    providers = make_providers(base_url="http://provider.test/v1")
    providers.providers["fake"].models["extra-priced"] = providers.providers["fake"].models[
        "mistral-large-latest"
    ].model_copy()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_models_payload(["mistral-large-latest", "extra-priced", "live-only"]))

    def token_for(name, provider):
        return "sk-x", "env"

    rows = list_provider_catalogs(
        providers, token_for, client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.reachable and row.cred == "env"
    assert [(m.id, m.priced) for m in row.models] == [
        ("extra-priced", True),
        ("live-only", False),
        ("mistral-large-latest", True),
    ]

    # a failing token resolver marks the row unreachable without aborting
    from turbochickens.seat import SeatError

    def bad_token_for(name, provider):
        raise SeatError("no seat session for provider 'fake'; run `tc auth login fake` first")

    rows = list_provider_catalogs(providers, bad_token_for)
    assert len(rows) == 1 and not rows[0].reachable
    assert "tc auth login fake" in (rows[0].error or "")


# --- pure seam: model_issues ---------------------------------------------------


def _spec(tmp_path: Path, adapter: str = "mistral-api", model: str = "mistral-large-latest") -> object:
    d = tmp_path / "job"
    (d / "schemas").mkdir(parents=True, exist_ok=True)
    (d / "schemas" / "out.schema.json").write_text('{"type": "object"}')
    (d / "job.toml").write_text(
        f"""
[job]
name = "j"
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
prompt_file = "prompt.txt"
attachments = []
[output]
format = "json"
schema_file = "schemas/out.schema.json"
"""
    )
    return load_spec(d / "job.toml")


def test_model_issues_unknown_provider_is_an_error(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("TURBOCHICKENS_PROVIDERS", raising=False)
    providers = load_providers(data_dir=tmp_path)
    errors, warnings = model_issues(_spec(tmp_path, model="ghost/model"), providers)
    assert warnings == []
    assert len(errors) == 1 and "harness.model" in errors[0] and "unknown provider" in errors[0]
    assert "mistral" in errors[0]  # known providers listed


def test_model_issues_unpriced_model_is_a_warning_not_error(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("TURBOCHICKENS_PROVIDERS", raising=False)
    providers = load_providers(data_dir=tmp_path)
    errors, warnings = model_issues(_spec(tmp_path, model="live-only-model"), providers)
    assert errors == []
    assert len(warnings) == 1 and "no pricing preset" in warnings[0] and "harness.model" in warnings[0]
    # a model with a preset: neither error nor warning
    errors, warnings = model_issues(_spec(tmp_path, model="mistral-small-latest"), providers)
    assert errors == [] and warnings == []


def test_model_issues_vibe_cli_against_probed_catalog(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("TURBOCHICKENS_PROVIDERS", raising=False)
    providers = load_providers(data_dir=tmp_path)
    errors, warnings = model_issues(
        _spec(tmp_path, adapter="vibe-cli", model="mistral-medium-3.5"), providers,
        vibe_models=["mistral-medium-3.5", "custom-alias"],
    )
    assert errors == [] and warnings == []
    errors, _ = model_issues(
        _spec(tmp_path, adapter="vibe-cli", model="mistral-small-latest"), providers,
        vibe_models=["mistral-medium-3.5"],
    )
    assert len(errors) == 1 and "mistral-small-latest" in errors[0] and "mistral-medium-3.5" in errors[0]
    # catalog unknown (probe failed): no check, no error
    errors, _ = model_issues(_spec(tmp_path, adapter="vibe-cli", model="whatever"), providers, vibe_models=None)
    assert errors == []


# --- e2e: hermetic loopback fake provider serving GET /models -------------------


class FakeModelsHandler(BaseHTTPRequestHandler):
    """Serves GET {base}/models from `server.model_ids`; records Authorization.
    With `server.require_auth`, only `Bearer server.expected_token` gets 200."""

    def log_message(self, *args) -> None:
        pass

    def do_GET(self) -> None:
        self.server.requests.append(
            {"path": self.path, "authorization": self.headers.get("Authorization")}
        )
        if not self.path.endswith("/models"):
            self._send(404, {"error": "not found"})
            return
        if getattr(self.server, "require_auth", False):
            if self.headers.get("Authorization") != f"Bearer {self.server.expected_token}":
                self._send(401, {"detail": "Invalid API Key"})
                return
        self._send(200, _models_payload(self.server.model_ids))

    def _send(self, code: int, payload: dict) -> None:
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def fake_api():
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeModelsHandler)
    server.requests = []
    server.model_ids = ["mistral-large-latest", "brand-new-model"]
    server.require_auth = False
    server.expected_token = ""
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield server
    server.shutdown()
    server.server_close()
    t.join(timeout=5)


PROVIDERS_TOML = """
[providers.mistral]
base_url = "http://127.0.0.1:{port}/v1"
api_key_env = "FAKE_API_KEY"

[providers.mistral.models.mistral-large-latest]
context_window = 128000
input_usd_per_mtok = 2.0
output_usd_per_mtok = 6.0

[providers.dead]
base_url = "http://127.0.0.1:9/v1"
api_key_env = "DEAD_API_KEY"

[providers.dead.models.anything]
context_window = 8192
input_usd_per_mtok = 1.0
output_usd_per_mtok = 1.0
"""

SEAT_PROVIDERS_TOML = """
[providers.seatco]
base_url = "http://127.0.0.1:{port}/v1"
api_key_env = "SEATCO_TOKEN"
auth = "seat"
oauth_device_url = "http://127.0.0.1:9/device"
oauth_token_url = "http://127.0.0.1:9/token"
oauth_client_id = "tc-test-client"

[providers.seatco.models.seat-model]
context_window = 8192
input_usd_per_mtok = 0.0
output_usd_per_mtok = 0.0
"""


@pytest.fixture
def models_env(tmp_path, monkeypatch, fake_api) -> SimpleNamespace:
    providers_file = tmp_path / "providers.toml"
    providers_file.write_text(PROVIDERS_TOML.format(port=fake_api.server_address[1]))
    monkeypatch.setenv("TURBOCHICKENS_PROVIDERS", str(providers_file))
    settings_file = tmp_path / "settings.toml"
    settings_file.write_text(f'data_dir = "{tmp_path / "data"}"\n')
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(settings_file))
    monkeypatch.delenv("FAKE_API_KEY", raising=False)
    monkeypatch.delenv("DEAD_API_KEY", raising=False)
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    return SimpleNamespace(tmp_path=tmp_path, api=fake_api, providers_file=providers_file)


def test_models_list_cli_live_ids_pricing_marks_and_env_key(models_env, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_API_KEY", "sk-test-secret")
    result = runner.invoke(app, ["models", "list"])
    out = combined_output(result)
    assert result.exit_code == 0, out
    assert "provider=mistral" in out and "reachable=yes" in out and "cred=env" in out
    assert "models=2" in out
    assert "mistral-large-latest  [priced]" in out
    assert "brand-new-model  [no-pricing-preset]" in out
    assert "provider=dead" in out and "reachable=no" in out  # unreachable row, not fatal
    # the /models call carried the allowlisted env var as bearer; never printed
    assert any(r["path"].endswith("/models") and r["authorization"] == "Bearer sk-test-secret"
               for r in models_env.api.requests)
    assert "sk-test-secret" not in out


def test_models_list_cli_without_key_is_unauthenticated_but_still_lists(models_env) -> None:
    result = runner.invoke(app, ["models", "list"])
    out = combined_output(result)
    assert result.exit_code == 0, out
    assert "provider=mistral" in out and "reachable=yes" in out and "cred=none" in out
    assert any(r["authorization"] is None for r in models_env.api.requests)


def test_models_list_cli_401_marks_unreachable_rc0(models_env) -> None:
    models_env.api.require_auth = True
    models_env.api.expected_token = "sk-required"
    result = runner.invoke(app, ["models", "list"])
    out = combined_output(result)
    assert result.exit_code == 0, out
    assert "provider=mistral" in out and "reachable=no" in out and "401" in out
    assert "provider=dead" in out and "reachable=no" in out


def test_models_list_cli_seat_provider_uses_session_token(tmp_path, monkeypatch, fake_api) -> None:
    fake_api.require_auth = True
    fake_api.expected_token = "seat-access-1"
    fake_api.model_ids = ["seat-model", "seat-new-model"]
    providers_file = tmp_path / "providers.toml"
    providers_file.write_text(SEAT_PROVIDERS_TOML.format(port=fake_api.server_address[1]))
    monkeypatch.setenv("TURBOCHICKENS_PROVIDERS", str(providers_file))
    settings_file = tmp_path / "settings.toml"
    settings_file.write_text(f'data_dir = "{tmp_path / "data"}"\n')
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(settings_file))
    monkeypatch.delenv("SEATCO_TOKEN", raising=False)
    # a valid stored seat session (not expired → no refresh, no OAuth call)
    SeatStore(tmp_path / "data").save(
        SeatSession(provider="seatco", access_token="seat-access-1", refresh_token="r1", expires_at=time.time() + 3600)
    )
    result = runner.invoke(app, ["models", "list"])
    out = combined_output(result)
    assert result.exit_code == 0, out
    assert "provider=seatco" in out and "auth=seat" in out and "cred=seat" in out and "reachable=yes" in out
    assert "seat-model  [priced]" in out and "seat-new-model  [no-pricing-preset]" in out
    assert any(r["authorization"] == "Bearer seat-access-1" for r in fake_api.requests)
    assert "seat-access-1" not in out  # token never printed


def test_models_list_cli_seat_without_session_marks_row_with_login_hint(tmp_path, monkeypatch, fake_api) -> None:
    providers_file = tmp_path / "providers.toml"
    providers_file.write_text(SEAT_PROVIDERS_TOML.format(port=fake_api.server_address[1]))
    monkeypatch.setenv("TURBOCHICKENS_PROVIDERS", str(providers_file))
    settings_file = tmp_path / "settings.toml"
    settings_file.write_text(f'data_dir = "{tmp_path / "data"}"\n')
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(settings_file))
    result = runner.invoke(app, ["models", "list"])
    out = combined_output(result)
    assert result.exit_code == 0, out
    assert "provider=seatco" in out and "reachable=no" in out
    assert "tc auth login seatco" in out
    assert fake_api.requests == []  # fail closed: no /models request without a session


def test_models_list_cli_invalid_providers_file_rc1(tmp_path, monkeypatch) -> None:
    bad = tmp_path / "providers.toml"
    bad.write_text("[providers.mistral]\nbogus_key = 1\nmodels = {}\n")
    monkeypatch.setenv("TURBOCHICKENS_PROVIDERS", str(bad))
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(tmp_path / "s.toml"))
    (tmp_path / "s.toml").write_text(f'data_dir = "{tmp_path / "data"}"\n')
    result = runner.invoke(app, ["models", "list"])
    assert result.exit_code == 1
    assert "bogus_key" in combined_output(result)


def test_models_list_cli_bad_adapter_flag_rc1(models_env) -> None:
    result = runner.invoke(app, ["models", "list", "--adapter", "gpt-9000"])
    assert result.exit_code == 1
    assert "--adapter" in combined_output(result)


# --- vibe-cli catalog via a fake vibe binary ------------------------------------

FAKE_VIBE = r'''#!/usr/bin/env python3
import sys
args = sys.argv[1:]
if "--version" in args:
    print("vibe 9.9.9-fake")
    sys.exit(0)
if "__turbochickens_model_probe__" in args:
    print("Error: Unknown model: __turbochickens_model_probe__. Available: mistral-medium-3.5, my-custom-model", file=sys.stderr)
    sys.exit(1)
print("{}")
'''

BROKEN_VIBE = r'''#!/usr/bin/env python3
import sys
if "--version" in args:
    print("vibe 0.0.0-broken")
    sys.exit(0)
print("something went wrong", file=sys.stderr)
sys.exit(3)
'''


@pytest.fixture
def fake_vibe(tmp_path, monkeypatch) -> Path:
    bin_path = tmp_path / "fake-vibe"
    bin_path.write_text(FAKE_VIBE)
    bin_path.chmod(0o755)
    monkeypatch.setenv("TURBOCHICKENS_VIBE_BIN", str(bin_path))
    return bin_path


def test_vibe_catalog_probe_parses_available_list(fake_vibe) -> None:
    cat = vibe_catalog()
    assert cat.reachable and cat.version == "9.9.9-fake"
    assert cat.models == ["mistral-medium-3.5", "my-custom-model"]


def test_models_list_vibe_cli_cli(fake_vibe) -> None:
    result = runner.invoke(app, ["models", "list", "--adapter", "vibe-cli"])
    out = combined_output(result)
    assert result.exit_code == 0, out
    assert "adapter=vibe-cli" in out and "version=9.9.9-fake" in out and "reachable=yes" in out
    assert "models=2" in out
    assert "mistral-medium-3.5" in out and "my-custom-model" in out


def test_models_list_vibe_cli_broken_binary_rc0(tmp_path, monkeypatch) -> None:
    bin_path = tmp_path / "broken-vibe"
    bin_path.write_text(BROKEN_VIBE)
    bin_path.chmod(0o755)
    monkeypatch.setenv("TURBOCHICKENS_VIBE_BIN", str(bin_path))
    cat = vibe_catalog()
    assert not cat.reachable and cat.error and "no model list" in cat.error
    result = runner.invoke(app, ["models", "list", "--adapter", "vibe-cli"])
    out = combined_output(result)
    assert result.exit_code == 0, out
    assert "reachable=no" in out


# --- tc jobs validate: model checks ---------------------------------------------


def _job_file(tmp_path: Path, adapter: str, model: str) -> Path:
    d = tmp_path / f"job-{adapter}-{model}".replace("/", "_")
    (d / "schemas").mkdir(parents=True, exist_ok=True)
    (d / "schemas" / "out.schema.json").write_text('{"type": "object"}')
    (d / "job.toml").write_text(
        f"""
[job]
name = "mjob"
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
prompt_file = "prompt.txt"
attachments = []
[output]
format = "json"
schema_file = "schemas/out.schema.json"
"""
    )
    return d / "job.toml"


def test_jobs_validate_priced_model_ok_no_warning(models_env) -> None:
    result = runner.invoke(app, ["jobs", "validate", str(_job_file(models_env.tmp_path, "mistral-api", "mistral-large-latest"))])
    out = combined_output(result)
    assert result.exit_code == 0, out
    assert "OK" in result.output and "sha256:" in result.output
    assert "warning" not in out


def test_jobs_validate_unpriced_model_warns_but_passes(models_env) -> None:
    result = runner.invoke(app, ["jobs", "validate", str(_job_file(models_env.tmp_path, "mistral-api", "brand-new-model"))])
    out = combined_output(result)
    assert result.exit_code == 0, out
    assert "OK" in result.output  # warn, not fail
    assert "warning" in out and "no pricing preset" in out and "harness.model" in out
    assert "tc models list" in out


def test_jobs_validate_unknown_provider_errors(models_env) -> None:
    result = runner.invoke(app, ["jobs", "validate", str(_job_file(models_env.tmp_path, "mistral-api", "ghost/model"))])
    out = combined_output(result)
    assert result.exit_code == 1, out
    assert "error" in out and "harness.model" in out and "unknown provider" in out
    assert "mistral" in out and "dead" in out  # known providers named
    assert "OK" not in result.output


def test_jobs_validate_vibe_cli_unknown_model_errors(fake_vibe, models_env) -> None:
    result = runner.invoke(app, ["jobs", "validate", str(_job_file(models_env.tmp_path, "vibe-cli", "mistral-small-latest"))])
    out = combined_output(result)
    assert result.exit_code == 1, out
    assert "harness.model" in out and "mistral-small-latest" in out
    assert "mistral-medium-3.5" in out and "my-custom-model" in out  # the offered list is named


def test_jobs_validate_vibe_cli_known_model_ok(fake_vibe, models_env) -> None:
    result = runner.invoke(app, ["jobs", "validate", str(_job_file(models_env.tmp_path, "vibe-cli", "my-custom-model"))])
    out = combined_output(result)
    assert result.exit_code == 0, out
    assert "OK" in result.output
