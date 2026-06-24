"""Weather tool — current conditions and forecast via open-meteo.com (free, no key)."""

from __future__ import annotations

from typing import Any

import httpx

from core.tool_framework import Tool

_API_URL = "https://api.open-meteo.com/v1/forecast"

_WMO_CODES: dict[int, str] = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Depositing rime fog",
    51: "Light drizzle",
    53: "Moderate drizzle",
    55: "Dense drizzle",
    56: "Light freezing drizzle",
    57: "Dense freezing drizzle",
    61: "Slight rain",
    63: "Moderate rain",
    65: "Heavy rain",
    66: "Light freezing rain",
    67: "Heavy freezing rain",
    71: "Slight snow",
    73: "Moderate snow",
    75: "Heavy snow",
    77: "Snow grains",
    80: "Slight rain showers",
    81: "Moderate rain showers",
    82: "Violent rain showers",
    85: "Slight snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm with slight hail",
    99: "Thunderstorm with heavy hail",
}


class WeatherTool(Tool):
    name = "get_weather"
    description = (
        "Get current weather and a short forecast for a location. "
        "Uses the user's cached GPS location if available. "
        "Otherwise accepts optional latitude/longitude parameters. "
        "Set location_name to label the result (e.g. 'Singapore')."
    )
    parameters = {
        "type": "object",
        "properties": {
            "latitude": {
                "type": "number",
                "description": "Optional latitude. Uses cached user location if omitted.",
            },
            "longitude": {
                "type": "number",
                "description": "Optional longitude. Uses cached user location if omitted.",
            },
            "location_name": {
                "type": "string",
                "description": "Optional name for the location (e.g. 'Singapore').",
            },
        },
    }

    async def execute(self, latitude: float = 0, longitude: float = 0, location_name: str = "", **kwargs: Any) -> str:
        lat, lng = latitude, longitude

        if not lat and not lng:
            from core.user_context import get_location
            cached = get_location()
            if cached and cached.get("lat") and cached.get("lng"):
                lat, lng = cached["lat"], cached["lng"]
            else:
                return (
                    "No location available. Share your location first (get_user_location), "
                    "or provide latitude and longitude directly."
                )

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(_API_URL, params={
                    "latitude": lat,
                    "longitude": lng,
                    "current": "temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,wind_speed_10m",
                    "daily": "weather_code,temperature_2m_max,temperature_2m_min",
                    "forecast_days": 3,
                    "timezone": "auto",
                })
                resp.raise_for_status()
                data = resp.json()

                current = data.get("current", {})
                daily = data.get("daily", {})

                label = location_name or f"{lat:.2f}, {lng:.2f}"
                lines = [f"Weather for {label}", ""]

                # Current conditions
                temp = current.get("temperature_2m", "?")
                feels = current.get("apparent_temperature", "?")
                humidity = current.get("relative_humidity_2m", "?")
                wind = current.get("wind_speed_10m", "?")
                code = int(current.get("weather_code", -1))
                condition = _WMO_CODES.get(code, f"Unknown ({code})")

                lines.append(f"**Now:** {condition}")
                lines.append(f"Temperature: {temp}°C (feels like {feels}°C)")
                lines.append(f"Humidity: {humidity}%  |  Wind: {wind} km/h")

                # Forecast
                lines.append("")
                lines.append("**Forecast:**")
                dates = daily.get("time", [])
                max_temps = daily.get("temperature_2m_max", [])
                min_temps = daily.get("temperature_2m_min", [])
                codes = daily.get("weather_code", [])

                for i in range(min(3, len(dates))):
                    day_condition = _WMO_CODES.get(int(codes[i]), "?") if i < len(codes) else "?"
                    lines.append(
                        f"{dates[i]}: {day_condition}, "
                        f"{min_temps[i] if i < len(min_temps) else '?'}°C – "
                        f"{max_temps[i] if i < len(max_temps) else '?'}°C"
                    )

                return "\n".join(lines)

        except httpx.HTTPError as e:
            return f"Weather API error: {e}"
        except Exception as e:
            return f"Weather lookup failed: {e}"
