from __future__ import annotations

import re

_INTERVAL_PATTERN = re.compile(r"([0-9]+)\s*([dhms]?)", flags=re.ASCII | re.IGNORECASE)
_UNIT_SECONDS = {"d": 86400, "h": 3600, "m": 60, "s": 1, "": 1}


def parse_interval_seconds(value: object) -> int:
    """Convert an integer interval with an optional d/h/m/s suffix to seconds."""
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, str):
        match = _INTERVAL_PATTERN.fullmatch(value.strip())
        if match:
            try:
                return int(match[1]) * _UNIT_SECONDS[match[2].lower()]
            except ValueError:
                pass
    raise ValueError("抓取间隔需为非负整数，可带 d/h/m/s 单位（天/时/分/秒），无单位按秒")
