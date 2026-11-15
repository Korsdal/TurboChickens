"""Tests for spec 32 ticket 03 (review finding G2): Perch logout validates the
provider against the provider registry and contains the session path inside
the data dir before any unlink — a crafted provider name like ``/etc/anything``
can never delete a ``*.json`` file outside the data dir, and the Headcount
never records a fake ``perch.released`` row.

Seams asserted at the highest existing level, negative + positive per fix:
- pure: ``SeatStore._path`` applies the shared Fence rule
  (``fence.resolve_contained``) to the provider name — traversal, absolute,
  backslash, NUL, and symlink-alias names are all refused before any
  filesystem touch;
- broker: ``SeatBroker.logout`` requires a registered provider name
  (providers.toml) — field-named ``provider:`` refusal otherwise;
- socket op: ``auth_logout`` over the raw daemon socket — unregistered and
  traversal providers refused with field-named errors (``errors[0].field ==
  "provider"``), provably nothing outside the data dir touched (full
  outside-the-data-dir snapshot before/after + sentinel file), zero
  ``perch.released`` rows for the refusals, and a real seat logout still
  releases the session with one truthful audit row — including a *registered*
  provider whose name is itself a traversal ref (the registry check passes,
  so the Fence containment is what refuses);
- HTTP: ``POST /api/auth/logout`` mirrors the same refusals as 422 with the
  field-named error body and still releases a real seat (200);
- CLI: ``tc auth logout`` refuses unregistered/traversal providers (rc 1)
  and still releases a real seat (rc 0).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from test_coop import http_client
from test_coop_auth import seat_coop_daemon, seat_coop_env  # noqa: F401 — fixtures
from test_daemon import DaemonThread
from test_run_e2e import combined_output
from test_seat_auth import seat_env  # noqa: F401 — fixture
from turbochickens.audit import AuditLog
from turbochickens.cli.main import app
from turbochickens.client import request
from turbochickens.fence import resolve_contained
from turbochickens.providers import load_providers
from turbochickens.seat import SeatBroker, SeatError, SeatStore
from turbochickens.settings import load_settings

runner = CliRunner()

#: a *registered* provider whose name is a traversal ref — the registry check
#: passes, so the Fence containment is what must refuse the logout
HOSTILE_PROVIDER_TOML = """
[providers."../outside/anything"]
base_url = "http://127.0.0.1:{api_port}/v1"
api_key_env = "HOSTILE_TOKEN"

[providers."../outside/anything".models.hostile-model]
context_window = 8192
input_usd_per_mtok = 1.0
output_usd_per_mtok = 2.0
"""

#: provider names that must never name a session file outside the seats dir
STORE_HOSTILE_PROVIDERS = [
    "/etc/anything",
    "../outside/anything",
    "a/../../outside/anything",
    "seats/../../outside/anything",
    "sub\\..\\..\\outside",
    "x\x00y",
]


def _snapshot_outside(root: Path, data_dir: Path) -> dict[str, bytes]:
    """Every file under `root` that is NOT inside `data_dir`, with contents —
    the proof that a hostile logout touched nothing outside the data dir."""
    data_resolved = data_dir.resolve()
    snap: dict[str, bytes] = {}
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rp = p.resolve()
        if rp == data_resolved or data_resolved in rp.parents:
            continue
        snap[str(p.relative_to(root))] = p.read_bytes()
    return snap


@pytest.fixture
def hostile_seat_env(seat_env):
    """seat_env plus the registered traversal-named provider appended to
    providers.toml (see HOSTILE_PROVIDER_TOML)."""
    seat_env.providers_file.write_text(
        seat_env.providers_file.read_text()
        + HOSTILE_PROVIDER_TOML.format(api_port=seat_env.api.server_address[1])
    )
    yield seat_env


@pytest.fixture
def hostile_seat_daemon(hostile_seat_env):
    settings = load_settings(hostile_seat_env.settings_file)
    dt = DaemonThread(settings)
    dt.start()
    yield dt
    dt.stop()


# --- pure seam: the SeatStore contains the session path (the Fence rule) ------


def test_seat_store_contains_session_path_inside_seats_dir(tmp_path: Path) -> None:
    store = SeatStore(tmp_path / "data")
    seats = (tmp_path / "data" / "seats").resolve()
    assert store._path("seatco") == seats / "seatco.json"
    # a nested name stays contained (the registry still gates which names exist)
    assert store._path("sub/dir") == seats / "sub" / "dir.json"

    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "anything.json"
    sentinel.write_text('{"sentinel": true}')
    hostile = [*STORE_HOSTILE_PROVIDERS, str(outside / "anything")]  # absolute aim at the sentinel
    for p in hostile:
        with pytest.raises(SeatError) as ei:
            store._path(p)
        msg = str(ei.value)
        assert msg.startswith("provider: the fox refuses"), (p, msg)
        assert "the fence holds" in msg
        assert resolve_contained(store.dir, f"{p}.json") is None
        # delete refuses before any filesystem touch
        with pytest.raises(SeatError):
            store.delete(p)
        assert sentinel.read_text() == '{"sentinel": true}'


def test_seat_store_refuses_symlink_alias_pointing_outside(tmp_path: Path) -> None:
    store = SeatStore(tmp_path / "data")
    store._ensure_dir()
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "secret.json"
    sentinel.write_text("SECRET")
    alias = store.dir / "alias.json"
    alias.symlink_to(sentinel)
    with pytest.raises(SeatError) as ei:
        store.delete("alias")
    assert "the fox refuses" in str(ei.value)
    assert sentinel.read_text() == "SECRET"  # the target was never touched
    assert alias.is_symlink()  # and the link itself was not unlinked either


# --- broker seam: logout requires a registered provider name ------------------


def test_broker_logout_requires_a_registered_provider(seat_env) -> None:
    settings = load_settings(seat_env.settings_file)
    providers = load_providers(settings.providers_path, settings.data_dir)
    broker = SeatBroker(settings, providers)
    outside = seat_env.tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "anything.json"
    sentinel.write_text("SENTINEL")
    for name in ("no-such-provider", "/etc/anything", "../outside/anything", str(outside / "anything")):
        with pytest.raises(SeatError) as ei:
            broker.logout(name)
        msg = str(ei.value)
        assert msg.startswith("provider:"), (name, msg)
        assert "unknown provider" in msg, (name, msg)
        assert sentinel.read_text() == "SENTINEL"
    # a registered provider without a stored seat is a truthful refusal too
    with pytest.raises(SeatError) as ei:
        broker.logout("seatco")
    assert str(ei.value).startswith("provider:")
    assert "no perch seat stored for provider 'seatco'" in str(ei.value)


# --- socket op seam: auth_logout over the raw daemon socket --------------------


def test_socket_logout_validates_provider_and_contains_session_path(hostile_seat_daemon) -> None:
    settings = hostile_seat_daemon.settings
    sock = settings.socket_path
    tmp_path = settings.data_dir.parent
    outside = tmp_path / "outside"
    outside.mkdir(exist_ok=True)
    sentinel = outside / "anything.json"
    sentinel.write_text('{"sentinel": "do-not-delete"}')

    # log in for real so a session exists
    start = request(sock, "auth_login_start", provider="seatco", timeout=5.0)
    assert start["ok"] is True
    poll = request(sock, "auth_login_poll", provider="seatco", timeout=5.0)
    assert poll["ok"] is True and poll["status"] == "authorized"
    assert (settings.seats_dir / "seatco.json").exists()

    audit = AuditLog(settings.audit_log_path)
    assert [r for r in audit.read_all() if r["event"] == "perch.released"] == []
    snap_before = _snapshot_outside(tmp_path, settings.data_dir)

    hostile = [
        "/etc/anything",
        "../outside/anything",  # registered hostile name → the Fence refuses
        "a/../../outside/anything",
        str(outside / "anything"),  # absolute aim at the sentinel
        "no-such-provider",
        "sub\\..\\..\\outside",
    ]
    for p in hostile:
        resp = request(sock, "auth_logout", provider=p, timeout=2.0)
        assert resp["ok"] is False, (p, resp)
        assert resp["error"].startswith("provider:"), (p, resp)
        assert resp["errors"][0]["field"] == "provider", (p, resp)
        if p == "../outside/anything":
            assert "the fox refuses" in resp["error"], (p, resp)
        else:
            assert "unknown provider" in resp["error"], (p, resp)

    # provably nothing outside the data dir was touched, and the Headcount
    # never recorded a fake perch.released row
    assert _snapshot_outside(tmp_path, settings.data_dir) == snap_before
    assert sentinel.read_text() == '{"sentinel": "do-not-delete"}'
    assert [r for r in audit.read_all() if r["event"] == "perch.released"] == []

    # positive: a registered seat logout still releases the session and
    # records exactly one truthful perch.released row
    out = request(sock, "auth_logout", provider="seatco", timeout=2.0)
    assert out["ok"] is True and out["provider"] == "seatco"
    assert not (settings.seats_dir / "seatco.json").exists()
    rows = [r for r in audit.read_all() if r["event"] == "perch.released"]
    assert len(rows) == 1 and rows[0]["provider"] == "seatco" and rows[0]["mode"] == "seat"
    again = request(sock, "auth_logout", provider="seatco", timeout=2.0)
    assert again["ok"] is False and "no perch seat" in again["error"]
    assert again["errors"][0]["field"] == "provider"


# --- HTTP seam: POST /api/auth/logout mirrors the same contract ----------------


def test_http_logout_refuses_traversal_and_still_releases_a_real_seat(seat_coop_daemon) -> None:  # noqa: F811
    settings = seat_coop_daemon.settings
    c = http_client(settings)
    tmp_path = settings.data_dir.parent
    outside = tmp_path / "outside"
    outside.mkdir(exist_ok=True)
    sentinel = outside / "anything.json"
    sentinel.write_text("SENTINEL")

    assert c.post("/api/auth/login", json={"provider": "seatco"}).status_code == 200
    r = c.post("/api/auth/login/status", json={"provider": "seatco"})
    assert r.status_code == 200 and r.json()["status"] == "authorized"
    assert (settings.seats_dir / "seatco.json").exists()

    for p in ("/etc/anything", "../outside/anything", str(outside / "anything"), "no-such"):
        r = c.post("/api/auth/logout", json={"provider": p})
        assert r.status_code == 422, (p, r.text)
        body = r.json()
        assert body["ok"] is False and body["error"].startswith("provider:"), (p, body)
        assert body["errors"][0]["field"] == "provider", (p, body)
    assert sentinel.read_text() == "SENTINEL"
    assert not any(row["event"] == "perch.released" for row in AuditLog(settings.audit_log_path).read_all())

    # positive over HTTP: the real seat logout still works (200) and audits once
    r = c.post("/api/auth/logout", json={"provider": "seatco"})
    assert r.status_code == 200 and r.json()["ok"] is True and r.json()["provider"] == "seatco"
    assert not (settings.seats_dir / "seatco.json").exists()
    rows = [row for row in AuditLog(settings.audit_log_path).read_all() if row["event"] == "perch.released"]
    assert len(rows) == 1 and rows[0]["provider"] == "seatco"
    assert c.post("/api/auth/logout", json={"provider": "seatco"}).status_code == 422


# --- CLI seam: tc auth logout ---------------------------------------------------


def test_cli_logout_refuses_unregistered_and_traversal_providers(seat_env) -> None:  # noqa: F811
    seat_env.providers_file.write_text(
        seat_env.providers_file.read_text()
        + HOSTILE_PROVIDER_TOML.format(api_port=seat_env.api.server_address[1])
    )
    outside = seat_env.tmp_path / "outside"
    outside.mkdir(exist_ok=True)
    sentinel = outside / "anything.json"
    sentinel.write_text("SENTINEL")

    for p in ("no-such-provider", "/etc/anything", "../outside/anything", str(outside / "anything")):
        r = runner.invoke(app, ["auth", "logout", p])
        assert r.exit_code == 1, (p, combined_output(r))
        out = combined_output(r)
        assert "provider:" in out and ("unknown provider" in out or "the fox refuses" in out), (p, out)
    assert sentinel.read_text() == "SENTINEL"

    # positive: login then logout still works through the CLI
    r = runner.invoke(app, ["auth", "login", "seatco"])
    assert r.exit_code == 0, combined_output(r)
    r = runner.invoke(app, ["auth", "logout", "seatco"])
    assert r.exit_code == 0, combined_output(r)
    assert "released perch seat for provider 'seatco'" in r.output
