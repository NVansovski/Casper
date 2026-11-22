"""Microsoft Graph access: MSAL device-code sign-in with a persistent token cache.

``auth_microsoft.py`` performs the one-time interactive sign-in. Afterwards the
tools obtain access tokens silently from the cache (``.msal_cache.json``) and
call the Graph REST API through :class:`GraphClient`.
"""

from __future__ import annotations

import logging
import os
import threading
from functools import lru_cache
from pathlib import Path
from typing import Any

import httpx
import msal

from config import get_settings
from tools.base import ToolError

logger = logging.getLogger(__name__)

GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
# MSAL adds the reserved scopes (openid, profile, offline_access) on its own.
GRAPH_SCOPES = ["Calendars.ReadWrite", "Tasks.ReadWrite", "Mail.Read", "User.Read"]
DEFAULT_TENANT = "common"
REQUEST_TIMEOUT_S = 15.0

NOT_SIGNED_IN_MESSAGE = (
    "Ich bin noch nicht mit deinem Microsoft-Konto verbunden. "
    "Bitte führe einmalig auf dem Computer das Skript auth_microsoft.py aus."
)
UNREACHABLE_MESSAGE = "Microsoft ist gerade nicht erreichbar. Versuch es später noch einmal."


class GraphAuthError(ToolError):
    """Authentication with Microsoft failed. The message is German and spoken."""


# Common Entra ID error codes and what the user has to change.
_AUTH_ERROR_HINTS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("AADSTS700016", "AADSTS700038", "AADSTS70001"), "Die MS_CLIENT_ID ist ungültig oder die App existiert nicht. Prüf die Anwendungs-ID in Azure."),
    (("AADSTS7000218", "AADSTS70002"), "Aktiviere in Azure unter Authentifizierung die Option \"Öffentliche Clientflows zulassen\"."),
    (("AADSTS90002", "AADSTS50059", "AADSTS900023"), "Die MS_TENANT_ID passt nicht. Prüf die Verzeichnis-ID oder lass sie für ein privates Konto leer."),
    (("AADSTS65001", "AADSTS650057"), "Der App fehlt die Zustimmung zu den Berechtigungen. Füge sie in Azure hinzu oder bestätige sie bei der Anmeldung."),
    (("AADSTS70016",), "Der Code ist abgelaufen. Starte die Anmeldung noch einmal."),
    (("AADSTS70043", "AADSTS700084"), "Die Anmeldung ist abgelaufen. Bitte melde dich noch einmal an."),
)


def explain_auth_error(description: str) -> str:
    """Turn an Entra ID error text into a short German hint, without trace IDs."""
    for codes, hint in _AUTH_ERROR_HINTS:
        if any(code in description for code in codes):
            return hint
    first_line = description.splitlines()[0] if description else "unbekannter Fehler"
    return first_line.split(" Trace ID")[0].strip()


class GraphAuth:
    """Wraps an MSAL public client with a file-backed, serialisable token cache."""

    def __init__(
        self,
        client_id: str,
        tenant_id: str,
        cache_path: Path,
        *,
        app: Any | None = None,
    ) -> None:
        self._client_id = client_id
        self._authority = f"https://login.microsoftonline.com/{tenant_id or DEFAULT_TENANT}"
        self._cache_path = cache_path
        self._cache = msal.SerializableTokenCache()
        self._app = app  # created lazily: building it contacts Microsoft
        self._lock = threading.RLock()
        self._load_cache()

    @property
    def cache_path(self) -> Path:
        return self._cache_path

    def account_name(self) -> str | None:
        """User name of the cached account, if somebody is signed in."""
        with self._lock:
            accounts = self._get_app().get_accounts()
        return str(accounts[0].get("username")) if accounts else None

    def acquire_token_silent(self) -> str | None:
        """Return a valid access token from the cache or ``None`` if sign-in is needed."""
        with self._lock:
            app = self._get_app()
            accounts = app.get_accounts()
            if not accounts:
                return None
            try:
                result = app.acquire_token_silent(GRAPH_SCOPES, account=accounts[0])
            except Exception as exc:
                logger.error("Silent token acquisition failed: %s", exc)
                raise GraphAuthError(UNREACHABLE_MESSAGE) from exc
            self._persist()
        if result and "access_token" in result:
            return str(result["access_token"])
        if result:
            logger.warning("Silent token refresh rejected: %s", result.get("error"))
        return None

    def start_device_flow(self) -> dict[str, Any]:
        """Begin the OAuth2 device code flow. The result holds ``user_code`` and ``verification_uri``."""
        try:
            flow: dict[str, Any] = self._get_app().initiate_device_flow(scopes=GRAPH_SCOPES)
        except Exception as exc:
            logger.error("Could not start the device flow: %s", exc)
            raise GraphAuthError(UNREACHABLE_MESSAGE) from exc
        if "user_code" not in flow:
            detail = str(flow.get("error_description") or flow.get("error") or "")
            raise GraphAuthError(f"Microsoft hat die Anmeldung abgelehnt. {explain_auth_error(detail)}")
        return flow

    def complete_device_flow(self, flow: dict[str, Any]) -> dict[str, Any]:
        """Block until the user has signed in (or the code expires) and cache the tokens."""
        try:
            result: dict[str, Any] = self._get_app().acquire_token_by_device_flow(flow)
        except Exception as exc:
            logger.error("Device flow failed: %s", exc)
            raise GraphAuthError(UNREACHABLE_MESSAGE) from exc
        with self._lock:
            self._persist()
        return result

    def sign_out(self) -> bool:
        """Remove the cached accounts and the cache file. Returns ``True`` if anything was removed."""
        removed = False
        with self._lock:
            try:
                app = self._get_app()
                for account in app.get_accounts():
                    app.remove_account(account)
                    removed = True
            except GraphAuthError:
                logger.info("Skipping account removal: Microsoft not reachable")
            if self._cache_path.exists():
                self._cache_path.unlink()
                removed = True
        return removed

    def _get_app(self) -> Any:
        if self._app is None:
            try:
                self._app = msal.PublicClientApplication(
                    self._client_id, authority=self._authority, token_cache=self._cache
                )
            except Exception as exc:  # network error, or a malformed client/tenant ID
                logger.error("Could not initialise MSAL: %s", exc)
                raise GraphAuthError(
                    "Ich konnte die Microsoft-Anmeldung nicht starten. Prüf bitte die "
                    "Internetverbindung sowie MS_CLIENT_ID und MS_TENANT_ID."
                ) from exc
        return self._app

    def _load_cache(self) -> None:
        if not self._cache_path.exists():
            return
        try:
            self._cache.deserialize(self._cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("Ignoring unreadable token cache %s: %s", self._cache_path, exc)

    def _persist(self) -> None:
        if not self._cache.has_state_changed:
            return
        temp_path = self._cache_path.with_name(self._cache_path.name + ".tmp")
        try:
            temp_path.write_text(self._cache.serialize(), encoding="utf-8")
            try:
                os.chmod(temp_path, 0o600)  # the cache holds a refresh token
            except OSError:
                pass  # not supported on every platform or file system
            os.replace(temp_path, self._cache_path)
            self._cache.has_state_changed = False
        except OSError as exc:
            logger.error("Could not save the token cache: %s", exc)


class GraphClient:
    """Minimal authenticated Graph REST client. All failures become German ``ToolError``s."""

    def __init__(self, auth: GraphAuth, timezone_name: str) -> None:
        self._auth = auth
        self._timezone_name = timezone_name

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._request("GET", path, params=params)

    def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", path, json_body=body)

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        token = self._auth.acquire_token_silent()
        if token is None:
            raise GraphAuthError(NOT_SIGNED_IN_MESSAGE)
        headers = {
            "Authorization": f"Bearer {token}",
            # Return calendar times in the user's timezone instead of UTC.
            "Prefer": f'outlook.timezone="{self._timezone_name}"',
        }
        try:
            with httpx.Client(timeout=REQUEST_TIMEOUT_S) as client:
                response = client.request(
                    method, f"{GRAPH_BASE_URL}{path}", headers=headers, params=params, json=json_body
                )
        except httpx.HTTPError as exc:
            logger.error("Graph request %s %s failed: %s", method, path, exc)
            raise ToolError(UNREACHABLE_MESSAGE) from exc

        if response.status_code >= 400:
            logger.error("Graph %s %s -> HTTP %s: %s", method, path, response.status_code, response.text[:300])
            raise ToolError(_message_for_status(response.status_code))
        return dict(response.json()) if response.content else {}


def _message_for_status(status: int) -> str:
    if status == 401:
        return NOT_SIGNED_IN_MESSAGE
    if status == 403:
        return (
            "Dafür fehlt mir die Berechtigung. Prüf bitte die App-Rechte in Azure "
            "und melde dich mit auth_microsoft.py neu an."
        )
    if status == 404:
        return "Das habe ich bei Microsoft nicht gefunden."
    if status == 429:
        return "Microsoft bremst mich gerade aus. Versuch es in einer Minute noch einmal."
    return "Microsoft hat einen Fehler gemeldet. Versuch es später noch einmal."


@lru_cache(maxsize=1)
def get_graph_auth() -> GraphAuth:
    """Process-wide MSAL wrapper built from the settings."""
    settings = get_settings()
    return GraphAuth(settings.ms_client_id.strip(), settings.ms_tenant_id.strip(), settings.msal_cache_file)


@lru_cache(maxsize=1)
def get_graph_client() -> GraphClient:
    """Process-wide Graph client built from the settings."""
    return GraphClient(get_graph_auth(), get_settings().timezone)
