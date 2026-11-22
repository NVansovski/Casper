"""Open Google web apps on demand and page through presentations by voice.

Both tools act on the computer that runs the backend: the browser opens there, and
slide keys go to whichever window has the keyboard focus (the presentation).
"""

from __future__ import annotations

import logging
import time
import webbrowser
from typing import Any, ClassVar
from urllib.parse import quote

from tools.base import BaseTool, ToolError
from tools.time_utils import clamp_int

logger = logging.getLogger(__name__)

# app -> (home url, "new document" url or None, Drive file type filter or None)
GOOGLE_APPS: dict[str, tuple[str, str | None, str | None]] = {
    "slides": ("https://docs.google.com/presentation/", "https://docs.google.com/presentation/create", "presentation"),
    "sheets": ("https://docs.google.com/spreadsheets/", "https://docs.google.com/spreadsheets/create", "spreadsheet"),
    "docs": ("https://docs.google.com/document/", "https://docs.google.com/document/create", "document"),
    "forms": ("https://docs.google.com/forms/", "https://docs.google.com/forms/create", "form"),
    "drive": ("https://drive.google.com/", None, None),
    "gmail": ("https://mail.google.com/", "https://mail.google.com/mail/?view=cm&fs=1", None),
    "calendar": ("https://calendar.google.com/", "https://calendar.google.com/calendar/r/eventedit", None),
    "meet": ("https://meet.google.com/", "https://meet.google.com/new", None),
    "maps": ("https://www.google.com/maps", None, None),
    "keep": ("https://keep.google.com/", None, None),
    "photos": ("https://photos.google.com/", None, None),
    "translate": ("https://translate.google.com/", None, None),
}
APP_ALIASES = {"präsentationen": "slides", "präsentation": "slides", "tabellen": "sheets", "dokumente": "docs"}


def build_google_url(app: str, action: str, query: str) -> str:
    """Return the URL for opening, creating or searching in a Google app."""
    key = APP_ALIASES.get(app.lower(), app.lower())
    if key not in GOOGLE_APPS:
        raise ToolError(f"Die Google-App '{app}' kenne ich nicht. Ich kenne: {', '.join(GOOGLE_APPS)}.")
    home, create_url, drive_type = GOOGLE_APPS[key]
    if action == "new":
        if create_url is None:
            raise ToolError("Dort kann ich kein neues Dokument anlegen.")
        return create_url
    if action == "search":
        if not query:
            raise ToolError("Wonach soll ich suchen?")
        if key == "maps":
            return f"https://www.google.com/maps/search/{quote(query)}"
        if key == "gmail":
            return f"https://mail.google.com/mail/u/0/#search/{quote(query)}"
        if drive_type or key == "drive":
            type_filter = f" type:{drive_type}" if drive_type else ""
            return f"https://drive.google.com/drive/search?q={quote(query + type_filter)}"
        raise ToolError("In dieser App kann ich nicht suchen.")
    return home


class OpenGoogleAppTool(BaseTool):
    name: ClassVar[str] = "open_google_app"
    description: ClassVar[str] = (
        "Open a Google app in the browser on this computer: slides (Präsentationen), sheets "
        "(Tabellen), docs, forms, drive, gmail, calendar, meet, maps, keep, photos, translate. "
        "action 'open' opens the app, 'new' creates a new document, 'search' searches by name in "
        "Drive (for slides/sheets/docs/drive), Maps or Gmail."
    )
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "app": {"type": "string", "description": "App name, e.g. 'slides' or 'sheets'."},
            "action": {"type": "string", "enum": ["open", "new", "search"], "description": "Default 'open'."},
            "query": {"type": "string", "description": "Search text for action 'search'."},
        },
        "required": ["app"],
    }

    def run(self, arguments: dict[str, Any]) -> dict[str, Any]:
        app = str(arguments.get("app") or "").strip()
        if not app:
            raise ToolError("Welche Google-App soll ich öffnen?")
        action = str(arguments.get("action") or "open")
        url = build_google_url(app, action, str(arguments.get("query") or "").strip())
        if not webbrowser.open(url):
            raise ToolError("Ich konnte den Browser auf diesem Gerät nicht öffnen.")
        return {"status": "opened", "app": app, "action": action}


class ControlPresentationTool(BaseTool):
    """Sends slide-navigation keys to the focused window (Google Slides, PowerPoint, PDF viewers)."""

    name: ClassVar[str] = "control_presentation"
    description: ClassVar[str] = (
        "Control a running slideshow by sending keys to the focused window: 'next' (Nächste Folie), "
        "'previous', 'first', 'last', 'start' (begin the slideshow), 'stop' (end it). Use 'count' to "
        "go forward or back several slides at once. Works for Google Slides and PowerPoint."
    )
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["next", "previous", "first", "last", "start", "stop"]},
            "count": {"type": "integer", "description": "How many slides to move (1-20). Default 1."},
            "target": {
                "type": "string",
                "enum": ["google_slides", "powerpoint"],
                "description": "Which program 'start' is for. Default google_slides.",
            },
        },
        "required": ["action"],
    }

    def run(self, arguments: dict[str, Any]) -> dict[str, Any]:
        action = str(arguments.get("action") or "")
        count = clamp_int(arguments.get("count"), default=1, low=1, high=20)
        target = str(arguments.get("target") or "google_slides")

        try:
            from pynput.keyboard import Controller, Key
        except Exception as exc:  # no display (headless Linux/Wayland) or pynput missing
            logger.error("Keyboard control unavailable: %s", exc)
            raise ToolError("Die Tastatursteuerung ist auf diesem Gerät nicht verfügbar.") from exc

        keyboard = Controller()
        try:
            if action in ("next", "previous"):
                key = Key.right if action == "next" else Key.left
                for _ in range(count):
                    keyboard.tap(key)
                    time.sleep(0.15)
            elif action == "first":
                keyboard.tap(Key.home)
            elif action == "last":
                keyboard.tap(Key.end)
            elif action == "stop":
                keyboard.tap(Key.esc)
            elif action == "start":
                if target == "powerpoint":
                    keyboard.tap(Key.f5)
                else:  # Google Slides: "present from beginning"
                    with keyboard.pressed(Key.ctrl):
                        keyboard.tap(Key.f5)
            else:
                raise ToolError("Diese Aktion für die Präsentation kenne ich nicht.")
        except ToolError:
            raise
        except Exception as exc:
            logger.error("Sending keys failed: %s", exc)
            raise ToolError("Ich konnte die Taste nicht an die Präsentation senden.") from exc
        return {"status": "done", "action": action, "count": count if action in ("next", "previous") else 1}


def create_tools() -> list[BaseTool]:
    """Factory used by the tool registry."""
    return [OpenGoogleAppTool(), ControlPresentationTool()]
