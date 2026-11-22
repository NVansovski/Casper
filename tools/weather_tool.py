"""Live weather via Open-Meteo (free, no API key required)."""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, ClassVar

import httpx

from config import get_settings
from tools.base import BaseTool, ToolError
from tools.time_utils import weekday_de

logger = logging.getLogger(__name__)

GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
REQUEST_TIMEOUT_S = 10.0
FORECAST_DAYS = 7
RELATIVE_DAYS = {0: "heute", 1: "morgen", 2: "übermorgen"}

# WMO weather interpretation codes, as used by Open-Meteo, in German.
WEATHER_CODES_DE: dict[int, str] = {
    0: "klarer Himmel",
    1: "überwiegend klar",
    2: "teilweise bewölkt",
    3: "bedeckt",
    45: "neblig",
    48: "Nebel mit Reifablagerung",
    51: "leichter Nieselregen",
    53: "mäßiger Nieselregen",
    55: "starker Nieselregen",
    56: "gefrierender Nieselregen",
    57: "starker gefrierender Nieselregen",
    61: "leichter Regen",
    63: "mäßiger Regen",
    65: "starker Regen",
    66: "gefrierender Regen",
    67: "starker gefrierender Regen",
    71: "leichter Schneefall",
    73: "mäßiger Schneefall",
    75: "starker Schneefall",
    77: "Schneegriesel",
    80: "leichte Regenschauer",
    81: "mäßige Regenschauer",
    82: "heftige Regenschauer",
    85: "leichte Schneeschauer",
    86: "starke Schneeschauer",
    95: "Gewitter",
    96: "Gewitter mit leichtem Hagel",
    99: "Gewitter mit starkem Hagel",
}


class WeatherTool(BaseTool):
    """Returns current conditions and today's range for a city."""

    name: ClassVar[str] = "get_weather"
    description: ClassVar[str] = (
        "Get the current weather and a 7-day daily forecast (date, weekday, min/max temperature, "
        "rain probability, conditions). Use it for today, tomorrow, the day after tomorrow, the "
        "weekend or any day this week. If the user names no place, omit 'city': the tool then "
        "uses the configured default city automatically."
    )
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "city": {
                "type": "string",
                "description": "City name, for example 'Berlin'. Omit it if the user names no place.",
            },
        },
        "required": [],
    }

    def run(self, arguments: dict[str, Any]) -> dict[str, Any]:
        city = str(arguments.get("city") or get_settings().default_city).strip()
        with httpx.Client(timeout=REQUEST_TIMEOUT_S) as client:
            place = self._geocode(client, city)
            forecast = self._forecast(client, place["latitude"], place["longitude"])

        current = forecast.get("current", {})
        return {
            "city": place.get("name", city),
            "country": place.get("country"),
            "current": {
                "condition": _describe(current.get("weather_code")),
                "temperature_c": _round(current.get("temperature_2m")),
                "feels_like_c": _round(current.get("apparent_temperature")),
                "humidity_percent": _round(current.get("relative_humidity_2m")),
                "wind_kmh": _round(current.get("wind_speed_10m")),
                "precipitation_mm": _round(current.get("precipitation")),
            },
            "forecast": _daily_forecast(forecast),
        }

    def _geocode(self, client: httpx.Client, city: str) -> dict[str, Any]:
        try:
            response = client.get(
                GEOCODING_URL, params={"name": city, "count": 1, "language": "de"}
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.error("Geocoding request failed: %s", exc)
            raise ToolError(_UNREACHABLE_MESSAGE) from exc

        results = payload.get("results") or []
        if not results:
            raise ToolError(f"Ich konnte den Ort '{city}' leider nicht finden.")
        return results[0]

    def _forecast(self, client: httpx.Client, latitude: float, longitude: float) -> dict[str, Any]:
        params = {
            "latitude": latitude,
            "longitude": longitude,
            "current": ",".join(
                [
                    "temperature_2m",
                    "relative_humidity_2m",
                    "apparent_temperature",
                    "precipitation",
                    "weather_code",
                    "wind_speed_10m",
                ]
            ),
            "daily": ",".join(
                [
                    "weather_code",
                    "temperature_2m_max",
                    "temperature_2m_min",
                    "precipitation_probability_max",
                ]
            ),
            "forecast_days": FORECAST_DAYS,
            "timezone": "auto",
        }
        try:
            response = client.get(FORECAST_URL, params=params)
            response.raise_for_status()
            payload: dict[str, Any] = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.error("Forecast request failed: %s", exc)
            raise ToolError(_UNREACHABLE_MESSAGE) from exc
        return payload


_UNREACHABLE_MESSAGE = "Der Wetterdienst ist gerade nicht erreichbar. Versuch es später noch einmal."


def _round(value: Any) -> float | None:
    return round(float(value), 1) if isinstance(value, (int, float)) else None


def _describe(code: Any) -> str:
    return WEATHER_CODES_DE.get(int(code), "unbekannte Wetterlage") if isinstance(code, (int, float)) else "unbekannte Wetterlage"


def _at(values: Any, index: int) -> Any:
    return values[index] if isinstance(values, list) and index < len(values) else None


def _daily_forecast(forecast: dict[str, Any]) -> list[dict[str, Any]]:
    """Build one entry per forecast day. "Today" is the local date of the place itself."""
    daily = forecast.get("daily", {})
    dates: list[str] = daily.get("time") or []
    try:
        today = date.fromisoformat(str(forecast["current"]["time"])[:10])
    except (KeyError, ValueError):
        today = date.fromisoformat(dates[0]) if dates else None

    days: list[dict[str, Any]] = []
    for index, iso_day in enumerate(dates):
        day = date.fromisoformat(iso_day)
        offset = (day - today).days if today else index
        days.append(
            {
                "date": iso_day,
                "weekday": weekday_de(day),
                "relative_day": RELATIVE_DAYS.get(offset),
                "condition": _describe(_at(daily.get("weather_code"), index)),
                "min_c": _round(_at(daily.get("temperature_2m_min"), index)),
                "max_c": _round(_at(daily.get("temperature_2m_max"), index)),
                "precipitation_probability_percent": _round(
                    _at(daily.get("precipitation_probability_max"), index)
                ),
            }
        )
    return days


def create_tool() -> BaseTool:
    """Factory used by the tool registry."""
    return WeatherTool()
