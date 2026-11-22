"""Live weather via Open-Meteo (free, no API key required)."""

from __future__ import annotations

import logging
from typing import Any, ClassVar

import httpx

from config import get_settings
from tools.base import BaseTool, ToolError

logger = logging.getLogger(__name__)

GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
REQUEST_TIMEOUT_S = 10.0

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
        "Get the current weather and today's forecast range for a city. "
        "Omit 'city' to use the user's default city."
    )
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "city": {
                "type": "string",
                "description": "City name, for example 'Berlin'. Optional.",
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
        daily = forecast.get("daily", {})
        condition_code = int(current.get("weather_code", -1))
        return {
            "city": place.get("name", city),
            "country": place.get("country"),
            "current": {
                "condition": WEATHER_CODES_DE.get(condition_code, "unbekannte Wetterlage"),
                "temperature_c": _round(current.get("temperature_2m")),
                "feels_like_c": _round(current.get("apparent_temperature")),
                "humidity_percent": _round(current.get("relative_humidity_2m")),
                "wind_kmh": _round(current.get("wind_speed_10m")),
                "precipitation_mm": _round(current.get("precipitation")),
            },
            "today": {
                "min_c": _first(daily.get("temperature_2m_min")),
                "max_c": _first(daily.get("temperature_2m_max")),
                "precipitation_probability_percent": _first(
                    daily.get("precipitation_probability_max")
                ),
            },
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
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
            "forecast_days": 1,
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


def _first(values: Any) -> float | None:
    if isinstance(values, list) and values:
        return _round(values[0])
    return None


def create_tool() -> BaseTool:
    """Factory used by the tool registry."""
    return WeatherTool()
