"""Tests for spec 32 ticket 06 — OAuth https-only + verification_uri scheme
validation at ingest and in the SPA (review findings G7 + G21 SPA).

Seams (highest existing first):
- providers.toml / ProvidersConfig: the Feeder OAuth device/token endpoints
  must be https — plain http only on loopback hosts (hermetic local testing),
  any other scheme refused — with field-named errors; https and loopback http
  stay valid (positive).
- SeatBroker over httpx.MockTransport: the device grant's `verification_uri` is
  scheme-validated when the grant is accepted — `javascript:` / `data:` / http /
  scheme-less URIs are refused by the Fox and never leave the perch; an https
  grant completes and stores the session 0600 (positive).
- daemon socket + coop HTTP against a hermetic loopback fake OAuth server: a
  hostile `verification_uri` → `auth_login_start` ok:false (field-named Fox
  refusal, payload never echoed), `POST /api/auth/login` → 422, poll → "no
  device login in progress", no session file; an https `verification_uri` →
  200 with the https URI on socket and HTTP (the SPA link-render condition).
- SPA asset grep-guards: no third-party runtime fetches in the served
  html/css/js (no absolute URL at all, no font CDN, no @import / url(http) in
  the CSS, system font stack present), and the device-verification link
  assigns `href` only through the https-only guard — never the raw response
  value.

All assertions are external behavior: field-named refusals, status codes,
payload shapes, on-disk modes, served asset bytes.
"""

from __future__ import annotations

import json
import re
import stat
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from test_coop import free_port, http_client, write_settings
from test_daemon import DaemonThread
from turbochickens.client import request
from turbochickens.providers import ProvidersConfig, ProvidersError, load_providers
from turbochickens.seat import SeatBroker, SeatError
from turbochickens.settings import Settings, load_settings

SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "turbochickens"
STATIC = SRC_ROOT / "coop_static"

SEAT_TOML = """
[providers.seatco]
base_url = "http://127.0.0.1:{api_port}/v1"
api_key_env = "SEATCO_TOKEN"
auth = "seat"
oauth_device_url = "{device}"
oauth_token_url = "{token}"
oauth_client_id = "tc-test-client"

[providers.seatco.models.seat-model]
context_window = 8192
input_usd_per_mtok = 1.0
output_usd_per_mtok = 2.0
"""


def _write_providers(tmp_path: Path, device: str, token: str, api_port: int = 1) -> Path:
    f = tmp_path / "providers.toml"
    f.write_text(SEAT_TOML.format(api_port=api_port, device=device, token=token))
    return f


def _seat_providers(device: str = "https://oauth.test/device", token: str = "https://oauth.test/token") -> ProvidersConfig:
    """A valid seat provider (https OAuth endpoints) for the broker seam."""
    return ProvidersConfig.model_validate(
        {
            "providers": {
                "seatco": {
                    "base_url": "https://api.test/v1",
                    "api_key_env": "SEATCO_TOKEN",
                    "auth": "seat",
                    "oauth_device_url": device,
                    "oauth_token_url": token,
                    "oauth_client_id": "tc-test-client",
                    "models": {
                        "seat-model": {
                            "context_window": 8192,
                            "input_usd_per_mtok": 1.0,
                            "output_usd_per_mtok": 2.0,
                        }
                    },
                }
            }
        }
    )


# --- providers seam: OAuth endpoints https-only, field-named refusals --------


@pytest.mark.parametrize(
    "device,token,field",
    [
        ("http://auth.example.com/device", "https://auth.example.com/token", "oauth_device_url"),
        ("https://auth.example.com/device", "http://auth.example.com/token", "oauth_token_url"),
        ("http://10.0.0.5/device", "https://auth.example.com/token", "oauth_device_url"),  # private-range http ≠ loopback
        ("javascript:alert(1)", "https://auth.example.com/token", "oauth_device_url"),
        ("https://auth.example.com/device", "data:text/html,<b>x</b>", "oauth_token_url"),
        ("ftp://auth.example.com/device", "https://auth.example.com/token", "oauth_device_url"),
        ("auth.example.com/device", "https://auth.example.com/token", "oauth_device_url"),  # no scheme
    ],
)
def test_non_https_oauth_endpoints_refused_with_field_named_error(
    tmp_path, monkeypatch, device, token, field
) -> None:
    monkeypatch.delenv("TURBOCHICKENS_PROVIDERS", raising=False)
    f = _write_providers(tmp_path, device, token)
    with pytest.raises(ProvidersError) as exc:
        load_providers(f, data_dir=tmp_path)
    msg = str(exc.value)
    assert field in msg, msg  # the offending field is named, like the existing contracts
    assert "https" in msg
    assert (device if field == "oauth_device_url" else token) in msg  # the offending value is quoted


def test_non_https_oauth_url_refused_on_direct_model_validation() -> None:
    with pytest.raises(ValidationError) as exc:
        _seat_providers(device="http://oauth.test/device")
    assert "oauth_device_url" in str(exc.value) and "https" in str(exc.value)


@pytest.mark.parametrize(
    "device,token",
    [
        ("https://auth.example.com/device", "https://auth.example.com/token"),
        ("http://127.0.0.1:9999/device", "http://127.0.0.1:9999/token"),  # loopback http: hermetic local testing
        ("http://localhost:9999/device", "http://[::1]:9999/token"),
    ],
)
def test_https_and_loopback_http_oauth_endpoints_still_valid(tmp_path, monkeypatch, device, token) -> None:
    """Positive: the legitimate configs (https everywhere; loopback http for the
    hermetic fakes) keep loading — machine interface unchanged."""
    monkeypatch.delenv("TURBOCHICKENS_PROVIDERS", raising=False)
    f = _write_providers(tmp_path, device, token)
    cfg = load_providers(f, data_dir=tmp_path)
    p = cfg.providers["seatco"]
    assert p.auth == "seat"
    assert p.oauth_device_url == device and p.oauth_token_url == token


# --- broker seam: verification_uri scheme-validated when the grant is accepted


def _device_handler(verification_uri: str):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/device":
            return httpx.Response(
                200,
                json={
                    "device_code": "dev-secret-1",
                    "user_code": "ABCD-EFGH",
                    "verification_uri": verification_uri,
                    "interval": 0.05,
                    "expires_in": 300,
                },
            )
        if request.url.path == "/token":
            return httpx.Response(
                200,
                json={
                    "access_token": "seat-access-1",
                    "refresh_token": "seat-refresh-1",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                },
            )
        return httpx.Response(404, json={"error": "not found"})

    return handler


def _broker(tmp_path: Path, verification_uri: str) -> SeatBroker:
    settings = Settings(data_dir=tmp_path / "data")
    return SeatBroker(
        settings,
        _seat_providers(),
        client=httpx.Client(transport=httpx.MockTransport(_device_handler(verification_uri))),
    )


@pytest.mark.parametrize(
    "uri",
    [
        "javascript:alert(document.domain)",
        "data:text/html,<script>alert(1)</script>",
        "http://auth.example.com/verify",
        "HTTP://AUTH.EXAMPLE.COM/verify",  # scheme is case-insensitive — still http
        "vbscript:msgbox(1)",
        "alert(1)",  # no scheme at all
    ],
)
def test_non_https_verification_uri_refused_at_ingest(tmp_path, uri) -> None:
    """Negative: a hostile/misconfigured auth server cannot get a scriptable
    URI past the perch — the grant is refused before anything is relayed."""
    broker = _broker(tmp_path, uri)
    with pytest.raises(SeatError) as exc:
        broker.start_device_flow("seatco")
    msg = str(exc.value)
    assert "verification_uri" in msg  # field-named
    assert "Fox" in msg and "https" in msg  # Fox refusal copy
    assert uri not in msg  # the hostile payload itself is never echoed back
    assert broker.store.load("seatco") is None  # nothing stored


def test_https_verification_uri_accepted_and_device_flow_completes(tmp_path) -> None:
    """Positive: an https verification URI flows through and the device flow
    completes, session stored 0600."""
    uri = "https://auth.example.com/verify?user_code=ABCD-EFGH"
    broker = _broker(tmp_path, uri)
    grant = broker.start_device_flow("seatco")
    assert grant.verification_uri == uri
    assert grant.user_code == "ABCD-EFGH"
    outcome = broker.poll_device_flow(grant)
    assert outcome["status"] == "authorized"
    session = broker.store.load("seatco")
    assert session is not None and session.access_token == "seat-access-1"
    f = broker.store.dir / "seatco.json"
    assert stat.S_IMODE(f.stat().st_mode) == 0o600


# --- socket + coop HTTP seam against a hermetic loopback fake OAuth server ----


class ScriptableOAuthHandler(BaseHTTPRequestHandler):
    """Loopback fake OAuth whose /device answer carries `server.verification_uri`
    verbatim — the hostile-server stand-in."""

    def log_message(self, *args) -> None:
        pass

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        if self.path == "/device":
            self._send(
                200,
                {
                    "device_code": "dev-secret-9",
                    "user_code": "WXYZ-1234",
                    "verification_uri": self.server.verification_uri,
                    "interval": 0.05,
                    "expires_in": 300,
                },
            )
        elif self.path == "/token":
            self._send(
                200,
                {
                    "access_token": "seat-access-9",
                    "refresh_token": "seat-refresh-9",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                },
            )
        else:
            self._send(404, {"error": "not found"})

    def _send(self, code: int, payload: dict) -> None:
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def oauth_server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), ScriptableOAuthHandler)
    srv.verification_uri = "https://auth.example.com/verify"
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()
    srv.server_close()
    t.join(timeout=5)


@pytest.fixture
def coop_daemon(tmp_path, monkeypatch, oauth_server):
    """Coop-enabled daemon in a thread; seatco's OAuth endpoints point at the
    loopback fake (http loopback is the allowed hermetic case)."""
    port = oauth_server.server_address[1]
    providers_file = _write_providers(
        tmp_path,
        device=f"http://127.0.0.1:{port}/device",
        token=f"http://127.0.0.1:{port}/token",
    )
    monkeypatch.setenv("TURBOCHICKENS_PROVIDERS", str(providers_file))
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    monkeypatch.delenv("SEATCO_TOKEN", raising=False)
    settings_file = write_settings(tmp_path, coop_enabled=True, coop_port=free_port(), coop_loopback_open=True)
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(settings_file))
    settings = load_settings(settings_file)
    dt = DaemonThread(settings)
    dt.start()
    yield SimpleNamespace(settings=settings, oauth=oauth_server, tmp_path=tmp_path)
    dt.stop()


@pytest.mark.parametrize(
    "evil_uri",
    [
        "javascript:alert(document.domain)",
        "data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==",
        "http://auth.example.com/verify",
    ],
)
def test_hostile_verification_uri_refused_on_socket_and_http(coop_daemon, evil_uri) -> None:
    """Negative e2e: the evil URI never reaches a caller — socket op refuses
    ok:false with the field-named Fox error, the HTTP mirror answers 422, the
    poll finds no grant in flight, no session is stored, and the hostile
    payload string is never echoed in any response."""
    coop_daemon.oauth.verification_uri = evil_uri
    sock = coop_daemon.settings.socket_path

    resp = request(sock, "auth_login_start", provider="seatco", timeout=5.0)
    assert resp["ok"] is False
    err = resp["error"]
    assert "verification_uri" in err and "https" in err and "Fox" in err
    assert evil_uri not in json.dumps(resp)
    assert "dev-secret-9" not in json.dumps(resp)  # device code stays server-side

    poll = request(sock, "auth_login_poll", provider="seatco", timeout=5.0)
    assert poll["ok"] is False and "no device login in progress" in poll["error"]

    c = http_client(coop_daemon.settings)
    r = c.post("/api/auth/login", json={"provider": "seatco"})
    assert r.status_code == 422
    body = r.json()
    assert body["ok"] is False and "verification_uri" in body["error"]
    assert evil_uri not in r.text

    assert not (coop_daemon.settings.seats_dir / "seatco.json").exists()


def test_https_verification_uri_relayed_on_socket_and_http(coop_daemon) -> None:
    """Positive e2e: an https device flow still completes end to end — the
    socket op and POST /api/auth/login relay the https verification_uri (the
    URI the SPA renders as a link), the poll authorizes, the session lands
    0600 on disk."""
    uri = "https://auth.example.com/verify?user_code=WXYZ-1234"
    coop_daemon.oauth.verification_uri = uri
    sock = coop_daemon.settings.socket_path

    resp = request(sock, "auth_login_start", provider="seatco", timeout=5.0)
    assert resp["ok"] is True
    assert resp["verification_uri"] == uri and resp["verification_uri"].startswith("https://")
    assert resp["user_code"] == "WXYZ-1234"
    assert "device_code" not in resp and "dev-secret-9" not in json.dumps(resp)

    c = http_client(coop_daemon.settings)
    r = c.post("/api/auth/login", json={"provider": "seatco"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["verification_uri"] == uri
    assert body["verification_uri"].startswith("https://")  # the SPA link-render condition

    poll = request(sock, "auth_login_poll", provider="seatco", timeout=5.0)
    assert poll["ok"] is True and poll["status"] == "authorized"
    f = coop_daemon.settings.seats_dir / "seatco.json"
    assert f.exists() and stat.S_IMODE(f.stat().st_mode) == 0o600


# --- SPA grep-guards: no third-party runtime fetches, https-only href ----------


def test_spa_assets_have_no_third_party_runtime_fetches() -> None:
    """G21 SPA: the shipped assets contain no third-party runtime fetch — no
    absolute URL of any kind in the served html/css/js, no font CDN, no
    @import / url(http) in the CSS — and the system font stack is in place."""
    for name in ("index.html", "app.js", "styles.css"):
        text = (STATIC / name).read_text(encoding="utf-8")
        assert "fonts.googleapis.com" not in text and "fonts.gstatic.com" not in text, name
        assert not re.search(r"https?://", text), f"{name} references an absolute URL"
    css = (STATIC / "styles.css").read_text(encoding="utf-8")
    assert "@import" not in css
    assert not re.search(r"url\(\s*[\"']?https?", css)
    assert "system-ui" in css and "ui-monospace" in css  # system font stack


def test_served_spa_assets_have_no_third_party_fetches(coop_daemon) -> None:
    """Same guard over the bytes the coop actually serves (in-memory dict)."""
    coop_daemon.oauth.verification_uri = "https://auth.example.com/verify"
    c = http_client(coop_daemon.settings)
    for path in ("/", "/app.js", "/styles.css"):
        r = c.get(path)
        assert r.status_code == 200, path
        assert "fonts.g" not in r.text, path
        assert not re.search(r"https?://", r.text), f"{path} serves an absolute URL"


def test_spa_device_verification_link_href_is_https_only() -> None:
    """G7 SPA: the device-verification anchor never takes the raw response
    value as href — it goes through the https-only guard, with a non-link
    fallback, so a `javascript:` URI can never execute in the coop origin."""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    # the guard exists and allows exactly the https: protocol
    assert re.search(r"function httpsOnlyHref\(uri\) \{\s*try \{\s*const u = new URL\(uri\);\s*return u\.protocol === \"https:\" \? u\.href : null;", js), "httpsOnlyHref guard missing/changed"
    # the raw value is never assigned to href directly anywhere
    assert "link.href = resp.verification_uri" not in js
    # the device-verification block assigns href only via the guard
    seg = js.split('$("#device-verification")')[1]
    seg = seg[: seg.index('$("#device-expires")')]
    assert "httpsOnlyHref(uri)" in seg
    assert "link.href = safeHref;" in seg
    assert 'link.removeAttribute("href")' in seg
    # textContent (a safe sink) still shows the URI as text
    assert "link.textContent = uri;" in seg
