"""Mock Outlook integration: calendar events and unread emails.

The data models and method signatures mirror Microsoft Graph
(``/me/calendarView``, ``/me/events``, ``/me/mailFolders/inbox/messages``).
To go live, implement :class:`OutlookBackend` with a Graph client; the tool
layer does not change.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, time, timedelta, tzinfo
from typing import Any, ClassVar
from zoneinfo import ZoneInfo

from config import get_settings
from tools.base import BaseTool, ToolError
from tools.data_store import JsonDataStore, get_data_store
from tools.time_utils import clamp_int, parse_date, parse_datetime

DEFAULT_EVENT_DURATION = timedelta(hours=1)
MAX_EVENT_RANGE_DAYS = 14
MAX_MESSAGES = 20


@dataclass(frozen=True, slots=True)
class CalendarEvent:
    """Mirrors Graph ``event``."""

    id: str
    subject: str
    start: datetime
    end: datetime
    location: str | None


@dataclass(frozen=True, slots=True)
class EmailMessage:
    """Mirrors Graph ``message``."""

    id: str
    subject: str
    sender_name: str
    sender_address: str
    received: datetime
    is_read: bool
    preview: str


class OutlookBackend(ABC):
    """Storage-agnostic Outlook operations. Swap the implementation to go live."""

    @abstractmethod
    def list_events(self, start: datetime, end: datetime) -> list[CalendarEvent]:
        """Return events overlapping [start, end) (Graph: ``GET /me/calendarView``)."""

    @abstractmethod
    def create_event(
        self, subject: str, start: datetime, end: datetime, location: str | None = None
    ) -> CalendarEvent:
        """Create an event (Graph: ``POST /me/events``)."""

    @abstractmethod
    def list_messages(self, *, unread_only: bool = True, limit: int = 10) -> list[EmailMessage]:
        """Return inbox messages, newest first (Graph: ``GET /me/mailFolders/inbox/messages``)."""


def _event_from_raw(raw: dict[str, Any]) -> CalendarEvent:
    return CalendarEvent(
        id=str(raw["id"]),
        subject=str(raw["subject"]),
        start=datetime.fromisoformat(raw["start"]),
        end=datetime.fromisoformat(raw["end"]),
        location=raw.get("location"),
    )


def _message_from_raw(raw: dict[str, Any]) -> EmailMessage:
    sender: dict[str, Any] = raw.get("sender") or {}
    return EmailMessage(
        id=str(raw["id"]),
        subject=str(raw["subject"]),
        sender_name=str(sender.get("name", "Unbekannt")),
        sender_address=str(sender.get("address", "")),
        received=datetime.fromisoformat(raw["receivedDateTime"]),
        is_read=bool(raw.get("isRead", False)),
        preview=str(raw.get("bodyPreview", "")),
    )


class LocalOutlookBackend(OutlookBackend):
    """Mock backend that keeps events and mail in the shared ``data.json`` file."""

    def __init__(self, store: JsonDataStore) -> None:
        self._store = store

    def list_events(self, start: datetime, end: datetime) -> list[CalendarEvent]:
        def read(data: dict[str, Any]) -> list[CalendarEvent]:
            events = [_event_from_raw(raw) for raw in data["outlook"]["events"]]
            overlapping = [event for event in events if event.start < end and event.end > start]
            return sorted(overlapping, key=lambda event: event.start)

        return self._store.read(read)

    def create_event(
        self, subject: str, start: datetime, end: datetime, location: str | None = None
    ) -> CalendarEvent:
        raw: dict[str, Any] = {
            "id": uuid.uuid4().hex,
            "subject": subject,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "location": location,
        }

        def write(data: dict[str, Any]) -> CalendarEvent:
            data["outlook"]["events"].append(raw)
            return _event_from_raw(raw)

        return self._store.write(write)

    def list_messages(self, *, unread_only: bool = True, limit: int = 10) -> list[EmailMessage]:
        def read(data: dict[str, Any]) -> list[EmailMessage]:
            messages = [_message_from_raw(raw) for raw in data["outlook"]["messages"]]
            if unread_only:
                messages = [message for message in messages if not message.is_read]
            messages.sort(key=lambda message: message.received, reverse=True)
            return messages[:limit]

        return self._store.read(read)


class OutlookTool(BaseTool):
    """Voice-facing tool for the calendar and the inbox."""

    name: ClassVar[str] = "outlook"
    description: ClassVar[str] = (
        "Access the user's Outlook calendar and inbox. Actions: "
        "'list_events' returns appointments for a start date and number of days; "
        "'create_event' adds an appointment (start in ISO 8601 local time, e.g. 2026-11-23T15:00); "
        "'list_unread_emails' returns unread emails."
    )
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list_events", "create_event", "list_unread_emails"],
            },
            "date": {
                "type": "string",
                "description": "Start date as YYYY-MM-DD for list_events. Defaults to today.",
            },
            "days": {
                "type": "integer",
                "description": "Number of days to cover for list_events (1-14). Defaults to 1.",
            },
            "subject": {"type": "string", "description": "Appointment title for create_event."},
            "start": {
                "type": "string",
                "description": "Start as ISO 8601 local date-time for create_event.",
            },
            "end": {
                "type": "string",
                "description": "End as ISO 8601 local date-time. Defaults to one hour after start.",
            },
            "location": {"type": "string", "description": "Optional location for create_event."},
            "limit": {
                "type": "integer",
                "description": "Maximum number of emails for list_unread_emails (1-20).",
            },
        },
        "required": ["action"],
    }

    def __init__(self, backend: OutlookBackend, timezone: tzinfo) -> None:
        self._backend = backend
        self._timezone = timezone

    def run(self, arguments: dict[str, Any]) -> dict[str, Any]:
        action = str(arguments.get("action", ""))
        if action == "list_events":
            return self._list_events(arguments)
        if action == "create_event":
            return self._create_event(arguments)
        if action == "list_unread_emails":
            return self._list_unread_emails(arguments)
        raise ToolError("Diese Outlook-Aktion kenne ich nicht.")

    def _list_events(self, arguments: dict[str, Any]) -> dict[str, Any]:
        day = parse_date(arguments.get("date"), self._timezone)
        days = clamp_int(arguments.get("days"), default=1, low=1, high=MAX_EVENT_RANGE_DAYS)
        start = datetime.combine(day, time.min, tzinfo=self._timezone)
        end = start + timedelta(days=days)
        events = self._backend.list_events(start, end)
        return {
            "from": start.isoformat(),
            "to": end.isoformat(),
            "count": len(events),
            "events": [self._describe_event(event) for event in events],
        }

    def _create_event(self, arguments: dict[str, Any]) -> dict[str, Any]:
        subject = str(arguments.get("subject", "")).strip()
        if not subject:
            raise ToolError("Wie soll der Termin heißen?")
        start = parse_datetime(arguments.get("start"), self._timezone)
        end_value = arguments.get("end")
        end = (
            parse_datetime(end_value, self._timezone)
            if end_value
            else start + DEFAULT_EVENT_DURATION
        )
        if end <= start:
            raise ToolError("Das Ende des Termins muss nach dem Beginn liegen.")
        location = arguments.get("location") or None
        event = self._backend.create_event(subject, start, end, location)
        return {"status": "created", "event": self._describe_event(event)}

    def _list_unread_emails(self, arguments: dict[str, Any]) -> dict[str, Any]:
        limit = clamp_int(arguments.get("limit"), default=5, low=1, high=MAX_MESSAGES)
        messages = self._backend.list_messages(unread_only=True, limit=limit)
        return {
            "count": len(messages),
            "messages": [
                {
                    "from": message.sender_name,
                    "subject": message.subject,
                    "received": message.received.astimezone(self._timezone).isoformat(),
                    "preview": message.preview,
                }
                for message in messages
            ],
        }

    def _describe_event(self, event: CalendarEvent) -> dict[str, Any]:
        return {
            "subject": event.subject,
            "start": event.start.astimezone(self._timezone).isoformat(),
            "end": event.end.astimezone(self._timezone).isoformat(),
            "location": event.location,
        }


def create_tool() -> BaseTool | None:
    """Factory used by the tool registry. Disabled when the real Microsoft tools are active."""
    if get_settings().graph_enabled:
        return None
    return OutlookTool(LocalOutlookBackend(get_data_store()), ZoneInfo(get_settings().timezone))
