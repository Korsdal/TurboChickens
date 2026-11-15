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
- **resolved-target privacy** (spec 32 ticket 07, review G4): the private
  blocklist is checked on the *resolved* target, not only the requested
  string — a Chicken-planted symlink alias, symlinked dir, or hardlink alias
  into ``.vibe/`` resolves into private config and is refused;
- **symlink/hardlink refusal**: peek targets whose final component is a
  symlink, or whose inode has ``nlink > 1`` (a hardlink alias), are refused
  outright — only the chicken's own plain files are served;
- **symlink-safe open** (spec 32 ticket 07): files are opened with
  ``O_NOFOLLOW``, component by component from the workspace dir fd
  (``os.open(..., dir_fd=…)``), so a resolve→open race — a component swapped
  for a symlink after the lock's resolve — cannot serve anything outside the
  workspace (``ELOOP``), and the opened fd is re-checked (regular file,
  ``nlink == 1``) before a single byte is read;
- **size caps**: the tree is capped at ``WORKSPACE_PEEK_MAX_ENTRIES`` entries
  (cap applied *before* sorting, so a huge directory is never fully sorted)
  and single-file content at ``WORKSPACE_PEEK_MAX_BYTES`` served bytes
  (``truncated`` flags the cut; oversized files are never fully read into a
  response);
- **off the event loop** (spec 32 ticket 07, review G20): the tree walk runs
  in a worker thread (``workspace_tree_async``), so a huge workspace cannot
  stall the daemon loop;
- **secrecy**: the chicken's private harness config is never served — the
  run's vibe config (``.vibe/``, which may hold resolved gate/env secrets from
  specs 27/28) and the generated farmhand settings file
  (``.tc-farmhand-settings.toml``) are refused by name. Tree entries carry
  paths and sizes only, never contents.

Read-only by construction: nothing here writes, executes, or shells out.
"""

from __future__ import annotations

import asyncio
import base64
import errno
import os
import stat
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
    paths, sorted by path; directories are listed (type ``"dir"``) and
    descended into. Symlinks are listed (type ``"symlink"``), never followed.
    Paths and sizes only — no contents ever leave in a tree.

    spec 32 (G20): the walk caps *before* sorting — a directory with a huge
    number of entries is never fully sorted, only the capped entry list is —
    and :func:`workspace_tree_async` runs this in a worker thread so the walk
    never blocks the daemon's event loop."""
    entries: list[dict[str, Any]] = []
    total_bytes = 0
    truncated = False
    pending: list[Path] = [root]
    while pending and not truncated:
        d = pending.pop()
        try:
            it = os.scandir(d)
        except OSError:
            continue  # raced with teardown — the next peek reports the workspace gone
        with it:
            for de in it:
                if len(entries) >= WORKSPACE_PEEK_MAX_ENTRIES:
                    truncated = True
                    break
                p = Path(de.path)
                rel = p.relative_to(root).as_posix()
                try:
                    if de.is_symlink():
                        entries.append({"path": rel, "type": "symlink", "size_bytes": 0})
                        continue  # never followed, never descended into
                    if de.is_dir(follow_symlinks=False):
                        entries.append({"path": rel, "type": "dir", "size_bytes": 0})
                        pending.append(p)
                        continue
                    size = de.stat(follow_symlinks=False).st_size
                except OSError:
                    continue  # raced with teardown
                entries.append({"path": rel, "type": "file", "size_bytes": size})
                total_bytes += size
    # sort only the capped list — never the full (possibly huge) directory
    entries.sort(key=lambda e: e["path"])
    return {
        "entries": entries,
        "n_entries": len(entries),
        "total_bytes": total_bytes,
        "truncated": truncated,
        "max_entries": WORKSPACE_PEEK_MAX_ENTRIES,
    }


async def workspace_tree_async(root: Path) -> dict[str, Any]:
    """The workspace tree with the filesystem walk off the event loop
    (spec 32 G20): the (capped) walk runs in a worker thread, so a chicken
    planting a huge workspace cannot stall the daemon loop."""
    return await asyncio.to_thread(workspace_tree, root)


def _open_no_follow(root_resolved: Path, rel: str) -> int:
    """Open ``root_resolved / rel`` symlink-safe and return the fd.

    Every path component is opened relative to its parent's dir fd with
    ``O_NOFOLLOW`` (and ``O_DIRECTORY`` for intermediate components), so a
    component swapped for a symlink after the lock's resolve fails with
    ``ELOOP`` instead of following the link — the resolve→open race cannot
    serve anything outside the workspace root. ``O_NONBLOCK`` keeps a
    chicken-planted FIFO from wedging the open; the caller re-checks the fd
    with ``fstat`` before reading. Raises ``OSError`` on any refusal."""
    dir_fd = os.open(root_resolved, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY)
    try:
        parts = rel.split("/")
        for part in parts[:-1]:
            try:
                nxt = os.open(
                    part,
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY | os.O_NONBLOCK,
                    dir_fd=dir_fd,
                )
            except OSError as e:
                if e.errno in (errno.ELOOP, errno.ENOTDIR):
                    # O_NOFOLLOW|O_DIRECTORY on a symlink surfaces as ELOOP on
                    # some kernels and ENOTDIR on others — confirm with a
                    # no-follow stat of the component before classifying
                    try:
                        cst = os.stat(part, dir_fd=dir_fd, follow_symlinks=False)
                    except OSError:
                        cst = None
                    if cst is not None and stat.S_ISLNK(cst.st_mode):
                        raise OSError(errno.ELOOP, os.strerror(errno.ELOOP)) from e
                raise
            os.close(dir_fd)
            dir_fd = nxt
        file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
        os.close(dir_fd)
        return file_fd
    except BaseException:
        try:
            os.close(dir_fd)
        except OSError:
            pass
        raise


def peek_file(root: Path, rel: str) -> dict[str, Any]:
    """Read one workspace file, size-capped at ``WORKSPACE_PEEK_MAX_BYTES``
    served bytes. Text files (UTF-8) come back as ``encoding="utf-8"``;
    anything else as ``encoding="base64"``. ``truncated=True`` means the file
    is larger than the cap and only the first cap bytes were served.

    spec 32 ticket 07 (review G4): containment + privacy are checked on the
    **resolved** target (a symlinked dir or alias into ``.vibe/`` resolves
    into private config and is refused), symlink and hardlink (``nlink > 1``)
    targets are refused outright, and the file is opened ``O_NOFOLLOW``
    component-wise from the workspace dir fd — the opened fd is re-checked
    with ``fstat`` before reading, so a resolve→open race cannot serve
    anything outside the workspace or any aliased inode.

    Returns a response dict — either an ``error`` or the content fields —
    never raises on bad input (fail closed)."""
    target = resolve_locked(root, rel)
    if target is None:
        return {
            "error": (
                f"unsafe workspace path: {rel!r} — the fox refuses: the beak only serves"
                " relative paths inside the chicken's workspace; the fence holds"
            )
        }
    root_resolved = root.resolve()
    # G4: privacy on the RESOLVED target, not only the requested string —
    # `alias -> .vibe/config.toml` and `linkdir -> .vibe` resolve into private
    # config and must be refused exactly like the direct path. Hardlink aliases
    # resolve to themselves, so they are caught by the nlink check below.
    resolved_rel = target.relative_to(root_resolved).as_posix()
    if is_private_relpath(rel):
        return {
            "error": (
                f"the fox refuses {rel!r} — the chicken's private harness config is never"
                " served by the beak (env/gate material may live there); the fence holds"
            ),
            "refused": "private",
        }
    if is_private_relpath(resolved_rel):
        return {
            "error": (
                f"the fox refuses {rel!r} — it resolves to the chicken's private harness"
                f" config ({resolved_rel!r}), which the beak never serves (env/gate material"
                " may live there); the fence holds"
            ),
            "refused": "private",
        }
    try:
        fd = _open_no_follow(root_resolved, rel)
    except OSError as e:
        # spec 32 (G17): the OS error carries the resolved host path — the
        # served error reports the workspace-relative name and the errno only
        if e.errno == errno.ELOOP:
            # a component is a symlink (e.g. swapped in after the resolve)
            return {
                "error": (
                    f"the fox refuses {rel!r} — a symlink is never served by the beak;"
                    " the fence holds: only plain files inside the chicken's workspace"
                ),
                "refused": "symlink",
            }
        if e.errno in (errno.ENOENT, errno.ENOTDIR):
            return {"error": f"no such file in the workspace: {rel!r}", "refused": "missing"}
        return {
            "error": f"cannot read {rel!r} from the workspace: {e.strerror or type(e).__name__}",
            "refused": "unreadable",
        }
    try:
        st = os.fstat(fd)
        if stat.S_ISDIR(st.st_mode):
            return {"error": f"{rel!r} is a directory — peek a file, or omit the path for the whole tree", "refused": "directory"}
        if not stat.S_ISREG(st.st_mode):
            # fifo/socket/device — never read (an open fifo could block)
            return {
                "error": f"cannot read {rel!r} from the workspace: not a regular file",
                "refused": "unreadable",
            }
        if st.st_nlink > 1:
            # hardlink alias (possibly swapped in after the resolve) — the
            # inode is shared with another name, refuse it
            return {
                "error": (
                    f"the fox refuses {rel!r} — a hardlink alias (nlink > 1) is never served"
                    " by the beak; the fence holds: only the chicken's own plain files"
                ),
                "refused": "hardlink",
            }
        size = st.st_size
        with os.fdopen(fd, "rb") as f:
            blob = f.read(WORKSPACE_PEEK_MAX_BYTES + 1)
            fd = -1  # consumed by fdopen
    except OSError as e:
        return {
            "error": f"cannot read {rel!r} from the workspace: {e.strerror or type(e).__name__}",
            "refused": "unreadable",
        }
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
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
