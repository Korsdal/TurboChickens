"""Connectors registry: `connectors.toml` — external MCP connectors (gates) chickens opt into (spec 28).

The platform **registers** connectors, it never provisions or pays for
third-party services (standing rule, spec 21). A gate is an entry in
``connectors.toml``::

    [connectors.toolx]
    transport = "stdio"
    command = "/usr/local/bin/toolx"
    args = ["--serve"]
    env = { LOG_LEVEL = "debug", TOOLX_TOKEN = "$TOOLX_TOKEN" }

    [connectors.webx]
    transport = "https"
    url = "https://mcp.example.com/mcp"
    headers = { Authorization = "Bearer $WEBX_TOKEN" }   # env refs stay refs

Resolution order for the file is exactly like `providers.toml` (spec 05):

1. explicit path (``Settings.connectors_path``)
2. ``$TURBOCHICKENS_CONNECTORS``
3. ``<data_dir>/connectors.toml`` if it exists

Unlike providers there are **no built-in defaults** — no file means an empty
registry.

A chicken opts in per egg via ``[harness] connectors = ["toolx", ...]``; the
daemon refuses unknown names at enqueue (field-named ``harness.connectors``
error) and refuses the list for adapters without a harness runtime (only
``vibe-cli``). At provisioning the adapter injects the chosen gates into the
run's harness runtime MCP config (``<workspace>/.vibe/config.toml``, the same
file the spec-27 Farmhand entry uses), as ``[mcp_servers.NAME]`` entries —
stdio gates as ``command``/``args``/``env``, https gates as ``url``/``headers``.

Secrecy: ``env`` values and ``headers`` values may be ``$VAR`` / ``${VAR}``
env refs; they are resolved against the daemon's environment **only at
injection time**, landing solely in the ephemeral (abattoir-attested) workspace
config. No env value or resolved header ever appears in any socket/HTTP
response, audit row, run log, or manifest — `connectors_list` serves stdio
``env`` as key names only and https ``headers`` as the refs themselves. Audit
rows (``gate.registered`` / ``gate.removed`` / ``gate.rejected``) carry the
name and transport only.
"""

from __future__ import annotations

import os
import re
import tomllib
import uuid
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, field_validator, model_validator

CONNECTORS_ENVVAR = "TURBOCHICKENS_CONNECTORS"
DEFAULT_DATA_DIR = Path("~/.turbochickens").expanduser()

#: connector names that may not be registered — reserved for the attached
#: Farmhand's own `[mcp_servers.farmhand]` entry in a run's vibe config
RESERVED_NAMES = ("farmhand",)

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

#: `$VAR` / `${VAR}` env references inside env/header values
_REF_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")

_BARE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class ConnectorsError(Exception):
    """connectors.toml missing, unreadable, or invalid — or an injection-time
    failure (unknown gate, unresolvable env ref). Messages never contain
    secret values, only names/refs."""


def valid_connector_name(name: str) -> bool:
    """A registerable gate name: a safe TOML key and spec-list entry."""
    return bool(_NAME_RE.match(name)) and name not in RESERVED_NAMES


class StdioConnector(BaseModel):
    """A stdio gate: spawned as a subprocess by the harness runtime."""

    model_config = ConfigDict(extra="forbid")

    transport: Literal["stdio"]
    command: str = Field(min_length=1)
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_env(self) -> "StdioConnector":
        for k, v in self.env.items():
            if not k or not v:
                raise ValueError("env keys and values must be non-empty strings")
        return self


class HttpsConnector(BaseModel):
    """An https gate: a streamable-HTTP MCP endpoint. `headers` values are
    env refs (e.g. ``"Bearer $TOKEN"``) or literals — never served resolved."""

    model_config = ConfigDict(extra="forbid")

    transport: Literal["https"]
    url: str = Field(min_length=1)
    headers: dict[str, str] = Field(default_factory=dict)

    @field_validator("url")
    @classmethod
    def _check_url(cls, v: str) -> str:
        if not (v.startswith("https://") or v.startswith("http://")):
            raise ValueError(f"url must be an http(s) URL, got {v!r}")
        return v

    @model_validator(mode="after")
    def _check_headers(self) -> "HttpsConnector":
        for k, v in self.headers.items():
            if not k or not v:
                raise ValueError("header names and values must be non-empty strings")
        return self


#: one gate entry, discriminated by `transport`
ConnectorEntry = Annotated[StdioConnector | HttpsConnector, Field(discriminator="transport")]

#: adapter for the discriminated union (validate/dump single entries)
_connector_adapter: TypeAdapter[Any] = TypeAdapter(ConnectorEntry)


def entry_to_dict(entry: StdioConnector | HttpsConnector) -> dict[str, Any]:
    """A gate entry as a plain dict for the registry file (defaults omitted,
    so an empty args/env/headers stays out of the TOML)."""
    return _connector_adapter.dump_python(entry, exclude_defaults=True)


class ConnectorsConfig(BaseModel):
    """The whole registry. Unknown keys are rejected like in the job spec."""

    model_config = ConfigDict(extra="forbid")

    connectors: dict[str, ConnectorEntry] = Field(default_factory=dict)


def fmt_error_loc(loc: tuple) -> str:
    """A dotted error location with the discriminated-union tag segment
    (`stdio`/`https`, which pydantic puts in the loc) stripped, so field names
    read like `connectors.NAME.command`."""
    return ".".join(str(x) for x in loc if x not in ("stdio", "https")) or "<root>"


def public_entry(name: str, entry: StdioConnector | HttpsConnector) -> dict[str, Any]:
    """The non-secret summary served by `connectors_list`: stdio gates expose
    the command/args and only the *names* of env keys; https gates expose the
    url and the header refs as references. Never an env value, never a
    resolved header."""
    if entry.transport == "stdio":
        assert isinstance(entry, StdioConnector)
        return {
            "name": name,
            "transport": "stdio",
            "command": entry.command,
            "args": list(entry.args),
            "env_keys": sorted(entry.env),
        }
    assert isinstance(entry, HttpsConnector)
    return {
        "name": name,
        "transport": "https",
        "url": entry.url,
        "headers": dict(entry.headers),  # refs stay refs
    }


def resolve_connectors_path(explicit: str | Path | None = None, data_dir: Path | None = None) -> Path | None:
    """Pick the connectors file to load, or None for an empty registry."""
    if explicit is not None:
        p = Path(explicit)
        if not p.is_file():
            raise ConnectorsError(f"connectors file not found: {p}")
        return p
    env = os.environ.get(CONNECTORS_ENVVAR)
    if env:
        p = Path(env)
        if not p.is_file():
            raise ConnectorsError(f"connectors file not found: {p} (from ${CONNECTORS_ENVVAR})")
        return p
    base = data_dir if data_dir is not None else DEFAULT_DATA_DIR
    p = base / "connectors.toml"
    return p if p.is_file() else None


def load_connectors(explicit: str | Path | None = None, data_dir: Path | None = None) -> ConnectorsConfig:
    """Load the connectors registry; an absent default file is an empty
    registry (there are no built-in gates)."""
    path = resolve_connectors_path(explicit, data_dir)
    if path is None:
        return ConnectorsConfig()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConnectorsError(f"cannot load connectors from {path}: {e}") from e
    try:
        return ConnectorsConfig.model_validate(data)
    except ValidationError as e:
        msgs = "; ".join(f"{fmt_error_loc(err['loc'])}: {err['msg']}" for err in e.errors())
        raise ConnectorsError(f"invalid connectors ({path}): {msgs}") from e


def read_registry_raw(path: Path | None) -> dict[str, Any]:
    """The raw parsed TOML of the registry file ({} when there is none)."""
    if path is None:
        return {}
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConnectorsError(f"cannot load connectors from {path}: {e}") from e
    return data if isinstance(data, dict) else {}


def validate_connector_payload(name: str, payload: dict[str, Any]) -> tuple[StdioConnector | HttpsConnector | None, list[dict[str, str]]]:
    """Validate one `connectors_add` payload into a gate entry. Returns
    (entry, errors) — errors are field-named (`connectors.<name>.<field>`)."""
    if not _NAME_RE.match(name or ""):
        return None, [{"field": "connectors.name", "message": f"invalid connector name {name!r} — must match {_NAME_RE.pattern}"}]
    if name in RESERVED_NAMES:
        return None, [
            {
                "field": "connectors.name",
                "message": f"connector name {name!r} is reserved (the attached Farmhand's own MCP entry)",
            }
        ]
    data = dict(payload)
    data["transport"] = payload.get("transport")
    try:
        entry = _connector_adapter.validate_python(data)
    except ValidationError as e:
        errors = [
            {
                "field": "connectors." + name + ("." + fmt_error_loc(err["loc"]) if err["loc"] else ""),
                "message": err["msg"],
            }
            for err in e.errors()
        ]
        return None, errors
    return entry, []


# --- TOML writing ------------------------------------------------------------


def _key(k: str) -> str:
    """A TOML key: bare when safe, quoted otherwise."""
    if _BARE_KEY_RE.match(k):
        return k
    return '"' + k.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{_key(k)} = {_toml_value(x)}" for k, x in v.items()) + " }"
    s = str(v).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


_REGISTRY_HEADER = """\
# TurboChickens connectors registry (spec 28) — external MCP connectors
# (gates) a chicken can opt into via [harness] connectors = ["name", ...].
# Resolved like providers.toml: settings.connectors_path >
# $TURBOCHICKENS_CONNECTORS > <data_dir>/connectors.toml.
#
# stdio gate: command (+ optional args, env). https gate: url (+ optional
# headers). env/header values may be env refs like "$TOKEN" — resolved
# against the daemon environment only at injection, never served.
"""


def dump_registry_toml(raw: dict[str, Any]) -> str:
    """Serialize the raw registry dict (`{"connectors": {name: entry}}`) back
    to connectors.toml text."""
    lines = [_REGISTRY_HEADER]
    connectors = raw.get("connectors") or {}
    for name in sorted(connectors):
        entry = connectors[name]
        if not isinstance(entry, dict):
            raise ConnectorsError(f"registry entry {name!r} is not a table")
        lines.append(f"[connectors.{_key(name)}]")
        for k, v in entry.items():
            lines.append(f"{_key(k)} = {_toml_value(v)}")
        lines.append("")
    return "\n".join(lines)


def save_registry(path: Path | None, raw: dict[str, Any], data_dir: Path) -> Path:
    """Atomically write the registry file, always 0600 — it may hold literal
    secret env values (refs are fine, literals are the operator's at-rest
    secret). Returns the written path."""
    target = path if path is not None else data_dir / "connectors.toml"
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + f".tmp-{uuid.uuid4().hex[:8]}")
    tmp.write_text(dump_registry_toml(raw), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, target)
    return target


# --- injection into a run's harness runtime MCP config -------------------------


def _resolve_refs(connector_name: str, mapping: dict[str, str], kind: str) -> dict[str, str]:
    """Resolve `$VAR`/`${VAR}` refs in an env/header mapping against the
    daemon environment. Unresolvable refs fail closed with a non-secret error
    (the variable *name* is a ref, never a value)."""
    out: dict[str, str] = {}

    def sub(m: re.Match[str]) -> str:
        var = m.group(1) or m.group(2)
        if var not in os.environ:
            raise ConnectorsError(
                f"connector {connector_name!r} {kind} references ${var}, which is not set in the"
                " daemon environment — set it there or use a literal value (values are never exposed)"
            )
        return os.environ[var]

    for k, v in mapping.items():
        out[k] = _REF_RE.sub(sub, v)
    return out


def mcp_config_entry(name: str, entry: StdioConnector | HttpsConnector) -> dict[str, Any]:
    """The `[mcp_servers.NAME]` entry for a run's vibe config, with env/header
    refs resolved (the resolved values land only in the ephemeral workspace
    config, never in a payload/log/audit row)."""
    if entry.transport == "stdio":
        assert isinstance(entry, StdioConnector)
        cfg: dict[str, Any] = {"command": entry.command, "args": list(entry.args)}
        env = _resolve_refs(name, entry.env, "env")
        if env:
            cfg["env"] = env
        return cfg
    assert isinstance(entry, HttpsConnector)
    cfg = {"url": entry.url}
    headers = _resolve_refs(name, entry.headers, "header")
    if headers:
        cfg["headers"] = headers
    return cfg


def attach_connectors_to_run(run: Any, settings: Any, names: list[str] | None = None) -> list[str]:
    """Spec 28: inject the egg's opted-in gates into the run's harness runtime
    MCP config — `[mcp_servers."NAME"]` entries appended to the run's vibe
    config (`<workspace>/.vibe/config.toml`, merged with a spec-27 Farmhand
    entry when both are present). Returns the attached names (manifest gets
    names only). Raises ConnectorsError (unknown gate / unresolvable ref) —
    the run fails closed at provisioning with an attested teardown."""
    if names is None:
        names = list(getattr(run.spec.harness, "connectors", []) or [])
    names = list(dict.fromkeys(names))
    if not names:
        return []
    registry = load_connectors(settings.connectors_path, settings.data_dir)
    entries: list[tuple[str, dict[str, Any]]] = []
    for name in names:
        entry = registry.connectors.get(name)
        if entry is None:
            raise ConnectorsError(
                f"unknown connector {name!r} — known connectors:"
                f" {', '.join(sorted(registry.connectors)) or '(none)'}"
            )
        entries.append((name, mcp_config_entry(name, entry)))
    vibe_dir = run.workspace / ".vibe"
    vibe_dir.mkdir(parents=True, exist_ok=True)
    config_path = vibe_dir / "config.toml"
    existing = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    lines = ["", "# spec 28: gates (registered connectors) this chicken opted into via harness.connectors", ""]
    for name, cfg in entries:
        lines.append(f"[mcp_servers.{_key(name)}]")
        for k, v in cfg.items():
            lines.append(f"{_key(k)} = {_toml_value(v)}")
        lines.append("")
    text = existing
    if text and not text.endswith("\n"):
        text += "\n"
    config_path.write_text(text + "\n".join(lines), encoding="utf-8")
    return names
