"""Microsoft Graph calendar tools: read and create appointments."""

from __future__ import annotations

from datetime import datetime, time, timedelta, tzinfo
from typing import Any, ClassVar
from zoneinfo import ZoneInfo

from config import get_settings
from tools.base import BaseTool, ToolError
from tools.graph_client import GraphClient, get_graph_client
from tools.time_utils import clamp_int, parse_date, parse_datetime, weekday_de

MAX_RANGE_DAYS = 14
DEFAULT_DURATION = timedelta(hours=1)
LOCAL_FORMAT = "%Y-%m-%dT%H:%M:%S"


class GetCalendarEventsTool(BaseTool):
    """``GET /me/calendarView``: appointments for a day or a date range."""

    name: ClassVar[str] = "get_calendar_events"
    description: ClassVar[str] = (
        "Read appointments from the user's Microsoft calendar. Use 'date' (YYYY-MM-DD, default "
        "today) and 'days' (1-14, default 1) to ask for today, tomorrow or a longer range."
    )
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "date": {"type": "string", "description": "First day as YYYY-MM-DD. Defaults to today."},
            "days": {"type": "integer", "description": "Number of days to cover (1-14). Defaults to 1."},
        },
        "required": [],
    }

    def __init__(self, client: GraphClient, timezone: tzinfo) -> None:
        self._client = client
        self._timezone = timezone

    def run(self, arguments: dict[str, Any]) -> dict[str, Any]:
        first_day = parse_date(arguments.get("date"), self._timezone)
        days = clamp_int(arguments.get("days"), default=1, low=1, high=MAX_RANGE_DAYS)
        start = datetime.combine(first_day, time.min, tzinfo=self._timezone)
        end = start + timedelta(days=days)

        payload = self._client.get(
            "/me/calendarView",
            params={
                "startDateTime": start.isoformat(),
                "endDateTime": end.isoformat(),
                "$orderby": "start/dateTime",
                "$top": "50",
                "$select": "subject,start,end,location,isAllDay",
            },
        )
        events = [_describe_event(raw) for raw in payload.get("value", [])]
        return {
            "from_date": first_day.isoformat(),
            "days": days,
            "count": len(events),
            "events": events,
        }


class CreateCalendarEventTool(BaseTool):
    """``POST /me/events``: create an appointment."""

    name: ClassVar[str] = "create_calendar_event"
    description: ClassVar[str] = (
        "Create a new appointment in the user's Microsoft calendar. 'start' is an ISO 8601 local "
        "date-time such as 2026-11-23T15:00. 'end' defaults to one hour after the start."
    )
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "subject": {"type": "string", "description": "Title of the appointment."},
            "start": {"type": "string", "description": "Start as ISO 8601 local date-time."},
            "end": {"type": "string", "description": "End as ISO 8601 local date-time. Optional."},
            "location": {"type": "string", "description": "Location. Optional."},
        },
        "required": ["subject", "start"],
    }

    def __init__(self, client: GraphClient, timezone: tzinfo, timezone_name: str) -> None:
        self._client = client
        self._timezone = timezone
        self._timezone_name = timezone_name

    def run(self, arguments: dict[str, Any]) -> dict[str, Any]:
        subject = str(arguments.get("subject", "")).strip()
        if not subject:
            raise ToolError("Wie soll der Termin heißen?")
        start = parse_datetime(arguments.get("start"), self._timezone)
        end_value = arguments.get("end")
        end = parse_datetime(end_value, self._timezone) if end_value else start + DEFAULT_DURATION
        if end <= start:
            raise ToolError("Das Ende des Termins muss nach dem Beginn liegen.")
        location = str(arguments.get("location") or "").strip()

        body: dict[str, Any] = {
            "subject": subject,
            "start": self._graph_time(start),
            "end": self._graph_time(end),
        }
        if location:
            body["location"] = {"displayName": location}
        self._client.post("/me/events", body)
        return {
            "status": "created",
            "subject": subject,
            "start": start.astimezone(self._timezone).strftime(LOCAL_FORMAT),
            "end": end.astimezone(self._timezone).strftime(LOCAL_FORMAT),
            "location": location or None,
        }

    def _graph_time(self, moment: datetime) -> dict[str, str]:
        local = moment.astimezone(self._timezone)
        return {"dateTime": local.strftime(LOCAL_FORMAT), "timeZone": self._timezone_name}


def _describe_event(raw: dict[str, Any]) -> dict[str, Any]:
    """Convert a Graph event. Times arrive in the requested timezone, without an offset."""
    start = str(raw["start"]["dateTime"])[:19]
    end = str(raw["end"]["dateTime"])[:19]
    location = (raw.get("location") or {}).get("displayName") or None
    return {
        "subject": raw.get("subject") or "(ohne Titel)",
        "weekday": weekday_de(datetime.fromisoformat(start).date()),
        "start": start,
        "end": end,
        "all_day": bool(raw.get("isAllDay")),
        "location": location,
    }


def create_tools() -> list[BaseTool]:
    """Factory used by the tool registry. Active only when Microsoft is configured."""
    settings = get_settings()
    if not settings.graph_enabled:
        return []
    client = get_graph_client()
    timezone = ZoneInfo(settings.timezone)
    return [
        GetCalendarEventsTool(client, timezone),
        CreateCalendarEventTool(client, timezone, settings.timezone),
    ]
