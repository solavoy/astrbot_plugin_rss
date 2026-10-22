import pytest
from astrbot_plugin_rss.core.config import PluginConfig
from astrbot_plugin_rss.core.errors import FeedFetchError
from astrbot_plugin_rss.core.models import Article, ContentBlock, FetchResult, ParsedFeed, now_ts
from astrbot_plugin_rss.core.runtime import RSSRuntime
from astrbot_plugin_rss.core.security import ValidatedURL
from astrbot_plugin_rss.storage.management_repository import ManagementRepository


class Fetcher:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    async def fetch(self, feed, force=False):
        self.calls += 1
        return self.result


class Parser:
    def __init__(self, parsed=None, error=None):
        self.parsed = parsed
        self.error = error

    def parse(self, result):
        if self.error:
            raise self.error
        return self.parsed


def make_article():
    return Article(
        "", "", "old-entry", "https://example.org/old", "old", content_blocks=[ContentBlock("text", text="old")]
    )


@pytest.mark.asyncio
async def test_feed_create_validates_before_insert_and_duplicate_does_not_refetch(tmp_path, monkeypatch):
    import astrbot_plugin_rss.services.facade as facade_module

    async def allow_url(_url):
        return ValidatedURL("https://example.org/rss", "example.org", 443, ())

    monkeypatch.setattr(facade_module, "validate_public_url", allow_url)
    fetcher = Fetcher(FetchResult("", "https://example.org/rss", status_code=200, raw_xml=b"<rss/>"))
    parser = Parser(ParsedFeed(title="Verified", description="verified", articles=[make_article()]))
    runtime = RSSRuntime(PluginConfig.from_mapping({}), tmp_path, fetcher=fetcher, parser=parser)
    await runtime.initialize()
    import asyncio

    created, concurrent = await asyncio.gather(
        runtime.services.feeds.create("https://EXAMPLE.org/rss", poll_interval="2h"),
        runtime.services.feeds.create("https://example.org/rss"),
    )
    assert created.initialized and created.title == "Verified"
    assert created.poll_interval == 7200
    assert concurrent.feed_id == created.feed_id
    assert len(await runtime.list_feed_articles(created.feed_id)) == 1
    assert fetcher.calls == 1
    target = await runtime.repository.create_target("p", "target", "test", "target")
    await runtime.services.subscriptions.subscribe(target.target_id, created.feed_id)
    assert await runtime.repository.pending_deliveries(10**9) == []
    await runtime.close()


@pytest.mark.asyncio
async def test_service_intervals_are_stored_as_seconds_and_zero_inherits(tmp_path):
    runtime = RSSRuntime({}, tmp_path)
    await runtime.initialize()
    try:
        feed = await runtime.repository.create_feed("https://example.org/interval-units")
        target = await runtime.repository.create_target("instance", "origin", "test", "session")
        list_id = await runtime.services.lists.create("Interval units", interval="2d")
        assert (await runtime.repository.get_list(list_id))["poll_interval"] == 172800

        updated = await runtime.services.feeds.update(feed.feed_id, poll_interval=" 3 H ")
        assert updated.poll_interval == 10800
        await runtime.services.lists.update(list_id, poll_interval="4m")
        assert (await runtime.repository.get_list(list_id))["poll_interval"] == 240
        await runtime.services.subscriptions.subscribe(target.target_id, feed.feed_id, interval="45s")
        await runtime.services.lists.subscribe(target.target_id, list_id, interval="90")
        assert (await runtime.repository.list_direct_subscriptions(target.target_id))[0]["poll_interval"] == 45
        row = await runtime.database.fetchone("SELECT poll_interval FROM list_subscriptions")
        assert row[0] == 90

        for inherited in (0, "0", "0d", None):
            updated = await runtime.services.feeds.update(feed.feed_id, poll_interval=inherited)
            assert updated.poll_interval is None
            await runtime.services.lists.update(list_id, poll_interval=inherited)
            await runtime.services.subscriptions.subscribe(target.target_id, feed.feed_id, interval=inherited)
            await runtime.services.lists.subscribe(target.target_id, list_id, interval=inherited)
            for table in ("subscription_lists", "direct_subscriptions", "list_subscriptions"):
                assert (await runtime.database.fetchone(f"SELECT poll_interval FROM {table}"))[0] is None

        for invalid in (-1, True, 1.5, "-1h", "1h30m", "1.5d", "1w", ""):
            with pytest.raises(ValueError):
                await runtime.services.feeds.update(feed.feed_id, poll_interval=invalid)
            with pytest.raises(ValueError):
                await runtime.services.lists.update(list_id, poll_interval=invalid)
            with pytest.raises(ValueError):
                await runtime.services.subscriptions.subscribe(target.target_id, feed.feed_id, interval=invalid)
            with pytest.raises(ValueError):
                await runtime.services.lists.subscribe(target.target_id, list_id, interval=invalid)
        assert (await runtime.repository.get_feed(feed.feed_id)).poll_interval is None
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_feed_create_rejects_invalid_response_without_persisting_feed(tmp_path, monkeypatch):
    import astrbot_plugin_rss.services.facade as facade_module

    async def allow_url(_url):
        return ValidatedURL("https://example.org/bad", "example.org", 443, ())

    monkeypatch.setattr(facade_module, "validate_public_url", allow_url)
    fetcher = Fetcher(
        FetchResult(
            "",
            "https://example.org/bad",
            status_code=200,
            raw_xml=b"not feed",
            fetch_method="browser",
            elapsed_ms=57,
            cookie_cache_hit=True,
            browser_attempted=True,
        )
    )
    runtime = RSSRuntime(
        PluginConfig.from_mapping({}),
        tmp_path,
        fetcher=fetcher,
        parser=Parser(error=ValueError("sensitive parser detail")),
    )
    await runtime.initialize()
    with pytest.raises(FeedFetchError, match="feed response is not a valid feed") as error:
        await runtime.services.feeds.create("https://example.org/bad")
    assert error.value.__cause__ is None
    assert await runtime.repository.get_feed_by_url("https://example.org/bad") is None
    logs = await runtime.database.fetchall("SELECT * FROM fetch_logs")
    assert len(logs) == 1
    assert logs[0]["error_type"] == "parse_error"
    assert logs[0]["error_message"] == "feed response is not a valid feed"
    assert "sensitive parser detail" not in logs[0]["error_message"]
    assert (logs[0]["method"], logs[0]["elapsed_ms"], logs[0]["cookie_cache_hit"], logs[0]["browser_attempted"]) == (
        "browser",
        57,
        1,
        1,
    )
    stats = (await ManagementRepository(runtime).dashboard())["fetch_stats"]
    assert stats["last_hour"] == 1
    assert stats["failures"] == 1
    assert stats["success_rate"] == 0
    await runtime.close()


@pytest.mark.asyncio
async def test_rsshub_route_is_stored_without_prefix_or_key_and_resolves_after_config_change(tmp_path, monkeypatch):
    import astrbot_plugin_rss.services.facade as facade_module

    checked = []

    async def allow_url(url):
        checked.append(url)
        return ValidatedURL(url, "rsshub.example", 443, ())

    monkeypatch.setattr(facade_module, "validate_public_url", allow_url)
    route = "/twitter/example?foo=bar"
    effective = "https://rsshub.example" + route + "&key=secret"
    result = FetchResult(
        "",
        effective,
        status_code=200,
        raw_xml=b"<rss/>",
        request_url_fingerprint="opaque-request-fingerprint",
    )
    fetcher = Fetcher(result)
    runtime = RSSRuntime(
        PluginConfig.from_mapping(
            {"rsshub": {"default_url_prefix": "https://rsshub.example", "rsshub_access_key": "secret"}}
        ),
        tmp_path,
        fetcher=fetcher,
        parser=Parser(ParsedFeed(title="RSSHub source", articles=[])),
    )
    await runtime.initialize()
    try:
        first = await runtime.services.feeds.create(route)
        duplicate = await runtime.services.feeds.create(route)
        assert first.feed_id == duplicate.feed_id
        assert fetcher.calls == 1
        assert checked == [effective]
        assert first.url == route
        assert first.metadata["request_url_fingerprint"] == "opaque-request-fingerprint"
        assert "secret" not in first.url

        runtime.config.default_url_prefix = ""
        runtime.config.rsshub_access_key = ""
        assert (await runtime.services.feeds.resolve(route)).feed_id == first.feed_id
        assert (await runtime.services.feeds.create(route)).feed_id == first.feed_id
        with pytest.raises(KeyError):
            await runtime.services.feeds.resolve("/twitter/missing")
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_rsshub_route_requires_prefix_but_absolute_url_is_unchanged(tmp_path, monkeypatch):
    import astrbot_plugin_rss.services.facade as facade_module

    checked = []

    async def allow_url(url):
        checked.append(url)
        return ValidatedURL(url, "example.org", 443, ())

    monkeypatch.setattr(facade_module, "validate_public_url", allow_url)
    fetcher = Fetcher(FetchResult("", "https://example.org/rss", status_code=200, raw_xml=b"<rss/>"))
    runtime = RSSRuntime(
        PluginConfig.from_mapping({}),
        tmp_path,
        fetcher=fetcher,
        parser=Parser(ParsedFeed(title="Example", articles=[])),
    )
    await runtime.initialize()
    try:
        with pytest.raises(ValueError, match="默认订阅地址前缀"):
            await runtime.services.feeds.create("/twitter/example")
        assert fetcher.calls == 0
        created = await runtime.services.feeds.create("https://example.org/rss")
        assert created.url == "https://example.org/rss"
        assert checked == ["https://example.org/rss"]
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_feed_url_update_accepts_rsshub_route_and_keeps_stored_url_key_free(tmp_path, monkeypatch):
    import astrbot_plugin_rss.services.facade as facade_module

    checked = []

    async def allow_url(url):
        checked.append(url)
        return ValidatedURL(url, "rsshub.example", 443, ())

    monkeypatch.setattr(facade_module, "validate_public_url", allow_url)
    runtime = RSSRuntime(
        PluginConfig.from_mapping(
            {"rsshub": {"default_url_prefix": "https://rsshub.example", "rsshub_access_key": "secret"}}
        ),
        tmp_path,
        fetcher=Fetcher(FetchResult("", "", status_code=200)),
        parser=Parser(),
    )
    await runtime.initialize()
    try:
        feed = await runtime.repository.create_feed("https://example.org/original")
        updated = await runtime.services.feeds.update(feed.feed_id, url="/twitter/example?lang=en")
        assert updated.url == "/twitter/example?lang=en"
        assert checked == ["https://rsshub.example/twitter/example?lang=en&key=secret"]
        with pytest.raises(ValueError, match="不要把 key 放入订阅地址"):
            await runtime.services.feeds.update(feed.feed_id, url="/twitter/example?key=private-token")
        stored = await runtime.repository.get_feed(feed.feed_id)
        assert stored.url == "/twitter/example?lang=en"
        assert "private-token" not in stored.url
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_first_persisted_fetch_sets_history_baseline_without_losing_new_entries(tmp_path):
    runtime = RSSRuntime(
        PluginConfig.from_mapping({}), tmp_path, fetcher=Fetcher(FetchResult("", "", status_code=200)), parser=Parser()
    )
    await runtime.initialize()
    feed = await runtime.repository.create_feed("https://example.org/first-fetch")
    target = await runtime.repository.create_target("p", "first-fetch", "test", "first-fetch")
    await runtime.repository.add_direct_subscription(target.target_id, feed.feed_id, send_history=True)
    list_id = await runtime.repository.create_list("first fetch list", interval=60)
    await runtime.repository.add_list_feed(list_id, feed.feed_id, send_history=True)
    await runtime.repository.subscribe_list(target.target_id, list_id, 60, send_history=True)
    result = FetchResult("", feed.url, status_code=200, raw_xml=b"valid")
    old = Article("", feed.feed_id, "old", "https://example.org/old", "old")
    runtime.parser = Parser(ParsedFeed(articles=[old]))
    await runtime._persist_fetch(feed, result)
    assert not await runtime.repository.delivery_has_active_path(target.target_id, old.article_id)
    new = Article("", feed.feed_id, "new", "https://example.org/new", "new")
    runtime.parser = Parser(ParsedFeed(articles=[new]))
    await runtime._persist_fetch(await runtime.repository.get_feed(feed.feed_id), result)
    assert await runtime.repository.enqueue_due_paths(now_ts() + 1, 300, 60) == 1
    assert (await runtime.repository.deliveries(target_id=target.target_id))[0]["article_id"] == new.article_id
    await runtime.close()


@pytest.mark.asyncio
async def test_cleanup_preserves_unknown_and_unqueued_slow_path_but_removes_terminal_history(tmp_path):
    runtime = RSSRuntime(
        PluginConfig.from_mapping({"history_retention_days": 1}),
        tmp_path,
        fetcher=Fetcher(FetchResult("", "", status_code=200)),
        parser=Parser(),
    )
    await runtime.initialize()
    feed = await runtime.repository.create_feed("https://example.org/history")
    target = await runtime.repository.create_target("p", "history", "test", "history")
    await runtime.repository.add_direct_subscription(target.target_id, feed.feed_id, send_history=True)
    first = make_article()
    await runtime.repository.save_articles(feed.feed_id, [first])
    await runtime.repository.enqueue_due_paths(now_ts() + 1, 300, 60)
    unknown = (await runtime.repository.deliveries(target_id=target.target_id))[0]
    await runtime.repository.mark_delivery(unknown["delivery_id"], "unknown", error="may have sent")
    async with runtime.database.transaction() as conn:
        await conn.execute("UPDATE deliveries SET updated_at=0 WHERE delivery_id=?", (unknown["delivery_id"],))

    terminal_article = Article("", feed.feed_id, "terminal", "https://example.org/terminal", "terminal")
    await runtime.repository.save_articles(feed.feed_id, [terminal_article])
    async with runtime.database.transaction() as conn:
        await runtime.repository._insert_delivery(conn, target.target_id, terminal_article.article_id, "test", 0)
        await conn.execute(
            "UPDATE deliveries SET status='failed',retryable=0,updated_at=0 WHERE article_id=?",
            (terminal_article.article_id,),
        )

    slow_feed = await runtime.repository.create_feed("https://example.org/slow-cleanup")
    list_id = await runtime.repository.create_list("slow cleanup", interval=600)
    await runtime.repository.add_list_feed(list_id, slow_feed.feed_id)
    await runtime.repository.subscribe_list(target.target_id, list_id)
    slow_article = Article("", slow_feed.feed_id, "slow", "https://example.org/slow", "slow")
    await runtime.repository.save_articles(slow_feed.feed_id, [slow_article])
    async with runtime.database.transaction() as conn:
        await conn.execute(
            "UPDATE articles SET first_seen_at=0 WHERE article_id IN (?,?)",
            (terminal_article.article_id, slow_article.article_id),
        )
        await conn.execute("UPDATE subscription_lists SET enabled=0 WHERE list_id=?", (list_id,))

    history = await runtime.cleanup("history")
    assert history["deliveries"] == 1
    assert await runtime.repository.delivery(unknown["delivery_id"]) is not None
    assert (await runtime.repository.article(terminal_article.article_id)) is not None
    await runtime.cleanup("articles")
    assert (await runtime.repository.article(slow_article.article_id)) is not None

    # A completed fast path is still a deduplication tombstone while a slower
    # qualifying path has not reached this article sequence.
    dedup_feed = await runtime.repository.create_feed("https://example.org/dedup-cleanup")
    await runtime.repository.add_direct_subscription(target.target_id, dedup_feed.feed_id, 60, send_history=True)
    list_id = await runtime.repository.create_list("dedup slow", interval=600)
    await runtime.repository.add_list_feed(list_id, dedup_feed.feed_id, send_history=True)
    await runtime.repository.subscribe_list(target.target_id, list_id, 600, send_history=True)
    dedup_article = Article("", dedup_feed.feed_id, "dedup", "https://example.org/dedup", "dedup")
    await runtime.repository.save_articles(dedup_feed.feed_id, [dedup_article])
    clock = now_ts()
    async with runtime.database.transaction() as conn:
        await conn.execute("UPDATE list_subscriptions SET next_check_at=? WHERE list_id=?", (clock + 600, list_id))
    await runtime.repository.enqueue_due_paths(clock + 1, 300, 60)
    dedup_delivery = (
        await runtime.repository.deliveries(target_id=target.target_id, article_id=dedup_article.article_id)
    )[0]
    await runtime.repository.mark_delivery(dedup_delivery["delivery_id"], "success")
    async with runtime.database.transaction() as conn:
        await conn.execute("UPDATE deliveries SET updated_at=0 WHERE delivery_id=?", (dedup_delivery["delivery_id"],))
    cleaned = await runtime.cleanup("history")
    assert cleaned["deliveries"] == 0
    await runtime.repository.enqueue_due_paths(clock + 601, 300, 60)
    assert (
        len(await runtime.repository.deliveries(target_id=target.target_id, article_id=dedup_article.article_id)) == 1
    )
    assert (await runtime.repository.delivery(dedup_delivery["delivery_id"]))["status"] == "success"
    await runtime.close()


@pytest.mark.asyncio
async def test_maintenance_runs_independently_of_scheduler_and_is_cancelled_on_close(tmp_path):
    runtime = RSSRuntime(
        PluginConfig.from_mapping({"enable_scheduler": False}),
        tmp_path,
        fetcher=Fetcher(FetchResult("", "", status_code=200)),
        parser=Parser(),
    )
    await runtime.initialize()
    result = await runtime.run_maintenance_once()
    assert {"cookies", "media", "articles", "deliveries", "batches"} <= result.keys()
    await runtime.start()
    assert any(task.get_name() == "rss-maintenance" for task in runtime._tasks)
    assert not any(task.get_name() == "rss-scheduler" for task in runtime._tasks)
    await runtime.close()
    assert not runtime._tasks


@pytest.mark.asyncio
async def test_maintenance_removes_only_expired_platform_media_keys(tmp_path):
    runtime = RSSRuntime(
        PluginConfig.from_mapping({}),
        tmp_path,
        fetcher=Fetcher(FetchResult("", "", status_code=200)),
        parser=Parser(),
    )
    await runtime.initialize()
    await runtime.repository.set_platform_media_cache(
        "instance", "https://example.org/old.png", "image", "old-key", "app", now_ts() - 1
    )
    await runtime.repository.set_platform_media_cache(
        "instance", "https://example.org/current.png", "image", "current-key", "app", now_ts() + 3600
    )
    result = await runtime.run_maintenance_once()
    assert result["platform_media"] == 1
    rows = await runtime.database.fetchall("SELECT media_url FROM platform_media_cache")
    assert [row[0] for row in rows] == ["https://example.org/current.png"]
    await runtime.close()


@pytest.mark.asyncio
async def test_persist_fetch_records_parse_failure_and_keeps_existing_validators(tmp_path):
    fetcher = Fetcher(
        FetchResult(
            "feed",
            "https://example.org/parse",
            status_code=200,
            raw_xml=b"<rss><channel><broken></channel></rss>",
            etag='"bad-response"',
        )
    )
    runtime = RSSRuntime(
        PluginConfig.from_mapping({}),
        tmp_path,
        fetcher=fetcher,
        parser=Parser(error=ValueError("parser rejected content")),
    )
    await runtime.initialize()
    feed = await runtime.repository.create_feed("https://example.org/parse", title="Parse test")
    async with runtime.database.transaction() as conn:
        await conn.execute("UPDATE feeds SET etag='\"old\"' WHERE feed_id=?", (feed.feed_id,))
    feed = await runtime.repository.get_feed(feed.feed_id)
    result = await runtime.refresh_feed(feed.feed_id)
    assert result["status"] == "error" and result["error"] == "feed parsing failed: ValueError"
    state = await runtime.database.fetchone("SELECT etag,status FROM feeds WHERE feed_id=?", (feed.feed_id,))
    assert tuple(state) == ('"old"', "parse_error")
    logs = await runtime.database.fetchall(
        "SELECT error_type,error_message FROM fetch_logs WHERE feed_id=? ORDER BY id", (feed.feed_id,)
    )
    assert [row[0] for row in logs] == ["parse_error"]
    assert logs[0][1] == "feed parsing failed: ValueError"
    stats = (await ManagementRepository(runtime).dashboard())["fetch_stats"]
    assert stats["last_hour"] == stats["failures"] == 1
    assert stats["success_rate"] == 0
    await runtime.close()
