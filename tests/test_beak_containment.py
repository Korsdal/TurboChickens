"""Tests for spec 32 ticket 07 — Beak containment (review findings G4 + G20).

- G4 resolved-target privacy: the private-config blocklist is checked on the
  *resolved* target, so a Chicken-planted symlink alias, symlinked dir, or
  hardlink alias into ``.vibe/`` is refused (three parametrized negatives);
- G4 symlink-safe open: peek targets that are symlinks or have ``nlink > 1``
  are refused, and the open is ``O_NOFOLLOW`` component-wise from the
  workspace dir fd, so a resolve→open race (component swapped for a symlink or
  a hardlink after the lock's resolve) cannot serve anything outside the
  workspace or any aliased inode — simulated deterministically by swapping
  the on-disk entry inside a wrapped ``resolve_locked``;
- positive: the chicken's own plain files still peek and list while it runs;
- G20 async capped tree: the walk caps before sorting (a huge directory is
  never fully sorted) and runs off the event loop (asserted via a spy on
  ``beak.workspace_tree`` seeing no running loop in its thread), reached
  through the real ``workspace_peek`` socket op on a running chicken;
- HTTP parity: ``GET /api/runs/{id}/workspace?path=…`` mirrors the socket
  refusal payloads exactly.

All assertions are external, observable behavior: response payloads, refused
fields, payload secrecy, HTTP status codes, and tree shape.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import threading
from pathlib import Path

import pytest

from test_beak import SECRET, beak_vibe, coop_daemon, enqueue, wait_status, wait_tree
from test_coop import http_client
from test_run_e2e import write_job
from turbochickens import beak
from turbochickens.client import request
from turbochickens.runner import TERMINAL_STATES


def _ws(tmp_path: Path) -> Path:
    """A workspace with the chicken's private harness config (holding the
    gate secret marker, as specs 27/28 really write it) and a plain file."""
    root = tmp_path / "ws"
    root.mkdir()
    (root / ".vibe").mkdir()
    (root / ".vibe" / "config.toml").write_text(
        f'[mcp_servers.gate]\nenv = {{ GATE_TOKEN = "{SECRET}" }}\n'
    )
    (root / "notes.txt").write_text("pecked notes\n")
    return root


# --- G4: aliases into private harness config are refused (resolved target) ------


@pytest.mark.parametrize("case", ["symlink-alias", "symlinked-dir", "hardlink-alias"])
def test_peek_refuses_aliases_into_private_config(tmp_path, case) -> None:
    root = _ws(tmp_path)
    if case == "symlink-alias":
        (root / "alias").symlink_to(root / ".vibe" / "config.toml")
        rel, want = "alias", "private"
    elif case == "symlinked-dir":
        (root / "linkdir").symlink_to(root / ".vibe")
        rel, want = "linkdir/config.toml", "private"
    else:  # hardlink-alias: same inode, the resolved path is the alias itself
        os.link(root / ".vibe" / "config.toml", root / "hard")
        rel, want = "hard", "hardlink"
    resp = beak.peek_file(root, rel)
    assert "error" in resp and resp["refused"] == want, (case, resp)
    assert "fox refuses" in resp["error"]
    assert SECRET not in json.dumps(resp)
    # the direct private path is still refused by name as before
    priv = beak.peek_file(root, ".vibe/config.toml")
    assert priv["refused"] == "private" and SECRET not in json.dumps(priv)


def test_peek_refuses_any_symlink_or_hardlink_target(tmp_path) -> None:
    root = _ws(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("host secret")
    # positive first: the chicken's own plain file peeks fine
    ok = beak.peek_file(root, "notes.txt")
    assert "error" not in ok and ok["content"] == "pecked notes\n"
    (root / "inside-alias").symlink_to(root / "notes.txt")
    (root / "outside-alias").symlink_to(outside)
    os.link(root / "notes.txt", root / "notes-hard")

    # a symlink to a plain in-workspace file is still refused (no aliasing at all)
    r = beak.peek_file(root, "inside-alias")
    assert r["refused"] == "symlink" and "pecked notes" not in json.dumps(r)
    # a symlink pointing out of the workspace resolves outside → unsafe
    r = beak.peek_file(root, "outside-alias")
    assert "unsafe workspace path" in r["error"] and "host secret" not in json.dumps(r)
    # a hardlink to a plain file is refused too (nlink > 1)
    r = beak.peek_file(root, "notes-hard")
    assert r["refused"] == "hardlink" and "pecked notes" not in json.dumps(r)


# --- G4: the resolve→open race cannot serve outside or aliased files -------------


def test_resolve_open_race_cannot_escape_workspace(tmp_path, monkeypatch) -> None:
    root = _ws(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text(SECRET + "\n")
    outside_dir = tmp_path / "outside-dir"
    outside_dir.mkdir()
    (outside_dir / "f.txt").write_text(SECRET + "\n")
    real_resolve = beak.resolve_locked

    # race 1: the final component is swapped for a symlink to a host file
    # after the lock's resolve, before the open (O_NOFOLLOW must bite)
    (root / "victim.txt").write_text("innocent\n")

    def swap_final(r: Path, rel: str):
        t = real_resolve(r, rel)
        if rel == "victim.txt" and (r / "victim.txt").is_file() and not (r / "victim.txt").is_symlink():
            (r / "victim.txt").unlink()
            (r / "victim.txt").symlink_to(outside)
        return t

    monkeypatch.setattr(beak, "resolve_locked", swap_final)
    resp = beak.peek_file(root, "victim.txt")
    assert resp.get("refused") == "symlink", resp
    assert SECRET not in json.dumps(resp)

    # race 2: an intermediate directory is swapped for a symlink to an
    # outside directory holding a same-named file
    (root / "sub").mkdir()
    (root / "sub" / "f.txt").write_text("sub file\n")

    def swap_dir(r: Path, rel: str):
        t = real_resolve(r, rel)
        sub = r / "sub"
        if rel == "sub/f.txt" and sub.is_dir() and not sub.is_symlink():
            shutil.rmtree(sub)
            sub.symlink_to(outside_dir)
        return t

    monkeypatch.setattr(beak, "resolve_locked", swap_dir)
    resp = beak.peek_file(root, "sub/f.txt")
    assert resp.get("refused") == "symlink", resp
    assert SECRET not in json.dumps(resp)

    # race 3: the file is swapped for a hardlink to the private config —
    # the opened fd's nlink check must refuse it
    (root / "victim2.txt").write_text("innocent2\n")

    def swap_hard(r: Path, rel: str):
        t = real_resolve(r, rel)
        v = r / "victim2.txt"
        if rel == "victim2.txt" and v.is_file() and not v.is_symlink():
            v.unlink()
            os.link(r / ".vibe" / "config.toml", v)
        return t

    monkeypatch.setattr(beak, "resolve_locked", swap_hard)
    resp = beak.peek_file(root, "victim2.txt")
    assert resp.get("refused") == "hardlink", resp
    assert SECRET not in json.dumps(resp)

    # positive control: with no swap the plain files peek fine
    monkeypatch.setattr(beak, "resolve_locked", real_resolve)
    assert beak.peek_file(root, "notes.txt")["content"] == "pecked notes\n"


# --- positive: legitimate files peekable and listed ------------------------------


def test_legitimate_files_still_peekable_and_listed(tmp_path) -> None:
    root = _ws(tmp_path)
    (root / "data").mkdir()
    (root / "data" / "big.txt").write_text("B" * 100)
    tree = beak.workspace_tree(root)
    paths = {e["path"]: e for e in tree["entries"]}
    assert paths["notes.txt"]["type"] == "file" and paths["notes.txt"]["size_bytes"] == len("pecked notes\n")
    assert paths["data"]["type"] == "dir" and paths["data/big.txt"]["size_bytes"] == 100
    assert paths[".vibe"]["type"] == "dir"  # listed by name only, never served
    assert tree["truncated"] is False
    got = beak.peek_file(root, "notes.txt")
    assert got["encoding"] == "utf-8" and got["content"] == "pecked notes\n"
    assert beak.peek_file(root, ".vibe/config.toml")["refused"] == "private"


# --- G20: the tree caps before sorting and runs off the event loop ----------------


def test_tree_caps_before_sorting(tmp_path) -> None:
    root = tmp_path / "ws"
    many = root / "many"
    many.mkdir(parents=True)
    # one huge directory: a full sort of it would be the G20 exhaustion path
    for i in range(beak.WORKSPACE_PEEK_MAX_ENTRIES + 500):
        (many / f"f{i:05d}.txt").write_text("x")
    (root / "notes.txt").write_text("hi")
    tree = beak.workspace_tree(root)
    assert tree["truncated"] is True
    assert tree["n_entries"] == beak.WORKSPACE_PEEK_MAX_ENTRIES
    assert tree["max_entries"] == beak.WORKSPACE_PEEK_MAX_ENTRIES
    paths = [e["path"] for e in tree["entries"]]
    assert paths == sorted(paths)  # the capped list is sorted, not the full dir


def test_tree_walk_runs_off_the_event_loop(tmp_path, monkeypatch) -> None:
    root = _ws(tmp_path)
    (root / "data").mkdir()
    (root / "data" / "a.txt").write_text("a")
    real = beak.workspace_tree
    seen: dict = {}

    def spy(r: Path) -> dict:
        seen["thread"] = threading.get_ident()
        try:
            asyncio.get_running_loop()
            seen["on_loop"] = True
        except RuntimeError:
            seen["on_loop"] = False
        return real(r)

    monkeypatch.setattr(beak, "workspace_tree", spy)

    async def go():
        loop_thread = threading.get_ident()
        tree = await beak.workspace_tree_async(root)
        return tree, loop_thread

    tree, loop_thread = asyncio.run(go())
    assert seen["on_loop"] is False  # the walk ran in a worker thread
    assert seen["thread"] != loop_thread
    assert tree == real(root)  # identical result to the synchronous walk
    assert {e["path"] for e in tree["entries"]} >= {"notes.txt", "data", "data/a.txt"}


# --- socket op + HTTP parity on a live running chicken ----------------------------


def test_socket_peek_refuses_planted_aliases_and_tree_runs_off_loop(
    coop_daemon, beak_vibe, tmp_path, monkeypatch
) -> None:
    sock = coop_daemon.settings.socket_path
    spec = write_job(tmp_path, name="plant-job", prompt="SLEEP 20 plant", timeout=120)
    run_id = enqueue(sock, spec)
    wait_status(sock, run_id, ("running",))
    wait_tree(sock, run_id, ".vibe/config.toml")  # the harness wrote its private config
    ws = coop_daemon.settings.data_dir / "workspaces" / run_id
    priv = ws / ".vibe" / "config.toml"
    assert SECRET in priv.read_text()

    # the chicken plants aliases into its own private config while running
    os.symlink(priv, ws / "alias")
    os.symlink(ws / ".vibe", ws / "linkdir")
    os.link(priv, ws / "hard")
    os.symlink(ws / "notes.txt", ws / "note-alias")

    for rel, want in (
        ("alias", "private"),
        ("linkdir/config.toml", "private"),
        ("hard", "hardlink"),
        ("note-alias", "symlink"),
    ):
        resp = request(sock, "workspace_peek", run_id=run_id, path=rel, timeout=2.0)
        assert resp["ok"] is False and resp["refused"] == want, (rel, resp)
        assert SECRET not in json.dumps(resp)

    # positive: the chicken's own plain file still peeks while it runs
    got = request(sock, "workspace_peek", run_id=run_id, path="notes.txt", timeout=2.0)
    assert got["ok"] is True and got["mode"] == "file"
    assert got["content"] == "pecked notes\nline two\n"

    # the tree walk behind the socket op ran off the event loop
    seen: dict = {}
    real = beak.workspace_tree

    def spy(r: Path) -> dict:
        try:
            asyncio.get_running_loop()
            seen["on_loop"] = True
        except RuntimeError:
            seen["on_loop"] = False
        return real(r)

    monkeypatch.setattr(beak, "workspace_tree", spy)
    tree_resp = request(sock, "workspace_peek", run_id=run_id, timeout=5.0)
    assert tree_resp["ok"] is True and tree_resp["mode"] == "tree"
    assert seen.get("on_loop") is False
    paths = {e["path"]: e for e in tree_resp["tree"]["entries"]}
    assert paths["notes.txt"]["type"] == "file"
    assert paths["alias"]["type"] == "symlink"  # symlinks listed, never followed
    assert SECRET not in json.dumps(tree_resp)
    assert str(tmp_path) not in json.dumps(tree_resp)
    assert str(coop_daemon.settings.data_dir) not in json.dumps(tree_resp)

    # HTTP parity: the refusals mirror the socket payloads exactly
    c = http_client(coop_daemon.settings)
    for rel in ("alias", "linkdir/config.toml", "hard", "note-alias"):
        r = c.get(f"/api/runs/{run_id}/workspace", params={"path": rel})
        assert r.status_code == 200, rel
        assert r.json() == request(sock, "workspace_peek", run_id=run_id, path=rel, timeout=2.0)
        assert r.json()["ok"] is False and SECRET not in r.text
    r = c.get(f"/api/runs/{run_id}/workspace", params={"path": "notes.txt"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert r.json()["content"] == "pecked notes\nline two\n"

    request(sock, "run_stop", run_id=run_id, timeout=30.0)
    wait_status(sock, run_id, TERMINAL_STATES, timeout=30.0)
