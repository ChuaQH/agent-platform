"""Shared user context — browser-provided data accessible to core tools.

Frontends (e.g. Chainlit app.py) push browser geolocation / timezone here.
Tools pull it. The request_browser_location() / resolve / reject pattern
enables on-demand browser permission prompts.
"""

from __future__ import annotations

import asyncio

_location: dict | None = None
_timezone: str | None = None
_pending_future: asyncio.Future | None = None
_requested: bool = False


def get_location() -> dict | None:
    return _location


def get_timezone() -> str | None:
    return _timezone


def set_location(lat: float, lng: float) -> None:
    global _location
    _location = {"lat": lat, "lng": lng}


def set_timezone(tz: str) -> None:
    global _timezone
    _timezone = tz


def is_pending() -> bool:
    return _requested and not _location


async def request_location() -> dict:
    """Block until browser provides location or user denies."""
    global _requested, _pending_future
    _requested = True
    if _location:
        return _location
    loop = asyncio.get_running_loop()
    _pending_future = loop.create_future()
    try:
        result = await asyncio.wait_for(_pending_future, timeout=120.0)
        return result
    except asyncio.TimeoutError:
        raise TimeoutError("Location request timed out — no response from browser.")
    finally:
        _pending_future = None


def resolve_location(lat: float, lng: float) -> None:
    global _location
    _location = {"lat": lat, "lng": lng}
    if _pending_future and not _pending_future.done():
        _pending_future.set_result(_location)


def reject_location() -> None:
    if _pending_future and not _pending_future.done():
        _pending_future.set_exception(PermissionError("User denied geolocation permission"))


def clear_pending() -> None:
    global _requested
    _requested = False


def reset_state() -> None:
    """Reset location state — call at session start. Timezone persists."""
    global _location, _pending_future, _requested
    _location = None
    _pending_future = None
    _requested = False
