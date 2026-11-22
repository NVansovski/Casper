"""Everyday helpers: timers, alarms and the combined daily briefing."""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, ClassVar
from zoneinfo import ZoneInfo

from config import get_settings
from tools.alerts import REPEAT_MODES, AlertService, get_alert_service
from tools.base import BaseTool, ToolError
from tools.time_utils import clamp_int, weekday_de

logger = logging.getLogger(__name__)

MAX_TIMER_S = 24 * 3600
_TIME_RE = re.compile(r"^\s*(\d{1,2})[:.](\d{2})\s*$")

Json = dict[str, Any]


class SetTimerTool(BaseTool):
    name: ClassVar[str] = "set_timer"
    description: ClassVar[str] = (
        "Start a countdown timer ('Stell einen Timer auf 10 Minuten'). When it expires, a clear "
        "alarm sound plays on the speakers and Casper announces it. Several timers can run at once."
    )
    input_schema: ClassVar[Json] = {
        "type": "object",
        "properties": {
            "hours": {"type": "integer", "description": "Hours. Optional."},
            "minutes": {"type": "integer", "description": "Minutes. Optional."},
            "seconds": {"type": "integer", "description": "Seconds. Optional."},
            "label": {"type": "string", "description": "What the timer is for, e.g. 'Nudeln'. Optional."},
        },
        "required": [],
    }

    def __init__(self, service: AlertService) -> None:
        self._service = service

    def run(self, arguments: Json) -> Json:
        total = (
            clamp_int(arguments.get("hours"), default=0, low=0, high=24) * 3600
            + clamp_int(arguments.get("minutes"), default=0, low=0, high=24 * 60) * 60
            + clamp_int(arguments.get("seconds"), default=0, low=0, high=24 * 3600)
        )
        if total <= 0:
            raise ToolError("Wie lange soll der Timer laufen?")
        if total > MAX_TIMER_S:
            raise ToolError("Ein Timer darf höchstens vierundzwanzig Stunden laufen. Dafür nimm bitte einen Wecker.")
        timer = self._service.add_timer(total, str(arguments.get("label") or "").strip())
        return {"status": "started", **timer}


class SetAlarmTool(BaseTool):
    name: ClassVar[str] = "set_alarm"
    description: ClassVar[str] = (
        "Set an alarm clock (Wecker) for a time of day, e.g. 'Weck mich um 6:30'. If the time has "
        "passed today, it rings tomorrow. 'repeat': once (default), daily, or weekdays."
    )
    input_schema: ClassVar[Json] = {
        "type": "object",
        "properties": {
            "time": {"type": "string", "description": "Time of day as HH:MM (24-hour)."},
            "label": {"type": "string", "description": "Optional label."},
            "repeat": {"type": "string", "enum": list(REPEAT_MODES), "description": "Default once."},
        },
        "required": ["time"],
    }

    def __init__(self, service: AlertService) -> None:
        self._service = service

    def run(self, arguments: Json) -> Json:
        match = _TIME_RE.match(str(arguments.get("time") or ""))
        if not match or int(match.group(1)) > 23 or int(match.group(2)) > 59:
            raise ToolError("Die Uhrzeit für den Wecker konnte ich nicht lesen.")
        repeat = str(arguments.get("repeat") or "once")
        if repeat not in REPEAT_MODES:
            raise ToolError("Ein Wecker kann einmal, täglich oder an Werktagen klingeln.")
        alarm = self._service.add_alarm(
            int(match.group(1)), int(match.group(2)), str(arguments.get("label") or "").strip(), repeat
        )
        return {"status": "set", **alarm}


class ListTimersAndAlarmsTool(BaseTool):
    name: ClassVar[str] = "list_timers_and_alarms"
    description: ClassVar[str] = "List all running timers (with remaining time) and all alarms."
    input_schema: ClassVar[Json] = {"type": "object", "properties": {}, "required": []}

    def __init__(self, service: AlertService) -> None:
        self._service = service

    def run(self, arguments: Json) -> Json:
        return self._service.list_all()


class CancelTimerOrAlarmTool(BaseTool):
    name: ClassVar[str] = "cancel_timer_or_alarm"
    description: ClassVar[str] = (
        "Cancel timers and/or alarms by label or id, or everything with query 'all'. "
        "kind: 'timer', 'alarm' or 'any' (default)."
    )
    input_schema: ClassVar[Json] = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Label or id, or 'all'."},
            "kind": {"type": "string", "enum": ["any", "timer", "alarm"]},
        },
        "required": ["query"],
    }

    def __init__(self, service: AlertService) -> None:
        self._service = service

    def run(self, arguments: Json) -> Json:
        query = str(arguments.get("query") or "").strip()
        if not query:
            raise ToolError("Welchen Timer oder Wecker meinst du?")
        removed = self._service.cancel(query, str(arguments.get("kind") or "any"))
        if not removed:
            raise ToolError("Dazu finde ich keinen laufenden Timer oder Wecker.")
        return {"status": "cancelled", "removed": removed}


# Sections of the briefing: (key, candidates). The first registered candidate tool is used,
# so the briefing works with the real Microsoft tools and with the local mock tools alike.
_BRIEFING_SECTIONS: tuple[tuple[str, tuple[tuple[str, Json], ...]], ...] = (
    ("weather", (("get_weather", {}),)),
    ("calendar_today", (("get_calendar_events", {}), ("outlook", {"action": "list_events"}))),
    ("tasks_my_day", (("get_todo_view", {"view": "my_day"}), ("microsoft_todo", {"action": "list_items", "list": "todo"}))),
    ("unread_mail_microsoft", (("get_unread_emails", {"limit": 5}), ("outlook", {"action": "list_unread_emails", "limit": 5}))),
    ("unread_mail_gmail", (("get_gmail_messages", {"limit": 5, "unread_only": True}),)),
    ("meal_today", (("familywall_meals", {"action": "get_plan"}),)),
    ("timers_and_alarms", (("list_timers_and_alarms", {}),)),
)


class DailyBriefingTool(BaseTool):
    name: ClassVar[str] = "get_daily_briefing"
    description: ClassVar[str] = (
        "Collect the day's overview in one step: weather, today's appointments, tasks due today "
        "('Mein Tag'), unread e-mails (Microsoft and Gmail), today's planned meal and running "
        "timers/alarms. Use it for 'Guten Morgen' and 'Tagesüberblick'. Set include_news=true to "
        "add world news headlines. Then summarise everything briefly and in order of importance."
    )
    input_schema: ClassVar[Json] = {
        "type": "object",
        "properties": {
            "include_news": {"type": "boolean", "description": "Also fetch world news headlines. Default false."},
        },
        "required": [],
    }

    def __init__(self, timezone: ZoneInfo) -> None:
        self._timezone = timezone

    def run(self, arguments: Json) -> Json:
        registry = self.registry
        if registry is None:
            raise ToolError("Das Tages-Briefing ist gerade nicht verfügbar.")

        jobs: list[tuple[str, str, Json]] = []
        for key, candidates in _BRIEFING_SECTIONS:
            for tool_name, tool_args in candidates:
                if registry.has(tool_name):
                    jobs.append((key, tool_name, tool_args))
                    break
        if arguments.get("include_news") and registry.has("get_news"):
            jobs.append(("news", "get_news", {"scope": "world", "limit": 5}))

        with ThreadPoolExecutor(max_workers=max(1, min(8, len(jobs)))) as pool:
            outcomes = list(pool.map(lambda job: registry.execute(job[1], dict(job[2])), jobs))

        sections: Json = {}
        for (key, _tool, _args), outcome in zip(jobs, outcomes, strict=True):
            if key == "weather" and "forecast" in outcome:
                outcome = {**outcome, "forecast": outcome["forecast"][:2]}  # today and tomorrow
            sections[key] = outcome  # failed sections carry {"error": ...} and are reported as such

        now = datetime.now(self._timezone)
        return {
            "date": now.strftime("%Y-%m-%d"),
            "weekday": weekday_de(now.date()),
            "time": now.strftime("%H:%M"),
            "sections": sections,
        }


def create_tools() -> list[BaseTool]:
    """Factory used by the tool registry. Also starts the timer/alarm scheduler."""
    service = get_alert_service()
    return [
        SetTimerTool(service),
        SetAlarmTool(service),
        ListTimersAndAlarmsTool(service),
        CancelTimerOrAlarmTool(service),
        DailyBriefingTool(ZoneInfo(get_settings().timezone)),
    ]
