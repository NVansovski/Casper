"""Einmalige Microsoft-Anmeldung für Casper (OAuth2 Device Code Flow).

Aufruf:
    python auth_microsoft.py            # anmelden (oder Anmeldung prüfen)
    python auth_microsoft.py --logout   # gespeicherte Anmeldung löschen

Die Tokens landen in ``.msal_cache.json`` (siehe .gitignore). Danach erneuert
Casper den Zugriff selbstständig, ohne dass du dich erneut anmelden musst.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

import httpx

from config import get_settings
from tools.base import ToolError
from tools.graph_client import (
    GRAPH_BASE_URL,
    GRAPH_SCOPES,
    GraphAuth,
    explain_auth_error,
    get_graph_auth,
)

EXIT_OK = 0
EXIT_ERROR = 1

SETUP_HINT = """\
MS_CLIENT_ID ist nicht gesetzt. So richtest du die App einmalig ein:

  1. Öffne https://entra.microsoft.com -> Identität -> Anwendungen -> App-Registrierungen
     und wähle "Neue Registrierung".
  2. Kontotyp: "Konten in einem beliebigen Organisationsverzeichnis und persönliche
     Microsoft-Konten" (für ein privates Konto) oder nur dein Verzeichnis (Firmenkonto).
  3. Unter "Authentifizierung" aktivierst du "Öffentliche Clientflows zulassen" = Ja.
  4. Unter "API-Berechtigungen" fügst du als delegierte Microsoft-Graph-Rechte hinzu:
     {scopes}
  5. Trage die "Anwendungs-(Client-)ID" als MS_CLIENT_ID in die .env ein. Bei einem
     Firmenkonto zusätzlich die "Verzeichnis-(Mandanten-)ID" als MS_TENANT_ID.
  6. Starte dieses Skript erneut.
"""


def sign_in(auth: GraphAuth) -> int:
    """Run the device code flow and verify the result with a call to ``/me``."""
    existing = _existing_user(auth)
    if existing:
        print(f"Du bist bereits als {existing} angemeldet. Casper kann Microsoft nutzen.")
        print("Zum Abmelden: python auth_microsoft.py --logout")
        return EXIT_OK

    flow = auth.start_device_flow()
    print()
    print("Microsoft-Anmeldung für Casper")
    print("==============================")
    print(f"1. Öffne diese Seite im Browser:  {flow['verification_uri']}")
    print(f"2. Gib dort diesen Code ein:      {flow['user_code']}")
    print("3. Melde dich mit deinem Microsoft-Konto an und bestätige die Berechtigungen.")
    print()
    minutes = max(1, int(flow.get("expires_in", 900)) // 60)
    print(f"Der Code ist etwa {minutes} Minuten gültig. Ich warte auf deine Anmeldung ...")

    result = auth.complete_device_flow(flow)
    if "access_token" not in result:
        description = str(result.get("error_description") or result.get("error") or "")
        print(f"\nDie Anmeldung ist fehlgeschlagen. {explain_auth_error(description)}", file=sys.stderr)
        return EXIT_ERROR

    name = _display_name(result["access_token"])
    print(f"\nFertig! Angemeldet als {name}.")
    print(f"Die Anmeldung ist in {auth.cache_path.name} gespeichert. Du kannst Casper jetzt starten.")
    return EXIT_OK


def _existing_user(auth: GraphAuth) -> str | None:
    """Return the signed-in user's name if the cached login still works."""
    if auth.account_name() and auth.acquire_token_silent():
        return auth.account_name()
    return None


def _display_name(access_token: str) -> str:
    """Look up the user's name via ``GET /me`` (scope ``User.Read``). Falls back gracefully."""
    try:
        response = httpx.get(
            f"{GRAPH_BASE_URL}/me",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=15.0,
        )
        response.raise_for_status()
        profile: dict[str, Any] = response.json()
    except (httpx.HTTPError, ValueError):
        return "deinem Microsoft-Konto"
    name = profile.get("displayName") or "Unbekannt"
    mail = profile.get("mail") or profile.get("userPrincipalName")
    return f"{name} ({mail})" if mail else str(name)


def main() -> int:
    parser = argparse.ArgumentParser(description="Einmalige Microsoft-Anmeldung für Casper.")
    parser.add_argument("--logout", action="store_true", help="Gespeicherte Anmeldung löschen.")
    args = parser.parse_args()

    settings = get_settings()
    if not settings.graph_enabled:
        print(SETUP_HINT.format(scopes=", ".join(GRAPH_SCOPES)), file=sys.stderr)
        return EXIT_ERROR

    auth = get_graph_auth()
    try:
        if args.logout:
            removed = auth.sign_out()
            print("Die Anmeldung wurde gelöscht." if removed else "Es war niemand angemeldet.")
            return EXIT_OK
        return sign_in(auth)
    except ToolError as exc:
        print(f"\nFehler: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        print("\nAbgebrochen.")
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
