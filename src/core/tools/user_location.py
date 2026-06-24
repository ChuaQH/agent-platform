"""User location tool — on-demand browser geolocation with consent.

Uses browser GPS (navigator.geolocation). When the tool is called, it
emits a Chainlit action button that the user clicks to provide the
required user gesture for the browser permission prompt.
"""

from __future__ import annotations

from typing import Any

import httpx

from core.tool_framework import Tool
from core.user_context import get_location, request_location, clear_pending


class UserLocationTool(Tool):
    name = "get_user_location"
    description = (
        "Get the user's geographic location (city, region, country). "
        "Calling this will show a button for the user to share their "
        "location via a browser GPS prompt."
    )
    parameters = {
        "type": "object",
        "properties": {},
    }

    async def execute(self, **kwargs: Any) -> str:
        cached = get_location()
        if cached and cached.get("lat") and cached.get("lng"):
            return await _reverse_geocode(cached["lat"], cached["lng"])

        try:
            loc = await request_location()
            return await _reverse_geocode(loc["lat"], loc["lng"])
        except PermissionError:
            clear_pending()
            return "Location access was denied. The user declined the browser permission prompt."
        except TimeoutError:
            clear_pending()
            return "Location request timed out — no response from the browser. Try again."


async def _reverse_geocode(lat: float, lng: float) -> str:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                "https://api.bigdatacloud.net/data/reverse-geocode-client",
                params={"latitude": lat, "longitude": lng, "localityLanguage": "en"},
            )
            resp.raise_for_status()
            data = resp.json()

            parts = []
            if data.get("city"):
                parts.append(data["city"])
            if data.get("principalSubdivision"):
                parts.append(data["principalSubdivision"])
            if data.get("countryName"):
                parts.append(f"{data['countryName']} ({data.get('countryCode', '')})")
            parts.append(f"GPS: {lat:.3f}, {lng:.3f}")

            return "Location: " + ", ".join(parts) if parts else f"Coordinates: {lat}, {lng}"
    except Exception:
        return f"GPS coordinates: {lat}, {lng} (reverse geocode failed)"
