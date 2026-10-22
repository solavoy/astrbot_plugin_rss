from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1].parent))

from astrbot_plugin_rss.core.config import PluginConfig
from astrbot_plugin_rss.core.models import now_ts
from astrbot_plugin_rss.core.runtime import RSSRuntime
from astrbot_plugin_rss.storage.management_repository import ManagementRepository


@pytest.mark.asyncio
async def test_dashboard_deduplicates_recent_errors_without_mutating_history(tmp_path: Path) -> None:
    runtime = RSSRuntime(PluginConfig.from_mapping({}), tmp_path)
    await runtime.initialize()
    try:
        repeated_feed = await runtime.repository.create_feed("https://example.org/repeated-rss", "Repeated")
        other_feed = await runtime.repository.create_feed("https://example.org/other-rss", "Other")
        base = now_ts() - 100

        # More than ten recent 429 rows differ in message and error_type, but
        # they represent the same feed/status group.
        for index in range(12):
            await runtime.repository.log_fetch(
                repeated_feed.feed_id,
                base + index,
                429,
                "curl",
                10,
                error_type=f"http_{index % 3}",
                error_message=f"429 message {index}",
            )
        # Equal timestamps are resolved by the larger fetch_logs.id.
        await runtime.repository.log_fetch(
            repeated_feed.feed_id,
            base + 11,
            429,
            "curl",
            10,
            error_type="changed_http_type",
            error_message="tie winner",
        )
        await runtime.repository.log_fetch(repeated_feed.feed_id, base + 12, 403, "curl", 10, "http_error", "forbidden")
        await runtime.repository.log_fetch(other_feed.feed_id, base + 13, 429, "curl", 10, "http_error", "other feed")
        await runtime.repository.log_fetch(
            repeated_feed.feed_id, base + 14, 200, "curl", 10, "parse_error", "parse detail"
        )
        await runtime.repository.log_fetch(
            repeated_feed.feed_id, base + 15, 0, "curl", 10, "network_error", "first network detail"
        )
        await runtime.repository.log_fetch(
            repeated_feed.feed_id, base + 16, 0, "curl", 10, "network_error", "newest network detail"
        )
        for offset, status in enumerate(range(418, 425), start=1):
            await runtime.repository.log_fetch(
                repeated_feed.feed_id,
                base - 8 + offset,
                status,
                "curl",
                10,
                "http_error",
                f"extra {status}",
            )

        before = await runtime.database.fetchone("SELECT count(*) FROM fetch_logs")
        dashboard = await ManagementRepository(runtime).dashboard()
        after = await runtime.database.fetchone("SELECT count(*) FROM fetch_logs")

        rows = dashboard["recent_errors"]
        assert before[0] == after[0] == 25
        assert len(rows) == 10
        feed_429 = [row for row in rows if row["feed_id"] == repeated_feed.feed_id and row["status_code"] == 429]
        assert len(feed_429) == 1
        assert feed_429[0]["error_message"] == "tie winner"
        assert any(row["feed_id"] == repeated_feed.feed_id and row["status_code"] == 403 for row in rows)
        assert any(row["feed_id"] == other_feed.feed_id and row["status_code"] == 429 for row in rows)
        assert any(row["error_type"] == "parse_error" for row in rows)
        network = [row for row in rows if row["error_type"] == "network_error"]
        assert len(network) == 1 and network[0]["error_message"] == "newest network detail"
        assert not any(row["status_code"] in {418, 419} for row in rows)
        assert dashboard["fetch_stats"]["last_hour"] == 25
        assert dashboard["fetch_stats"]["failures"] == 25
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_feed_management_reads_include_boolean_translation_setting(tmp_path: Path) -> None:
    runtime = RSSRuntime(PluginConfig.from_mapping({}), tmp_path)
    await runtime.initialize()
    try:
        feed = await runtime.repository.create_feed("https://example.org/translation-setting", translation_enabled=True)
        management = ManagementRepository(runtime)
        page = await management.feed_page("", 1, 20, "all")
        detail = await management.feed_detail(feed.feed_id)
        assert page["items"][0]["translation_enabled"] is True
        assert detail["feed"]["translation_enabled"] is True
        assert (await runtime.repository.list_feeds())[0]["translation_enabled"] is True
    finally:
        await runtime.close()
