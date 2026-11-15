"""Model discovery (spec 10): which models can actually be used right now.

Two sources of truth, both queried on demand per invocation (no daemon
state, no caching):

- **API providers** from `providers.toml`: `GET {base_url}/models`
  (OpenAI-compatible). The call is authenticated with the provider's
  allowlisted ``api_key_env`` var (spec 05) — or, for ``auth = "seat"``
  providers, with the seat broker's current access token (spec 09). No
  credential is ever printed, logged, or included in an error string.
  Live model ids are marked with whether they also have a pricing preset
  in `providers.toml`.
- **The vibe-cli harness**: the models the `vibe` binary itself offers.
  vibe has no "list models" command, so we probe it headless with a
  deliberately unknown ``--model`` and parse the ``Unknown model: ...
  Available: ...`` diagnostic — exactly the list a vibe-cli run validates
  ``--model`` against at spawn time.

`model_issues` backs `tc jobs validate`: a model unknown to the chosen
harness is an error; a mistral-api model without a local pricing preset is
a warning (the live catalog may still offer it — check `tc models list`).
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

from turbochickens.adapter import resolve_vibe_bin, vibe_version
from turbochickens.providers import DEFAULT_PROVIDER, ProviderProfile, ProvidersConfig
from turbochickens.seat import SeatError
from turbochickens.spec.model import JobSpec
from turbochickens.util import http_error_detail

#: Sentinel model for the vibe probe — never a real model, so vibe rejects it
#: before any API call and reports its available list instead.
PROBE_MODEL = "__turbochickens_model_probe__"

MODELS_HTTP_TIMEOUT_SEC = 10.0
VIBE_PROBE_TIMEOUT_SEC = 30.0


@dataclass
class ModelEntry:
    """One live model id from a provider's /models, plus whether providers.toml
    carries a pricing/context preset for it."""

    id: str
    priced: bool


@dataclass
class ProviderCatalog:
    provider: str
    base_url: str
    auth: str
    reachable: bool
    cred: str = "none"  # where the /models credential came from: seat | env | none
    error: str | None = None
    models: list[ModelEntry] = field(default_factory=list)


@dataclass
class VibeCatalog:
    bin: str
    version: str
    reachable: bool
    error: str | None = None
    models: list[str] = field(default_factory=list)


def fetch_models(
    provider: ProviderProfile,
    token: str | None,
    *,
    client: httpx.Client | None = None,
    timeout: float = MODELS_HTTP_TIMEOUT_SEC,
) -> tuple[list[str] | None, str | None]:
    """GET {base_url}/models → (sorted model ids, None) or (None, error).

    The bearer token (allowlisted env var or seat token) is only ever sent in
    the Authorization header — never appears in returned error strings.
    """
    url = provider.base_url.rstrip("/") + "/models"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    owned = client is None
    client = client or httpx.Client(timeout=timeout)
    try:
        resp = client.get(url, headers=headers)
    except httpx.TransportError as e:
        # no URL in the message: a base_url could in principle embed userinfo
        return None, f"transport error ({type(e).__name__})"
    finally:
        if owned:
            client.close()
    if resp.status_code != 200:
        return None, f"http {resp.status_code}: {_error_detail(resp)}"
    try:
        data = resp.json()
        raw = data["data"]
        if not isinstance(raw, list):
            raise TypeError("data is not a list")
        ids = sorted({str(m["id"]) for m in raw if isinstance(m, dict) and m.get("id")})
    except (ValueError, KeyError, TypeError, AttributeError):
        return None, 'malformed /models response (expected {"data": [{"id": ...}]})'
    return ids, None


def _error_detail(resp: httpx.Response) -> str:
    # G10: only the parsed `error` field may surface — the raw body is
    # untrusted feeder bytes (credential echo / terminal escapes), never text.
    return http_error_detail(resp)


#: Credential source for a /models call: the seat broker (spec 09), the
#: allowlisted env var (spec 05), or none (unauthenticated attempt).
TokenResolver = Callable[[str, ProviderProfile], tuple[str | None, str]]


def env_token_resolver() -> TokenResolver:
    """Spec 05 credential handling: the provider's allowlisted `api_key_env`
    from the environment; no key → an unauthenticated attempt (some providers
    serve /models publicly)."""

    def resolve(name: str, provider: ProviderProfile) -> tuple[str | None, str]:
        token = os.environ.get(provider.api_key_env)
        return token, "env" if token else "none"

    return resolve


def list_provider_catalogs(
    providers: ProvidersConfig,
    token_for: TokenResolver,
    *,
    client: httpx.Client | None = None,
    timeout: float = MODELS_HTTP_TIMEOUT_SEC,
) -> list[ProviderCatalog]:
    """One catalog row per provider in providers.toml, sorted by name. A
    provider whose credential resolution fails (e.g. no perch seat) is
    marked unreachable with that error — the listing never aborts, rc stays 0."""
    rows: list[ProviderCatalog] = []
    for name in sorted(providers.providers):
        provider = providers.providers[name]
        try:
            token, source = token_for(name, provider)
        except SeatError as e:
            rows.append(ProviderCatalog(name, provider.base_url, provider.auth, False, "none", str(e)))
            continue
        ids, err = fetch_models(provider, token, client=client, timeout=timeout)
        if err is not None:
            rows.append(ProviderCatalog(name, provider.base_url, provider.auth, False, source, err))
            continue
        presets = provider.models
        rows.append(
            ProviderCatalog(
                provider=name,
                base_url=provider.base_url,
                auth=provider.auth,
                reachable=True,
                cred=source,
                models=[ModelEntry(id=i, priced=i in presets) for i in ids],
            )
        )
    return rows


def vibe_catalog(bin_path: str | None = None, timeout: float = VIBE_PROBE_TIMEOUT_SEC) -> VibeCatalog:
    """The models the vibe binary itself offers, via the unknown-model probe.

    Runs ``vibe --prompt … --model <sentinel>`` headless in a throwaway
    workdir with the base env passthrough (so HOME/VIBE_HOME reach vibe's own
    config); the sentinel is never a valid model, so vibe fails fast — before
    any API call — with ``Unknown model: <sentinel>. Available: <csv>``, the
    same list a vibe-cli run validates `--model` against. When the output
    carries no such list (older/newer vibe, broken binary), the catalog is
    reported unreachable with the output tail as the error.
    """
    bin_path = bin_path or resolve_vibe_bin()
    version = vibe_version(bin_path)
    # Full user env, not the per-run allowlist: this probe runs the user's own
    # vibe binary as the user's shell would (vibe resolves its credentials from
    # the session keyring / env before validating --model). The per-run env
    # discipline applies to job workspaces, not to local diagnostics; tc only
    # parses the "Available:" list and never logs env or credentials.
    env = dict(os.environ)
    with tempfile.TemporaryDirectory(prefix="tc-vibe-probe-") as workdir:
        cmd = [
            bin_path,
            "--prompt",
            "tc model catalog probe",
            "--model",
            PROBE_MODEL,
            "--output",
            "text",
            "--workdir",
            workdir,
        ]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout, env=env, cwd=workdir
            )
        except subprocess.TimeoutExpired:
            return VibeCatalog(bin_path, version, False, f"probe timed out after {timeout:g}s", [])
        except OSError as e:
            return VibeCatalog(bin_path, version, False, f"cannot run {bin_path}: {e}", [])
    out = (proc.stdout or "") + (proc.stderr or "")
    m = re.search(r"Available:\s*(.+)", out)
    if not m:
        lines = [ln for ln in out.strip().splitlines() if ln.strip()]
        tail = lines[-1][:200] if lines else f"exit {proc.returncode}"
        return VibeCatalog(bin_path, version, False, f"no model list in probe output: {tail}", [])
    models = [x.strip() for x in m.group(1).split(",") if x.strip()]
    return VibeCatalog(bin_path, version, True, None, models)


def model_issues(
    spec: JobSpec,
    providers: ProvidersConfig,
    vibe_models: list[str] | None = None,
) -> tuple[list[str], list[str]]:
    """(errors, warnings) for `harness.model` against the chosen harness.

    - mistral-api: an unknown *provider* is an error (no endpoint/credential
      is configured, the harness cannot address it); a known provider with no
      pricing preset for the model is a warning — the live catalog may still
      offer it (`tc models list`), and adding a preset makes it runnable.
    - vibe-cli: when the vibe catalog was probed successfully and the model is
      not in it, that is an error naming the models vibe does offer. When the
      catalog could not be determined, no check is made.
    """
    errors: list[str] = []
    warnings: list[str] = []
    model = spec.harness.model
    if spec.harness.adapter == "mistral-api":
        if "/" in model:
            provider_name, model_name = model.split("/", 1)
        else:
            provider_name, model_name = DEFAULT_PROVIDER, model
        provider = providers.providers.get(provider_name)
        if provider is None:
            known = ", ".join(sorted(providers.providers)) or "(none)"
            errors.append(
                f"harness.model: unknown provider {provider_name!r} for model {model!r}; "
                f"known providers: {known} — add it to providers.toml (see `tc models list`)"
            )
        elif model_name not in provider.models:
            warnings.append(
                f"harness.model: model {model_name!r} has no pricing preset under provider "
                f"{provider_name!r} in providers.toml; the live catalog may still offer it — "
                f"check `tc models list` and add a preset to run it"
            )
    elif spec.harness.adapter == "vibe-cli" and vibe_models is not None:
        if model not in vibe_models:
            errors.append(
                f"harness.model: unknown model {model!r} for adapter 'vibe-cli'; "
                f"the vibe binary offers: {', '.join(vibe_models)}"
            )
    return errors, warnings
