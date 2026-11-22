"""Live train and public-transport connections via the free ``v6.db.transport.rest`` API."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, tzinfo
from typing import Any, ClassVar
from zoneinfo import ZoneInfo

import httpx

from config import get_settings
from tools.base import BaseTool, ToolError
from tools.time_utils import clamp_int

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT_S = 15.0
USER_AGENT = "CasperAssistant/1.0"
DEFAULT_RESULTS = 3
MAX_RESULTS = 5
UNREACHABLE_MESSAGE = "Die Bahn-Auskunft ist gerade nicht erreichbar. Versuch es später noch einmal."

Json = dict[str, Any]


class TransitTool(BaseTool):
    name: ClassVar[str] = "get_train_connections"
    description: ClassVar[str] = (
        "Look up real-time train and public transport connections in Germany (Deutsche Bahn, "
        "regional and local transport) with platform and delay. If the user names no starting "
        "station, omit 'from_station': the tool then starts at the configured default city."
    )
    input_schema: ClassVar[Json] = {
        "type": "object",
        "properties": {
            "from_station": {"type": "string", "description": "Start station or city. Optional."},
            "to_station": {"type": "string", "description": "Destination station or city."},
            "date": {"type": "string", "description": "Departure date YYYY-MM-DD. Default today."},
            "time": {"type": "string", "description": "Departure time HH:MM. Default now."},
            "results": {"type": "integer", "description": "Number of connections (1-5). Default 3."},
        },
        "required": ["to_station"],
    }

    def __init__(self, base_url: str, timezone: tzinfo, default_city: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._timezone = timezone
        self._default_city = default_city

    def run(self, arguments: Json) -> Json:
        destination = str(arguments.get("to_station") or "").strip()
        if not destination:
            raise ToolError("Wohin möchtest du fahren?")
        origin = str(arguments.get("from_station") or "").strip() or self._default_city
        results = clamp_int(arguments.get("results"), default=DEFAULT_RESULTS, low=1, high=MAX_RESULTS)
        departure = self._departure(arguments.get("date"), arguments.get("time"))

        with httpx.Client(timeout=REQUEST_TIMEOUT_S, headers={"User-Agent": USER_AGENT}) as client:
            origin_stop = self._find_station(client, origin)
            destination_stop = self._find_station(client, destination)
            payload = self._request(
                client,
                "/journeys",
                {
                    "from": origin_stop["id"],
                    "to": destination_stop["id"],
                    "departure": departure.isoformat(),
                    "results": results,
                    "stopovers": "false",
                    "language": "de",
                },
            )

        journeys = [self._describe_journey(raw) for raw in (payload.get("journeys") or [])[:results]]
        if not journeys:
            raise ToolError(
                f"Ich habe keine Verbindung von {origin_stop['name']} nach {destination_stop['name']} gefunden."
            )
        return {
            "from": origin_stop["name"],
            "to": destination_stop["name"],
            "searched_departure": departure.strftime("%Y-%m-%d %H:%M"),
            "connections": journeys,
        }

    # ------------------------------------------------------------------ requests
    def _request(self, client: httpx.Client, path: str, params: Json) -> Json:
        try:
            response = client.get(f"{self._base_url}{path}", params=params)
        except httpx.HTTPError as exc:
            logger.error("Transit request %s failed: %s", path, exc)
            raise ToolError(UNREACHABLE_MESSAGE) from exc
        if response.status_code == 429:
            raise ToolError("Die Bahn-Auskunft ist gerade überlastet. Versuch es in einer Minute noch einmal.")
        if response.status_code >= 400:
            logger.error("Transit %s -> HTTP %s: %s", path, response.status_code, response.text[:200])
            raise ToolError(UNREACHABLE_MESSAGE)
        try:
            data: Json | list[Any] = response.json()
        except ValueError as exc:
            raise ToolError(UNREACHABLE_MESSAGE) from exc
        return data if isinstance(data, dict) else {"items": data}

    def _find_station(self, client: httpx.Client, name: str) -> Json:
        """Resolve a free-text place to a stop. Cities prefer their main station (Hbf)."""
        queries = [name] if "hbf" in name.lower() or "bahnhof" in name.lower() else [f"{name} Hbf", name]
        for query in queries:
            payload = self._request(
                client,
                "/locations",
                {"query": query, "results": 3, "stops": "true", "addresses": "false", "poi": "false", "language": "de"},
            )
            stops = payload.get("items") if "items" in payload else payload.get("locations", [])
            for stop in stops or []:
                if isinstance(stop, dict) and stop.get("id") and stop.get("type") in ("stop", "station", None):
                    return {"id": str(stop["id"]), "name": str(stop.get("name") or name)}
        raise ToolError(f"Einen Bahnhof oder eine Haltestelle '{name}' habe ich nicht gefunden.")

    def _departure(self, date_text: Any, time_text: Any) -> datetime:
        now = datetime.now(self._timezone)
        try:
            day = datetime.fromisoformat(str(date_text)).date() if date_text else now.date()
            if time_text:
                hour, minute = (int(part) for part in str(time_text).split(":")[:2])
                moment = datetime(day.year, day.month, day.day, hour, minute, tzinfo=self._timezone)
                if not date_text and moment < now - timedelta(minutes=5):
                    moment += timedelta(days=1)  # "um 7 Uhr" asked in the evening means tomorrow
                return moment
            if date_text:
                return datetime(day.year, day.month, day.day, 0, 0, tzinfo=self._timezone)
        except (ValueError, TypeError) as exc:
            raise ToolError("Datum oder Uhrzeit konnte ich leider nicht lesen.") from exc
        return now

    # ------------------------------------------------------------------ formatting
    def _clock(self, iso: Any) -> str | None:
        if not iso:
            return None
        try:
            return datetime.fromisoformat(str(iso)).astimezone(self._timezone).strftime("%H:%M")
        except ValueError:
            return None

    def _describe_journey(self, journey: Json) -> Json:
        legs = [leg for leg in journey.get("legs", []) if isinstance(leg, dict)]
        rides = [self._describe_leg(leg) for leg in legs if not leg.get("walking")]
        first, last = (legs[0], legs[-1]) if legs else ({}, {})
        duration: int | None = None
        try:
            start = datetime.fromisoformat(str(first.get("departure") or first.get("plannedDeparture")))
            end = datetime.fromisoformat(str(last.get("arrival") or last.get("plannedArrival")))
            duration = int((end - start).total_seconds() // 60)
        except (ValueError, TypeError):
            pass
        return {
            "departure": self._clock(first.get("departure") or first.get("plannedDeparture")),
            "arrival": self._clock(last.get("arrival") or last.get("plannedArrival")),
            "duration_minutes": duration,
            "transfers": max(len(rides) - 1, 0),
            "legs": rides,
        }

    def _describe_leg(self, leg: Json) -> Json:
        line = leg.get("line") or {}
        platform = leg.get("departurePlatform") or leg.get("plannedDeparturePlatform")
        planned_platform = leg.get("plannedDeparturePlatform")
        info: Json = {
            "line": line.get("name") or line.get("productName") or "Verbindung",
            "direction": leg.get("direction"),
            "from": (leg.get("origin") or {}).get("name"),
            "to": (leg.get("destination") or {}).get("name"),
            "departure": self._clock(leg.get("departure") or leg.get("plannedDeparture")),
            "arrival": self._clock(leg.get("arrival") or leg.get("plannedArrival")),
            "platform": platform,
            "delay_minutes": _minutes(leg.get("departureDelay")),
            "arrival_delay_minutes": _minutes(leg.get("arrivalDelay")),
        }
        if platform and planned_platform and platform != planned_platform:
            info["platform_changed_from"] = planned_platform
        if leg.get("cancelled"):
            info["cancelled"] = True
        return info


def _minutes(seconds: Any) -> int | None:
    return round(seconds / 60) if isinstance(seconds, (int, float)) else None


def create_tool() -> BaseTool:
    """Factory used by the tool registry."""
    settings = get_settings()
    return TransitTool(settings.transit_api_url, ZoneInfo(settings.timezone), settings.default_city)
