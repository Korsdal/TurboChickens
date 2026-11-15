"""Tests for spec 18: the coop Docker container.

No Docker daemon is required (the repo's test environment has none): the
image is a thin shell around `tc daemon start` with the shipped default
settings, so these tests cover exactly what the container runs, in-process:

- the shipped settings file (docker/settings.toml) loads and carries the
  container defaults (data_dir=/data, coop on 0.0.0.0:8737, no fixed token);
- the Dockerfile contract (base image, EXPOSE, VOLUME, HEALTHCHECK on
  loopback /api/ping, ENV, CMD) and the .dockerignore context trim;
- the compose example and the docs (build/run/compose/token/reverse-proxy);
- spec 32 / G12: the non-root + hardened container contract — the Dockerfile
  USER (created with uid/gid > 0, /data chowned to it before VOLUME,
  PYTHONDONTWRITEBYTECODE for the read-only rootfs), every compose hardening
  property (user pin, cap_drop ALL, no-new-privileges, read_only + tmpfs
  /tmp, init, memory/CPU limits), the writable-paths-covered cross-check,
  the docs, and a subprocess e2e proving the CMD serves the Coop, runs a
  job, and shuts down gracefully as a non-root user;
- a subprocess e2e of the container CMD semantics — `tc daemon start` with
  the shipped settings (only data_dir/port redirected to the sandbox) —
  serving the GUI, printing the ephemeral token exactly once, answering the
  healthcheck on loopback, enforcing bearer auth, running a job over HTTP,
  and keeping jobs/runs/results/schedules across a daemon restart on the
  same data dir (the /data volume).

The real `docker build` + `docker run` smoke (image builds, healthcheck
green, container replacement over the volume) is documented in
docs/coop-container.md for environments with a Docker daemon.
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
import yaml
from typer.testing import CliRunner

from turbochickens.cli.main import app
from turbochickens.client import DaemonUnreachable, request
from turbochickens.settings import Settings, load_settings

runner = CliRunner()

REPO = Path(__file__).resolve().parents[1]
DOCKERFILE = REPO / "Dockerfile"
DOCKERIGNORE = REPO / ".dockerignore"
SHIPPED_SETTINGS = REPO / "docker" / "settings.toml"
COMPOSE = REPO / "docker" / "compose.yaml"
DOCS = REPO / "docs" / "coop-container.md"

# A stand-in for the real `vibe` binary (same hermetic fake as test_run_e2e).
FAKE_VIBE = '''#!/usr/bin/env python3
import json, os, re, sys, time
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
        json.dump({"usage": {"input_tokens": 12, "output_tokens": 34, "cost_usd": 0.001}}, f)
print(json.dumps({"message": "hello from fake vibe", "echo": prompt.strip()[:40]}))
sys.exit(0)
'''


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def write_job(base: Path, name: str, trigger_type: str = "manual", schedule: str = "") -> Path:
    d = base / name
    (d / "prompts").mkdir(parents=True, exist_ok=True)
    (d / "schemas").mkdir(parents=True, exist_ok=True)
    (d / "prompts" / "hello.txt").write_text("Say hello")
    (d / "schemas" / "out.schema.json").write_text('{"type": "object"}')
    schedule_line = f'schedule = "{schedule}"' if trigger_type == "cron" else ""
    (d / "job.toml").write_text(
        f"""
[job]
name = "{name}"
description = "container test job"

[trigger]
type = "{trigger_type}"
{schedule_line}

[harness]
adapter = "vibe-cli"
model = "fake-model"
temperature = 0.0

[resources]
timeout_sec = 30
max_context_mb = 2
network = "none"

[input]
prompt_file = "prompts/hello.txt"
attachments = []

[output]
format = "json"
schema_file = "schemas/out.schema.json"
"""
    )
    return d / "job.toml"


# --- the shipped container assets ---------------------------------------------


def test_shipped_settings_file_carries_container_defaults() -> None:
    settings = load_settings(SHIPPED_SETTINGS)
    assert settings.data_dir == Path("/data")
    assert settings.coop_enabled is True
    assert settings.coop_host == "0.0.0.0"
    assert settings.coop_port == 8737
    assert settings.coop_auth_token is None  # ephemeral token is minted at startup
    # every derived stateful path lives under the data dir (the /data volume)
    for p in (
        settings.socket_path,
        settings.db_path,
        settings.audit_log_path,
        settings.results_dir,
        settings.quarantine_dir,
        settings.seats_dir,
        settings.uploads_dir,
    ):
        assert p == Path("/data") or p.is_relative_to("/data"), p
    # the GUI settings payload carries the coop block but never the token
    public = settings.public_dict()
    assert public["coop_enabled"] is True and public["coop_port"] == 8737
    assert "coop_auth_token" not in public


def test_dockerfile_contract() -> None:
    text = DOCKERFILE.read_text()
    # base image pinned by digest (spec 32 ticket 16 / G22) — the deep pin
    # contract lives in tests/test_supply_chain.py
    assert re.search(r"^FROM python:3\.12-slim@sha256:[0-9a-f]{64}$", text, re.M)
    assert re.search(r"^EXPOSE 8737$", text, re.M)
    assert re.search(r'^VOLUME \["/data"\]', text, re.M)
    # settings shipped into the image and picked up via the env var
    assert "TURBOCHICKENS_SETTINGS=/etc/turbochickens/settings.toml" in text
    assert "COPY docker/settings.toml /etc/turbochickens/settings.toml" in text
    # unbuffered stdout so the one-time token line hits `docker logs` at once
    assert "PYTHONUNBUFFERED=1" in text
    # healthcheck hits GET /api/ping on loopback
    health = re.search(r"^HEALTHCHECK .*?CMD (.*)$", text, re.M | re.S)
    assert health and "/api/ping" in health.group(1) and "127.0.0.1:8737" in health.group(1)
    # CMD is the daemon with coop enabled via settings (exec form for SIGTERM)
    assert 'CMD ["tc", "daemon", "start"]' in text


def test_dockerignore_trims_build_context() -> None:
    entries = {l.strip() for l in DOCKERIGNORE.read_text().splitlines() if l.strip() and not l.startswith("#")}
    for heavy in (".git", ".venv", "__pycache__", ".pytest_cache", "tests", "specs"):
        assert any(heavy in e for e in entries), heavy
    # spec 32 ticket 16 / G22: uv.lock must STAY in the context (the image
    # installs from the hash-pinned lock) — pinned by tests/test_supply_chain.py
    assert not any(e in ("uv.lock", "*.lock", "*") for e in entries)


def test_compose_example() -> None:
    text = COMPOSE.read_text()
    assert "image: coop" in text
    # spec 32 ticket 16 / G23: published on loopback only by default
    assert '"127.0.0.1:8737:8737"' in text
    ports = yaml.safe_load(text)["services"]["coop"]["ports"]
    assert ports == ["127.0.0.1:8737:8737"], ports
    assert "coop-data:/data" in text
    assert "coop-data:" in text  # named volume declared


def test_docs_cover_build_run_compose_token_proxy_smoke() -> None:
    text = DOCS.read_text()
    assert "docker build -t coop ." in text
    assert "docker run -p 127.0.0.1:8737:8737 -v coop-data:/data coop" in text
    assert "docker compose -f docker/compose.yaml" in text
    assert "coop auth token:" in text  # ephemeral token handling
    assert "/api/ping" in text and "HEALTHCHECK" in text
    assert re.search(r"reverse proxy", text, re.I) and re.search(r"TLS", text)
    assert "docker logs coop" in text  # manual smoke: token from the logs
    assert "docker rm -f coop" in text  # manual smoke: container replacement over the volume


# --- subprocess e2e: the container CMD with the shipped settings ---------------


def _container_settings(tmp_path: Path, port: int) -> Path:
    """The shipped docker/settings.toml with only data_dir/port redirected
    into the sandbox — coop_enabled/coop_host stay exactly as shipped."""
    text = SHIPPED_SETTINGS.read_text()
    text = text.replace('data_dir = "/data"', f'data_dir = "{tmp_path / "data"}"')
    text = text.replace("coop_port = 8737", f"coop_port = {port}")
    f = tmp_path / "container-settings.toml"
    f.write_text(text)
    return f


class _DaemonProc:
    """A `tc daemon start` subprocess (the container CMD) with captured stdout."""

    def __init__(self, env: dict) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "turbochickens", "daemon", "start"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        self.lines: list[str] = []
        self._reader = threading.Thread(target=self._drain, daemon=True)
        self._reader.start()

    def _drain(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self.lines.append(line.rstrip("\n"))

    def token(self, timeout: float = 10.0) -> str:
        deadline = time.time() + timeout
        while time.time() < deadline:
            found = [l for l in self.lines if l.startswith("coop auth token: ")]
            if found:
                return found[0].split(": ", 1)[1].strip()
            time.sleep(0.05)
        raise AssertionError(f"no ephemeral token printed; stdout so far: {self.lines}")

    def token_lines(self) -> list[str]:
        return [l for l in self.lines if l.startswith("coop auth token: ")]

    def wait_socket(self, settings: Settings, timeout: float = 15.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if settings.socket_path.exists():
                try:
                    if request(settings.socket_path, "ping", timeout=1.0).get("ok"):
                        return
                except DaemonUnreachable:
                    pass
            if self.proc.poll() is not None:
                raise AssertionError(f"daemon exited early rc={self.proc.returncode}: {self.lines}")
            time.sleep(0.05)
        raise AssertionError(f"daemon did not start answering: {self.lines}")

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        self._reader.join(timeout=5)


def test_container_cmd_serves_gui_and_persists_over_data_dir(tmp_path, monkeypatch) -> None:
    port = free_port()
    settings_file = _container_settings(tmp_path, port)
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(settings_file))
    settings = load_settings(settings_file)
    assert settings.coop_enabled and settings.coop_host == "0.0.0.0"  # as shipped

    bin_path = tmp_path / "fake-vibe"
    bin_path.write_text(FAKE_VIBE)
    bin_path.chmod(0o755)
    monkeypatch.setenv("TURBOCHICKENS_VIBE_BIN", str(bin_path))

    env = dict(os.environ)
    manual_job = write_job(tmp_path, "container-manual")
    cron_job = write_job(tmp_path, "container-cron", trigger_type="cron", schedule="0 7 * * 1")
    base = f"http://127.0.0.1:{port}"

    daemon = _DaemonProc(env)
    try:
        daemon.wait_socket(settings)
        token = daemon.token()
        auth = {"Authorization": f"Bearer {token}"}
        c = httpx.Client(base_url=base, timeout=5.0)

        # healthcheck path: /api/ping on loopback, unauthenticated
        assert c.get("/api/ping").status_code == 200
        # GUI shell served; /api/* require the bearer (non-loopback bind)
        page = c.get("/")
        assert page.status_code == 200 and "TurboChickens" in page.text
        assert c.get("/api/status").status_code == 401
        st = c.get("/api/status", headers=auth)
        assert st.status_code == 200
        assert st.json()["coop"] == {
            "enabled": True,
            "url": f"http://0.0.0.0:{port}/",
            "auth_required": True,
        }
        assert token not in settings.audit_log_path.read_text()

        # one full run over HTTP, like the browser does
        r = c.post("/api/runs", headers=auth, json={"spec_path": str(manual_job)})
        assert r.status_code == 200 and r.json()["ok"], r.text
        run_id = r.json()["run_id"]
        deadline = time.time() + 30
        while time.time() < deadline:
            show = c.get(f"/api/runs/{run_id}", headers=auth).json()
            if show["run"]["status"] in ("succeeded", "failed"):
                break
            time.sleep(0.2)
        assert show["run"]["status"] == "succeeded", show
        res = c.get(f"/api/results/{run_id}", headers=auth)
        assert res.status_code == 200 and res.json()["ok"] is True
        assert res.json()["output"]["message"] == "hello from fake vibe"

        # a schedule over HTTP
        r = c.post("/api/schedule", headers=auth, json={"spec_path": str(cron_job)})
        assert r.status_code == 200 and r.json()["ok"], r.text
        sched = c.get("/api/schedule", headers=auth).json()
        assert any(row["job_id"] == "container-cron" for row in sched["schedules"])
    finally:
        daemon.stop()
    assert daemon.proc.returncode == 0
    assert len(daemon.token_lines()) == 1  # ephemeral token printed exactly once

    # container replacement over the same /data: restart the daemon on the
    # same data dir — jobs, runs, results and schedules survive
    daemon2 = _DaemonProc(env)
    try:
        daemon2.wait_socket(settings)
        token2 = daemon2.token()
        auth2 = {"Authorization": f"Bearer {token2}"}
        assert token2 != token  # a fresh ephemeral token per start
        c = httpx.Client(base_url=base, timeout=5.0)
        assert c.get("/api/ping").status_code == 200
        jobs = c.get("/api/jobs", headers=auth2).json()
        assert {j["job_id"] for j in jobs["jobs"]} >= {"container-manual", "container-cron"}
        runs = c.get("/api/runs", headers=auth2).json()
        assert any(r["id"] == run_id and r["status"] == "succeeded" for r in runs["runs"])
        res = c.get(f"/api/results/{run_id}", headers=auth2)
        assert res.status_code == 200 and res.json()["ok"] is True
        sched = c.get("/api/schedule", headers=auth2).json()
        assert any(row["job_id"] == "container-cron" for row in sched["schedules"])
        # and the CLI inside the "container" sees the same state
        result = runner.invoke(app, ["schedule", "list"])
        assert result.exit_code == 0 and "container-cron" in result.output, result.output
    finally:
        daemon2.stop()
    assert daemon2.proc.returncode == 0
    assert len(daemon2.token_lines()) == 1


# --- spec 32 / G12: the coop container runs non-root and hardened --------------
#
# No Docker daemon is needed: USER and the runtime hardening live in the
# Dockerfile / compose file, so contract tests pin them there, and one
# subprocess e2e proves the container CMD still starts, serves the Coop, runs
# a job, and shuts down gracefully while running as a non-root user.


def test_dockerfile_runs_as_non_root_user() -> None:
    text = DOCKERFILE.read_text()
    # explicit runtime USER, and it is not root
    user_m = re.search(r"^USER (\S+)$", text, re.M)
    assert user_m, "Dockerfile must set an explicit USER"
    user = user_m.group(1)
    assert user.split(":")[0] not in ("root", "0")
    # the user is created with a real uid/gid > 0 (no login shell, no home)
    uid_m = re.search(r"useradd .*?--uid (\d+)", text)
    gid_m = re.search(r"groupadd .*?--gid (\d+)", text)
    assert uid_m and gid_m, "Dockerfile must create the runtime user"
    assert int(uid_m.group(1)) > 0 and int(gid_m.group(1)) > 0
    assert "nologin" in text
    # /data is chowned to that user BEFORE VOLUME, so a fresh named volume
    # inherits the ownership and the non-root daemon can write its socket,
    # db, audit, results and workspaces there
    chown_m = re.search(r"^RUN .*chown (\S+) /data\s*$", text, re.M)
    vol_m = re.search(r'^VOLUME \["/data"\]$', text, re.M)
    assert chown_m and vol_m and chown_m.start() < vol_m.start()
    assert chown_m.group(1).split(":")[0] == user.split(":")[0]
    # nothing ever switches back to root
    assert not re.search(r"^USER (root|0)\s*$", text, re.M)
    # with a read-only rootfs the interpreter must not need bytecode writes
    assert "PYTHONDONTWRITEBYTECODE=1" in text


def test_compose_pins_every_hardening_property() -> None:
    text = COMPOSE.read_text()
    # non-root runtime user pinned in compose too (matches the image USER)
    user_m = re.search(r'^\s*user:\s*"(\d+):(\d+)"\s*$', text, re.M)
    assert user_m and int(user_m.group(1)) != 0 and int(user_m.group(2)) != 0
    # all Linux capabilities dropped
    assert re.search(r"cap_drop:\s*\n\s*-\s*ALL\s*$", text, re.M)
    # no-new-privileges: setuid/file-caps can never re-elevate
    assert re.search(r"security_opt:\s*\n\s*-\s*no-new-privileges:true\s*$", text, re.M)
    # read-only root filesystem …
    assert re.search(r"^\s*read_only:\s*true\s*$", text, re.M)
    # … with a tmpfs /tmp for scratch
    assert re.search(r"tmpfs:\s*\n\s*-\s*/tmp\s*$", text, re.M)
    # an init process so PID 1 reaps orphaned children
    assert re.search(r"^\s*init:\s*true\s*$", text, re.M)
    # memory limit of at least 256 MiB
    mem_m = re.search(r'^\s*mem_limit:\s*"?(\d+(?:\.\d+)?)\s*([kKmMgG])"?\s*$', text, re.M)
    assert mem_m, "compose must set a memory limit"
    mult = {"k": 1024, "m": 1024**2, "g": 1024**3}[mem_m.group(2).lower()]
    assert float(mem_m.group(1)) * mult >= 256 * 1024**2
    # CPU limit > 0
    cpus_m = re.search(r'^\s*cpus:\s*"?(\d+(?:\.\d+)?)"?\s*$', text, re.M)
    assert cpus_m and float(cpus_m.group(1)) > 0
    # the /data volume stays mounted — the only writable stateful path under
    # a read-only rootfs
    assert re.search(r"-\s*coop-data:/data\s*$", text, re.M)


def test_read_only_rootfs_writable_paths_are_all_covered() -> None:
    """read_only only works because every stateful path of the shipped
    settings lives under the /data volume (plus the /tmp tmpfs): pin that
    combination across the shipped settings, Dockerfile and compose file."""
    settings = load_settings(SHIPPED_SETTINGS)
    stateful = [
        settings.socket_path,
        settings.db_path,
        settings.audit_log_path,
        settings.results_dir,
        settings.quarantine_dir,
        settings.seats_dir,
        settings.uploads_dir,
        settings.data_dir / "workspaces",
    ]
    for p in stateful:
        assert p == Path("/data") or p.is_relative_to("/data"), p
    dockerfile = DOCKERFILE.read_text()
    compose = COMPOSE.read_text()
    assert re.search(r'^VOLUME \["/data"\]$', dockerfile, re.M)
    assert re.search(r"-\s*coop-data:/data\s*$", compose, re.M)
    assert re.search(r"tmpfs:\s*\n\s*-\s*/tmp\s*$", compose, re.M)
    assert re.search(r"^\s*read_only:\s*true\s*$", compose, re.M)
    assert "PYTHONDONTWRITEBYTECODE=1" in dockerfile


def test_docs_cover_the_hardened_non_root_runtime() -> None:
    text = DOCS.read_text()
    for flag in (
        "--user 10001:10001",
        "--cap-drop ALL",
        "no-new-privileges:true",
        "--read-only",
        "--tmpfs /tmp",
        "--init",
        "--memory",
        "--cpus",
    ):
        assert flag in text, flag
    assert re.search(r"non-root", text, re.I) and "10001" in text


@pytest.mark.skipif(
    os.geteuid() == 0,
    reason="the non-root e2e needs a non-root test runner (CI runs as non-root); "
    "the Dockerfile/compose contract tests pin USER + hardening statically",
)
def test_container_cmd_serves_coop_and_shuts_down_gracefully_as_non_root(tmp_path, monkeypatch) -> None:
    """Positive path: the container CMD (`tc daemon start` with the shipped
    settings, only data_dir/port redirected) starts as the current non-root
    user, serves the Coop, runs a job end to end, and shuts down gracefully
    on SIGTERM."""
    assert os.geteuid() != 0
    port = free_port()
    settings_file = _container_settings(tmp_path, port)
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(settings_file))
    settings = load_settings(settings_file)
    assert settings.coop_enabled and settings.coop_host == "0.0.0.0"  # as shipped

    bin_path = tmp_path / "fake-vibe"
    bin_path.write_text(FAKE_VIBE)
    bin_path.chmod(0o755)
    monkeypatch.setenv("TURBOCHICKENS_VIBE_BIN", str(bin_path))

    env = dict(os.environ)
    job = write_job(tmp_path, "nonroot-job")
    base = f"http://127.0.0.1:{port}"

    daemon = _DaemonProc(env)
    try:
        daemon.wait_socket(settings)
        # the daemon really runs non-root: the socket it created on disk is
        # owned by the (non-root) uid of this test process
        assert os.stat(settings.socket_path).st_uid == os.geteuid() != 0
        token = daemon.token()
        auth = {"Authorization": f"Bearer {token}"}
        c = httpx.Client(base_url=base, timeout=5.0)

        # healthcheck + GUI served; /api/* behind the bearer
        assert c.get("/api/ping").status_code == 200
        page = c.get("/")
        assert page.status_code == 200 and "TurboChickens" in page.text
        assert c.get("/api/status").status_code == 401
        st = c.get("/api/status", headers=auth)
        assert st.status_code == 200 and st.json()["coop"]["auth_required"] is True
        assert token not in settings.audit_log_path.read_text()

        # one full run as the non-root user: egg queued → hatched (workspace
        # under the data dir) → clutch in the nest box → attested teardown
        r = c.post("/api/runs", headers=auth, json={"spec_path": str(job)})
        assert r.status_code == 200 and r.json()["ok"], r.text
        run_id = r.json()["run_id"]
        deadline = time.time() + 30
        while time.time() < deadline:
            show = c.get(f"/api/runs/{run_id}", headers=auth).json()
            if show["run"]["status"] in ("succeeded", "failed"):
                break
            time.sleep(0.2)
        assert show["run"]["status"] == "succeeded", show
        res = c.get(f"/api/results/{run_id}", headers=auth)
        assert res.status_code == 200 and res.json()["ok"] is True
        assert res.json()["output"]["message"] == "hello from fake vibe"
        # the abattoir removed the workspace
        assert not (settings.data_dir / "workspaces" / run_id).exists()
    finally:
        daemon.stop()
    # graceful shutdown as non-root: SIGTERM → exit 0, exactly one token
    # line, and the HTTP port refuses connections afterwards
    assert daemon.proc.returncode == 0
    assert len(daemon.token_lines()) == 1
    with pytest.raises(httpx.ConnectError):
        httpx.get(f"{base}/api/ping", timeout=2.0)
