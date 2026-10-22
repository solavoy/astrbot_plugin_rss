from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo


def format_published(timestamp: float | int | None, timezone: str = "Asia/Shanghai") -> str:
    if timestamp is None:
        return ""
    zone = ZoneInfo(timezone or "Asia/Shanghai")
    return datetime.fromtimestamp(timestamp, UTC).astimezone(zone).strftime("%Y-%m-%d %H:%M")
