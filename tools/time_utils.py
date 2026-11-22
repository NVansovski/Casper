"""Date and time helpers shared by the calendar and weather tools."""

from __future__ import annotations

from datetime import date, datetime, tzinfo
from typing import Any

from tools.base import ToolError

WEEKDAYS_DE = ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag")


def weekday_de(day: date) -> str:
    """German weekday name, independent of the system locale."""
    return WEEKDAYS_DE[day.weekday()]


def parse_date(value: Any, timezone: tzinfo) -> date:
    """Parse ``YYYY-MM-DD``. An empty value means today in ``timezone``."""
    if not value:
        return datetime.now(timezone).date()
    try:
        return datetime.fromisoformat(str(value)).date()
    except ValueError as exc:
        raise ToolError("Das Datum konnte ich leider nicht lesen.") from exc


def parse_datetime(value: Any, timezone: tzinfo) -> datetime:
    """Parse an ISO 8601 date-time. Naive values are interpreted in ``timezone``."""
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise ToolError("Die Uhrzeit konnte ich leider nicht lesen.") from exc
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone)


def clamp_int(value: Any, *, default: int, low: int, high: int) -> int:
    """Convert ``value`` to an int within [low, high], falling back to ``default``."""
    try:
        number = int(value) if value is not None else default
    except (TypeError, ValueError):
        number = default
    return max(low, min(high, number))
