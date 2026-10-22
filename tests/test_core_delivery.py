import asyncio
from collections import deque
from types import SimpleNamespace

import astrbot_plugin_rss.core.runtime as runtime_module
import pytest
from astrbot_plugin_rss.core.config import PluginConfig
from astrbot_plugin_rss.core.models import Article, ContentBlock, RenderedMessage, now_ts
from astrbot_plugin_rss.core.runtime import RSSRuntime


class FakeFetcher:
    async def close(self):
        pass


class FakeParser:
    def parse(self, result):
        raise AssertionError("not used")


class FakeRegistry:
    def __init__(self):
        self.render_calls = 0

    async def render(self, articles, target, source_name, is_list=False, batch_id="", **kwargs):
        self.render_calls += 1
        return [
            RenderedMessage("fake", "text", {"text": "two"}, [a.article_id for a in articles[:2]], batch_id),
            RenderedMessage("fake", "text", {"text": "one"}, [a.article_id for a in articles[2:]], batch_id),
        ]


class FlakySender:
    def __init__(self):
        self.calls = []
        self.failed = False

    async def send(self, target, message, idempotency_key):
        self.calls.append((idempotency_key, message.payload))
        if message.payload == {"text": "one"} and not self.failed:
            self.failed = True
            from astrbot_plugin_rss.core.errors import RetryableSendError

            raise RetryableSendError("temporary platform failure")
        return SimpleNamespace(platform_message_id="msg-" + idempotency_key[:6])


@pytest.mark.asyncio
async def test_list_multicard_partial_success_retries_exact_failed_card_after_restart(tmp_path):
    registry, sender = FakeRegistry(), FlakySender()
    runtime = RSSRuntime(
        PluginConfig.from_mapping({"retry_backoff_base": 0.01}),
        tmp_path,
        fetcher=FakeFetcher(),
        parser=FakeParser(),
        registry=registry,
        sender=sender,
    )
    await runtime.initialize()
    feed = await runtime.repository.create_feed("https://example.org/multi")
    target = await runtime.repository.create_target("p", "multi", "test", "multi")
    list_id = await runtime.repository.create_list("batch", interval=60)
    await runtime.repository.add_list_feed(list_id, feed.feed_id)
    await runtime.repository.subscribe_list(target.target_id, list_id)
    articles = [
        Article(
            "",
            feed.feed_id,
            f"id-{i}",
            f"https://example.org/{i}",
            str(i),
            content_blocks=[ContentBlock("text", text=str(i))],
        )
        for i in range(3)
    ]
    await runtime.repository.save_articles(feed.feed_id, articles)
    async with runtime.database.transaction() as conn:
        await conn.execute("UPDATE list_feeds SET baseline_seq=0 WHERE list_id=?", (list_id,))
        await conn.execute(
            "UPDATE list_subscriptions SET baseline_seq=0,last_seq=0,next_check_at=0 WHERE list_id=?", (list_id,)
        )
    await runtime.repository.enqueue_due_paths(now_ts() + 1, 300, 60)
    rows = await runtime.repository.pending_deliveries(now_ts() + 1, 10, 5)
    sem = __import__("asyncio").Semaphore(1)
    await runtime._deliver_group(rows, sem)
    deliveries = await runtime.repository.deliveries(target_id=target.target_id)
    assert sorted(d["status"] for d in deliveries) == ["failed", "success", "success"]
    sent_first = list(sender.calls)
    assert len(sent_first) == 2
    await runtime.close()
    resumed_registry = FakeRegistry()
    resumed = RSSRuntime(
        PluginConfig.from_mapping({"retry_backoff_base": 0.01}),
        tmp_path,
        fetcher=FakeFetcher(),
        parser=FakeParser(),
        registry=resumed_registry,
        sender=sender,
    )
    await resumed.initialize()
    retry = await resumed.repository.pending_deliveries(now_ts() + 1, 10, 5)
    await resumed._deliver_group(retry, sem)
    assert len(sender.calls) == 3
    assert sender.calls[-1] == sent_first[-1]
    assert registry.render_calls == 1
    assert resumed_registry.render_calls == 0
    assert all(d["status"] == "success" for d in await resumed.repository.deliveries(target_id=target.target_id))
    await resumed.close()


@pytest.mark.asyncio
async def test_old_failed_group_and_new_article_are_claimed_as_separate_groups(tmp_path):
    runtime = RSSRuntime(
        PluginConfig.from_mapping({"retry_backoff_base": 0.01}),
        tmp_path,
        fetcher=FakeFetcher(),
        parser=FakeParser(),
        registry=FakeRegistry(),
        sender=FlakySender(),
    )
    await runtime.initialize()
    feed = await runtime.repository.create_feed("https://example.org/group-split")
    target = await runtime.repository.create_target("p", "group-split", "test", "group-split")
    list_id = await runtime.repository.create_list("batch", interval=60)
    await runtime.repository.add_list_feed(list_id, feed.feed_id)
    await runtime.repository.subscribe_list(target.target_id, list_id)
    initial = [
        Article("", feed.feed_id, f"first-{index}", f"https://example.org/first-{index}", f"first-{index}")
        for index in range(3)
    ]
    await runtime.repository.save_articles(feed.feed_id, initial)
    async with runtime.database.transaction() as conn:
        await conn.execute("UPDATE list_feeds SET baseline_seq=0 WHERE list_id=?", (list_id,))
        await conn.execute("UPDATE list_subscriptions SET baseline_seq=0,last_seq=0 WHERE list_id=?", (list_id,))
    await runtime.repository.enqueue_due_paths(now_ts() + 1, 300, 60)
    semaphore = __import__("asyncio").Semaphore(1)
    await runtime._deliver_group(await runtime.repository.pending_deliveries(now_ts() + 1), semaphore)
    later = Article("", feed.feed_id, "later", "https://example.org/later", "later")
    await runtime.repository.save_articles(feed.feed_id, [later])
    await runtime.repository.enqueue_due_paths(now_ts() + 100, 300, 60, force=True)
    mixed = await runtime.repository.pending_deliveries(now_ts() + 100, 10, 5)
    assert len(mixed) == 2
    groups = [await runtime.repository.batch_group([row["delivery_id"]]) for row in mixed]
    assert sum(group is not None for group in groups) == 1
    assert sum(group is None for group in groups) == 1
    await runtime._deliver_group(mixed, semaphore)
    assert all(row["status"] == "success" for row in await runtime.repository.deliveries(target_id=target.target_id))
    await runtime.close()


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_successful_delivery_records_one_attempt_with_platform_receipt(tmp_path):
    class SuccessSender:
        async def send(self, target, message, idempotency_key):
            return SimpleNamespace(platform_message_id="platform-receipt")

    runtime = RSSRuntime(
        PluginConfig.from_mapping({}),
        tmp_path,
        fetcher=FakeFetcher(),
        parser=FakeParser(),
        registry=FakeRegistry(),
        sender=SuccessSender(),
    )
    await runtime.initialize()
    feed = await runtime.repository.create_feed("https://example.org/success-attempt")
    target = await runtime.repository.create_target("p", "success-attempt", "test", "success-attempt")
    await runtime.repository.add_direct_subscription(target.target_id, feed.feed_id, 60, send_history=True)
    article = Article("", feed.feed_id, "entry", "https://example.org/entry", "entry")
    await runtime.repository.save_articles(feed.feed_id, [article])
    await runtime.repository.enqueue_due_paths(now_ts() + 1, 300, 60)
    await runtime._deliver_group(
        await runtime.repository.pending_deliveries(now_ts() + 1), __import__("asyncio").Semaphore(1)
    )
    delivery = (await runtime.repository.deliveries(target_id=target.target_id))[0]
    attempts = await runtime.database.fetchall(
        "SELECT result,platform_message_id FROM delivery_attempts WHERE delivery_id=?",
        (delivery["delivery_id"],),
    )
    assert delivery["status"] == "success"
    assert delivery["platform_message_id"] == "platform-receipt"
    assert [tuple(row) for row in attempts] == [("success", "platform-receipt")]
    await runtime.close()


@pytest.mark.asyncio
async def test_send_rate_limit_wait_for_one_instance_does_not_block_another(tmp_path, monkeypatch):
    runtime = RSSRuntime(PluginConfig.from_mapping({"delivery_rate_limit": 1}), tmp_path)
    clock = [100.0]
    monkeypatch.setattr(runtime_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    sleep_started = asyncio.Event()
    release_sleep = asyncio.Event()

    async def gated_sleep(_delay):
        sleep_started.set()
        await release_sleep.wait()

    monkeypatch.setattr(runtime_module, "asyncio", SimpleNamespace(sleep=gated_sleep))
    runtime._send_times["platform-a"] = deque([clock[0]])
    waiting = asyncio.create_task(runtime._respect_send_rate_limit("platform-a"))
    await asyncio.wait_for(sleep_started.wait(), timeout=1)

    independent = asyncio.create_task(runtime._respect_send_rate_limit("platform-b"))
    try:
        done, _ = await asyncio.wait({independent}, timeout=0.2)
        assert independent in done
        await independent
    finally:
        clock[0] += 60
        release_sleep.set()
        await asyncio.gather(waiting, independent, return_exceptions=True)

    assert len(runtime._send_times["platform-a"]) == 1
    assert len(runtime._send_times["platform-b"]) == 1


@pytest.mark.asyncio
async def test_send_rate_limit_serializes_same_instance_quota_under_concurrency(tmp_path, monkeypatch):
    runtime = RSSRuntime(PluginConfig.from_mapping({"delivery_rate_limit": 2}), tmp_path)
    clock = [100.0]
    monkeypatch.setattr(runtime_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    sleep_started = asyncio.Event()
    release_sleep = asyncio.Event()

    async def gated_sleep(_delay):
        sleep_started.set()
        await release_sleep.wait()

    monkeypatch.setattr(runtime_module, "asyncio", SimpleNamespace(sleep=gated_sleep))
    tasks = [asyncio.create_task(runtime._respect_send_rate_limit("same")) for _ in range(3)]
    await asyncio.wait_for(sleep_started.wait(), timeout=1)
    done, pending = await asyncio.wait(tasks, timeout=0.2)
    assert len(done) == 2 and len(pending) == 1
    assert list(runtime._send_times["same"]) == [100.0, 100.0]

    clock[0] += 60
    release_sleep.set()
    await asyncio.gather(*tasks)
    assert list(runtime._send_times["same"]) == [160.0]


@pytest.mark.asyncio
async def test_delivery_metadata_failure_makes_claim_retryable_without_restart(tmp_path, monkeypatch):
    runtime = RSSRuntime(
        PluginConfig.from_mapping({"retry_backoff_base": 0.01}),
        tmp_path,
        fetcher=FakeFetcher(),
        parser=FakeParser(),
        registry=FakeRegistry(),
        sender=FlakySender(),
    )
    await runtime.initialize()
    feed = await runtime.repository.create_feed("https://example.org/metadata-failure")
    target = await runtime.repository.create_target("p", "metadata-failure", "test", "metadata-failure")
    await runtime.repository.add_direct_subscription(target.target_id, feed.feed_id, 60, send_history=True)
    article = Article("", feed.feed_id, "entry", "https://example.org/entry", "entry")
    await runtime.repository.save_articles(feed.feed_id, [article])
    await runtime.repository.enqueue_due_paths(now_ts() + 1, 300, 60)

    original_batch_cards = runtime.repository.batch_cards
    failed_once = False

    async def fail_first_read(group_id):
        nonlocal failed_once
        if not failed_once:
            failed_once = True
            raise OSError("temporary batch lookup failure")
        return await original_batch_cards(group_id)

    monkeypatch.setattr(runtime.repository, "batch_cards", fail_first_read)
    semaphore = asyncio.Semaphore(1)
    await runtime._deliver_group(await runtime.repository.pending_deliveries(now_ts() + 1), semaphore)
    delivery = (await runtime.repository.deliveries(target_id=target.target_id))[0]
    assert delivery["status"] == "failed" and delivery["attempts"] == 1

    retry = await runtime.repository.pending_deliveries(now_ts() + 1)
    assert len(retry) == 1
    await runtime._deliver_group(retry, semaphore)
    assert (await runtime.repository.deliveries(target_id=target.target_id))[0]["status"] == "success"
    await runtime.close()


@pytest.mark.asyncio
async def test_restart_restores_rendering_claim_without_consuming_last_attempt(tmp_path):
    config = PluginConfig.from_mapping({"max_delivery_retries": 0})
    runtime = RSSRuntime(
        config,
        tmp_path,
        fetcher=FakeFetcher(),
        parser=FakeParser(),
        registry=FakeRegistry(),
        sender=FlakySender(),
    )
    await runtime.initialize()
    feed = await runtime.repository.create_feed("https://example.org/restart-rendering")
    target = await runtime.repository.create_target("p", "restart-rendering", "test", "restart-rendering")
    await runtime.repository.add_direct_subscription(target.target_id, feed.feed_id, 60, send_history=True)
    await runtime.repository.save_articles(
        feed.feed_id,
        [
            Article("", feed.feed_id, f"article-{index}", f"https://example.org/{index}", str(index))
            for index in range(2)
        ],
    )
    await runtime.repository.enqueue_due_paths(now_ts() + 1, 300, 60)
    pending = await runtime.repository.pending_deliveries(now_ts() + 1, 10, max_attempts=1)
    claimed = await runtime.repository.claim_deliveries([row["delivery_id"] for row in pending], max_attempts=1)
    assert len(claimed) == 2
    rendering_id, sending_id = (row["delivery_id"] for row in claimed)
    await runtime.repository.mark_delivery(sending_id, "sending")
    await runtime.close()

    resumed = RSSRuntime(
        config,
        tmp_path,
        fetcher=FakeFetcher(),
        parser=FakeParser(),
        registry=FakeRegistry(),
        sender=FlakySender(),
    )
    await resumed.initialize()
    deliveries = {row["delivery_id"]: row for row in await resumed.repository.deliveries(target_id=target.target_id)}
    assert deliveries[rendering_id]["status"] == "pending"
    assert deliveries[rendering_id]["attempts"] == 0
    assert deliveries[sending_id]["status"] == "unknown"
    assert deliveries[sending_id]["attempts"] == 1

    retryable = await resumed.repository.pending_deliveries(now_ts() + 1, 10, max_attempts=1)
    assert [row["delivery_id"] for row in retryable] == [rendering_id]
    reclaimed = await resumed.repository.claim_deliveries([rendering_id], max_attempts=1)
    assert len(reclaimed) == 1
    assert reclaimed[0]["attempts"] == 1
    await resumed.close()


@pytest.mark.parametrize("paused_path", ["feed", "list"])
async def test_paused_subscription_path_defers_without_consuming_attempt(tmp_path, paused_path):
    runtime = RSSRuntime(
        PluginConfig.from_mapping({}),
        tmp_path,
        fetcher=FakeFetcher(),
        parser=FakeParser(),
        registry=FakeRegistry(),
        sender=FlakySender(),
    )
    await runtime.initialize()
    feed = await runtime.repository.create_feed(f"https://example.org/paused-{paused_path}")
    target = await runtime.repository.create_target("p", f"paused-{paused_path}", "test", paused_path)
    list_id = None
    if paused_path == "feed":
        await runtime.repository.add_direct_subscription(target.target_id, feed.feed_id, 60, send_history=True)
    else:
        list_id = await runtime.repository.create_list("paused", interval=60)
        await runtime.repository.add_list_feed(list_id, feed.feed_id, send_history=True)
        await runtime.repository.subscribe_list(target.target_id, list_id, 60, send_history=True)
    article = Article("", feed.feed_id, "entry", "https://example.org/entry", "entry")
    await runtime.repository.save_articles(feed.feed_id, [article])
    await runtime.repository.enqueue_due_paths(now_ts() + 1, 300, 60)
    if paused_path == "feed":
        await runtime.repository.update_feed(feed.feed_id, enabled=False)
    else:
        async with runtime.database.transaction() as conn:
            await conn.execute("UPDATE subscription_lists SET enabled=0 WHERE list_id=?", (list_id,))
    semaphore = __import__("asyncio").Semaphore(1)
    queued = await runtime.repository.pending_deliveries(now_ts() + 1)
    await runtime._deliver_group(queued, semaphore)
    delivery = (await runtime.repository.deliveries(target_id=target.target_id))[0]
    assert delivery["status"] == "pending" and delivery["attempts"] == 0
    if paused_path == "feed":
        await runtime.repository.update_feed(feed.feed_id, enabled=True)
    else:
        async with runtime.database.transaction() as conn:
            await conn.execute("UPDATE subscription_lists SET enabled=1 WHERE list_id=?", (list_id,))
    await runtime._deliver_group(await runtime.repository.pending_deliveries(now_ts() + 1000), semaphore)
    delivery = (await runtime.repository.deliveries(target_id=target.target_id))[0]
    assert delivery["status"] == "success"
    await runtime.close()


@pytest.mark.asyncio
async def test_platform_unsupported_preflight_falls_back_before_persisting_any_card(tmp_path):
    from astrbot_plugin_rss.core.errors import PlatformUnsupportedError

    class Default:
        async def render(self, articles, target, source_name, is_list, batch_id):
            return [
                RenderedMessage("default", "text", {"text": "fallback"}, [a.article_id for a in articles], batch_id)
            ]

    class UnsupportedRegistry(FakeRegistry):
        def __init__(self):
            super().__init__()
            self.default = Default()

        async def render(self, articles, target, source_name, is_list=False, batch_id="", **kwargs):
            self.render_calls += 1
            return [
                RenderedMessage("specific", "interactive", {"card": {}}, [a.article_id for a in articles], batch_id)
            ]

    class PreflightSender(FlakySender):
        async def preflight(self, target, message):
            if message.message_type == "interactive":
                raise PlatformUnsupportedError("capability unavailable")

    runtime = RSSRuntime(
        PluginConfig.from_mapping({"enable_default_renderer": True}),
        tmp_path,
        fetcher=FakeFetcher(),
        parser=FakeParser(),
        registry=UnsupportedRegistry(),
        sender=PreflightSender(),
    )
    await runtime.initialize()
    feed = await runtime.repository.create_feed("https://example.org/fallback")
    target = await runtime.repository.create_target("p", "fallback", "test", "fallback")
    await runtime.repository.add_direct_subscription(target.target_id, feed.feed_id, send_history=True)
    article = Article("", feed.feed_id, "entry", "https://example.org/entry", "entry")
    await runtime.repository.save_articles(feed.feed_id, [article])
    await runtime.repository.enqueue_due_paths(now_ts() + 1, 300, 60)
    await runtime._deliver_group(
        await runtime.repository.pending_deliveries(now_ts() + 1), __import__("asyncio").Semaphore(1)
    )
    cards = await runtime.repository.batch_cards(
        (
            await runtime.repository.batch_group(
                [(await runtime.repository.deliveries(target_id=target.target_id))[0]["delivery_id"]]
            )
        )
    )
    assert cards and all(card["payload"]["renderer"] == "default" for card in cards)
    assert (await runtime.repository.deliveries(target_id=target.target_id))[0]["status"] == "success"
    await runtime.close()


@pytest.mark.asyncio
async def test_same_article_card_and_file_resume_only_failed_operation_after_cleanup(tmp_path):
    class TwoOperations:
        async def render(self, articles, target, source_name, is_list=False, batch_id="", **kwargs):
            aid = articles[0].article_id
            return [
                RenderedMessage("fake", "text", {"part": "primary"}, [aid], batch_id),
                RenderedMessage("fake", "file", {"part": "video"}, [aid], batch_id),
            ]

    class FileFailSender:
        def __init__(self):
            self.calls = []
            self.failed = False

        async def send(self, target, message, idempotency_key):
            self.calls.append(message.payload["part"])
            if message.payload["part"] == "video" and not self.failed:
                self.failed = True
                from astrbot_plugin_rss.core.errors import RetryableSendError

                raise RetryableSendError("temporary file send failure")
            return SimpleNamespace(platform_message_id="ok")

    sender = FileFailSender()
    runtime = RSSRuntime(
        PluginConfig.from_mapping({"history_retention_days": 1, "retry_backoff_base": 0.01}),
        tmp_path,
        fetcher=FakeFetcher(),
        parser=FakeParser(),
        registry=TwoOperations(),
        sender=sender,
    )
    await runtime.initialize()
    feed = await runtime.repository.create_feed("https://example.org/card-file")
    target = await runtime.repository.create_target("p", "card-file", "test", "card-file")
    await runtime.repository.add_direct_subscription(target.target_id, feed.feed_id, 60, send_history=True)
    article = Article("", feed.feed_id, "entry", "https://example.org/entry", "entry")
    await runtime.repository.save_articles(feed.feed_id, [article])
    await runtime.repository.enqueue_due_paths(now_ts() + 1, 300, 60)
    semaphore = __import__("asyncio").Semaphore(1)
    await runtime._deliver_group(await runtime.repository.pending_deliveries(now_ts() + 1), semaphore)
    assert sender.calls == ["primary", "video"]
    async with runtime.database.transaction() as conn:
        await conn.execute("UPDATE delivery_batches SET updated_at=0")
        await conn.execute("UPDATE deliveries SET updated_at=0")
    cleanup = await runtime.cleanup("history")
    assert cleanup["batches"] == 0
    await runtime.close()

    resumed = RSSRuntime(
        PluginConfig.from_mapping({"history_retention_days": 1, "retry_backoff_base": 0.01}),
        tmp_path,
        fetcher=FakeFetcher(),
        parser=FakeParser(),
        registry=TwoOperations(),
        sender=sender,
    )
    await resumed.initialize()
    pending = await resumed.repository.pending_deliveries(now_ts() + 1000)
    await resumed._deliver_group(pending, semaphore)
    assert sender.calls == ["primary", "video", "video"]
    delivery = (await resumed.repository.deliveries(target_id=target.target_id))[0]
    assert delivery["status"] == "success"
    await resumed.close()


@pytest.mark.asyncio
async def test_zero_delivery_retries_still_allows_one_real_attempt(tmp_path):
    from astrbot_plugin_rss.core.errors import RetryableSendError

    class RejectOnce:
        async def send(self, target, message, idempotency_key):
            raise RetryableSendError("temporary rejection")

    runtime = RSSRuntime(
        PluginConfig.from_mapping({"max_delivery_retries": 0}),
        tmp_path,
        fetcher=FakeFetcher(),
        parser=FakeParser(),
        registry=FakeRegistry(),
        sender=RejectOnce(),
    )
    await runtime.initialize()
    feed = await runtime.repository.create_feed("https://example.org/zero-retry")
    target = await runtime.repository.create_target("p", "zero-retry", "test", "zero-retry")
    await runtime.repository.add_direct_subscription(target.target_id, feed.feed_id, 60, send_history=True)
    article = Article("", feed.feed_id, "entry", "https://example.org/entry", "entry")
    await runtime.repository.save_articles(feed.feed_id, [article])
    await runtime.repository.enqueue_due_paths(now_ts() + 1, 300, 60)
    await runtime._deliver_group(
        await runtime.repository.pending_deliveries(now_ts() + 1), __import__("asyncio").Semaphore(1)
    )
    delivery = (await runtime.repository.deliveries(target_id=target.target_id))[0]
    assert delivery["status"] == "failed" and delivery["attempts"] == 1 and delivery["retryable"] == 0
    assert await runtime.repository.pending_deliveries(now_ts() + 1000, max_attempts=1) == []
    await runtime.close()


@pytest.mark.asyncio
async def test_group_target_uses_real_astrbot_event_getters_and_shared_group_route(tmp_path):
    runtime = RSSRuntime(
        PluginConfig.from_mapping({}),
        tmp_path,
        fetcher=FakeFetcher(),
        parser=FakeParser(),
        registry=FakeRegistry(),
        sender=FlakySender(),
    )
    await runtime.initialize()

    class GroupEvent:
        def __init__(self, user, group="group-42", instance="lark-instance"):
            self.group = group
            self.instance = instance
            self.unified_msg_origin = f"lark:GroupMessage:user-{user}%{group}"

        def get_platform_id(self):
            return self.instance

        def get_platform_name(self):
            return "lark"

        def get_session_id(self):
            return f"user%{self.group}"

        def get_group_id(self):
            return self.group

        def get_message_type(self):
            return SimpleNamespace(value="GroupMessage")

    first = await runtime.services.targets.from_event(GroupEvent("a"))
    second = await runtime.services.targets.from_event(GroupEvent("b"))
    other_group = await runtime.services.targets.from_event(GroupEvent("a", group="group-99"))
    other_instance = await runtime.services.targets.from_event(GroupEvent("a", instance="lark-instance-2"))
    assert first.target_id == second.target_id
    assert first.session_id == second.session_id == "group-42"
    assert second.unified_origin.endswith("user-b%group-42")
    assert other_group.target_id != first.target_id
    assert other_instance.target_id != first.target_id
    assert (await runtime.repository.status())["targets"] == 3
    await runtime.close()


@pytest.mark.asyncio
async def test_manual_test_push_is_tracked_separately_from_normal_dedup(tmp_path):
    runtime = RSSRuntime(
        PluginConfig.from_mapping({}),
        tmp_path,
        fetcher=FakeFetcher(),
        parser=FakeParser(),
        registry=FakeRegistry(),
        sender=FlakySender(),
    )
    await runtime.initialize()
    feed = await runtime.repository.create_feed("https://example.org/test-push", title="Test Feed")
    target = await runtime.repository.create_target("p", "test-push", "test", "test-push")
    await add_test_article(runtime, feed.feed_id)
    result = await runtime.test_push(feed.feed_id, target.target_id)
    # The test renderer deliberately returns two operations; the second one
    # fails once, so the independent test operation must expose that failure.
    assert result["status"] == "failed"
    retried = await runtime.retry_test_push(result["operation_id"])
    assert retried["status"] == "success"
    assert (await runtime.repository.status())["deliveries"] == 0
    assert (await runtime.repository.status())["test_operations"] == 1
    await runtime.close()


async def add_test_article(runtime, feed_id):
    article = Article(
        "",
        feed_id,
        "manual-test",
        "https://example.org/test-push/article",
        "Manual test",
        content_blocks=[ContentBlock("text", text="test")],
    )
    await runtime.repository.save_articles(feed_id, [article])
