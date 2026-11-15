"""Tests for spec 29: the Beak — read-only live view into a running chicken.

Seams (per the spec's checkbox list):
- the pure seam `beak.py` (path lock, private-config blocklist, tree, size-capped
  file peek) on a temp workspace;
- the `workspace_peek` socket op against a real daemon in a thread, with a
  hermetic fake vibe binary ($TURBOCHICKENS_VIBE_BIN) that writes files into
  its workspace (cwd) and then sleeps, so the run stays in flight while we peek;
- HTTP parity: `GET /api/runs/{id}/workspace(?path=…)` == the socket payload,
  404 unknown run, 200 structured closed-beak payload for terminal runs,
  200 {ok:false, daemon.up:false} on a dead-socket transport, bearer 401 when
  required;
- secrecy: no token/seat/gate material in any tree, file content, or response;
- served SPA markup for the Beak tab (no host paths in the assets);
- a subprocess e2e (`tc coop start` + fake vibe): peek a running chicken over
  HTTP, then stop it and watch the beak close.

All assertions are external, observable behavior: socket payloads, HTTP status
codes and JSON bodies, served markup, audit contents.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from test_coop import _wait_socket, free_port, http_client, write_settings
from test_daemon import DaemonThread
from test_run_e2e import combined_output, run_id_of, write_job
from turbochickens.audit import AuditLog
from turbochickens import beak
from turbochickens.cli.main import app
from turbochickens.client import request
from turbochickens.coop import CoopServer, SocketTransport
from turbochickens.runner import TERMINAL_STATES
from turbochickens.settings import load_settings
from turbochickens.spec.loader import spec_hash, load_spec

runner = CliRunner()

SECRET = "BEAK-SECRET-9f3a-do-not-serve"

# A stand-in for the real `vibe` binary that acts like a harness working in its
# workspace (cwd): it writes notes + a big file + a binary blob + the private
# harness config (as specs 27/28 really do), then honors "SLEEP <s>" so the
# chicken stays in flight while the tests peek, then prints the JSON output.
BEAK_VIBE = '''#!/usr/bin/env python3
import json, os, re, sys, time
args = sys.argv[1:]
if "--version" in args:
    print("vibe 9.9.9-fake")
    sys.exit(0)
prompt = ""
if "--prompt-file" in args:
    with open(args[args.index("--prompt-file") + 1]) as f:
        prompt = f.read()
os.makedirs("data", exist_ok=True)
with open("notes.txt", "w") as f:
    f.write("pecked notes\\nline two\\n")
with open("data/big.txt", "w") as f:
    f.write("A" * 200000)
with open("blob.bin", "wb") as f:
    f.write(bytes(range(256)))
os.makedirs(".vibe", exist_ok=True)
with open(".vibe/config.toml", "w") as f:
    f.write("[mcp_servers.gate]\\nenv = { GATE_TOKEN = \\"''' + SECRET + '''\\" }\\n")
with open(".tc-farmhand-settings.toml", "w") as f:
    f.write("# token-free settings for the farmhand\\ndata_dir = \\"/daemon/private\\"\\n")
m = re.search(r"SLEEP ([0-9.]+)", prompt)
if m:
    time.sleep(float(m.group(1)))
out_dir = args[args.index("--output-dir") + 1] if "--output-dir" in args else None
if out_dir:
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "export.json"), "w") as f:
        json.dump({"usage": {"input_tokens": 12, "output_tokens": 34}}, f)
print(json.dumps({"message": "hello from fake vibe", "echo": prompt.strip()[:40]}))
'''


@pytest.fixture
def coop_daemon(tmp_path, monkeypatch):
    f = write_settings(tmp_path, coop_enabled=True, coop_port=free_port(), coop_loopback_open=True)
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(f))
    settings = load_settings(f)
    dt = DaemonThread(settings)
    dt.start()
    yield dt
    dt.stop()


@pytest.fixture
def budget1_daemon(tmp_path, monkeypatch):
    """Budget 1: a second enqueued vibe-cli egg (weight 1.0) stays queued."""
    f = tmp_path / "settings.toml"
    f.write_text(
        f'data_dir = "{tmp_path / "data"}"\nweight_budget_override = 1\n'
        f'coop_enabled = true\ncoop_port = {free_port()}\n'
    )
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(f))
    settings = load_settings(f)
    dt = DaemonThread(settings)
    dt.start()
    yield dt
    dt.stop()


@pytest.fixture
def auth_daemon(tmp_path, monkeypatch):
    f = write_settings(
        tmp_path,
        coop_enabled=True,
        coop_host="0.0.0.0",
        coop_port=free_port(),
        coop_auth_token="spec29-coop-token",
    )
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(f))
    settings = load_settings(f)
    dt = DaemonThread(settings)
    dt.start()
    yield dt
    dt.stop()


@pytest.fixture
def beak_vibe(tmp_path, monkeypatch):
    bin_path = tmp_path / "fake-vibe-beak"
    bin_path.write_text(BEAK_VIBE)
    bin_path.chmod(0o755)
    monkeypatch.setenv("TURBOCHICKENS_VIBE_BIN", str(bin_path))
    return bin_path


def enqueue(sock, spec_path: Path) -> str:
    digest = spec_hash(load_spec(spec_path))
    resp = request(sock, "enqueue", timeout=5.0, spec_path=str(spec_path.resolve()), spec_hash=digest)
    assert resp["ok"], resp
    return resp["run_id"]


def wait_status(sock, run_id: str, want, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = request(sock, "runs_show", run_id=run_id, timeout=2.0)
        if resp.get("ok") and resp["run"]["status"] in want:
            return resp["run"]
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} did not reach {want} within {timeout}s")


def wait_tree(sock, run_id: str, want_path: str, timeout: float = 15.0) -> dict:
    """Poll workspace_peek until the harness has written `want_path` into the
    workspace (status `running` is set just before the harness spawns)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = request(sock, "workspace_peek", run_id=run_id, timeout=2.0)
        if resp.get("ok") and any(e["path"] == want_path for e in resp["tree"]["entries"]):
            return resp
        time.sleep(0.1)
    raise AssertionError(f"workspace of {run_id} did not list {want_path!r} within {timeout}s")


# --- pure seam: path lock, private blocklist, tree, size-capped peek ------------


def test_beak_pure_seam_path_lock_and_private_blocklist(tmp_path) -> None:
    root = tmp_path / "ws"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "f.txt").write_text("hi")
    (root / ".vibe").mkdir()
    (root / ".vibe" / "config.toml").write_text(f'secret = "{SECRET}"')
    (root / ".tc-farmhand-settings.toml").write_text("data_dir = '/x'")
    outside = tmp_path / "outside.txt"
    outside.write_text("host secret")
    (root / "escape").symlink_to(outside)

    # safe_relpath: relative POSIX only
    assert beak.safe_relpath("a/b.txt") == "a/b.txt"
    for bad in ("", "  ", "/abs/path", "../up", "a/../b", "a//b", "./x", "a\\b", "a\x00b", ".."):
        assert beak.safe_relpath(bad) is None, bad

    # resolve_locked: inside ok, symlink escape and '..' refused
    assert beak.resolve_locked(root, "sub/f.txt") == (root / "sub" / "f.txt").resolve()
    assert beak.resolve_locked(root, "escape") is None
    assert beak.resolve_locked(root, "sub/../sub/f.txt") is None
    assert beak.resolve_locked(root, "../outside.txt") is None

    # private harness config is blocked by name, ordinary dotfiles are not
    assert beak.is_private_relpath(".vibe/config.toml")
    assert beak.is_private_relpath(".vibe")
    assert beak.is_private_relpath(".tc-farmhand-settings.toml")
    assert not beak.is_private_relpath(".gitignore")
    assert not beak.is_private_relpath("notes.txt")


def test_beak_pure_seam_tree_and_peek_file(tmp_path) -> None:
    root = tmp_path / "ws"
    (root / "data").mkdir(parents=True)
    (root / "notes.txt").write_text("hello beak\n")
    (root / "data" / "big.txt").write_text("B" * (beak.WORKSPACE_PEEK_MAX_BYTES + 5000))
    (root / "blob.bin").write_bytes(bytes(range(256)))

    tree = beak.workspace_tree(root)
    paths = [e["path"] for e in tree["entries"]]
    assert paths == sorted(paths)
    assert {"path": "data", "type": "dir", "size_bytes": 0} in tree["entries"]
    assert any(e["path"] == "notes.txt" and e["type"] == "file" and e["size_bytes"] == 11 for e in tree["entries"])
    assert tree["truncated"] is False and tree["n_entries"] == len(tree["entries"])
    assert tree["total_bytes"] == 11 + beak.WORKSPACE_PEEK_MAX_BYTES + 5000 + 256

    # text file: utf-8 content, no truncation
    got = beak.peek_file(root, "notes.txt")
    assert got["encoding"] == "utf-8" and got["content"] == "hello beak\n"
    assert got["truncated"] is False and got["size_bytes"] == got["served_bytes"] == 11

    # over-cap file: served bytes capped, truncated flag set
    big = beak.peek_file(root, "data/big.txt")
    assert big["truncated"] is True
    assert big["served_bytes"] == beak.WORKSPACE_PEEK_MAX_BYTES
    assert big["size_bytes"] == beak.WORKSPACE_PEEK_MAX_BYTES + 5000
    assert big["content"] == "B" * beak.WORKSPACE_PEEK_MAX_BYTES

    # binary file: base64, decodes back to the capped prefix
    blob = beak.peek_file(root, "blob.bin")
    assert blob["encoding"] == "base64"
    assert base64.b64decode(blob["content"]) == bytes(range(256))

    # refusals: traversal, directory, missing, private
    assert "unsafe workspace path" in beak.peek_file(root, "../outside.txt")["error"]
    assert beak.peek_file(root, "data")["refused"] == "directory"
    assert beak.peek_file(root, "nope.txt")["refused"] == "missing"
    (root / ".vibe").mkdir(exist_ok=True)
    (root / ".vibe" / "config.toml").write_text(f'secret = "{SECRET}"')
    priv = beak.peek_file(root, ".vibe/config.toml")
    assert priv["refused"] == "private" and SECRET not in json.dumps(priv)


# --- socket op: unknown / queued / terminal --------------------------------------


def test_workspace_peek_unknown_run_and_missing_run_id(coop_daemon) -> None:
    sock = coop_daemon.settings.socket_path
    resp = request(sock, "workspace_peek", run_id="run-nope", timeout=2.0)
    assert resp["ok"] is False and "unknown chicken" in resp["error"]
    resp = request(sock, "workspace_peek", timeout=2.0)
    assert resp["ok"] is False and "requires run_id" in resp["error"]


def test_workspace_peek_queued_egg_has_no_workspace_yet(budget1_daemon, beak_vibe, tmp_path) -> None:
    sock = budget1_daemon.settings.socket_path
    slow = write_job(tmp_path, name="slow-1", prompt="SLEEP 20 one", timeout=120)
    slow2 = write_job(tmp_path, name="slow-2", prompt="SLEEP 20 two", timeout=120)
    first = enqueue(sock, slow)
    second = enqueue(sock, slow2)
    wait_status(sock, first, ("running",))  # first fills the budget of 1
    queued = wait_status(sock, second, ("queued",))
    assert queued["status"] == "queued"
    resp = request(sock, "workspace_peek", run_id=second, timeout=2.0)
    assert resp["ok"] is False and resp["workspace_gone"] is True
    assert resp["status"] == "queued" and "no workspace yet" in resp["error"]
    request(sock, "run_stop", run_id=first, timeout=30.0)  # clean up
    wait_status(sock, second, TERMINAL_STATES, timeout=30.0)


def test_workspace_peek_terminal_run_is_a_clear_closed_beak(coop_daemon, beak_vibe, tmp_path) -> None:
    sock = coop_daemon.settings.socket_path
    spec = write_job(tmp_path, name="quick-job", prompt="Say hello")
    run_id = enqueue(sock, spec)
    run = wait_status(sock, run_id, ("succeeded", "failed"))
    assert run["status"] == "succeeded"
    resp = request(sock, "workspace_peek", run_id=run_id, timeout=2.0)
    assert resp["ok"] is False and resp["workspace_gone"] is True
    assert resp["status"] == "succeeded" and "terminal" in resp["error"] and "abattoir" in resp["error"]
    # the workspace really is gone (attested teardown)
    assert not (coop_daemon.settings.data_dir / "workspaces" / run_id).exists()
    # a stopped chicken is terminal too
    slow = write_job(tmp_path, name="stoppable", prompt="SLEEP 30 stop me", timeout=120)
    rid = enqueue(sock, slow)
    wait_status(sock, rid, ("running",))
    stopped = request(sock, "run_stop", run_id=rid, timeout=30.0)
    assert stopped["ok"] and stopped["status"] == "stopped"
    resp = request(sock, "workspace_peek", run_id=rid, timeout=2.0)
    assert resp["ok"] is False and resp["workspace_gone"] is True and resp["status"] == "stopped"
    assert "terminal (stopped)" in resp["error"]


# --- socket op: live tree + file peek on a running chicken ------------------------


def test_workspace_peek_running_chicken_tree_file_cap_and_lock(coop_daemon, beak_vibe, tmp_path) -> None:
    sock = coop_daemon.settings.socket_path
    spec = write_job(tmp_path, name="live-job", prompt="SLEEP 15 live peek", timeout=120)
    run_id = enqueue(sock, spec)
    wait_status(sock, run_id, ("running",))
    tree_resp = wait_tree(sock, run_id, "notes.txt")  # the harness has written its files

    # tree
    assert tree_resp["ok"] is True and tree_resp["mode"] == "tree" and tree_resp["run_id"] == run_id
    entries = tree_resp["tree"]["entries"]
    paths = {e["path"]: e for e in entries}
    for expected in ("prompt.txt", "notes.txt", "data", "data/big.txt", "blob.bin", ".vibe", ".vibe/config.toml", ".tc-farmhand-settings.toml"):
        assert expected in paths, expected
    assert paths["data"]["type"] == "dir" and paths["notes.txt"]["type"] == "file"
    # no host path ever leaves the daemon in the payload — workspace-relative only
    assert str(tmp_path) not in json.dumps(tree_resp)
    assert str(coop_daemon.settings.data_dir) not in json.dumps(tree_resp)
    assert SECRET not in json.dumps(tree_resp)

    # single-file peek: text
    got = request(sock, "workspace_peek", run_id=run_id, path="notes.txt", timeout=2.0)
    assert got["ok"] is True and got["mode"] == "file" and got["path"] == "notes.txt"
    assert got["encoding"] == "utf-8" and got["content"] == "pecked notes\nline two\n"
    assert got["truncated"] is False and got["size_bytes"] == 22

    # binary file → base64
    blob = request(sock, "workspace_peek", run_id=run_id, path="blob.bin", timeout=2.0)
    assert blob["ok"] and blob["encoding"] == "base64"
    assert base64.b64decode(blob["content"]) == bytes(range(256))

    # size cap: 200 KB file is truncated at the cap
    big = request(sock, "workspace_peek", run_id=run_id, path="data/big.txt", timeout=2.0)
    assert big["ok"] and big["truncated"] is True
    assert big["served_bytes"] == beak.WORKSPACE_PEEK_MAX_BYTES and big["size_bytes"] == 200_000
    assert big["content"] == "A" * beak.WORKSPACE_PEEK_MAX_BYTES

    # path lock: traversal + absolute refused, never served
    for bad in ("../turbochickens.db", "/etc/passwd", "data/../../x", ".vibe/../notes.txt"):
        resp = request(sock, "workspace_peek", run_id=run_id, path=bad, timeout=2.0)
        assert resp["ok"] is False, bad
        assert "unsafe workspace path" in resp["error"], bad

    # private harness config refused by name — the secret never leaves
    for priv in (".vibe/config.toml", ".vibe", ".tc-farmhand-settings.toml"):
        resp = request(sock, "workspace_peek", run_id=run_id, path=priv, timeout=2.0)
        assert resp["ok"] is False and resp["refused"] == "private", priv
        assert SECRET not in json.dumps(resp)
        assert "/daemon/private" not in json.dumps(resp)

    # directory + missing file
    assert request(sock, "workspace_peek", run_id=run_id, path="data", timeout=2.0)["refused"] == "directory"
    assert request(sock, "workspace_peek", run_id=run_id, path="ghost.txt", timeout=2.0)["refused"] == "missing"

    request(sock, "run_stop", run_id=run_id, timeout=30.0)  # let the daemon stop cleanly
    wait_status(sock, run_id, TERMINAL_STATES, timeout=30.0)


# --- HTTP parity + status codes ----------------------------------------------------


def test_http_workspace_mirrors_socket_exactly(coop_daemon, beak_vibe, tmp_path) -> None:
    c = http_client(coop_daemon.settings)
    sock = coop_daemon.settings.socket_path
    spec = write_job(tmp_path, name="parity-job", prompt="SLEEP 15 parity", timeout=120)
    run_id = enqueue(sock, spec)
    wait_status(sock, run_id, ("running",))
    wait_tree(sock, run_id, "notes.txt")

    r = c.get(f"/api/runs/{run_id}/workspace")
    assert r.status_code == 200
    assert r.json() == request(sock, "workspace_peek", run_id=run_id, timeout=2.0)
    r = c.get(f"/api/runs/{run_id}/workspace", params={"path": "notes.txt"})
    assert r.status_code == 200
    assert r.json() == request(sock, "workspace_peek", run_id=run_id, path="notes.txt", timeout=2.0)
    assert r.json()["content"] == "pecked notes\nline two\n"
    # refusals mirror too (200 with the ok:false payload, like quarantined/expired)
    r = c.get(f"/api/runs/{run_id}/workspace", params={"path": "../x"})
    assert r.status_code == 200 and r.json()["ok"] is False and "unsafe workspace path" in r.json()["error"]
    # a percent-encoded traversal in the query string decodes to ../.. → refused as unsafe
    r = c.get(f"/api/runs/{run_id}/workspace?path=..%2F..%2Fturbochickens.db")
    assert r.status_code == 200 and r.json()["ok"] is False and "unsafe workspace path" in r.json()["error"]
    r = c.get(f"/api/runs/{run_id}/workspace", params={"path": ".vibe/config.toml"})
    assert r.status_code == 200 and r.json()["refused"] == "private" and SECRET not in r.text

    # unknown run → 404 (not a broken page)
    assert c.get("/api/runs/run-nope/workspace").status_code == 404
    assert c.get("/api/runs/run-nope/workspace", params={"path": "x"}).status_code == 404

    request(sock, "run_stop", run_id=run_id, timeout=30.0)
    wait_status(sock, run_id, TERMINAL_STATES, timeout=30.0)
    # terminal run → 200 with the structured closed-beak payload (SPA empty state)
    r = c.get(f"/api/runs/{run_id}/workspace")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False and body["workspace_gone"] is True and body["status"] == "stopped"
    assert body == request(sock, "workspace_peek", run_id=run_id, timeout=2.0)


def test_workspace_endpoint_bearer_401_when_auth_required(auth_daemon, beak_vibe, tmp_path) -> None:
    c = http_client(auth_daemon.settings)
    headers = {"Authorization": "Bearer spec29-coop-token"}
    assert c.get("/api/runs/run-x/workspace").status_code == 401
    assert c.get("/api/runs/run-x/workspace", params={"path": "x"}, headers=headers).status_code == 404
    assert c.get("/api/ping").status_code == 200  # healthcheck stays open

    # a live chicken peeked with the bearer: the coop token never appears in the payload
    sock = auth_daemon.settings.socket_path
    spec = write_job(tmp_path, name="secret-job", prompt="SLEEP 12 secret run", timeout=120)
    run_id = enqueue(sock, spec)
    wait_status(sock, run_id, ("running",))
    wait_tree(sock, run_id, "notes.txt")
    r = c.get(f"/api/runs/{run_id}/workspace", headers=headers)
    assert r.status_code == 200 and r.json()["ok"] is True
    assert "spec29-coop-token" not in r.text and SECRET not in r.text
    r = c.get(f"/api/runs/{run_id}/workspace", params={"path": "notes.txt"}, headers=headers)
    assert "spec29-coop-token" not in r.text and SECRET not in r.text
    # and the audit log never saw either
    audit_text = AuditLog(auth_daemon.settings.audit_log_path).read_all()
    assert all("spec29-coop-token" not in json.dumps(e) and SECRET not in json.dumps(e) for e in audit_text)
    request(sock, "run_stop", run_id=run_id, timeout=30.0)
    wait_status(sock, run_id, TERMINAL_STATES, timeout=30.0)


def test_workspace_endpoint_daemon_down_payload_not_500(tmp_path, monkeypatch) -> None:
    f = write_settings(tmp_path, coop_enabled=True, coop_port=free_port(), coop_loopback_open=True)
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(f))
    settings = load_settings(f)
    port = settings.coop_port
    server = CoopServer(settings, transport=SocketTransport(tmp_path / "dead.sock"))

    async def go() -> dict:
        await server.start()
        try:
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=5.0) as c:
                out = {}
                r = await c.get("/api/runs/run-x/workspace")
                out["tree"] = (r.status_code, r.json())
                r = await c.get("/api/runs/run-x/workspace", params={"path": "notes.txt"})
                out["file"] = (r.status_code, r.json())
                return out
        finally:
            await server.stop()

    out = asyncio.run(go())
    for key in ("tree", "file"):
        code, body = out[key]
        assert code == 200, key
        assert body["ok"] is False and body["daemon"]["up"] is False and body["error"], key


# --- served SPA markup for the Beak tab ---------------------------------------------


def test_spa_serves_beak_tab_and_no_host_paths(coop_daemon, tmp_path) -> None:
    c = http_client(coop_daemon.settings)
    html = c.get("/").text
    for marker in (
        'id="tab-beak"', ">Beak</button>",
        'id="pane-beak"', 'id="beak-label"', 'id="beak-state"', 'id="beak-grid"',
        'id="beak-tree"', 'id="beak-pre"', 'id="beak-log"', 'data-copy-target="beak-pre"',
        "Workspace files", "Live log tail",
    ):
        assert marker in html, marker
    js = c.get("/app.js").text
    assert '"/workspace"' in js and '"/workspace?path="' in js and "renderBeakPane" in js and "beakSelectedPath" in js
    css = c.get("/styles.css").text
    for cls in (".beak-grid", ".beak-tree", ".beak-file", ".beak-private", ".beak-pre", ".beak-log", ".beak-subhead"):
        assert cls in css, cls
    # no host paths ever enter the served assets
    for asset in (html, js, css):
        assert str(tmp_path) not in asset


# --- subprocess e2e: peek a running chicken over HTTP, then the beak closes ---------


def test_e2e_peek_running_chicken_then_beak_closes_on_stop(tmp_path, monkeypatch) -> None:
    port = free_port()
    f = write_settings(tmp_path, coop_port=port, coop_loopback_open=True)  # `tc coop start` forces coop on; opt-in keeps the loopback bind open
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(f))
    bin_path = tmp_path / "fake-vibe-beak"
    bin_path.write_text(BEAK_VIBE)
    bin_path.chmod(0o755)
    monkeypatch.setenv("TURBOCHICKENS_VIBE_BIN", str(bin_path))
    settings = load_settings(f)
    env = dict(os.environ)
    proc = subprocess.Popen(
        [sys.executable, "-m", "turbochickens", "coop", "start"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        _wait_socket(settings, proc)
        c = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5.0)
        assert c.get("/api/ping").status_code == 200

        spec_path = write_job(tmp_path, name="e2e-beak-job", prompt="SLEEP 25 e2e beak", timeout=120)
        r = c.post("/api/runs", json={"spec_path": str(spec_path.resolve())})
        assert r.status_code == 200, r.text
        run_id = r.json()["run_id"]

        # wait until the chicken is running, then peek over HTTP
        deadline = time.time() + 30
        while time.time() < deadline:
            show = c.get(f"/api/runs/{run_id}").json()
            if show.get("ok") and show["run"]["status"] == "running":
                break
            time.sleep(0.1)
        else:
            raise AssertionError("run did not reach running")

        # the harness writes its workspace files just after spawn — poll the beak until they appear
        deadline = time.time() + 15
        paths = set()
        while time.time() < deadline:
            tree = c.get(f"/api/runs/{run_id}/workspace")
            assert tree.status_code == 200 and tree.json()["ok"] is True
            paths = {e["path"] for e in tree.json()["tree"]["entries"]}
            if {"prompt.txt", "notes.txt", "data/big.txt", "blob.bin"} <= paths:
                break
            time.sleep(0.1)
        assert {"prompt.txt", "notes.txt", "data/big.txt", "blob.bin"} <= paths
        assert SECRET not in tree.text and str(tmp_path) not in tree.text

        got = c.get(f"/api/runs/{run_id}/workspace", params={"path": "notes.txt"})
        assert got.status_code == 200 and got.json()["content"] == "pecked notes\nline two\n"

        # CLI parity: stop the chicken, then the beak is closed over HTTP
        cli = runner.invoke(app, ["runs", "stop", run_id])
        assert cli.exit_code == 0, combined_output(cli)
        assert "stopped" in combined_output(cli)
        closed = c.get(f"/api/runs/{run_id}/workspace")
        assert closed.status_code == 200
        body = closed.json()
        assert body["ok"] is False and body["workspace_gone"] is True and body["status"] == "stopped"
        assert "beak is closed" in body["error"]

        # the SPA serves the Beak tab
        page = c.get("/")
        assert page.status_code == 200 and 'id="tab-beak"' in page.text and 'id="pane-beak"' in page.text

        result = runner.invoke(app, ["daemon", "stop"])
        assert result.exit_code == 0, result.output
        proc.wait(timeout=10)
        assert proc.returncode == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
