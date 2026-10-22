from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1].parent))

from astrbot_plugin_rss.core.models import Article
from astrbot_plugin_rss.core.runtime import RSSRuntime
from astrbot_plugin_rss.services.management import ManagementService
from astrbot_plugin_rss.storage.management_repository import ManagementRepository


@pytest.mark.asyncio
async def test_feed_latest_articles_use_effective_time_before_sql_limits(tmp_path: Path) -> None:
    runtime = RSSRuntime({}, tmp_path / "feed-order")
    await runtime.initialize()
    try:
        feed = await runtime.repository.create_feed("https://example.org/feed-order", title="Feed")
        articles = [
            Article(
                "old-sequence-new-publication",
                feed.feed_id,
                "entry-special",
                "https://example.org/special",
                "Newest by publication",
                published_at=10_000,
                updated_at=1_000,
                first_seen_at=1,
            ),
            Article(
                "updated-fallback",
                feed.feed_id,
                "entry-updated",
                "https://example.org/updated",
                "Updated fallback",
                published_at=None,
                updated_at=101,
                first_seen_at=9_000,
            ),
            Article(
                "first-seen-fallback",
                feed.feed_id,
                "entry-first-seen",
                "https://example.org/first-seen",
                "First seen fallback",
                published_at=None,
                updated_at=None,
                first_seen_at=100.5,
            ),
            Article(
                "epoch-zero-is-valid",
                feed.feed_id,
                "entry-zero",
                "https://example.org/zero",
                "Epoch zero",
                published_at=0,
                updated_at=99_999,
                first_seen_at=99_998,
            ),
        ]
        articles.extend(
            Article(
                f"article-{index}",
                feed.feed_id,
                f"entry-{index}",
                f"https://example.org/{index}",
                f"Article {index}",
                published_at=97 if index == 4 else 100 - index,
                first_seen_at=5_000 + index,
            )
            for index in range(3, 56)
        )
        await runtime.repository.save_articles(feed.feed_id, articles)

        latest = await runtime.list_feed_articles(feed.feed_id, limit=5)
        assert [article.article_id for article in latest] == [
            "old-sequence-new-publication",
            "updated-fallback",
            "first-seen-fallback",
            "article-4",
            "article-3",
        ]

        detail = await ManagementRepository(runtime).feed_detail(feed.feed_id)
        article_ids = [row["article_id"] for row in detail["articles"]]
        assert len(article_ids) == 50
        assert article_ids[:5] == [
            "old-sequence-new-publication",
            "updated-fallback",
            "first-seen-fallback",
            "article-4",
            "article-3",
        ]
        assert "epoch-zero-is-valid" not in article_ids
        assert article_ids[-1] == "article-49"

        dashboard = await ManagementRepository(runtime).dashboard()
        assert dashboard["recent_articles"][0]["article_id"] == "old-sequence-new-publication"

        for fetched_at in (10, 20, 20):
            await runtime.repository.log_fetch(feed.feed_id, fetched_at, 200, "curl", 1)
        feed_detail = await ManagementRepository(runtime).feed_detail(feed.feed_id)
        assert [row["fetched_at"] for row in feed_detail["fetch_logs"][:3]] == [20, 20, 10]
        assert feed_detail["fetch_logs"][0]["id"] > feed_detail["fetch_logs"][1]["id"]

        target = await runtime.repository.create_target("instance", "origin", "test", "session", "group", "Target")
        article_id = "old-sequence-new-publication"
        delivery_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{target.target_id}:{article_id}"))
        async with runtime.database.transaction() as conn:
            await conn.execute(
                "INSERT INTO deliveries(delivery_id,target_id,article_id,source_path,idempotency_key,created_at,updated_at) "
                "VALUES(?,?,?,'direct:test',?,?,?)",
                (delivery_id, target.target_id, article_id, delivery_id, 1, 1),
            )
            for attempted_at in (10, 20, 20):
                await conn.execute(
                    "INSERT INTO delivery_attempts(delivery_id,attempted_at,result) VALUES(?,?,'failed')",
                    (delivery_id, attempted_at),
                )
        delivery_detail = await ManagementRepository(runtime).delivery_detail(delivery_id)
        attempts = delivery_detail["attempts"]
        assert [attempt["attempted_at"] for attempt in attempts] == [20, 20, 10]
        assert attempts[0]["attempt_id"] > attempts[1]["attempt_id"]
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_list_preview_selects_global_latest_twenty_across_feeds(tmp_path: Path) -> None:
    runtime = RSSRuntime({}, tmp_path / "list-order")
    await runtime.initialize()
    try:
        first = await runtime.repository.create_feed("https://example.org/list-first", title="First")
        second = await runtime.repository.create_feed("https://example.org/list-second", title="Second")
        list_id = await runtime.repository.create_list("Combined")
        await runtime.repository.add_list_feed(list_id, first.feed_id)
        await runtime.repository.add_list_feed(list_id, second.feed_id)

        await runtime.repository.save_articles(
            first.feed_id,
            [
                Article(
                    f"first-{index}",
                    first.feed_id,
                    f"first-entry-{index}",
                    f"https://example.org/first/{index}",
                    f"First {index}",
                    published_at=230 - index,
                )
                for index in range(1, 10)
            ],
        )
        await runtime.repository.save_articles(
            second.feed_id,
            [
                Article(
                    f"second-{index}",
                    second.feed_id,
                    f"second-entry-{index}",
                    f"https://example.org/second/{index}",
                    f"Second {index}",
                    published_at=221 - index,
                )
                for index in range(1, 26)
            ],
        )

        class Registry:
            async def render(self, articles, target, source_name, is_list=False):
                return [{"article_ids": [article.article_id for article in articles]}]

        runtime.registry = Registry()
        result = await ManagementService(runtime)._preview_list(list_id, None)
        rendered_ids = result["messages"][0]["article_ids"]
        assert len(rendered_ids) == 20
        assert rendered_ids[:3] == ["first-1", "first-2", "first-3"]
        assert rendered_ids[9:] == [f"second-{index}" for index in range(1, 12)]

        list_detail = await ManagementRepository(runtime).list_detail(list_id)
        assert [row["article_id"] for row in list_detail["articles"][:20]] == rendered_ids
    finally:
        await runtime.close()
