"""Tests for spec 15: upload + validate job specs from the browser.

Seams (per spec 11/15 testing decisions — no browser automation):
- the `jobs_upload` socket op standalone (valid bundle stored under
  uploads/<job>-<spec_hash>/ and enqueueable by that path; invalid bundles
  field-named errors, nothing stored, nothing registered; cap, path safety,
  base64 and shape errors);
- the HTTP layer against a real coop-enabled daemon booted in a thread on a
  temp port: POST /api/jobs/validate (200 valid / 422 field-named errors /
  413 cap / 400 malformed), POST /api/runs with a bundle, HTTP == socket
  parity, auth-required 401s, dead-socket daemon-down payloads;
- a subprocess e2e (`tc coop start` + hermetic fake vibe binary): a brand-new
  job created, validated, run and its result fetched entirely over HTTP.

All assertions are external, observable behavior: HTTP status codes and JSON
bodies, socket payloads, CLI output, on-disk upload layout, served SPA markup,
audit contents. No host path ever enters the UI or the error payloads.
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
from test_coop_runs import auth_daemon, coop_daemon, fake_vibe, wait_terminal_http
from test_daemon import DaemonThread
from test_run_e2e import FAKE_VIBE, combined_output, wait_terminal, write_job
from turbochickens.audit import AuditLog
from turbochickens.cli.main import app
from turbochickens.client import request
from turbochickens.coop import CoopServer, SocketTransport
from turbochickens.settings import UPLOAD_MAX_BYTES, load_settings
from turbochickens.spec.loader import load_spec, spec_hash

runner = CliRunner()


def b64_bytes(data: bytes) -> str:
    return base64.b64encode(data).decode()


def b64_text(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode()


def bundle_of(job_path: Path) -> dict[str, str]:
    """The upload bundle for a job directory: every file under the spec's
    dir, keyed by bundle-relative path (job.toml at the root)."""
    root = job_path.parent
    return {
        str(p.relative_to(root)): b64_bytes(p.read_bytes())
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def bad_cron_bundle(job_path: Path) -> dict[str, str]:
    """A bundle whose job.toml is invalid: cron trigger without a schedule."""
    files = bundle_of(job_path)
    toml = base64.b64decode(files["job.toml"]).decode("utf-8")
    files["job.toml"] = b64_text(toml.replace('type = "manual"', 'type = "cron"'))
    return files


def missing_schema_bundle(job_path: Path) -> dict[str, str]:
    """A valid job.toml but the referenced schema file is not in the bundle."""
    files = bundle_of(job_path)
    for key in [k for k in files if k.startswith("schemas/")]:
        del files[key]
    return files


# --- socket op: valid bundle stored under uploads/<job>-<spec_hash>/ ---------


def test_jobs_upload_socket_valid_bundle_stored_and_enqueueable(coop_daemon, fake_vibe, tmp_path) -> None:
    sock = coop_daemon.settings.socket_path
    spec_path = write_job(tmp_path, name="upload-job")
    files = bundle_of(spec_path)

    resp = request(sock, "jobs_upload", files=files, timeout=10.0)
    assert resp["ok"] is True and resp["valid"] is True
    assert resp["job_id"] == "upload-job"
    assert resp["spec_hash"] == spec_hash(load_spec(spec_path))
    assert sorted(resp["files"]) == sorted(files)

    # landed under <data_dir>/uploads/<job>-<spec_hash>/ with exact contents
    upload_dir = coop_daemon.settings.uploads_dir / f"upload-job-{resp['spec_hash']}"
    assert resp["spec_path"] == str(upload_dir / "job.toml")
    assert upload_dir.is_dir()
    for rel, b64 in files.items():
        assert (upload_dir / rel).read_bytes() == base64.b64decode(b64)
    assert spec_hash(load_spec(upload_dir / "job.toml")) == resp["spec_hash"]

    # validate alone does not register the job
    assert request(sock, "jobs_list", timeout=2.0)["jobs"] == []

    # enqueueable via POST-socket enqueue by that stored path; full pipeline
    enq = request(sock, "enqueue", spec_path=resp["spec_path"], spec_hash=resp["spec_hash"], trigger="manual", timeout=5.0)
    assert enq["ok"] is True
    run = wait_terminal(sock, enq["run_id"])
    assert run["status"] == "succeeded"
    assert run["manifest"]["spec_hash"] == f"sha256:{resp['spec_hash']}"
    jobs = request(sock, "jobs_list", timeout=2.0)["jobs"]
    assert len(jobs) == 1 and jobs[0]["job_id"] == "upload-job" and jobs[0]["spec_hash"] == resp["spec_hash"]
    # the upload was audited (no file contents)
    events = AuditLog(coop_daemon.settings.audit_log_path).read_all()
    uploaded = [e for e in events if e["event"] == "egg.candled"]
    assert len(uploaded) == 1 and uploaded[0]["job_id"] == "upload-job"
    assert uploaded[0]["spec_path"] == resp["spec_path"] and uploaded[0]["size_bytes"] > 0


# --- socket op: invalid bundle → field-named errors, nothing stored -----------


def test_jobs_upload_socket_invalid_bundle_errors_named_nothing_stored(coop_daemon, tmp_path) -> None:
    sock = coop_daemon.settings.socket_path
    spec_path = write_job(tmp_path, name="bad-job")
    files = bad_cron_bundle(spec_path)

    resp = request(sock, "jobs_upload", files=files, timeout=10.0)
    assert resp["ok"] is False and resp["valid"] is False
    fields = [e["field"] for e in resp["errors"]]
    assert "trigger.schedule" in fields  # exactly like `tc jobs validate`
    for e in resp["errors"]:
        assert set(e) == {"field", "message"}
        assert e["field"] in e["message"]  # rendered as "trigger.schedule: …"
        # no host/staging path leaks into the error payloads
        assert str(tmp_path) not in e["message"] and ".staging-" not in e["message"]

    # a valid TOML with the schema missing from the bundle → output.schema_file
    resp = request(sock, "jobs_upload", files=missing_schema_bundle(spec_path), timeout=10.0)
    assert resp["valid"] is False
    assert any(e["field"] == "output.schema_file" for e in resp["errors"])

    # nothing stored, nothing registered
    assert not coop_daemon.settings.uploads_dir.exists() or list(coop_daemon.settings.uploads_dir.iterdir()) == []
    assert request(sock, "jobs_list", timeout=2.0)["jobs"] == []
    events = [e["event"] for e in AuditLog(coop_daemon.settings.audit_log_path).read_all()]
    assert "egg.rejected" in events and "egg.candled" not in events


def test_jobs_upload_socket_missing_prompt_and_attachments_named(coop_daemon, tmp_path) -> None:
    sock = coop_daemon.settings.socket_path
    spec_path = write_job(tmp_path, name="no-prompt-job")
    files = bundle_of(spec_path)
    for key in [k for k in files if k.startswith("prompts/")]:
        del files[key]
    resp = request(sock, "jobs_upload", files=files, timeout=5.0)
    assert resp["valid"] is False
    assert any(e["field"] == "input.prompt_file" for e in resp["errors"])


# --- socket op: cap, path safety, base64, shape --------------------------------


def test_jobs_upload_socket_cap_enforced_with_clear_error(coop_daemon, tmp_path) -> None:
    sock = coop_daemon.settings.socket_path
    files = bundle_of(write_job(tmp_path, name="big-job"))
    files["schemas/out.schema.json"] = b64_text('{"type": "object"} // ' + "x" * (UPLOAD_MAX_BYTES + 1))
    resp = request(sock, "jobs_upload", files=files, timeout=30.0)
    assert resp["ok"] is False and resp["too_large"] is True
    assert "bundle too large" in resp["error"] and str(UPLOAD_MAX_BYTES) in resp["error"]
    assert resp["size_bytes"] > UPLOAD_MAX_BYTES and resp["cap_bytes"] == UPLOAD_MAX_BYTES
    assert list(coop_daemon.settings.uploads_dir.iterdir()) == [] if coop_daemon.settings.uploads_dir.exists() else True


def test_jobs_upload_socket_rejects_unsafe_paths_bad_base64_and_shape(coop_daemon, tmp_path) -> None:
    sock = coop_daemon.settings.socket_path
    files = bundle_of(write_job(tmp_path, name="shape-job"))

    for key in ("../evil.txt", "/abs/evil.txt", "a/../b.txt", "a//b.txt", ".."):
        trav = dict(files)
        trav[key] = b64_text("x")
        resp = request(sock, "jobs_upload", files=trav, timeout=5.0)
        assert resp["ok"] is False and resp["valid"] is False, key
        assert "unsafe bundle path" in resp["errors"][0]["message"], key

    bad = dict(files)
    bad["job.toml"] = "!!!not-base64!!!"
    resp = request(sock, "jobs_upload", files=bad, timeout=5.0)
    assert resp["valid"] is False and "not valid base64" in resp["errors"][0]["message"]

    resp = request(sock, "jobs_upload", timeout=5.0)
    assert resp["ok"] is False and "files" in resp["error"]
    resp = request(sock, "jobs_upload", files={}, timeout=5.0)
    assert resp["ok"] is False
    resp = request(sock, "jobs_upload", files={"prompts/p.txt": b64_text("x")}, timeout=5.0)
    assert resp["valid"] is False and "job.toml" in resp["errors"][0]["message"]
    # nothing was written anywhere
    assert not coop_daemon.settings.uploads_dir.exists() or list(coop_daemon.settings.uploads_dir.iterdir()) == []


# --- HTTP: POST /api/jobs/validate + POST /api/runs bundle ---------------------


def test_validate_over_http_and_enqueue_by_stored_path(coop_daemon, fake_vibe, tmp_path) -> None:
    c = http_client(coop_daemon.settings)
    sock = coop_daemon.settings.socket_path
    spec_path = write_job(tmp_path, name="http-upload-job")
    files = bundle_of(spec_path)

    r = c.post("/api/jobs/validate", json={"files": files})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["valid"] is True
    assert body["job_id"] == "http-upload-job"
    assert body["spec_hash"] == spec_hash(load_spec(spec_path))
    # HTTP payload == socket payload (modulo op/protocol keys)
    sock_resp = request(sock, "jobs_upload", files=files, timeout=10.0)
    for key in ("ok", "valid", "job_id", "spec_hash", "spec_path", "size_bytes", "files", "warnings"):
        assert body[key] == sock_resp[key], key

    # enqueueable via POST /api/runs by that stored path; result over HTTP
    r = c.post("/api/runs", json={"spec_path": body["spec_path"], "spec_hash": body["spec_hash"]})
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]
    run = wait_terminal_http(c, run_id)
    assert run["status"] == "succeeded" and run["trigger"] == "manual"
    res = c.get(f"/api/results/{run_id}")
    assert res.status_code == 200 and res.json()["ok"] is True
    assert res.json()["output"]["message"] == "hello from fake vibe"
    assert res.json() == request(sock, "results_get", run_id=run_id, timeout=2.0)
    cli = runner.invoke(app, ["results", "get", run_id])
    assert cli.exit_code == 0 and json.loads(cli.output) == res.json()["output"]
    # the job shows up in the Jobs view data
    jobs = c.get("/api/jobs").json()["jobs"]
    assert len(jobs) == 1 and jobs[0]["job_id"] == "http-upload-job"


def test_post_api_runs_accepts_bundle_directly(coop_daemon, fake_vibe, tmp_path) -> None:
    c = http_client(coop_daemon.settings)
    files = bundle_of(write_job(tmp_path, name="bundle-run-job"))
    r = c.post("/api/runs", json={"files": files})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["job_id"] == "bundle-run-job" and body["status"] == "queued"
    run = wait_terminal_http(c, body["run_id"])
    assert run["status"] == "succeeded"
    res = c.get(f"/api/results/{body['run_id']}")
    assert res.status_code == 200 and res.json()["output"]["message"] == "hello from fake vibe"


def test_validate_over_http_invalid_bundle_422_field_errors_nothing_registered(coop_daemon, tmp_path) -> None:
    c = http_client(coop_daemon.settings)
    sock = coop_daemon.settings.socket_path
    files = bad_cron_bundle(write_job(tmp_path, name="bad-http-job"))

    r = c.post("/api/jobs/validate", json={"files": files})
    assert r.status_code == 422
    body = r.json()
    assert body["ok"] is False and body["valid"] is False
    fields = [e["field"] for e in body["errors"]]
    assert "trigger.schedule" in fields
    assert all(e["field"] in e["message"] for e in body["errors"])
    assert str(tmp_path) not in json.dumps(body["errors"])

    # schema referenced by a valid spec but missing from the bundle → named error
    spec_path = write_job(tmp_path, name="bad-http-job")
    r_ms = c.post("/api/jobs/validate", json={"files": missing_schema_bundle(spec_path)})
    assert r_ms.status_code == 422
    assert "output.schema_file" in [e["field"] for e in r_ms.json()["errors"]]

    # the upload path of POST /api/runs refuses the same bundle with 422
    r2 = c.post("/api/runs", json={"files": files})
    assert r2.status_code == 422 and r2.json()["valid"] is False
    assert "trigger.schedule" in [e["field"] for e in r2.json()["errors"]]

    # nothing registered as a job, no run queued, nothing stored
    assert c.get("/api/jobs").json()["jobs"] == []
    assert c.get("/api/runs").json()["runs"] == []
    assert request(sock, "jobs_list", timeout=2.0)["jobs"] == []
    assert not coop_daemon.settings.uploads_dir.exists() or list(coop_daemon.settings.uploads_dir.iterdir()) == []


def test_validate_over_http_cap_is_413_with_clear_error(coop_daemon, tmp_path) -> None:
    c = http_client(coop_daemon.settings)
    files = bundle_of(write_job(tmp_path, name="cap-job"))
    files["schemas/out.schema.json"] = b64_text("x" * (UPLOAD_MAX_BYTES + 512 * 1024))
    r = c.post("/api/jobs/validate", json={"files": files})
    assert r.status_code == 413
    body = r.json()
    assert body["ok"] is False and body["too_large"] is True
    assert "bundle too large" in body["error"] and "10 MiB" in body["error"]
    # same through the runs upload path
    r2 = c.post("/api/runs", json={"files": files})
    assert r2.status_code == 413 and r2.json()["too_large"] is True


def test_validate_over_http_malformed_bodies_400(coop_daemon) -> None:
    c = http_client(coop_daemon.settings)
    r = c.post("/api/jobs/validate", content=b"not json", headers={"Content-Type": "application/json"})
    assert r.status_code == 400
    assert c.post("/api/jobs/validate", json={}).status_code == 400
    assert c.post("/api/jobs/validate", json={"files": []}).status_code == 400
    assert c.post("/api/jobs/validate", json={"files": {}}).status_code == 400
    assert c.post("/api/runs", json={"files": "nope"}).status_code == 400


# --- auth required + daemon-down -------------------------------------------------


def test_upload_endpoints_require_bearer_when_auth_required(auth_daemon, tmp_path) -> None:
    c = http_client(auth_daemon.settings)
    headers = {"Authorization": "Bearer spec14-secret-token"}
    files = bundle_of(write_job(tmp_path, name="auth-upload-job"))
    assert c.post("/api/jobs/validate", json={"files": files}).status_code == 401
    assert c.post("/api/runs", json={"files": files}).status_code == 401
    r = c.post("/api/jobs/validate", json={"files": files}, headers=headers)
    assert r.status_code == 200 and r.json()["valid"] is True
    r2 = c.post("/api/runs", json={"files": files}, headers=headers)
    assert r2.status_code == 200 and r2.json()["ok"] is True
    assert c.get("/api/ping").status_code == 200  # healthcheck stays open


def test_upload_endpoints_daemon_down_payload_not_500(tmp_path, monkeypatch) -> None:
    f = write_settings(tmp_path, coop_enabled=True, coop_port=free_port())
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(f))
    settings = load_settings(f)
    port = settings.coop_port
    server = CoopServer(settings, transport=SocketTransport(tmp_path / "dead.sock"))

    async def go() -> dict:
        await server.start()
        try:
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=5.0) as c:
                files = {"job.toml": b64_text("[job]\nname = \"x\"\n")}
                out = {}
                r = await c.post("/api/jobs/validate", json={"files": files})
                out["validate"] = (r.status_code, r.json())
                r = await c.post("/api/runs", json={"files": files})
                out["runs-files"] = (r.status_code, r.json())
                return out
        finally:
            await server.stop()

    out = asyncio.run(go())
    for key in ("validate", "runs-files"):
        code, body = out[key]
        assert code == 200, key  # structured payload, not a broken 500
        assert body["ok"] is False and body["daemon"]["up"] is False and body["error"], key


# --- SPA serves the Jobs upload form ----------------------------------------------


def test_spa_serves_jobs_upload_form_and_no_host_paths(coop_daemon, tmp_path) -> None:
    c = http_client(coop_daemon.settings)
    html = c.get("/").text
    for marker in (
        'id="view-jobs"', 'id="upload-card"', 'id="upload-form"',
        'id="up-job"', 'id="up-prompt"', 'id="up-prompt-path"', 'id="up-schema"', 'id="up-schema-path"',
        'id="up-attachments"', 'id="up-job-error"', 'id="up-prompt-error"', 'id="up-schema-error"',
        'id="up-attachments-error"', 'id="upload-error"', 'id="upload-submit"', 'id="upload-result"',
        ">Validate</button>", "Egg (job.toml)", "Prompt file", "Schema file", "Attachments (optional)",
    ):
        assert marker in html, marker
    js = c.get("/app.js").text
    assert '"/api/jobs/validate"' in js and '"/api/runs"' in js and "apiPost" in js
    assert "readFileB64" in js and "showUploadErrors" in js and "Queue egg" in js
    css = c.get("/styles.css").text
    for cls in (".upload-card", ".upload-form", ".upload-grid", ".upload-field", ".upload-actions", ".upload-error"):
        assert cls in css, cls
    # no host paths ever enter the UI (served assets are static)
    for asset in (html, js, css):
        assert str(tmp_path) not in asset


# --- subprocess e2e: a brand-new job created, run and read entirely over HTTP -----


def test_e2e_create_run_and_read_job_entirely_over_http(tmp_path, monkeypatch) -> None:
    port = free_port()
    f = write_settings(tmp_path, coop_port=port)  # `tc coop start` forces coop on
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(f))
    bin_path = tmp_path / "fake-vibe"
    bin_path.write_text(FAKE_VIBE)
    bin_path.chmod(0o755)
    monkeypatch.setenv("TURBOCHICKENS_VIBE_BIN", str(bin_path))
    settings = load_settings(f)
    proc = subprocess.Popen(
        [sys.executable, "-m", "turbochickens", "coop", "start"],
        env=dict(os.environ),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        _wait_socket(settings, proc)
        c = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10.0)
        assert c.get("/api/ping").status_code == 200

        # 1. upload + validate a brand-new bundle over HTTP
        files = bundle_of(write_job(tmp_path, name="e2e-upload-job"))
        r = c.post("/api/jobs/validate", json={"files": files})
        assert r.status_code == 200, r.text
        v = r.json()
        assert v["valid"] is True and v["job_id"] == "e2e-upload-job"
        assert v["spec_hash"] == spec_hash(load_spec(write_job(tmp_path, name="e2e-upload-job")))
        assert Path(v["spec_path"]).is_file()  # stored under uploads/<job>-<hash>/
        assert f"e2e-upload-job-{v['spec_hash']}" in v["spec_path"]

        # 2. an invalid variant is refused with field-named errors, stores nothing
        bad = bad_cron_bundle(write_job(tmp_path, name="e2e-upload-job"))
        rb = c.post("/api/jobs/validate", json={"files": bad})
        assert rb.status_code == 422 and "trigger.schedule" in [e["field"] for e in rb.json()["errors"]]
        assert str(tmp_path) not in json.dumps(rb.json()["errors"])

        # 3. run the validated bundle entirely over HTTP (no path in the UI flow)
        r = c.post("/api/runs", json={"files": files})
        assert r.status_code == 200, r.text
        run_id = r.json()["run_id"]
        run = wait_terminal_http(c, run_id, timeout=30.0)
        assert run["status"] == "succeeded" and run["trigger"] == "manual"

        # 4. the result over HTTP, with CLI parity
        res = c.get(f"/api/results/{run_id}")
        assert res.status_code == 200 and res.json()["ok"] is True
        assert res.json()["output"]["message"] == "hello from fake vibe"
        cli = runner.invoke(app, ["results", "get", run_id])
        assert cli.exit_code == 0 and json.loads(cli.output) == res.json()["output"]
        cli_list = runner.invoke(app, ["runs", "list"])
        assert cli_list.exit_code == 0 and run_id in cli_list.output

        # 5. the job is registered and visible in the Jobs view data; audited
        jobs = c.get("/api/jobs").json()["jobs"]
        assert [j["job_id"] for j in jobs] == ["e2e-upload-job"]
        events = [e["event"] for e in AuditLog(settings.audit_log_path).read_all()]
        assert "egg.candled" in events and "egg.queued" in events and "clutch.stored" in events

        # 6. the SPA serves the upload form, with no host paths in the assets
        page = c.get("/")
        assert page.status_code == 200 and 'id="upload-form"' in page.text and 'id="up-job"' in page.text
        for asset in (page.text, c.get("/app.js").text, c.get("/styles.css").text):
            assert str(tmp_path) not in asset

        result = runner.invoke(app, ["daemon", "stop"])
        assert result.exit_code == 0, result.output
        proc.wait(timeout=10)
        assert proc.returncode == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
