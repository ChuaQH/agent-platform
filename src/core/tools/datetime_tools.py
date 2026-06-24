"""Shared datetime utility tools — usable by all agents."""

from __future__ import annotations

import calendar
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from core.tool_framework import Tool


def _get_local_tz() -> timezone:
    try:
        import tzlocal
        return tzlocal.get_localzone()
    except Exception:
        return timezone.utc


def _resolve_timezone(tz_name: str) -> tuple[timezone, str]:
    """Return (timezone, source) where source is 'explicit', 'browser', or 'system'."""
    import logging
    _log = logging.getLogger(__name__)
    if tz_name:
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(tz_name), "explicit"
        except Exception:
            pass
    try:
        from core.user_context import get_timezone
        browser_tz = get_timezone()
        _log.info("_resolve_timezone: browser_tz=%s", browser_tz)
        if browser_tz:
            try:
                from zoneinfo import ZoneInfo
                return ZoneInfo(browser_tz), "browser"
            except Exception as e:
                _log.info("_resolve_timezone: ZoneInfo failed for %s: %s", browser_tz, e)
    except Exception as e:
        _log.info("_resolve_timezone: get_timezone failed: %s", e)
    return _get_local_tz(), "system"


def _get_system_locale() -> str:
    try:
        import locale
        loc = locale.getlocale(locale.LC_TIME)
        return loc[0] or "en_US"
    except Exception:
        return "en_US"


class CurrentDatetimeTool(Tool):
    name = "get_current_datetime"
    description = (
        "Get the current date and time. "
        "Accepts an optional IANA timezone (e.g. 'America/New_York', 'Asia/Tokyo'). "
        "If omitted, uses the system's local timezone. "
        "Returns ISO 8601 formatted datetime with timezone."
    )
    parameters = {
        "type": "object",
        "properties": {
            "timezone": {
                "type": "string",
                "description": "Optional IANA timezone name, e.g. 'America/New_York'.",
            },
        },
    }

    async def execute(self, timezone: str = "", **kwargs: Any) -> str:
        tz, source = _resolve_timezone(timezone)
        now = datetime.now(tz)
        lines = [
            f"Current datetime: {now.isoformat(timespec='seconds')}",
            f"Date: {now.strftime('%A, %B %d, %Y')}",
            f"Time: {now.strftime('%I:%M:%S %p')}",
            f"Timezone: {tz}",
        ]
        if source == "system":
            lines.append("⚠️ Using system default timezone — may not match user's actual location.")
        return "\n".join(lines)


class UserTimezoneTool(Tool):
    name = "get_user_timezone"
    description = (
        "Get the user's local timezone. "
        "Accepts an optional IANA timezone to validate. "
        "If omitted, auto-detects from the system."
    )
    parameters = {
        "type": "object",
        "properties": {
            "timezone": {
                "type": "string",
                "description": "Optional IANA timezone name to validate.",
            },
        },
    }

    async def execute(self, timezone: str = "", **kwargs: Any) -> str:
        tz, source = _resolve_timezone(timezone)
        now = datetime.now(tz)
        offset = now.utcoffset()
        if offset is not None:
            hours = offset.total_seconds() / 3600
            sign = "+" if hours >= 0 else "-"
            offset_str = f"UTC{sign}{abs(hours):02.0f}:{abs(offset.total_seconds()) % 3600 // 60:02.0f}"
        else:
            offset_str = str(tz)
        lines = [f"Timezone: {tz} ({offset_str})"]
        if source == "system":
            lines.append("⚠️ Using system default timezone — may not match user's actual location.")
        return "\n".join(lines)


class UserLocaleTool(Tool):
    name = "get_user_locale"
    description = "Get the user's system locale."
    parameters = {
        "type": "object",
        "properties": {},
    }

    async def execute(self, **kwargs: Any) -> str:
        loc = _get_system_locale()
        return f"Locale: {loc}"


class DateMathTool(Tool):
    name = "date_math"
    description = (
        "Perform date arithmetic. Provide a natural language expression. "
        "Accepts an optional IANA timezone (e.g. 'America/New_York'). "
        "Supports: 'now', 'today', 'tomorrow', 'yesterday', "
        "'in N days/weeks/months/years', 'N days/weeks ago', "
        "'next Monday/Friday', 'last Monday', 'first day of next month', "
        "'last day of month', and combinations like '3 days from now'."
    )
    parameters = {
        "type": "object",
        "properties": {
            "expression": {
                "type": "string",
                "description": "A natural language date expression to evaluate.",
            },
            "timezone": {
                "type": "string",
                "description": "Optional IANA timezone name, e.g. 'America/New_York'.",
            },
        },
        "required": ["expression"],
    }

    async def execute(self, expression: str, timezone: str = "", **kwargs: Any) -> str:
        try:
            tz, source = _resolve_timezone(timezone)
            result = _parse_date_expression(expression.strip().lower(), tz)
            lines = [
                f"Expression: {expression}",
                f"Result: {result.isoformat(timespec='seconds')}",
                f"Formatted: {result.strftime('%A, %B %d, %Y')}",
            ]
            if source == "system":
                lines.append("⚠️ Using system default timezone — may not match user's actual location.")
            return "\n".join(lines)
        except Exception as e:
            return f"Error parsing date expression '{expression}': {e}"


def _parse_date_expression(expr: str, tz: timezone) -> datetime:
    now = datetime.now(tz)

    expr = expr.strip().lower()
    if expr in ("now", "today"):
        return now
    if expr == "tomorrow":
        return now + timedelta(days=1)
    if expr == "yesterday":
        return now - timedelta(days=1)

    m = re.match(r'in\s+(\d+)\s+(minute|minutes|hour|hours|day|days|week|weeks|month|months|year|years)', expr)
    if m:
        return _add_duration(now, int(m.group(1)), m.group(2))

    m = re.match(r'(\d+)\s+(minute|minutes|hour|hours|day|days|week|weeks|month|months|year|years)\s+ago', expr)
    if m:
        return _add_duration(now, -int(m.group(1)), m.group(2))

    m = re.match(r'(\d+)\s+(minute|minutes|hour|hours|day|days|week|weeks|month|months|year|years)\s+from\s+now', expr)
    if m:
        return _add_duration(now, int(m.group(1)), m.group(2))

    m = re.match(r'next\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)', expr)
    if m:
        return _next_weekday(now, m.group(1))

    m = re.match(r'last\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)', expr)
    if m:
        return _last_weekday(now, m.group(1))

    if expr == 'first day of next month':
        if now.month == 12:
            return datetime(now.year + 1, 1, 1, tzinfo=tz)
        return datetime(now.year, now.month + 1, 1, tzinfo=tz)

    if expr in ('last day of month', 'end of month'):
        _, last_day = calendar.monthrange(now.year, now.month)
        return datetime(now.year, now.month, last_day, tzinfo=tz)

    if expr == 'start of week':
        return now - timedelta(days=now.weekday())

    if expr == 'end of week':
        return now + timedelta(days=6 - now.weekday())

    raise ValueError(f"Unrecognized date expression: {expr}")


def _add_duration(dt: datetime, amount: int, unit: str) -> datetime:
    unit = unit.rstrip("s")
    if unit == "minute":
        return dt + timedelta(minutes=amount)
    if unit == "hour":
        return dt + timedelta(hours=amount)
    if unit == "day":
        return dt + timedelta(days=amount)
    if unit == "week":
        return dt + timedelta(weeks=amount)
    if unit == "month":
        new_month = dt.month - 1 + amount
        year = dt.year + new_month // 12
        month = new_month % 12 + 1
        day = min(dt.day, calendar.monthrange(year, month)[1])
        return dt.replace(year=year, month=month, day=day)
    if unit == "year":
        return dt.replace(year=dt.year + amount)
    return dt


def _next_weekday(dt: datetime, weekday: str) -> datetime:
    target = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"].index(weekday)
    days_ahead = target - dt.weekday()
    if days_ahead <= 0:
        days_ahead += 7
    return dt + timedelta(days=days_ahead)


def _last_weekday(dt: datetime, weekday: str) -> datetime:
    target = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"].index(weekday)
    days_behind = dt.weekday() - target
    if days_behind <= 0:
        days_behind += 7
    return dt - timedelta(days=days_behind)
