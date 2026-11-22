"""Google OAuth helpers shared by ``auth_google.py`` and the Gmail tool.

All Google libraries are imported lazily so a missing package or a missing
``credentials.json`` never prevents the server from starting.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import threading
from pathlib import Path
from typing import Any

from config import get_settings
from tools.base import ToolError

logger = logging.getLogger(__name__)

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]

NOT_SIGNED_IN_MESSAGE = (
    "Ich bin noch nicht mit deinem Google-Konto verbunden. "
    "Bitte führe einmalig auf dem Computer das Skript auth_google.py aus."
)
UNREACHABLE_MESSAGE = "Google ist gerade nicht erreichbar. Versuch es später noch einmal."

_lock = threading.Lock()


def google_libraries_installed() -> bool:
    return all(
        importlib.util.find_spec(name) is not None
        for name in ("googleapiclient", "google_auth_oauthlib", "google.oauth2")
    )


def gmail_enabled() -> bool:
    """True when the libraries are installed and a ``credentials.json`` was provided."""
    settings = get_settings()
    return settings.google_credentials_file.is_file() and google_libraries_installed()


def save_token(credentials: Any, token_path: Path) -> None:
    """Write the OAuth token (contains a refresh token) with owner-only permissions."""
    temp_path = token_path.with_name(token_path.name + ".tmp")
    temp_path.write_text(credentials.to_json(), encoding="utf-8")
    try:
        os.chmod(temp_path, 0o600)
    except OSError:
        pass
    os.replace(temp_path, token_path)


def load_credentials() -> Any:
    """Return valid cached credentials, refreshing them if needed. Raises ``ToolError`` otherwise."""
    from google.auth.exceptions import RefreshError, TransportError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    token_path = get_settings().google_token_file
    if not token_path.is_file():
        raise ToolError(NOT_SIGNED_IN_MESSAGE)
    try:
        credentials = Credentials.from_authorized_user_file(str(token_path), GMAIL_SCOPES)
    except (ValueError, OSError) as exc:
        logger.error("Unreadable Google token file: %s", exc)
        raise ToolError(NOT_SIGNED_IN_MESSAGE) from exc

    if credentials.valid:
        return credentials
    if credentials.expired and credentials.refresh_token:
        with _lock:
            try:
                credentials.refresh(Request())
            except RefreshError as exc:
                logger.error("Google refresh token rejected: %s", exc)
                raise ToolError(NOT_SIGNED_IN_MESSAGE) from exc
            except TransportError as exc:
                logger.error("Google token refresh failed: %s", exc)
                raise ToolError(UNREACHABLE_MESSAGE) from exc
            save_token(credentials, token_path)
        return credentials
    raise ToolError(NOT_SIGNED_IN_MESSAGE)


def build_gmail_service() -> Any:
    """Create a Gmail API client. A new one per call: the underlying HTTP client is not thread-safe."""
    from googleapiclient.discovery import build

    return build("gmail", "v1", credentials=load_credentials(), cache_discovery=False)
