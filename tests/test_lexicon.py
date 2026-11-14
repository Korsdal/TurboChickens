"""Tests for spec 20: lexicon adoption — every surface speaks farm.

Covers the spec-20 acceptance additions: the legacy→lexicon alias map applied
on read/display only (raw Headcount rows on disk never rewritten), the grep
guard that no legacy event string appears anywhere in src/ outside the alias
map, the SPA serving lexicon labels (ids, data-* hooks, /api/* paths and JSON
field names untouched), socket+HTTP `?event=` accepting old and new names, and
the full lifecycle Headcount order on a real run:
egg.queued → egg.hatched → … → clutch.stored → abattoir.attested →
chicken.finished (and legacy rows written as run.queued / result.stored /
teardown.attested still filter and display correctly via the alias map).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from test_coop import free_port, http_client, write_settings
from test_daemon import DaemonThread
from test_run_e2e import FAKE_VIBE, write_job
from turbochickens.audit import LEGACY_EVENT_ALIASES, AuditLog
from turbochickens.client import request
from turbochickens.settings import load_settings
from turbochickens.spec.loader import load_spec, spec_hash

SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "turbochickens"


@pytest.fixture
def daemon_file(tmp_path, monkeypatch):
    f = write_settings(tmp_path)  # plain daemon, coop disabled
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(f))
    return f


@pytest.fixture
def daemon(daemon_file):
    settings = load_settings(daemon_file)
    dt = DaemonThread(settings)
    dt.start()
    yield dt
    dt.stop()


@pytest.fixture
def fake_vibe(tmp_path, monkeypatch):
    bin_path = tmp_path / "fake-vibe"
    bin_path.write_text(FAKE_VIBE)
    bin_path.chmod(0o755)
    monkeypatch.setenv("TURBOCHICKENS_VIBE_BIN", str(bin_path))
    return bin_path


@pytest.fixture
def coop_daemon(tmp_path, monkeypatch):
    f = write_settings(tmp_path, coop_enabled=True, coop_port=free_port())
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(f))
    settings = load_settings(f)
    dt = DaemonThread(settings)
    dt.start()
    yield dt
    dt.stop()


# --- grep guard: no legacy event string is emitted anywhere in src/ ------------


def test_no_legacy_event_strings_outside_the_alias_map() -> None:
    """Every legacy event name may appear only in audit.py's alias map —
    nowhere else in src/ (no emission site, no docstring, no SPA asset).
    Matches the quoted form (``"run.queued"``) so JSON field accesses like
    ``clutch.quarantined`` in JS (a machine flag, not an event) don't trip it."""
    offenders: list[str] = []
    for path in sorted(SRC_ROOT.rglob("*")):
        if not path.is_file() or path.suffix not in (".py", ".js", ".html", ".css", ".svg"):
            continue
        text = path.read_text(encoding="utf-8")
        if path.name == "audit.py":
            continue
        for legacy in LEGACY_EVENT_ALIASES:
            if f'"{legacy}"' in text or f"'{legacy}'" in text:
                offenders.append(f"{path.relative_to(SRC_ROOT)}: {legacy}")
    assert not offenders, "legacy event strings emitted outside audit.py: " + "; ".join(offenders)


# --- legacy Headcount rows: aliased on read/display, raw bytes untouched -------


def test_legacy_rows_aliased_on_read_only(tmp_path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    log.append({"event": "run.queued", "run_id": "run-legacy1", "n": 1})
    log.append({"event": "result.stored", "run_id": "run-legacy1", "n": 2})
    log.append({"event": "teardown.attested", "run_id": "run-legacy1", "n": 3})
    log.append({"event": "egg.queued", "run_id": "run-new1", "n": 4})  # already lexicon
    raw_bytes = (tmp_path / "audit.jsonl").read_bytes()

    # filtering by the NEW name matches legacy rows; rows display normalized
    by_new = log.tail(10, event="egg.queued")
    assert [r["n"] for r in by_new] == [4, 1]
    assert all(r["event"] == "egg.queued" for r in by_new)
    # filtering by the LEGACY name matches both legacy and new rows
    by_old = log.tail(10, event="run.queued")
    assert [r["n"] for r in by_old] == [4, 1]
    assert all(r["event"] == "egg.queued" for r in by_old)
    # a lexicon filter for another event works the same way
    assert [r["n"] for r in log.tail(10, event="clutch.stored")] == [2]
    assert [r["n"] for r in log.tail(10, event="abattoir.attested")] == [3]
    # read_all is the raw inspection seam: legacy names exactly as written
    assert [r["event"] for r in log.read_all()] == [
        "run.queued",
        "result.stored",
        "teardown.attested",
        "egg.queued",
    ]
    # the raw file on disk is never rewritten by any read
    assert (tmp_path / "audit.jsonl").read_bytes() == raw_bytes
    assert b'"run.queued"' in raw_bytes


def test_audit_tail_socket_and_http_accept_old_and_new_names(coop_daemon) -> None:
    """`audit_tail` / `?event=` match old and new names alike; the Headcount
    chips (which come from an unfiltered tail) show lexicon labels for legacy
    rows; the raw log keeps the legacy bytes."""
    settings = coop_daemon.settings
    sock = settings.socket_path
    # append a legacy-named row straight into the real log (as an older
    # version would have written it)
    AuditLog(settings.audit_log_path).append({"event": "run.queued", "run_id": "run-legacy9"})
    raw = settings.audit_log_path.read_bytes()
    assert b'"run.queued"' in raw

    # socket op: new name and legacy name both match the legacy row
    by_new = request(sock, "audit_tail", limit=1000, event="egg.queued", timeout=2.0)
    assert by_new["ok"] is True
    legacy_rows = [e for e in by_new["events"] if e.get("run_id") == "run-legacy9"]
    assert len(legacy_rows) == 1 and legacy_rows[0]["event"] == "egg.queued"  # normalized for display
    by_old = request(sock, "audit_tail", limit=1000, event="run.queued", timeout=2.0)
    assert by_old["ok"] is True and by_old["events"] == by_new["events"]  # identical match set

    # HTTP mirror: ?event= accepts both spellings, same rows either way
    c = http_client(settings)
    http_new = c.get("/api/audit?limit=1000&event=egg.queued")
    http_old = c.get("/api/audit?limit=1000&event=run.queued")
    assert http_new.status_code == 200 and http_old.status_code == 200
    assert http_new.json()["events"] == by_new["events"]
    assert http_old.json()["events"] == by_new["events"]
    # the unfiltered tail (what the Headcount chips are built from) shows the
    # lexicon label for the legacy row too
    chips = c.get("/api/audit?limit=200").json()["events"]
    assert any(e.get("run_id") == "run-legacy9" and e["event"] == "egg.queued" for e in chips)
    # raw rows on disk untouched
    assert settings.audit_log_path.read_bytes() == raw


# --- SPA serves the lexicon labels, machine hooks untouched ----------------------


def test_spa_serves_lexicon_labels(coop_daemon) -> None:
    c = http_client(coop_daemon.settings)
    html = c.get("/").text
    js = c.get("/app.js").text
    # nav + view copy in the register
    for marker in (
        ">Chickens</a>", ">Eggs</a>", ">Rooster</a>", ">Feed</a>", ">Headcount</a>", ">Perch</a>",
        "Pecking order", "Turn the compost", "Queue egg", "Add a crow", "Egg (job.toml)",
        "Perch seat", "No chickens yet", "The rooster has no crows yet",
        "No feed recorded yet", "The headcount is empty.",
        ">pecking order</text>", ">rooster</text>", ">coopd</text>", ">nest box</text>",
    ):
        assert marker in html, marker
    for marker in ("coopd unreachable", "coopd is not reachable", "Queue egg", "quarantined clutch",
                   "expected feed / chicken", "headcount", "composted"):
        assert marker in js, marker
    # machine hooks untouched: ids, data-*, /api/* paths, data-stat keys
    for marker in (
        'data-view="runs"', 'id="view-runs"', 'id="runs-tbody"', 'data-stat="queue_depth"',
        'id="view-jobs"', 'id="view-schedules"', 'id="view-stats"', 'id="view-audit"', 'id="view-auth"',
        'id="sweep-now"', 'id="retention-card"',
    ):
        assert marker in html, marker
    for api in ('"/api/runs"', '"/api/jobs"', '"/api/schedule"', '"/api/stats"',
                '"/api/audit?limit=100&event="', '"/api/auth/providers"', '"/api/retention/sweep"'):
        assert api in js, api
    # old platform copy is gone from the served assets
    for gone in (">Runs</a>", ">Jobs</a>", ">Schedules</a>", ">Audit</a>", ">Auth</a>", "Run job",
                 "Sweep now", "Add schedule</button>", "Seat session", "Queue depth"):
        assert gone not in html, gone
    assert "Run job" not in js and "seat session stored" not in js


# --- the full lifecycle Headcount order on a real run ----------------------------


def test_run_headcount_order_egg_to_chicken(daemon, fake_vibe, tmp_path) -> None:
    spec_path = write_job(tmp_path, "lexicon-run")
    digest = spec_hash(load_spec(spec_path))
    enq = request(daemon.settings.socket_path, "enqueue", spec_path=str(spec_path.resolve()),
                  spec_hash=digest, trigger="manual", timeout=5.0)
    assert enq["ok"] is True
    run_id = enq["run_id"]
    deadline = time.time() + 30
    while time.time() < deadline:
        show = request(daemon.settings.socket_path, "runs_show", run_id=run_id, timeout=2.0)
        if show["run"]["status"] in ("succeeded", "failed"):
            break
        time.sleep(0.2)
    assert show["run"]["status"] == "succeeded", show["run"].get("manifest")

    events = [r["event"] for r in AuditLog(daemon.settings.audit_log_path).read_all()
              if r.get("run_id") == run_id]
    expected_order = ["egg.queued", "egg.hatched", "clutch.stored", "abattoir.attested", "chicken.finished"]
    pos = 0
    for want in expected_order:
        assert want in events, f"missing {want} in {events}"
        assert events.index(want) >= pos, f"{want} out of order in {events}"
        pos = events.index(want)
