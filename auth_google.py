"""Einmalige Google-Anmeldung für Casper (Gmail).

Aufruf:
    python auth_google.py            # im Browser anmelden
    python auth_google.py --logout   # gespeicherte Anmeldung löschen

Voraussetzung ist eine ``credentials.json`` (OAuth-Client "Desktop-App") im
Projektordner. Das Ergebnis landet in ``token_google.json`` (siehe .gitignore).
"""

from __future__ import annotations

import argparse
import sys

from config import get_settings
from tools.google_client import GMAIL_SCOPES, google_libraries_installed, save_token

EXIT_OK = 0
EXIT_ERROR = 1

SETUP_HINT = """\
Die Datei {path} fehlt. So bekommst du sie:

  1. Öffne https://console.cloud.google.com und lege ein Projekt an (oder wähle eins).
  2. "APIs & Dienste" -> "Bibliothek" -> "Gmail API" aktivieren.
  3. "APIs & Dienste" -> "OAuth-Zustimmungsbildschirm": Typ "Extern", deine Mail-Adresse
     als Testnutzer eintragen. Wichtig: Im Status "Testing" läuft die Anmeldung nach
     7 Tagen ab. Setze den Status auf "In Produktion", wenn du das vermeiden willst
     (für den Eigengebrauch ist die Warnung "nicht verifizierte App" in Ordnung).
  4. "Anmeldedaten" -> "Anmeldedaten erstellen" -> "OAuth-Client-ID" -> Typ "Desktop-App".
  5. JSON herunterladen und als credentials.json in den Projektordner legen.
  6. Starte dieses Skript erneut.
"""


def sign_in() -> int:
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    settings = get_settings()
    flow = InstalledAppFlow.from_client_secrets_file(str(settings.google_credentials_file), GMAIL_SCOPES)
    print()
    print("Google-Anmeldung für Casper")
    print("===========================")
    print("Ich öffne jetzt den Browser. Melde dich mit deinem Google-Konto an und erlaube den Zugriff")
    print("auf Gmail (Lesen und Senden). Falls sich kein Browser öffnet, nutze den Link, der gleich erscheint.")
    print()
    credentials = flow.run_local_server(
        port=0,
        prompt="consent",
        authorization_prompt_message="Öffne diesen Link im Browser:\n{url}\n",
        success_message="Fertig! Du kannst dieses Fenster schließen und zu Casper zurückkehren.",
    )
    save_token(credentials, settings.google_token_file)

    try:
        profile = build("gmail", "v1", credentials=credentials, cache_discovery=False)
        address = profile.users().getProfile(userId="me").execute().get("emailAddress", "deinem Konto")
    except Exception:  # the sign-in itself worked; the name lookup is only a courtesy
        address = "deinem Google-Konto"
    print(f"\nFertig! Angemeldet als {address}.")
    print(f"Die Anmeldung ist in {settings.google_token_file.name} gespeichert. Du kannst Casper jetzt starten.")
    return EXIT_OK


def main() -> int:
    parser = argparse.ArgumentParser(description="Einmalige Google-Anmeldung für Casper.")
    parser.add_argument("--logout", action="store_true", help="Gespeicherte Anmeldung löschen.")
    args = parser.parse_args()
    settings = get_settings()

    if args.logout:
        if settings.google_token_file.exists():
            settings.google_token_file.unlink()
            print("Die Anmeldung wurde gelöscht.")
        else:
            print("Es war niemand angemeldet.")
        return EXIT_OK

    if not google_libraries_installed():
        print("Die Google-Bibliotheken fehlen. Installiere sie mit: pip install -r requirements.txt", file=sys.stderr)
        return EXIT_ERROR
    if not settings.google_credentials_file.is_file():
        print(SETUP_HINT.format(path=settings.google_credentials_file.name), file=sys.stderr)
        return EXIT_ERROR

    try:
        return sign_in()
    except KeyboardInterrupt:
        print("\nAbgebrochen.")
        return EXIT_ERROR
    except Exception as exc:
        print(f"\nDie Anmeldung ist fehlgeschlagen: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
