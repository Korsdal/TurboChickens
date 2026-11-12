"""Tests for spec 02: daemon socket protocol, `tc status`, `tc daemon start/stop`.

Seams: the daemon is booted on a temp socket (in a thread for protocol/CLI
tests, as a real `python -m turbochickens daemon start` subprocess for the
e2e test) and every assertion goes through the JSON-lines socket or the CLI —
external, observable behavior only.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

import turbochickens
from turbochickens.audit import AuditLog
from turbochickens.cli.main import app
from turbochickens.client import DaemonUnreachable, request
from turbochickens.daemon import run_daemon
from turbochickens.settings import load_settings

runner = CliRunner()


@pytest.fixture
def settings_file(tmp_path, monkeypatch) -> Path:
    f = tmp_path / "settings.toml"
    f.write_text(f'data_dir = "{tmp_path / "data"}"\nweight_budget_override = 4\n')
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(f))
    return f


class DaemonThread:
    """Run the daemon in-process on a background thread with its own loop."""

    def __init__(self, settings) -> None:
        self.settings = settings
        self.error: Exception | None = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        try:
            asyncio.run(run_daemon(self.settings))
        except Exception as e:  # surfaced by start() if the daemon never comes up
            self.error = e

    def start(self) -> None:
        self.thread.start()
        deadline = time.time() + 10
        while time.time() < deadline:
            if self.settings.socket_path.exists():
                try:
                    if request(self.settings.socket_path, "ping", timeout=1.0).get("ok"):
                        return
                except DaemonUnreachable:
                    pass
            time.sleep(0.05)
        raise RuntimeError(f"daemon did not start: {self.error!r}")

    def stop(self) -> None:
        try:
            request(self.settings.socket_path, "shutdown", timeout=2.0)
        except DaemonUnreachable:
            pass
        self.thread.join(timeout=10)
        assert not self.thread.is_alive(), "daemon thread did not stop"


@pytest.fixture
def daemon(settings_file):
    settings = load_settings(settings_file)
    dt = DaemonThread(settings)
    dt.start()
    yield dt
    dt.stop()


def raw_lines(socket_path: Path, payloads: list[bytes]) -> list[dict]:
    """Send raw lines over the socket and read one JSON response per line."""

    async def go() -> list[dict]:
        reader, writer = await asyncio.open_unix_connection(str(socket_path))
        writer.write(b"".join(payloads))
        await writer.drain()
        out = []
        for _ in payloads:
            line = await reader.readline()
            assert line, "daemon closed connection early"
            out.append(json.loads(line.decode("utf-8")))
        writer.close()
        return out

    return asyncio.run(go())


# --- protocol ---


def test_ping_answers_json_lines(daemon) -> None:
    resp = request(daemon.settings.socket_path, "ping", timeout=2.0)
    assert resp["ok"] is True
    assert resp["pong"] is True
    assert resp["version"] == turbochickens.__version__
    assert resp["pid"] == os.getpid()
    assert resp["protocol"] >= 1


def test_status_reports_up_empty_queue_zero_in_flight(daemon) -> None:
    resp = request(daemon.settings.socket_path, "status", timeout=2.0)
    assert resp["ok"] is True
    assert resp["daemon"]["up"] is True
    assert resp["queue_depth"] == 0
    assert resp["in_flight"] == []
    assert resp["weight_budget"] == 4  # from the fixture's settings override
    assert resp["socket"] == str(daemon.settings.socket_path)


def test_unknown_op_returns_error(daemon) -> None:
    resp = request(daemon.settings.socket_path, "frobnicate", timeout=2.0)
    assert resp["ok"] is False
    assert "unknown op" in resp["error"]


def test_malformed_and_non_object_lines_get_errors(daemon) -> None:
    resps = raw_lines(
        daemon.settings.socket_path,
        [b"this is not json\n", b"[1, 2, 3]\n", b'{"op": "ping"}\n'],
    )
    assert resps[0]["ok"] is False and "malformed" in resps[0]["error"]
    assert resps[1]["ok"] is False and "JSON object" in resps[1]["error"]
    assert resps[2]["ok"] is True and resps[2]["pong"] is True  # connection stays usable


def test_shutdown_op_stops_daemon_and_cleans_socket(settings_file) -> None:
    settings = load_settings(settings_file)
    dt = DaemonThread(settings)
    dt.start()
    resp = request(settings.socket_path, "shutdown", timeout=2.0)
    assert resp["ok"] is True
    dt.thread.join(timeout=10)
    assert not dt.thread.is_alive()
    assert not settings.socket_path.exists()  # socket removed on shutdown
    with pytest.raises(DaemonUnreachable):
        request(settings.socket_path, "ping", timeout=1.0)


def test_audit_log_records_daemon_start_and_stop(settings_file) -> None:
    settings = load_settings(settings_file)
    dt = DaemonThread(settings)
    dt.start()
    dt.stop()
    events = [r["event"] for r in AuditLog(settings.audit_log_path).read_all()]
    assert events == ["daemon.started", "daemon.stopped"]


# --- CLI against a live daemon ---


def test_tc_status_shows_up_empty_queue_zero_in_flight(daemon) -> None:
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    assert "daemon: up" in result.output
    assert f"version={turbochickens.__version__}" in result.output
    assert "queue_depth: 0" in result.output
    assert "in_flight: 0" in result.output
    assert "weight_budget: 4" in result.output


def test_tc_status_when_daemon_down_exits_1(settings_file) -> None:
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 1
    assert "no daemon" in result.output


def test_tc_daemon_stop_round_trip(daemon) -> None:
    result = runner.invoke(app, ["daemon", "stop"])
    assert result.exit_code == 0, result.output
    assert "daemon stopped" in result.output
    daemon.thread.join(timeout=10)
    assert not daemon.thread.is_alive()
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 1


def test_tc_daemon_stop_when_down_exits_1(settings_file) -> None:
    result = runner.invoke(app, ["daemon", "stop"])
    assert result.exit_code == 1
    assert "no daemon" in result.output


# --- end-to-end: real daemon subprocess + real CLI ---


def test_e2e_daemon_subprocess_and_tc_status(settings_file) -> None:
    settings = load_settings(settings_file)
    env = dict(os.environ)  # includes TURBOCHICKENS_SETTINGS from the fixture
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
                out = proc.stdout.read() if proc.stdout else ""
                raise AssertionError(f"daemon process exited early rc={proc.returncode}: {out}")
            time.sleep(0.05)
        else:
            raise AssertionError("daemon subprocess did not start answering")

        result = runner.invoke(app, ["status"])
        assert result.exit_code == 0, result.output
        assert "daemon: up" in result.output
        assert "queue_depth: 0" in result.output
        assert "in_flight: 0" in result.output

        events = [r["event"] for r in AuditLog(settings.audit_log_path).read_all()]
        assert "daemon.started" in events

        result = runner.invoke(app, ["daemon", "stop"])
        assert result.exit_code == 0, result.output
        proc.wait(timeout=10)
        assert proc.returncode == 0

        events = [r["event"] for r in AuditLog(settings.audit_log_path).read_all()]
        assert "daemon.stopped" in events
        with pytest.raises(DaemonUnreachable):
            request(settings.socket_path, "ping", timeout=1.0)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
