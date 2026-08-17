"""Current-time tool for answering 'today', 'now', weekday questions."""
from __future__ import annotations

from typing import Any

from src.tools.registry import ToolContext, default_registry, tool


@tool(
    default_registry,
    name="get_current_time",
    description=(
        "Return the current wall-clock time. Uses the server default "
        "timezone (DEFAULT_TIMEZONE env) unless 'timezone' is provided. "
        "Useful for 'today', 'now', 'this week', or weekday questions."
    ),
    parameters={
        "type": "object",
        "properties": {
            "timezone": {
                "type": "string",
                "description": (
                    "Optional IANA timezone (e.g. 'Asia/Seoul', 'UTC', "
                    "'America/New_York'). Omit to use the server default."
                ),
            }
        },
        "required": [],
    },
)
def get_current_time(ctx: ToolContext, timezone: str | None = None) -> dict[str, Any]:
    from datetime import datetime
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    tz_name = timezone or ctx.settings.default_timezone
    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"unknown timezone: {tz_name}") from exc
    now = datetime.now(tz)
    return {
        "iso": now.isoformat(timespec="seconds"),
        "timezone": tz_name,
        "weekday": now.strftime("%A"),
        "unix": int(now.timestamp()),
    }
