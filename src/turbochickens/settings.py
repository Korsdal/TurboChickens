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

from pydantic import BaseModel, ConfigDict, Field, ValidationError

SETTINGS_ENVVAR = "TURBOCHICKENS_SETTINGS"
DEFAULT_DATA_DIR = Path("~/.turbochickens").expanduser()


class SettingsError(Exception):
    """Settings file missing, unreadable, or invalid."""


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data_dir: Path = Field(default=DEFAULT_DATA_DIR)
    weight_budget_override: int | None = Field(default=None, gt=0)
    audit_retention: Literal["forever"] = "forever"  # MVP: the audit log is never TTL-deleted
    results_retention_months: int = Field(default=6, gt=0)

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
    def weight_budget(self) -> int:
        """Effective resource-weight budget (baseline v0.2, decision 2)."""
        if self.weight_budget_override is not None:
            return self.weight_budget_override
        return default_weight_budget()


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
