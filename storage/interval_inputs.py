from __future__ import annotations

import json

from ..core.intervals import parse_interval_seconds


def interval_input_key(entity: str, entity_id: str) -> str:
    return f"poll_interval_input:{entity}:{entity_id}"


def interval_input_text(value, seconds: int | None) -> str:
    """Keep valid input spelling independently of the effective interval."""
    if value is not None and type(value) not in (int, str):
        raise ValueError("poll_interval_input must be an interval string or integer")
    text = value if isinstance(value, str) else str(value or 0)
    if (parse_interval_seconds(text) or None) != (seconds or None):
        raise ValueError("poll_interval_input must match poll_interval")
    return text


async def save_interval_input(connection, entity: str, entity_id: str, seconds: int | None, value=None):
    text = interval_input_text(value if value is not None else seconds, seconds)
    await connection.execute(
        "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (interval_input_key(entity, entity_id), json.dumps({"seconds": seconds or None, "input": text})),
    )


async def attach_interval_inputs(database, entity: str, rows: list[dict]):
    if not rows:
        return
    keys = {interval_input_key(entity, row[f"{entity}_id"]): row for row in rows}
    for row in rows:
        row["poll_interval_input"] = str(row["poll_interval"] or 0)
    # Import/export can include many more rows than a management page.
    key_list = list(keys)
    for start in range(0, len(key_list), 500):
        batch = key_list[start : start + 500]
        saved = await database.fetchall(
            f"SELECT key,value FROM settings WHERE key IN ({','.join('?' for _ in batch)})", tuple(batch)
        )
        for record in saved:
            row = keys[record["key"]]
            try:
                data = json.loads(record["value"])
                if isinstance(data, dict) and data.get("seconds") == (row["poll_interval"] or None):
                    text = data.get("input")
                    if isinstance(text, str):
                        row["poll_interval_input"] = interval_input_text(text, row["poll_interval"])
            except (TypeError, ValueError):
                # Old rows and stale/corrupt display metadata fall back to seconds.
                continue
