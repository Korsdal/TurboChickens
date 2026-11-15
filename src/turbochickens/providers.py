"""Platform-level provider registry: `providers.toml` (spec 05).

Provider/model presets and per-token pricing live in config, not code, so
adding another OpenAI-compatible provider is a config change (baseline v0.2
§6/§7). A job picks a provider implicitly: `model = "tensorx/foo"` selects
provider `tensorx` with model `foo`; a bare model name selects the adapter's
home provider (`mistral` for `mistral-api`).

Resolution order for the file:

1. explicit path (e.g. ``Settings.providers_path``)
2. ``$TURBOCHICKENS_PROVIDERS``
3. ``<data_dir>/providers.toml`` if it exists
4. built-in defaults (Mistral presets below)

The file overlays the defaults **per provider**: a ``[providers.X]`` table
replaces the built-in provider `X` wholesale (base_url, key env, models);
providers not mentioned keep their defaults.

Pricing is per million tokens in USD (``input_usd_per_mtok`` /
``output_usd_per_mtok``) and is consumed for per-run `cost_usd` telemetry now
and per-job expected-cost stats in spec 08. ``context_window`` feeds the
artifact/context caps of spec 07.
"""

from __future__ import annotations

import copy
import ipaddress
import os
import tomllib
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

PROVIDERS_ENVVAR = "TURBOCHICKENS_PROVIDERS"
DEFAULT_DATA_DIR = Path("~/.turbochickens").expanduser()

#: The adapter's home provider when the job's model has no "provider/" prefix.
DEFAULT_PROVIDER = "mistral"


class ProvidersError(Exception):
    """providers.toml missing, unreadable, or invalid."""


def url_scheme(url: str) -> str:
    """The lowercased scheme of `url` ("" when unparseable/schemeless)."""
    try:
        return (urlsplit(url).scheme or "").lower()
    except ValueError:
        return ""


def url_host(url: str) -> str:
    """The lowercased host of `url` ("" when absent/unparseable)."""
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def is_loopback_host(host: str) -> bool:
    """True for `localhost` (RFC 6761) and any loopback IP (127.0.0.0/8, ::1).
    Loopback http is a secure context (like browsers) and stays allowed for
    hermetic local testing; it never leaves the host."""
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def oauth_endpoint_scheme_error(url: str) -> str | None:
    """None when `url` is acceptable as a Feeder OAuth endpoint; otherwise a
    refusal message. OAuth device/token endpoints must be https — plain http
    is allowed only on loopback hosts (never across the network), and no other
    scheme (javascript:, data:, ftp:, …) is accepted, so a misconfigured or
    hostile auth server cannot be aimed at a non-TLS or scriptable target."""
    scheme, host = url_scheme(url), url_host(url)
    if scheme == "https":
        return None
    if scheme == "http" and is_loopback_host(host):
        return None
    return f"must be an https URL (http allowed only on loopback hosts, e.g. 127.0.0.1); got scheme {scheme!r} host {host!r}"


def verification_uri_scheme_error(uri: str) -> str | None:
    """None when `uri` may be relayed to an operator as the device-flow
    verification link; otherwise a refusal message. The verification URI is
    rendered as a link in the coop, so only https is accepted — a hostile or
    misconfigured auth server must not be able to inject a `javascript:` (or
    any other non-https) URI that would execute in the coop origin."""
    scheme = url_scheme(uri)
    if scheme == "https":
        return None
    return f"scheme {scheme!r} is not https — only https verification links are relayed"


class ModelProfile(BaseModel):
    """One model's preset: context window, pricing, JSON-mode support."""

    model_config = ConfigDict(extra="forbid")

    context_window: int = Field(gt=0, description="tokens; feeds context/artifact caps (spec 07)")
    input_usd_per_mtok: float = Field(ge=0, description="USD per million input tokens")
    output_usd_per_mtok: float = Field(ge=0, description="USD per million output tokens")
    json_mode: bool = Field(default=True, description="supports response_format json_object")


class ProviderProfile(BaseModel):
    """One OpenAI-compatible provider: endpoint, credential env var, models.

    Auth (spec 09): `auth = "api-key"` (default, unchanged spec-05 behavior)
    injects the `api_key_env` var from the daemon environment. `auth = "seat"`
    bills via a seat/subscription (OAuth device flow): the credential broker
    (seat.py) keeps a session under the data dir, mints/refreshes short-lived
    access tokens, and injects the token under the same `api_key_env` name, so
    adapters need no seat-specific code. The `oauth_*` fields locate the
    provider's device-authorization and token endpoints and are required for
    `auth = "seat"`, rejected for `auth = "api-key"`. Both OAuth endpoints must
    be https — plain http is accepted only on loopback hosts (hermetic local
    testing); any other scheme is refused (spec 32, review finding G7).
    """

    model_config = ConfigDict(extra="forbid")

    base_url: str = Field(min_length=1, description="OpenAI-compatible base, e.g. https://api.mistral.ai/v1")
    api_key_env: str = Field(default="MISTRAL_API_KEY", min_length=1)
    auth: Literal["api-key", "seat"] = "api-key"
    oauth_device_url: str | None = Field(default=None, description="OAuth device-authorization endpoint (seat auth)")
    oauth_token_url: str | None = Field(default=None, description="OAuth token endpoint (seat auth)")
    oauth_client_id: str | None = Field(default=None, description="OAuth client id registered with the provider (seat auth)")
    oauth_scope: str | None = Field(default=None, description="OAuth scope to request (seat auth)")
    models: dict[str, ModelProfile]

    @model_validator(mode="after")
    def _check_auth_fields(self) -> "ProviderProfile":
        oauth_fields = ("oauth_device_url", "oauth_token_url", "oauth_client_id", "oauth_scope")
        if self.auth == "seat":
            missing = [f for f in oauth_fields[:3] if not getattr(self, f)]
            if missing:
                raise ValueError(f"auth='seat' requires: {', '.join(missing)}")
        else:
            present = [f for f in oauth_fields if getattr(self, f)]
            if present:
                raise ValueError(f"{', '.join(present)} only valid with auth='seat' (this provider is auth='api-key')")
        # spec 32 (G7): OAuth endpoints are https-only — http only on loopback
        # (hermetic local testing), never any other scheme. Field-named.
        for f in ("oauth_device_url", "oauth_token_url"):
            url = getattr(self, f)
            if url:
                err = oauth_endpoint_scheme_error(url)
                if err:
                    raise ValueError(f"{f} {err}: {url!r}")
        return self


class ProvidersConfig(BaseModel):
    """The whole registry. Unknown keys are rejected like in the job spec."""

    model_config = ConfigDict(extra="forbid")

    providers: dict[str, ProviderProfile]

    def resolve(self, model: str, default_provider: str = DEFAULT_PROVIDER) -> tuple[str, str, ProviderProfile, ModelProfile]:
        """Resolve a job's `model` to (provider_name, model_name, provider, model_profile).

        ``"provider/model"`` picks that provider; a bare name uses
        `default_provider`. Raises ProvidersError when either is unknown.
        """
        if "/" in model:
            provider_name, model_name = model.split("/", 1)
        else:
            provider_name, model_name = default_provider, model
        provider = self.providers.get(provider_name)
        if provider is None:
            raise ProvidersError(
                f"unknown provider {provider_name!r} for model {model!r}; "
                f"known providers: {', '.join(sorted(self.providers)) or '(none)'}"
            )
        profile = provider.models.get(model_name)
        if profile is None:
            raise ProvidersError(
                f"model {model_name!r} not found in providers.toml under provider {provider_name!r}; "
                f"known models: {', '.join(sorted(provider.models)) or '(none)'}"
            )
        return provider_name, model_name, provider, profile

    def cost_usd(self, model_profile: ModelProfile, tokens_in: int, tokens_out: int) -> float:
        """Expected cost of a run from per-token pricing — telemetry, not a budget."""
        return (tokens_in * model_profile.input_usd_per_mtok + tokens_out * model_profile.output_usd_per_mtok) / 1_000_000


#: Built-in presets so the platform works with zero config; a providers.toml
#: overlays these per provider. Prices are USD per million tokens.
DEFAULT_PROVIDERS: dict[str, Any] = {
    "providers": {
        "mistral": {
            "base_url": "https://api.mistral.ai/v1",
            "api_key_env": "MISTRAL_API_KEY",
            "models": {
                "mistral-large-latest": {
                    "context_window": 256000,
                    "input_usd_per_mtok": 2.0,
                    "output_usd_per_mtok": 6.0,
                    "json_mode": True,
                },
                "mistral-small-latest": {
                    "context_window": 128000,
                    "input_usd_per_mtok": 0.2,
                    "output_usd_per_mtok": 0.6,
                    "json_mode": True,
                },
                "ministral-8b-latest": {
                    "context_window": 128000,
                    "input_usd_per_mtok": 0.4,
                    "output_usd_per_mtok": 0.4,
                    "json_mode": True,
                },
                "ministral-3b-latest": {
                    "context_window": 128000,
                    "input_usd_per_mtok": 0.04,
                    "output_usd_per_mtok": 0.04,
                    "json_mode": True,
                },
            },
        },
    }
}


def resolve_providers_path(explicit: str | Path | None = None, data_dir: Path | None = None) -> Path | None:
    """Pick the providers file to load, or None for built-in defaults."""
    if explicit is not None:
        p = Path(explicit)
        if not p.is_file():
            raise ProvidersError(f"providers file not found: {p}")
        return p
    env = os.environ.get(PROVIDERS_ENVVAR)
    if env:
        p = Path(env)
        if not p.is_file():
            raise ProvidersError(f"providers file not found: {p} (from ${PROVIDERS_ENVVAR})")
        return p
    base = data_dir if data_dir is not None else DEFAULT_DATA_DIR
    p = base / "providers.toml"
    return p if p.is_file() else None


def load_providers(explicit: str | Path | None = None, data_dir: Path | None = None) -> ProvidersConfig:
    """Load the provider registry: built-in defaults overlaid per provider with
    the resolved providers.toml file, if any."""
    path = resolve_providers_path(explicit, data_dir)
    data: dict[str, Any] = copy.deepcopy(DEFAULT_PROVIDERS)
    if path is not None:
        try:
            file_data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as e:
            raise ProvidersError(f"cannot load providers from {path}: {e}") from e
        file_providers = file_data.get("providers", {})
        if not isinstance(file_providers, dict):
            raise ProvidersError(f"invalid providers file {path}: top-level 'providers' must be a table")
        data["providers"].update(file_providers)
    try:
        return ProvidersConfig.model_validate(data)
    except ValidationError as e:
        msgs = "; ".join(
            f"{'.'.join(str(x) for x in err['loc']) or '<root>'}: {err['msg']}" for err in e.errors()
        )
        raise ProvidersError(f"invalid providers ({path or 'defaults'}): {msgs}") from e
