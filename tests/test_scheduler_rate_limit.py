import asyncio

import pytest
from astrbot_plugin_rss.core.config import PluginConfig
from astrbot_plugin_rss.core.models import FetchResult, ParsedFeed
from astrbot_plugin_rss.core.runtime import RSSRuntime
from astrbot_plugin_rss.scheduler import FeedScheduler


class ManualClock:
    def __init__(self, value):
        self.value = value

    def now(self):
        return self.value


class RateLimitedFetcher:
    def __init__(self, clock, *, block_first=False, succeed_after_limit=False):
        self.clock = clock
        self.calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.block_first = block_first
        self.succeed_after_limit = succeed_after_limit

    async def fetch(self, feed, force=False):
        self.calls += 1
        self.started.set()
        if self.block_first and self.calls == 1:
            await self.release.wait()
        if self.calls > 1 and self.succeed_after_limit:
            return FetchResult(
                feed.feed_id,
                feed.url,
                status_code=200,
                raw_xml=b"<rss><channel/></rss>",
                fetched_at=self.clock.value,
            )
        return FetchResult(
            feed.feed_id,
            feed.url,
            status_code=429,
            fetched_at=self.clock.value,
            error_type="http_429",
            error_message="rate limited",
            retry_after_at=self.clock.value + 60,
        )


class Parser:
    def parse(self, result):
        return ParsedFeed()


class DelayedSuccessFetcher:
    def __init__(self, clock):
        self.clock = clock
        self.calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def fetch(self, feed, force=False):
        self.calls += 1
        self.started.set()
        if self.calls == 1:
            await self.release.wait()
        return FetchResult(
            feed.feed_id,
            feed.url,
            status_code=200,
            raw_xml=b"<rss><channel/></rss>",
            fetched_at=self.clock.value,
        )


def patch_clock(monkeypatch, clock):
    import astrbot_plugin_rss.core.runtime as runtime_module
    import astrbot_plugin_rss.scheduler.feed_scheduler as scheduler_module
    import astrbot_plugin_rss.storage.repository as repository_module

    monkeypatch.setattr(runtime_module, "now_ts", clock.now)
    monkeypatch.setattr(scheduler_module, "now_ts", clock.now)
    monkeypatch.setattr(repository_module, "now_ts", clock.now)


async def make_runtime(tmp_path, monkeypatch, clock, fetcher):
    patch_clock(monkeypatch, clock)
    runtime = RSSRuntime(
        PluginConfig.from_mapping({"global_poll_interval": 5}),
        tmp_path,
        fetcher=fetcher,
        parser=Parser(),
    )
    await runtime.initialize()
    feed = await runtime.repository.create_feed("https://example.org/rate-limited")
    target = await runtime.repository.create_target("instance", "target", "test", "target")
    await runtime.repository.add_direct_subscription(target.target_id, feed.feed_id, interval=5)
    return runtime, feed


@pytest.mark.asyncio
async def test_manual_and_scheduler_share_refresh_lock_and_persist_429_cooldown(tmp_path, monkeypatch):
    clock = ManualClock(1_800_000_000)
    fetcher = RateLimitedFetcher(clock, block_first=True, succeed_after_limit=True)
    runtime, feed = await make_runtime(tmp_path, monkeypatch, clock, fetcher)
    try:
        manual = asyncio.create_task(runtime.refresh_feed(feed.feed_id))
        await asyncio.wait_for(fetcher.started.wait(), timeout=1)
        scheduled = asyncio.create_task(FeedScheduler(runtime).run_once(force=True))
        await asyncio.sleep(0)
        fetcher.release.set()
        manual_result, scheduler_result = await asyncio.gather(manual, scheduled)

        assert manual_result["status"] == "error"
        assert scheduler_result["checked"] == 1
        assert fetcher.calls == 1
        state = await runtime.database.fetchone(
            "SELECT status,next_fetch_at FROM feeds WHERE feed_id=?", (feed.feed_id,)
        )
        assert state["status"] == "http_429"
        assert state["next_fetch_at"] == clock.value + 60
        assert (await runtime.database.fetchone("SELECT COUNT(*) FROM fetch_logs"))[0] == 1

        with pytest.raises(ValueError, match="冷却期"):
            await runtime.refresh_feed(feed.feed_id)
        force_result = await FeedScheduler(runtime).run_once(force=True)
        assert force_result["checked"] == 0
        assert fetcher.calls == 1

        # The cooldown is durable and still wins after a path interval change.
        async with runtime.database.transaction() as conn:
            await conn.execute("UPDATE direct_subscriptions SET poll_interval=1 WHERE feed_id=?", (feed.feed_id,))
        await runtime.close()
        runtime = RSSRuntime(
            PluginConfig.from_mapping({"global_poll_interval": 5}),
            tmp_path,
            fetcher=fetcher,
            parser=Parser(),
        )
        await runtime.initialize()
        feed = await runtime.repository.get_feed(feed.feed_id)
        clock.value += 59
        assert await runtime.repository.due_feeds(clock.value, 5, 1) == []
        assert (await runtime.repository.feed_retry_deadline(feed.feed_id)) == clock.value + 1
        clock.value += 1
        due = await runtime.repository.due_feeds(clock.value, 5, 1)
        assert len(due) == 1
        recovered = await FeedScheduler(runtime).run_once()
        assert recovered["fetched"] == 1
        assert fetcher.calls == 2
        recovered_state = await runtime.database.fetchone(
            "SELECT status,next_fetch_at FROM feeds WHERE feed_id=?", (feed.feed_id,)
        )
        assert recovered_state["status"] == "active"
        assert recovered_state["next_fetch_at"] == clock.value + 1
        clock.value += 0.99
        assert await runtime.repository.due_feeds(clock.value, 5, 1) == []
    finally:
        if runtime.database.connection is not None:
            await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("force", [False, True])
async def test_concurrent_run_once_deduplicates_a_locked_success(tmp_path, monkeypatch, force):
    clock = ManualClock(1_800_000_000)
    fetcher = DelayedSuccessFetcher(clock)
    runtime, _feed = await make_runtime(tmp_path, monkeypatch, clock, fetcher)
    try:
        scheduler = FeedScheduler(runtime)
        candidates_ready = asyncio.Event()
        query_name = "active_feeds" if force else "due_feeds"
        original_query = getattr(runtime.repository, query_name)
        query_count = 0

        async def observe_query(*args, **kwargs):
            nonlocal query_count
            rows = await original_query(*args, **kwargs)
            query_count += 1
            if query_count == 2:
                candidates_ready.set()
            return rows

        monkeypatch.setattr(runtime.repository, query_name, observe_query)
        first = asyncio.create_task(scheduler.run_once(force=force))
        await asyncio.wait_for(fetcher.started.wait(), timeout=1)
        second = asyncio.create_task(scheduler.run_once(force=force))
        await asyncio.wait_for(candidates_ready.wait(), timeout=1)
        fetcher.release.set()
        await asyncio.gather(first, second)
        assert fetcher.calls == 1
        assert (await runtime.database.fetchone("SELECT COUNT(*) FROM fetch_logs"))[0] == 1

        if force:
            # A later independent force check still bypasses the ordinary interval.
            await scheduler.run_once(force=True)
            assert fetcher.calls == 2
        else:
            clock.value += 4.99
            await scheduler.run_once()
            assert fetcher.calls == 1
            clock.value += 0.01
            await scheduler.run_once()
            assert fetcher.calls == 2
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_manual_refresh_and_ordinary_scheduler_check_do_not_fetch_twice(tmp_path, monkeypatch):
    clock = ManualClock(1_800_000_000)
    fetcher = DelayedSuccessFetcher(clock)
    runtime, feed = await make_runtime(tmp_path, monkeypatch, clock, fetcher)
    try:
        candidates_ready = asyncio.Event()
        original_query = runtime.repository.due_feeds

        async def observe_query(*args, **kwargs):
            rows = await original_query(*args, **kwargs)
            candidates_ready.set()
            return rows

        monkeypatch.setattr(runtime.repository, "due_feeds", observe_query)
        manual = asyncio.create_task(runtime.refresh_feed(feed.feed_id))
        await asyncio.wait_for(fetcher.started.wait(), timeout=1)
        scheduled = asyncio.create_task(FeedScheduler(runtime).run_once())
        await asyncio.wait_for(candidates_ready.wait(), timeout=1)
        fetcher.release.set()
        await asyncio.gather(manual, scheduled)
        assert fetcher.calls == 1
        assert (await runtime.database.fetchone("SELECT COUNT(*) FROM fetch_logs"))[0] == 1
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_manual_refresh_waiting_for_scheduler_success_is_coalesced_but_later_manual_runs(tmp_path, monkeypatch):
    clock = ManualClock(1_800_000_000)
    fetcher = DelayedSuccessFetcher(clock)
    runtime, feed = await make_runtime(tmp_path, monkeypatch, clock, fetcher)
    try:
        scheduled = asyncio.create_task(FeedScheduler(runtime).run_once())
        await asyncio.wait_for(fetcher.started.wait(), timeout=1)
        manual = asyncio.create_task(runtime.refresh_feed(feed.feed_id))
        await asyncio.sleep(0)
        fetcher.release.set()
        scheduled_result, manual_result = await asyncio.gather(scheduled, manual)

        assert scheduled_result["fetched"] == 1
        assert manual_result == {
            "feed_id": feed.feed_id,
            "status": "skipped",
            "reason": "concurrent_refresh",
        }
        assert fetcher.calls == 1
        assert (await runtime.database.fetchone("SELECT COUNT(*) FROM fetch_logs"))[0] == 1

        # A new manual request created after the prior refresh has finished remains an explicit force.
        next_result = await runtime.refresh_feed(feed.feed_id)
        assert next_result["status"] == "ok"
        assert fetcher.calls == 2
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_two_concurrent_manual_refreshes_are_coalesced(tmp_path, monkeypatch):
    clock = ManualClock(1_800_000_000)
    fetcher = DelayedSuccessFetcher(clock)
    runtime, feed = await make_runtime(tmp_path, monkeypatch, clock, fetcher)
    try:
        first = asyncio.create_task(runtime.refresh_feed(feed.feed_id))
        await asyncio.wait_for(fetcher.started.wait(), timeout=1)
        second = asyncio.create_task(runtime.refresh_feed(feed.feed_id))
        await asyncio.sleep(0)
        fetcher.release.set()
        results = await asyncio.gather(first, second)

        assert sorted(result["status"] for result in results) == ["ok", "skipped"]
        assert fetcher.calls == 1
        assert (await runtime.database.fetchone("SELECT COUNT(*) FROM fetch_logs"))[0] == 1
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_manual_waiting_for_scheduler_429_reports_cooldown_without_second_fetch(tmp_path, monkeypatch):
    clock = ManualClock(1_800_000_000)
    fetcher = RateLimitedFetcher(clock, block_first=True)
    runtime, feed = await make_runtime(tmp_path, monkeypatch, clock, fetcher)
    try:
        scheduled = asyncio.create_task(FeedScheduler(runtime).run_once())
        await asyncio.wait_for(fetcher.started.wait(), timeout=1)
        manual = asyncio.create_task(runtime.refresh_feed(feed.feed_id))
        await asyncio.sleep(0)
        fetcher.release.set()
        scheduled_result, manual_result = await asyncio.gather(scheduled, manual, return_exceptions=True)

        assert scheduled_result["errors"] == 1
        assert isinstance(manual_result, ValueError)
        assert "冷却期" in str(manual_result)
        assert fetcher.calls == 1
        assert (await runtime.database.fetchone("SELECT COUNT(*) FROM fetch_logs"))[0] == 1
        state = await runtime.database.fetchone(
            "SELECT status,next_fetch_at FROM feeds WHERE feed_id=?", (feed.feed_id,)
        )
        assert state["status"] == "http_429"
        assert state["next_fetch_at"] == clock.value + 60
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_due_feeds_still_applies_interval_minimum_for_non_429_feeds(tmp_path):
    from astrbot_plugin_rss.storage import Database, Repository

    database = Database(tmp_path / "intervals.sqlite3")
    await database.initialize()
    repository = Repository(database)
    try:
        feed = await repository.create_feed("https://example.org/ordinary")
        target = await repository.create_target("instance", "ordinary", "test", "ordinary")
        await repository.add_direct_subscription(target.target_id, feed.feed_id, interval=1)
        async with database.transaction() as conn:
            await conn.execute(
                "UPDATE feeds SET last_fetch_at=100,next_fetch_at=1000,status='active' WHERE feed_id=?",
                (feed.feed_id,),
            )

        due = await repository.due_feeds(101, 300, 1)
        assert len(due) == 1
        assert due[0]["required_interval"] == 1
        assert due[0]["next_fetch_at"] == 101
    finally:
        await database.close()
