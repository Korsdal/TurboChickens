"""Seat/subscription auth: the credential broker (spec 09).

Providers that bill via a seat/subscription (e.g. a Mistral Le Chat seat with
OAuth device login) declare ``auth = "seat"`` in providers.toml. This module
implements the ``CredentialProvider`` extension point for them: the broker
keeps the seat session under ``<data_dir>/seats/`` (never in job specs or the
audit log, files 0600), mints/refreshes short-lived access tokens before a
run, and injects the token per-run under the provider's usual ``api_key_env``
name — so the adapters and the ``build_per_run_env`` allowlist discipline are
unchanged, and no ambient secrets leak.

Only non-secret telemetry leaves this module: the auth mode and the token
expiry (never the token itself) via ``auth_telemetry``, which the adapters
record in the run manifest and the runner in the audit log.

The device flow (``tc auth login <provider>``) is the only interactive part;
token refresh is automatic and fail-closed (a revoked seat fails the run with
a clear error, never a request without a valid credential).
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import httpx

from turbochickens.adapter import DEFAULT_CREDENTIAL_ALLOWLIST, RunContext
from turbochickens.providers import ProviderProfile, ProvidersConfig, ProvidersError
from turbochickens.settings import Settings

#: refresh a token this many seconds before its stated expiry
EXPIRY_LEEWAY_SEC = 30.0

DEVICE_CODE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"


class SeatError(Exception):
    """Seat auth is unavailable/misconfigured — runs fail closed on this."""


def _iso(epoch: float) -> str:
    """Epoch seconds → the same ISO-8601 UTC ms format as utcnow_iso()."""
    return (
        datetime.fromtimestamp(epoch, tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


@dataclass
class DeviceGrant:
    """An in-flight OAuth device-flow grant (spec 09/19). The `device_code`
    is the grant credential — it stays server-side (daemon memory), it is
    never returned to a caller or written to the audit log. Only the
    user-facing `user_code` + `verification_uri` leave the broker (CLI
    prints them; the GUI relays them to the browser)."""

    provider: str
    device_code: str
    user_code: str
    verification_uri: str
    interval: float
    expires_at: float  # epoch seconds

    @property
    def expired(self) -> bool:
        return time.time() >= self.expires_at


@dataclass
class SeatSession:
    """One provider's stored seat session. The access/refresh tokens are
    secrets: they live only in the 0600 file, never in manifests/audit."""

    provider: str
    access_token: str
    refresh_token: str | None
    expires_at: float  # epoch seconds
    token_type: str = "Bearer"
    scope: str | None = None

    @property
    def expired(self) -> bool:
        return time.time() >= self.expires_at - EXPIRY_LEEWAY_SEC

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "token_type": self.token_type,
            "scope": self.scope,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SeatSession":
        return cls(
            provider=str(data["provider"]),
            access_token=str(data["access_token"]),
            refresh_token=data.get("refresh_token"),
            expires_at=float(data["expires_at"]),
            token_type=str(data.get("token_type", "Bearer")),
            scope=data.get("scope"),
        )


class SeatStore:
    """On-disk seat sessions under ``<data_dir>/seats/`` — one 0600 JSON file
    per provider. This is the only place tokens are stored."""

    def __init__(self, data_dir: Path) -> None:
        self.dir = Path(data_dir) / "seats"

    def _path(self, provider: str) -> Path:
        return self.dir / f"{provider}.json"

    def _ensure_dir(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)

    def save(self, session: SeatSession) -> None:
        self._ensure_dir()
        path = self._path(session.provider)
        payload = json.dumps(session.to_dict(), ensure_ascii=False, indent=2) + "\n"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, payload.encode("utf-8"))
        finally:
            os.close(fd)
        os.chmod(path, 0o600)  # in case the file pre-existed with other perms

    def load(self, provider: str) -> SeatSession | None:
        """Load a session, None when absent. A corrupt/unreadable file raises
        SeatError — fail closed rather than silently re-logging-in."""
        path = self._path(provider)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return SeatSession.from_dict(data)
        except (OSError, ValueError, KeyError, TypeError) as e:
            raise SeatError(f"seat session for provider {provider!r} is unreadable ({path}): {e}; run `tc auth login {provider}` again") from e

    def delete(self, provider: str) -> bool:
        path = self._path(provider)
        if path.exists():
            path.unlink()
            return True
        return False

    def list_sessions(self) -> list[SeatSession]:
        if not self.dir.exists():
            return []
        out = []
        for p in sorted(self.dir.glob("*.json")):
            sess = self.load(p.stem)
            if sess is not None:
                out.append(sess)
        return out


class SeatBroker:
    """Credential broker for seat providers, slotting into the existing
    ``CredentialProvider`` protocol (baseline v0.2 §7 extension point).

    - ``credentials(run)`` — for a seat provider, returns the current access
      token under the provider's ``api_key_env`` (refreshing first when
      expired); for an ``api-key`` provider (or a non-API adapter such as
      vibe-cli), falls back to the env allowlist, exactly like the MVP
      provider. Fail-closed: no valid seat session → SeatError, no credential.
    - ``auth_telemetry(run)`` — non-secret ``{"mode", "token_expires_at"}``
      for the run manifest / audit log.
    - ``login(provider)`` — OAuth device flow, storing the session.

    HTTP here is synchronous (``prepare`` is sync); only the rare refresh and
    the interactive login block, each bounded by ``http_timeout_sec``.
    """

    def __init__(
        self,
        settings: Settings,
        providers: ProvidersConfig,
        client: httpx.Client | None = None,
        http_timeout_sec: float = 15.0,
        env_allowlist: tuple[str, ...] = DEFAULT_CREDENTIAL_ALLOWLIST,
    ) -> None:
        self.settings = settings
        self.providers = providers
        self.store = SeatStore(settings.data_dir)
        self.client = client  # test seam: inject a client (e.g. MockTransport)
        self.http_timeout_sec = http_timeout_sec
        self.env_allowlist = env_allowlist

    # --- CredentialProvider protocol ----------------------------------------

    def credentials(self, run: RunContext) -> dict[str, str]:
        if run.spec.harness.adapter == "mistral-api":
            provider_name, _, provider, _ = self.providers.resolve(run.spec.harness.model)
            if provider.auth == "seat":
                session = self._valid_session(provider_name, provider)
                # the access token rides under the same env var an api-key
                # provider would use — the adapter needs no seat-specific code
                return {provider.api_key_env: session.access_token}
            allow = tuple(dict.fromkeys((*self.env_allowlist, provider.api_key_env)))
            return {k: v for k in allow if (v := os.environ.get(k)) is not None}
        # vibe-cli and any future subprocess adapter: env allowlist only. A
        # seat-configured vibe profile is picked up by the vibe binary itself
        # from ~/.vibe/config.toml (HOME/XDG_* pass through BASE_ENV_KEYS).
        return {k: v for k in self.env_allowlist if (v := os.environ.get(k)) is not None}

    def auth_telemetry(self, run: RunContext) -> dict[str, Any]:
        """Non-secret auth facts for the manifest/audit: mode + token expiry."""
        if run.spec.harness.adapter == "mistral-api":
            try:
                provider_name, _, provider, _ = self.providers.resolve(run.spec.harness.model)
            except ProvidersError:
                return {"mode": "api-key", "token_expires_at": None}
            if provider.auth == "seat":
                try:
                    session = self.store.load(provider_name)
                except SeatError:
                    session = None
                return {
                    "mode": "seat",
                    "token_expires_at": _iso(session.expires_at) if session else None,
                }
            return {"mode": "api-key", "token_expires_at": None}
        present = any(os.environ.get(k) is not None for k in self.env_allowlist)
        return {"mode": "api-key" if present else "none", "token_expires_at": None}

    def bearer_token(self, provider_name: str) -> str:
        """The current access token for a seat provider (refreshed first when
        expired) — for out-of-band authenticated calls like `tc models list`
        hitting the provider's /models. Raises SeatError for api-key providers
        (use the env var there) and whenever no valid session exists; the
        token itself is only ever returned, never logged."""
        provider = self.providers.providers.get(provider_name)
        if provider is None:
            raise SeatError(
                f"unknown provider {provider_name!r}; known providers: {', '.join(sorted(self.providers.providers)) or '(none)'}"
            )
        if provider.auth != "seat":
            raise SeatError(
                f"provider {provider_name!r} uses auth='api-key'; no seat token — set ${provider.api_key_env} instead"
            )
        return self._valid_session(provider_name, provider).access_token

    # --- session lifecycle ---------------------------------------------------

    def _valid_session(self, provider_name: str, provider: ProviderProfile) -> SeatSession:
        session = self.store.load(provider_name)
        if session is None:
            raise SeatError(
                f"no seat session for provider {provider_name!r}; run `tc auth login {provider_name}` first"
            )
        if session.expired:
            session = self._refresh(provider_name, provider, session)
        return session

    def _http(self) -> tuple[httpx.Client, bool]:
        if self.client is not None:
            return self.client, False
        return httpx.Client(timeout=self.http_timeout_sec), True

    def _refresh(self, provider_name: str, provider: ProviderProfile, session: SeatSession) -> SeatSession:
        """Refresh an expired access token. Fail closed on revocation: the
        stored session is deleted and SeatError raised — the run never sends
        a request with a stale credential."""
        assert provider.oauth_token_url and provider.oauth_client_id
        if not session.refresh_token:
            self.store.delete(provider_name)
            raise SeatError(
                f"seat session for provider {provider_name!r} is expired and has no refresh token; "
                f"run `tc auth login {provider_name}` again"
            )
        client, owned = self._http()
        try:
            resp = client.post(
                provider.oauth_token_url,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": session.refresh_token,
                    "client_id": provider.oauth_client_id,
                },
            )
        except httpx.TransportError as e:
            raise SeatError(f"token endpoint for provider {provider_name!r} unreachable: {e}") from e
        finally:
            if owned:
                client.close()
        if resp.status_code != 200:
            self.store.delete(provider_name)
            raise SeatError(
                f"seat session for provider {provider_name!r} is revoked or expired "
                f"(token endpoint http {resp.status_code}: {resp.text[:200]}); "
                f"run `tc auth login {provider_name}` again"
            )
        session = self._session_from_token(provider_name, resp.json(), fallback_refresh=session.refresh_token)
        self.store.save(session)
        return session

    def _session_from_token(
        self, provider_name: str, payload: dict[str, Any], fallback_refresh: str | None = None
    ) -> SeatSession:
        access_token = payload.get("access_token")
        if not access_token:
            raise SeatError(f"token endpoint for provider {provider_name!r} returned no access_token")
        expires_in = int(payload.get("expires_in", 3600) or 3600)
        return SeatSession(
            provider=provider_name,
            access_token=str(access_token),
            refresh_token=payload.get("refresh_token") or fallback_refresh,
            expires_at=time.time() + expires_in,
            token_type=str(payload.get("token_type", "Bearer")),
            scope=payload.get("scope"),
        )

    # --- device flow (tc auth login / spec 19 GUI login) ----------------------

    def _seat_provider(self, provider_name: str) -> ProviderProfile:
        provider = self.providers.providers.get(provider_name)
        if provider is None:
            raise SeatError(
                f"unknown provider {provider_name!r}; known providers: {', '.join(sorted(self.providers.providers)) or '(none)'}"
            )
        if provider.auth != "seat":
            raise SeatError(
                f"provider {provider_name!r} uses auth='api-key'; no seat login — set ${provider.api_key_env} instead"
            )
        assert provider.oauth_device_url and provider.oauth_token_url and provider.oauth_client_id
        return provider

    def start_device_flow(self, provider_name: str) -> DeviceGrant:
        """Start the OAuth device flow: POST the device endpoint and return
        the grant. Only the user-facing user_code + verification_uri ride on
        the grant; the device_code stays inside the broker (and, for the GUI
        path, in daemon memory) — never in a response or the audit log."""
        provider = self._seat_provider(provider_name)
        assert provider.oauth_device_url and provider.oauth_token_url and provider.oauth_client_id
        client, owned = self._http()
        try:
            resp = client.post(
                provider.oauth_device_url,
                data={"client_id": provider.oauth_client_id, "scope": provider.oauth_scope or ""},
            )
            if resp.status_code != 200:
                raise SeatError(
                    f"device endpoint for provider {provider_name!r} http {resp.status_code}: {resp.text[:200]}"
                )
            dev = resp.json()
            device_code = dev.get("device_code")
            user_code = dev.get("user_code", "")
            verification_uri = dev.get("verification_uri") or dev.get("verification_uri_complete") or ""
            interval = float(dev.get("interval", 5) or 5)
            expires_in = float(dev.get("expires_in", 1800) or 1800)
            if not device_code or not verification_uri:
                raise SeatError(f"device endpoint for provider {provider_name!r} returned an incomplete device grant")
            return DeviceGrant(
                provider=provider_name,
                device_code=str(device_code),
                user_code=str(user_code),
                verification_uri=str(verification_uri),
                interval=interval,
                expires_at=time.time() + expires_in,
            )
        except httpx.TransportError as e:
            raise SeatError(f"OAuth endpoint for provider {provider_name!r} unreachable: {e}") from e
        finally:
            if owned:
                client.close()

    def poll_device_flow(self, grant: DeviceGrant) -> dict[str, Any]:
        """One poll step of the device flow. Returns ``{"status": "pending"}``
        (authorization_pending, or slow_down — which also widens
        ``grant.interval``) or ``{"status": "authorized", "session": …}`` —
        on success the session is stored 0600 exactly as the CLI path.
        Raises SeatError on a hard failure (rejected grant, transport)."""
        provider = self.providers.providers[grant.provider]
        assert provider.oauth_token_url and provider.oauth_client_id
        client, owned = self._http()
        try:
            resp = client.post(
                provider.oauth_token_url,
                data={
                    "grant_type": DEVICE_CODE_GRANT,
                    "device_code": grant.device_code,
                    "client_id": provider.oauth_client_id,
                },
            )
        except httpx.TransportError as e:
            raise SeatError(f"token endpoint for provider {grant.provider!r} unreachable: {e}") from e
        finally:
            if owned:
                client.close()
        if resp.status_code == 200:
            session = self._session_from_token(grant.provider, resp.json())
            self.store.save(session)
            return {"status": "authorized", "session": session}
        try:
            error = resp.json().get("error", "")
        except ValueError:
            error = resp.text[:200]
        if error == "authorization_pending":
            return {"status": "pending"}
        if error == "slow_down":
            grant.interval = min(grant.interval * 1.5, 30.0)
            return {"status": "pending"}
        raise SeatError(
            f"device login for provider {grant.provider!r} failed (http {resp.status_code}: {error})"
        )

    def login(self, provider_name: str, notify: Callable[[str], None] = print) -> SeatSession:
        """Run the OAuth device flow for a seat provider and store the session.
        Prints the verification URI + user code via `notify`, then polls the
        token endpoint until the user authorizes, it expires, or times out.
        Composed from `start_device_flow` + `poll_device_flow` — the same two
        steps the spec-19 GUI path drives one request at a time."""
        grant = self.start_device_flow(provider_name)
        notify(f"authorize TurboChickens: open {grant.verification_uri} and enter code {grant.user_code}")
        while not grant.expired:
            time.sleep(max(grant.interval, 0.05))
            outcome = self.poll_device_flow(grant)
            if outcome["status"] == "authorized":
                return outcome["session"]
        raise SeatError(f"device login for provider {provider_name!r} timed out (device code expired)")

    def logout(self, provider_name: str) -> None:
        if not self.store.delete(provider_name):
            raise SeatError(f"no seat session stored for provider {provider_name!r}")

    def status(self) -> list[dict[str, Any]]:
        """Non-secret overview for `tc auth status`: each provider's auth mode
        plus its stored session's expiry (never any token material)."""
        rows: list[dict[str, Any]] = []
        for name, provider in sorted(self.providers.providers.items()):
            row: dict[str, Any] = {"provider": name, "mode": provider.auth, "session": None}
            if provider.auth == "seat":
                try:
                    sess = self.store.load(name)
                except SeatError as e:
                    row["session_error"] = str(e)
                else:
                    if sess is not None:
                        row["session"] = {
                            "expires_at": _iso(sess.expires_at),
                            "expired": sess.expired,
                            "token_type": sess.token_type,
                        }
            rows.append(row)
        return rows
