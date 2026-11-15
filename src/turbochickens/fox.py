"""The Fox — the security seam that watches the fence (docs/lexicon.md).

Spec 32 / review finding G11 (perms hardening): **all state the daemon creates
is restrictive, with no umask window on any created state file.**

- The daemon process establishes a restrictive umask (0o077) at start
  (`apply_restrictive_umask` in `run_daemon`), so every file it creates — db
  and its sidecar journals, the Headcount, the socket at bind, workspaces,
  results/quarantine artifacts, uploads, injected Farmhand/Gate config — is
  owner-only **from creation**.
- On top of the umask, every named piece of state also gets an explicit mode
  (`ensure_dir` / `write_*_0600` / `open_append_0600`), which additionally
  tightens a pre-existing file or directory that was created looser — so a
  data dir left 0755 by an older version is closed on the next start.

Modes: directories (data dir, workspaces, seats, uploads, injected `.vibe`
harness-config dirs) are 0700; files (socket, db, Headcount, registry +
registry tmp, seat sessions, results/quarantine artifacts, injected config
files) are 0600. On a multi-user host other local users can no longer read
Clutches, quarantine, the Headcount, or a live Chicken's resolved Gate
secrets.
"""

from __future__ import annotations

import os
from pathlib import Path

#: umask established by the daemon at start: every created file/dir is
#: owner-only from creation (0o777 & ~0o077 = 0o700 dirs, 0o666 & ~0o077 = 0o600 files)
RESTRICTIVE_UMASK = 0o077

#: restrictive modes for created state
DIR_MODE = 0o700
FILE_MODE = 0o600


def apply_restrictive_umask() -> int:
    """Set the process umask to `RESTRICTIVE_UMASK` and return the previous
    umask (so a caller can restore it). Called once at daemon start, before
    any state is created — this is what closes the umask window: nothing the
    daemon creates is ever group/other-readable, even between create and an
    explicit chmod."""
    return os.umask(RESTRICTIVE_UMASK)


def ensure_dir(path: str | Path, mode: int = DIR_MODE) -> Path:
    """Create `path` (and parents) if needed and set its mode to `mode`
    (0700). The explicit chmod also tightens a pre-existing directory."""
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    os.chmod(p, mode)
    return p


def ensure_file_mode(path: str | Path, mode: int = FILE_MODE) -> Path:
    """Set an existing file's mode to `mode` (0600) — no-op when absent."""
    p = Path(path)
    if p.exists():
        os.chmod(p, mode)
    return p


def write_bytes_0600(path: str | Path, data: bytes, mode: int = FILE_MODE) -> Path:
    """Write `data` to `path`, creating the file 0600 **at creation** (the
    mode argument of `os.open` is only umask-masked, and 0600 survives any
    umask that leaves owner bits) and `fchmod`-ing it, so a pre-existing file
    with looser perms is tightened too. No umask window."""
    p = Path(path)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb", closefd=False) as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
    finally:
        os.close(fd)
    return p


def write_text_0600(path: str | Path, text: str, mode: int = FILE_MODE) -> Path:
    """`write_bytes_0600` for text (utf-8)."""
    return write_bytes_0600(path, text.encode("utf-8"), mode=mode)


def open_append_0600(path: str | Path, mode: int = FILE_MODE) -> int:
    """Open `path` for append, creating it 0600 at creation and `fchmod`-ing
    it (a pre-existing looser file is tightened). Returns the raw fd — the
    caller writes and closes (used by the append-only Headcount, which is
    never opened for write/truncate)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, mode)
    os.fchmod(fd, mode)
    return fd
