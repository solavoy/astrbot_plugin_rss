from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1].parent))

from astrbot_plugin_rss.core.models import Article, FetchResult
from astrbot_plugin_rss.core.runtime import RSSRuntime
from astrbot_plugin_rss.parsers.feed_parser import FeedParser
from astrbot_plugin_rss.services.management import ManagementService


@pytest.mark.asyncio
async def test_feed_article_listing_uses_one_join_and_keeps_current_feed_title(tmp_path, monkeypatch):
    runtime = RSSRuntime({}, tmp_path / "feed-articles")
    await runtime.initialize()
    try:
        feed = await runtime.repository.create_feed("https://example.org/feed", title="Old title")
        await runtime.repository.save_articles(
            feed.feed_id,
            [
                Article("older", feed.feed_id, "older", "https://example.org/old", "Older", published_at=1),
                Article("newer", feed.feed_id, "newer", "https://example.org/new", "Newer", published_at=2),
            ],
        )
        async with runtime.database.transaction() as conn:
            await conn.execute("UPDATE feeds SET title='Current title' WHERE feed_id=?", (feed.feed_id,))

        original_fetchall = runtime.database.fetchall
        joined_reads = 0

        async def count_joined_reads(sql, params=()):
            nonlocal joined_reads
            if "FROM articles a JOIN feeds f" in sql:
                joined_reads += 1
            return await original_fetchall(sql, params)

        async def reject_per_article_lookup(_article_id):
            raise AssertionError("list_feed_articles must not issue per-article lookups")

        monkeypatch.setattr(runtime.database, "fetchall", count_joined_reads)
        monkeypatch.setattr(runtime.repository, "article", reject_per_article_lookup)
        articles = await runtime.list_feed_articles(feed.feed_id, limit=1)

        assert joined_reads == 1
        assert [article.article_id for article in articles] == ["newer"]
        assert articles[0].metadata["feed_title"] == "Current title"
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_article_cleanup_removes_orphan_telegraph_pages_only(tmp_path):
    runtime = RSSRuntime({}, tmp_path / "telegraph-cleanup")
    await runtime.initialize()
    try:
        feed = await runtime.repository.create_feed("https://example.org/telegraph")
        await runtime.repository.save_articles(
            feed.feed_id,
            [
                Article("expired", feed.feed_id, "expired", "https://example.org/expired", "Expired", first_seen_at=0),
                Article("live", feed.feed_id, "live", "https://example.org/live", "Live"),
            ],
        )
        async with runtime.database.transaction() as conn:
            for article_id, content_hash in (
                ("expired", "expired-hash"),
                ("live", "live-hash"),
                ("missing", "old-hash"),
            ):
                await conn.execute(
                    "INSERT INTO telegraph_pages(article_id,content_hash,url,created_at) VALUES(?,?,?,?)",
                    (article_id, content_hash, f"https://telegra.ph/{article_id}", 1),
                )

        result = await runtime.cleanup("articles")
        cached = await runtime.database.fetchall("SELECT article_id FROM telegraph_pages ORDER BY article_id")

        assert result["articles"] == 1
        assert result["telegraph_pages"] == 2
        assert [row[0] for row in cached] == ["live"]
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_maintenance_logs_safe_failure_type_and_continues(tmp_path, caplog):
    runtime = RSSRuntime({"cookie_cleanup_interval": 1}, tmp_path / "maintenance-log")
    await runtime.initialize()
    calls = 0

    async def fail_once_then_stop():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("sensitive URL token=do-not-log")
        runtime._stop.set()

    runtime.run_maintenance_once = fail_once_then_stop
    try:
        with caplog.at_level(logging.WARNING, logger="astrbot_plugin_rss.core.runtime"):
            await asyncio.wait_for(runtime._maintenance_loop(), timeout=2)

        assert calls == 2
        assert "run_maintenance_once" in caplog.text
        assert "RuntimeError" in caplog.text
        assert "do-not-log" not in caplog.text
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_management_all_cleanup_includes_cookies_media_and_database(tmp_path):
    runtime = RSSRuntime({}, tmp_path / "all-cleanup")
    await runtime.initialize()

    class CookieCleaner:
        def clear_expired(self):
            return 2

    class MediaCleaner:
        def cleanup(self):
            return 3

    try:
        feed = await runtime.repository.create_feed("https://example.org/all-cleanup")
        await runtime.repository.save_articles(
            feed.feed_id,
            [Article("expired", feed.feed_id, "expired", "https://example.org/expired", "Expired", first_seen_at=0)],
        )
        await runtime.repository.set_platform_media_cache(
            "instance", "https://example.org/expired.png", "image", "key", "app", 0
        )
        runtime.fetcher = SimpleNamespace(cookies=CookieCleaner())
        runtime.media_downloader = MediaCleaner()

        result = await ManagementService(runtime).post("data/cleanup", {"confirm": True, "kind": "all"})

        assert result["kind"] == "all"
        assert result["articles"] == 1
        assert {"deliveries", "batches", "test_operations", "attempts", "fetch_logs"} <= result.keys()
        assert result["cookies"] == 2
        assert result["media"] == 3
        assert result["platform_media"] == 1
        assert await runtime.database.fetchone("SELECT 1 FROM platform_media_cache") is None
    finally:
        await runtime.close()


def test_feedparser_does_not_synthesize_updated_date_from_published_date():
    xml = b"""<rss version="2.0"><channel><title>Only published</title><link>https://example.org/</link>
    <description>Test</description><item><guid>published-only</guid><title>Published</title>
    <link>https://example.org/published</link><pubDate>Mon, 01 Jan 2024 12:00:00 GMT</pubDate>
    </item></channel></rss>"""
    parsed = FeedParser().parse(FetchResult("feed", "https://example.org/rss", status_code=200, raw_xml=xml))

    article = parsed.articles[0]
    assert article.published_at == 1_704_110_400.0
    assert article.updated_at is None


def test_feedparser_uses_updated_date_as_published_fallback():
    xml = b"""<feed xmlns="http://www.w3.org/2005/Atom"><title>Only updated</title>
    <id>https://example.org/</id><entry><id>updated-only</id><title>Updated</title>
    <link href="https://example.org/updated"/><updated>2024-01-01T12:00:00Z</updated>
    </entry></feed>"""
    parsed = FeedParser().parse(FetchResult("feed", "https://example.org/atom", status_code=200, raw_xml=xml))

    article = parsed.articles[0]
    assert article.published_at == 1_704_110_400.0
    assert article.updated_at == 1_704_110_400.0


def test_management_date_filters_reject_nonfinite_timestamps_and_date_overflow():
    service = object.__new__(ManagementService)
    service.runtime = SimpleNamespace(config=SimpleNamespace(timezone="UTC"))

    for value in (float("nan"), float("inf"), float("-inf"), "nan", "Infinity"):
        with pytest.raises(ValueError, match="finite timestamp"):
            service._date_filter("date_from", value)
    with pytest.raises(ValueError, match="date_to must be a date"):
        service._date_filter("date_to", "9999-12-31")
