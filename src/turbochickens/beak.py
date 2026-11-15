"""The Beak — a read-only live view into a running chicken's workspace (spec 29).

The workspace of a run lives at ``<data_dir>/workspaces/<run_id>`` and exists
only while the chicken is in flight: the Hatchery provisions it at hatch and
the Abattoir's attested teardown removes it, so the Beak is open exactly while
the workspace directory exists (terminal runs → closed, a clear error).

This module is the pure seam behind the daemon's ``workspace_peek`` socket op
(the single place that touches workspace files — the coop web layer only ever
sees the op's payload over the transport seam):

- **path lock**: every requested path is a workspace-relative POSIX path; the
  resolved target must stay inside the workspace root (symlinks that point
  out are refused), so ``../`` traversal and absolute paths never escape;
- **size caps**: the tree is capped at ``WORKSPACE_PEEK_MAX_ENTRIES`` entries
  and single-file content at ``WORKSPACE_PEEK_MAX_BYTES`` served bytes
  (``truncated`` flags the cut; oversized files are never fully read into a
  response);
- **secrecy**: the chicken's private harness config is never served — the
  run's vibe config (``.vibe/``, which may hold resolved gate/env secrets from
  specs 27/28) and the generated farmhand settings file
  (``.tc-farmhand-settings.toml``) are refused by name. Tree entries carry
  paths and sizes only, never contents.

Read-only by construction: nothing here writes, executes, or shells out.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Any

# spec 32 (G1): the path lock mechanics are the shared Fence idiom
# (`fence.py`) — the Beak's lock was the reference implementation, so the
# two names below delegate to it and stay byte-stable for callers.
from turbochickens.fence import resolve_contained, safe_ref

#: spec 29: cap on served bytes of one workspace file per peek
WORKSPACE_PEEK_MAX_BYTES = 64 * 1024

#: spec 29: cap on tree entries per peek (larger trees are flagged `truncated`)
WORKSPACE_PEEK_MAX_ENTRIES = 1000

#: spec 29: workspace-relative paths the Beak never serves — the chicken's
#: private harness config. ``.vibe/config.toml`` can contain gate env/header
#: refs resolved against the daemon environment (spec 28), and the farmhand
#: settings file (spec 27) points at daemon-internal paths; neither is ever
#: exposed. (The settings file is token-free, but it is still daemon config,
#: not chicken work.) The Scratch (``.tc-home/``, spec 32 ticket 12 — the
#: chicken's per-run HOME/XDG with the sanitized vibe profile) is private too.
BEAK_PRIVATE_DIRS = frozenset({".vibe", ".tc-home"})
BEAK_PRIVATE_FILES = frozenset({".tc-farmhand-settings.toml"})


def safe_relpath(path: str) -> str | None:
    """Normalize one requested workspace path: relative, POSIX, no empty/'.'/'..'
    segments, no backslash or NUL, not absolute. Returns the normalized relative
    path, or None when the request tries to leave the workspace.
    (spec 32: delegates to the shared Fence rule in ``fence.py``.)"""
    return safe_ref(path)


def is_private_relpath(rel: str) -> bool:
    """True when the workspace-relative path is the chicken's private harness
    config, which the Beak never serves (spec 29 secrecy)."""
    first = rel.split("/", 1)[0]
    if first in BEAK_PRIVATE_DIRS:
        return True
    return rel in BEAK_PRIVATE_FILES


def resolve_locked(root: Path, rel: str) -> Path | None:
    """Resolve ``root / rel`` and require the result to stay inside ``root``.
    The request must first pass ``safe_relpath`` (no ``..``/absolute/backslash),
    and the resolved target must additionally stay inside the resolved root —
    symlink-safe: a link pointing out of the workspace resolves outside and is
    refused. Returns the resolved path, or None on any escape.
    (spec 32: delegates to the shared Fence rule in ``fence.py``.)"""
    return resolve_contained(root, rel)


def workspace_tree(root: Path) -> dict[str, Any]:
    """The workspace file tree, capped at ``WORKSPACE_PEEK_MAX_ENTRIES`` entries.
    Entries are ``{"path", "type", "size_bytes"}`` with workspace-relative
    paths, sorted by path; directories are listed (type ``"dir"``) but not
    descended into beyond the cap. Symlinks are listed, never followed.
    Paths and sizes only — no contents ever leave in a tree."""
    entries: list[dict[str, Any]] = []
    total_bytes = 0
    truncated = False
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        # do not descend into symlinked directories
        dirnames[:] = [d for d in dirnames if not (Path(dirpath) / d).is_symlink()]
        for name in sorted(dirnames + filenames):
            if len(entries) >= WORKSPACE_PEEK_MAX_ENTRIES:
                truncated = True
                break
            p = Path(dirpath) / name
            rel = p.relative_to(root).as_posix()
            try:
                if p.is_symlink():
                    entries.append({"path": rel, "type": "symlink", "size_bytes": 0})
                    continue
                if p.is_dir():
                    entries.append({"path": rel, "type": "dir", "size_bytes": 0})
                    continue
                size = p.stat().st_size
            except OSError:
                continue  # raced with teardown — the next peek reports the workspace gone
            entries.append({"path": rel, "type": "file", "size_bytes": size})
            total_bytes += size
        if truncated:
            break
    entries.sort(key=lambda e: e["path"])
    return {
        "entries": entries,
        "n_entries": len(entries),
        "total_bytes": total_bytes,
        "truncated": truncated,
        "max_entries": WORKSPACE_PEEK_MAX_ENTRIES,
    }


def peek_file(root: Path, rel: str) -> dict[str, Any]:
    """Read one workspace file, size-capped at ``WORKSPACE_PEEK_MAX_BYTES``
    served bytes. Text files (UTF-8) come back as ``encoding="utf-8"``;
    anything else as ``encoding="base64"``. ``truncated=True`` means the file
    is larger than the cap and only the first cap bytes were served.

    Returns a response dict — either an ``error`` or the content fields —
    never raises on bad input (fail closed)."""
    target = resolve_locked(root, rel)
    if target is None:
        return {"error": f"unsafe workspace path: {rel!r} — the beak only serves paths inside the chicken's workspace"}
    if is_private_relpath(rel):
        return {
            "error": (
                f"refused: {rel!r} is the chicken's private harness config —"
                " the beak never serves it (env/gate material may live there)"
            ),
            "refused": "private",
        }
    try:
        if not target.exists():
            return {"error": f"no such file in the workspace: {rel!r}", "refused": "missing"}
        if target.is_dir():
            return {"error": f"{rel!r} is a directory — peek a file, or omit the path for the whole tree", "refused": "directory"}
        size = target.stat().st_size
        with open(target, "rb") as f:
            blob = f.read(WORKSPACE_PEEK_MAX_BYTES + 1)
    except OSError as e:
        # spec 32 (G17): the OS error carries the resolved host path — the
        # served error reports the workspace-relative name and the errno only
        return {
            "error": f"cannot read {rel!r} from the workspace: {e.strerror or type(e).__name__}",
            "refused": "unreadable",
        }
    truncated = len(blob) > WORKSPACE_PEEK_MAX_BYTES
    served = blob[:WORKSPACE_PEEK_MAX_BYTES]
    try:
        content = served.decode("utf-8")
        encoding = "utf-8"
    except UnicodeDecodeError:
        content = base64.b64encode(served).decode("ascii")
        encoding = "base64"
    return {
        "path": rel,
        "size_bytes": size,
        "served_bytes": len(served),
        "truncated": truncated,
        "cap_bytes": WORKSPACE_PEEK_MAX_BYTES,
        "encoding": encoding,
        "content": content,
    }
