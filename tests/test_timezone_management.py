from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1].parent))

from astrbot_plugin_rss.core.runtime import RSSRuntime
from astrbot_plugin_rss.services.management import ManagementService
from astrbot_plugin_rss.storage.management_repository import _local_day_start


@pytest.mark.asyncio
async def test_article_management_responses_expose_configured_timezone(tmp_path: Path) -> None:
    runtime = RSSRuntime({"general": {"timezone": "America/New_York"}}, tmp_path / "timezone-api")
    await runtime.initialize()
    try:
        feed = await runtime.repository.create_feed("https://example.org/timezone", title="Timezone")
        list_id = await runtime.repository.create_list("Timezone list")
        await runtime.repository.add_list_feed(list_id, feed.feed_id)
        service = ManagementService(runtime)

        assert (await service.get("dashboard", {}))["timezone"] == "America/New_York"
        assert (await service.get("feeds", {}))["timezone"] == "America/New_York"
        assert (await service.get(f"feeds/{feed.public_id}", {}))["timezone"] == "America/New_York"
        assert (await service.get("lists", {}))["timezone"] == "America/New_York"
        assert (await service.get("lists/1", {}))["timezone"] == "America/New_York"
        assert (await service._preview_feed(feed.feed_id, None))["timezone"] == "America/New_York"
        assert (await service._preview_list(list_id, None))["timezone"] == "America/New_York"
    finally:
        await runtime.close()


def test_management_date_filters_use_timezone_midnights_across_dst() -> None:
    service = object.__new__(ManagementService)
    service.runtime = SimpleNamespace(config=SimpleNamespace(timezone="America/New_York"))
    start_op, new_york_start = service._date_filter("date_from", "2026-03-08")
    end_op, new_york_end = service._date_filter("date_to", "2026-03-08")
    assert start_op == ">="
    assert end_op == "<"
    assert new_york_start == 1_772_946_000  # 2026-03-08 00:00 EST
    assert new_york_end == 1_773_028_800  # 2026-03-09 00:00 EDT
    assert new_york_end - new_york_start == 23 * 60 * 60

    service.runtime.config.timezone = "UTC"
    _, utc_start = service._date_filter("date_from", "2026-03-08")
    _, utc_end = service._date_filter("date_to", "2026-03-08")
    assert utc_end - utc_start == 24 * 60 * 60

    timestamp = datetime(2026, 1, 1, 1, 30, tzinfo=UTC).timestamp()
    shanghai_start = datetime(2025, 12, 31, 16, 0, tzinfo=UTC).timestamp()
    utc_start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC).timestamp()
    assert _local_day_start(timestamp, "Asia/Shanghai") == shanghai_start
    assert _local_day_start(timestamp, "UTC") == utc_start
    assert _local_day_start(new_york_end, "America/New_York") == new_york_end
    assert _local_day_start(new_york_start, "America/New_York") == new_york_start
