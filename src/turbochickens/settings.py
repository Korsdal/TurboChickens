"""Settings loading with defaults and TOML overrides.

Resolution order for the settings file:

1. explicit ``--settings`` path (must exist)
2. ``$TURBOCHICKENS_SETTINGS``
3. ``~/.turbochickens/settings.toml`` if it exists
4. built-in defaults

Defaults follow baseline spec v0.2 (decision log): the resource weight budget
is derived from machine size unless overridden, the audit log is kept forever,
and results expire after 6 months. Deletion of either is audit-logged (spec 08).
"""

from __future__ import annotations

import math
import os
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

SETTINGS_ENVVAR = "TURBOCHICKENS_SETTINGS"
DEFAULT_DATA_DIR = Path("~/.turbochickens").expanduser()

#: spec 15: size cap for a job-spec bundle uploaded over HTTP (decoded bytes,
#: job TOML + prompt + schema + attachments). The coop HTTP layer sets its
#: aiohttp client_max_size backstop from UPLOAD_MAX_HTTP_BODY (base64 expands
#: ~4/3), and the daemon's JSON-lines socket gets SOCKET_STREAM_LIMIT so one
#: request line can carry a whole bundle at the cap.
UPLOAD_MAX_BYTES = 10 * 1024 * 1024
UPLOAD_MAX_HTTP_BODY = UPLOAD_MAX_BYTES * 4 // 3 + 2 * 1024 * 1024
SOCKET_STREAM_LIMIT = UPLOAD_MAX_HTTP_BODY + 1024 * 1024


class SettingsError(Exception):
    """Settings file missing, unreadable, or invalid."""


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data_dir: Path = Field(default=DEFAULT_DATA_DIR)
    weight_budget_override: int | None = Field(default=None, gt=0)
    audit_retention: Literal["forever"] = "forever"  # MVP: the audit log is never TTL-deleted
    results_retention_months: int = Field(default=6, gt=0)
    providers_path: Path | None = Field(
        default=None,
        description="Explicit providers.toml path (spec 05); else $TURBOCHICKENS_PROVIDERS, "
        "else <data_dir>/providers.toml, else built-in defaults.",
    )
    # spec 12 (coop): the daemon serves the TurboChickens GUI over HTTP in its
    # own event loop when enabled — `tc coop start`, or `tc daemon start` with
    # coop_enabled = true (the coop container's CMD)
    coop_enabled: bool = False
    coop_host: str = "127.0.0.1"
    coop_port: int = Field(default=8737, gt=0, le=65535)
    coop_auth_token: str | None = Field(
        default=None,
        description="Shared bearer token for the coop HTTP surface. Required when bound "
        "non-loopback; when unset there, an ephemeral token is generated at startup and "
        "printed once to stdout. Never written to the audit log.",
    )

    # spec 26 (Farmhand): the MCP server over the farm's own socket ops —
    # farmhand_transport picks the served transport(s) ("stdio" for
    # same-machine agents gated by the 0600 socket, "https" for the
    # streamable-HTTP endpoint on the coop listener, "both" = the two at
    # once), and farmhand_https_path is that endpoint's path
    farmhand_transport: Literal["stdio", "https", "both"] = "both"
    farmhand_https_path: str = Field(default="/mcp", min_length=2)

    @field_validator("farmhand_https_path")
    @classmethod
    def _farmhand_https_path_shape(cls, v: str) -> str:
        if not v.startswith("/") or "//" in v or ".." in v or "\\" in v or v.endswith("/"):
            raise ValueError("farmhand_https_path must be a single absolute path segment like /mcp")
        return v

    @property
    def coop_url(self) -> str:
        return f"http://{self.coop_host}:{self.coop_port}/"

    @property
    def socket_path(self) -> Path:
        return self.data_dir / "daemon.sock"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "turbochickens.db"

    @property
    def audit_log_path(self) -> Path:
        return self.data_dir / "audit.jsonl"

    @property
    def results_dir(self) -> Path:
        """On-disk result artifacts, keyed by run ID (spec 04)."""
        return self.data_dir / "results"

    @property
    def quarantine_dir(self) -> Path:
        """Quarantined (schema-invalid) output, kept for inspection (spec 04)."""
        return self.data_dir / "quarantine"

    @property
    def seats_dir(self) -> Path:
        """Seat/subscription auth sessions (spec 09) — secrets at rest, kept
        out of job specs and the audit log; files are created 0600."""
        return self.data_dir / "seats"

    @property
    def uploads_dir(self) -> Path:
        """Job-spec bundles uploaded over HTTP (spec 15), stored under
        `<job>-<spec_hash>/` so each is enqueueable by an in-container path."""
        return self.data_dir / "uploads"

    @property
    def weight_budget(self) -> int:
        """Effective resource-weight budget (baseline v0.2, decision 2)."""
        if self.weight_budget_override is not None:
            return self.weight_budget_override
        return default_weight_budget()

    def public_dict(self) -> dict:
        """The effective settings as served to the GUI (spec 13 `settings_get`).

        Every field except secrets: `coop_auth_token` is deliberately omitted —
        it is never served, printed (beyond the one-time startup line), logged,
        or audited. Paths are included as strings so the payload is JSON-safe.
        """
        return {
            "data_dir": str(self.data_dir),
            "socket_path": str(self.socket_path),
            "db_path": str(self.db_path),
            "audit_log_path": str(self.audit_log_path),
            "results_dir": str(self.results_dir),
            "quarantine_dir": str(self.quarantine_dir),
            "seats_dir": str(self.seats_dir),
            "uploads_dir": str(self.uploads_dir),
            "providers_path": str(self.providers_path) if self.providers_path else None,
            "weight_budget_override": self.weight_budget_override,
            "weight_budget": self.weight_budget,
            "audit_retention": self.audit_retention,
            "results_retention_months": self.results_retention_months,
            "coop_enabled": self.coop_enabled,
            "coop_host": self.coop_host,
            "coop_port": self.coop_port,
            "coop_url": self.coop_url,
            "farmhand_transport": self.farmhand_transport,
            "farmhand_https_path": self.farmhand_https_path,
        }


def default_weight_budget() -> int:
    """max(1, min(vCPU, floor(RAM_GB / 1.5))) — Linux MVP."""
    vcpu = os.cpu_count() or 1
    try:
        ram_bytes = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (AttributeError, OSError, ValueError):
        ram_bytes = 8 * 1024**3
    ram_gb = ram_bytes / (1024**3)
    return max(1, min(vcpu, math.floor(ram_gb / 1.5)))


def default_settings_path() -> Path:
    return DEFAULT_DATA_DIR / "settings.toml"


def resolve_settings_path(explicit: str | Path | None = None) -> Path | None:
    """Pick the settings file to load, or None for built-in defaults."""
    if explicit is not None:
        p = Path(explicit)
        if not p.is_file():
            raise SettingsError(f"settings file not found: {p}")
        return p
    env = os.environ.get(SETTINGS_ENVVAR)
    if env:
        p = Path(env)
        if not p.is_file():
            raise SettingsError(f"settings file not found: {p} (from ${SETTINGS_ENVVAR})")
        return p
    p = default_settings_path()
    return p if p.is_file() else None


def load_settings(explicit: str | Path | None = None) -> Settings:
    """Load settings: defaults overlaid with the resolved TOML file, if any."""
    path = resolve_settings_path(explicit)
    data: dict = {}
    if path is not None:
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as e:
            raise SettingsError(f"cannot load settings from {path}: {e}") from e
    base = {"data_dir": str(DEFAULT_DATA_DIR)}
    base.update(data)
    try:
        return Settings.model_validate(base)
    except ValidationError as e:
        msgs = "; ".join(
            f"{'.'.join(str(x) for x in err['loc']) or '<root>'}: {err['msg']}" for err in e.errors()
        )
        raise SettingsError(f"invalid settings ({path or 'defaults'}): {msgs}") from e
