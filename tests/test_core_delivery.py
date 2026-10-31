import asyncio
import logging
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


async def _make_multicard_list_runtime(tmp_path, registry, sender, *, retries=1):
    runtime = RSSRuntime(
        PluginConfig.from_mapping({"max_delivery_retries": retries, "retry_backoff_base": 0.01}),
        tmp_path,
        fetcher=FakeFetcher(),
        parser=FakeParser(),
        registry=registry,
        sender=sender,
    )
    await runtime.initialize()
    feed = await runtime.repository.create_feed("https://example.org/recovery-list")
    target = await runtime.repository.create_target("p", "recovery-list", "test", "recovery-list")
    list_id = await runtime.repository.create_list("recovery", interval=60)
    await runtime.repository.add_list_feed(list_id, feed.feed_id)
    await runtime.repository.subscribe_list(target.target_id, list_id)
    await runtime.repository.save_articles(
        feed.feed_id,
        [Article("", feed.feed_id, label, f"https://example.org/{label}", label) for label in ("a", "b", "c")],
    )
    async with runtime.database.transaction() as conn:
        await conn.execute("UPDATE list_feeds SET baseline_seq=0 WHERE list_id=?", (list_id,))
        await conn.execute(
            "UPDATE list_subscriptions SET baseline_seq=0,last_seq=0,next_check_at=0 WHERE list_id=?", (list_id,)
        )
    await runtime.repository.enqueue_due_paths(now_ts() + 1, 300, 60)
    return runtime, target


class ExplicitCardRegistry:
    def __init__(self, cards):
        self.cards = cards

    async def render(self, articles, target, source_name, is_list=False, batch_id="", **kwargs):
        return [
            RenderedMessage(
                "fake",
                "text",
                {"part": label},
                [articles[index].article_id for index in article_indexes],
                batch_id,
            )
            for label, article_indexes in self.cards
        ]


@pytest.mark.asyncio
async def test_list_partial_failure_releases_unattempted_claims_without_restart(tmp_path):
    from astrbot_plugin_rss.core.errors import RetryableSendError

    class FailMiddleOnce:
        def __init__(self):
            self.calls = []
            self.failed = False

        async def send(self, target, message, idempotency_key):
            part = message.payload["part"]
            self.calls.append((part, idempotency_key))
            if part == "b" and not self.failed:
                self.failed = True
                raise RetryableSendError("temporary middle card failure")
            return SimpleNamespace(platform_message_id="sent")

    sender = FailMiddleOnce()
    registry = ExplicitCardRegistry([("a-first", [0]), ("b", [1]), ("a-second", [0]), ("c", [2])])
    runtime, target = await _make_multicard_list_runtime(tmp_path, registry, sender, retries=0)
    semaphore = asyncio.Semaphore(1)
    rows = await runtime.repository.pending_deliveries(now_ts() + 1, 10, 1)
    await runtime._deliver_group(rows, semaphore)

    first_state = {row["article_id"]: row for row in await runtime.repository.deliveries(target_id=target.target_id)}
    by_title = {(await runtime.repository.article(row["article_id"])).title: row for row in first_state.values()}
    assert by_title["a"]["status"] == "pending" and by_title["a"]["attempts"] == 0
    assert by_title["b"]["status"] == "failed" and by_title["b"]["attempts"] == 1 and by_title["b"]["retryable"] == 0
    assert by_title["c"]["status"] == "pending" and by_title["c"]["attempts"] == 0
    successful_first_card_key = sender.calls[0][1]

    retry = await runtime.repository.pending_deliveries(now_ts() + 1000, 10, 1)
    assert len(retry) == 2
    await runtime._deliver_group(retry, semaphore)
    assert successful_first_card_key not in [key for _part, key in sender.calls[1:]]
    assert [part for part, _key in sender.calls] == ["a-first", "b", "a-second", "c"]
    final = {row["article_id"]: row for row in await runtime.repository.deliveries(target_id=target.target_id)}
    final_by_title = {(await runtime.repository.article(row["article_id"])).title: row for row in final.values()}
    assert final_by_title["a"]["status"] == "success"
    assert final_by_title["b"]["status"] == "failed" and final_by_title["b"]["retryable"] == 0
    assert final_by_title["c"]["status"] == "success"
    await runtime.close()


@pytest.mark.asyncio
async def test_permanent_failure_blocks_later_mixed_card_but_allows_unrelated_card(tmp_path):
    from astrbot_plugin_rss.core.errors import PermanentSendError

    class FailPermanentFirst:
        def __init__(self):
            self.calls = []
            self.failed = False

        async def send(self, target, message, idempotency_key):
            part = message.payload["part"]
            self.calls.append(part)
            if part == "first" and not self.failed:
                self.failed = True
                raise PermanentSendError("permanent reject")
            return SimpleNamespace(platform_message_id="sent")

    sender = FailPermanentFirst()
    registry = ExplicitCardRegistry([("first", [0]), ("mixed", [0, 1]), ("unrelated", [2])])
    runtime, target = await _make_multicard_list_runtime(tmp_path, registry, sender)
    semaphore = asyncio.Semaphore(1)
    await runtime._deliver_group(await runtime.repository.pending_deliveries(now_ts() + 1, 10, 2), semaphore)
    await runtime._deliver_group(await runtime.repository.pending_deliveries(now_ts() + 1000, 10, 2), semaphore)

    deliveries = await runtime.repository.deliveries(target_id=target.target_id)
    by_title = {(await runtime.repository.article(row["article_id"])).title: row for row in deliveries}
    assert sender.calls == ["first", "unrelated"]
    assert by_title["a"]["status"] == "failed" and by_title["a"]["retryable"] == 0
    assert by_title["b"]["status"] == "failed" and by_title["b"]["retryable"] == 0
    assert by_title["c"]["status"] == "success"

    assert await runtime.repository.retry_delivery(by_title["b"]["delivery_id"])
    await runtime._deliver_group(await runtime.repository.pending_deliveries(now_ts() + 1000, 10, 2), semaphore)
    assert sender.calls == ["first", "unrelated"]
    assert await runtime.repository.retry_delivery(by_title["a"]["delivery_id"])
    assert await runtime.repository.retry_delivery(by_title["b"]["delivery_id"])
    await runtime._deliver_group(await runtime.repository.pending_deliveries(now_ts() + 1000, 10, 2), semaphore)
    assert sender.calls == ["first", "unrelated", "first", "mixed"]
    assert all(row["status"] == "success" for row in await runtime.repository.deliveries(target_id=target.target_id))
    await runtime.close()


@pytest.mark.asyncio
async def test_unknown_outcome_blocks_mixed_card_until_operator_resolves_it(tmp_path):
    class UnknownFirst:
        def __init__(self):
            self.calls = []

        async def send(self, target, message, idempotency_key):
            part = message.payload["part"]
            self.calls.append(part)
            if part == "uncertain":
                raise ValueError("ambiguous platform result")
            return SimpleNamespace(platform_message_id="sent")

    sender = UnknownFirst()
    registry = ExplicitCardRegistry([("uncertain", [0]), ("mixed", [0, 1]), ("unrelated", [2])])
    runtime, target = await _make_multicard_list_runtime(tmp_path, registry, sender)
    semaphore = asyncio.Semaphore(1)
    await runtime._deliver_group(await runtime.repository.pending_deliveries(now_ts() + 1, 10, 2), semaphore)
    await runtime._deliver_group(await runtime.repository.pending_deliveries(now_ts() + 1000, 10, 2), semaphore)

    deliveries = await runtime.repository.deliveries(target_id=target.target_id)
    by_title = {(await runtime.repository.article(row["article_id"])).title: row for row in deliveries}
    assert sender.calls == ["uncertain", "unrelated"]
    assert by_title["a"]["status"] == "unknown"
    assert by_title["b"]["status"] == "failed" and by_title["b"]["retryable"] == 0
    assert by_title["c"]["status"] == "success"

    assert await runtime.repository.resolve_unknown(by_title["a"]["delivery_id"], confirmed_sent=True)
    assert await runtime.repository.retry_delivery(by_title["b"]["delivery_id"])
    await runtime._deliver_group(await runtime.repository.pending_deliveries(now_ts() + 1000, 10, 2), semaphore)
    assert sender.calls == ["uncertain", "unrelated", "mixed"]
    assert all(row["status"] == "success" for row in await runtime.repository.deliveries(target_id=target.target_id))
    await runtime.close()


@pytest.mark.asyncio
async def test_post_send_batch_persistence_failure_is_unknown_and_never_replayed(tmp_path, monkeypatch):
    class SuccessSender:
        def __init__(self):
            self.keys = []

        async def send(self, target, message, idempotency_key):
            self.keys.append(idempotency_key)
            return SimpleNamespace(platform_message_id="sent")

    sender = SuccessSender()
    runtime, target = await _make_multicard_list_runtime(tmp_path, ExplicitCardRegistry([("a", [0])]), sender)
    original = runtime.repository.set_batch_card_status
    failed_once = False

    async def fail_success_write(batch_id, status, *, platform_message_id=None, error=None):
        nonlocal failed_once
        if status == "success" and not failed_once:
            failed_once = True
            raise OSError("receipt persistence unavailable")
        return await original(batch_id, status, platform_message_id=platform_message_id, error=error)

    monkeypatch.setattr(runtime.repository, "set_batch_card_status", fail_success_write)
    semaphore = asyncio.Semaphore(1)
    await runtime._deliver_group(await runtime.repository.pending_deliveries(now_ts() + 1, 10, 2), semaphore)
    batches = await runtime.database.fetchall("SELECT group_id FROM delivery_batches LIMIT 1")
    card = (await runtime.repository.batch_cards(batches[0]["group_id"]))[0]
    delivery = await runtime.repository.delivery(card["delivery_ids"][0])
    assert delivery["status"] == "unknown"
    unknown_key = card["idempotency_key"]
    assert sender.keys.count(unknown_key) == 1

    await runtime._deliver_group(await runtime.repository.pending_deliveries(now_ts() + 1000, 10, 2), semaphore)
    assert sender.keys.count(unknown_key) == 1
    assert (await runtime.repository.batch_cards(batches[0]["group_id"]))[0]["status"] == "unknown"
    await runtime.close()


@pytest.mark.asyncio
async def test_render_failures_become_terminal_when_attempt_budget_is_exhausted(tmp_path):
    class BrokenRenderer:
        async def render(self, *args, **kwargs):
            raise RuntimeError("temporary render failure")

    class NoSend:
        async def send(self, target, message, idempotency_key):
            raise AssertionError("render failures must not send")

    runtime, target = await _make_multicard_list_runtime(tmp_path, BrokenRenderer(), NoSend(), retries=1)
    semaphore = asyncio.Semaphore(1)
    await runtime._deliver_group(await runtime.repository.pending_deliveries(now_ts() + 1, 10, 2), semaphore)
    first = (await runtime.repository.deliveries(target_id=target.target_id))[0]
    assert first["status"] == "failed" and first["attempts"] == 1 and first["retryable"] == 1

    await runtime._deliver_group(await runtime.repository.pending_deliveries(now_ts() + 1000, 10, 2), semaphore)
    exhausted = (await runtime.repository.deliveries(target_id=target.target_id))[0]
    assert exhausted["status"] == "failed" and exhausted["attempts"] == 2 and exhausted["retryable"] == 0
    assert await runtime.repository.pending_deliveries(now_ts() + 10000, 10, 2) == []
    await runtime.close()


@pytest.mark.asyncio
async def test_persisted_card_pre_send_error_consumes_only_its_claim_at_zero_retries(tmp_path, monkeypatch):
    class RecordingSender:
        def __init__(self):
            self.calls = []

        async def send(self, target, message, idempotency_key):
            self.calls.append(message.payload["part"])
            return SimpleNamespace(platform_message_id="sent")

    sender = RecordingSender()
    registry = ExplicitCardRegistry([("a", [0]), ("b", [1]), ("c", [2])])
    runtime, target = await _make_multicard_list_runtime(tmp_path, registry, sender, retries=0)
    original_rate_limit = runtime._respect_send_rate_limit
    failed_once = False

    async def fail_before_send(platform_instance):
        nonlocal failed_once
        if not failed_once:
            failed_once = True
            raise RuntimeError("transient send setup failure")
        await original_rate_limit(platform_instance)

    monkeypatch.setattr(runtime, "_respect_send_rate_limit", fail_before_send)
    semaphore = asyncio.Semaphore(1)
    await runtime._deliver_group(await runtime.repository.pending_deliveries(now_ts() + 1, 10, 1), semaphore)
    first = {row["article_id"]: row for row in await runtime.repository.deliveries(target_id=target.target_id)}
    by_title = {(await runtime.repository.article(row["article_id"])).title: row for row in first.values()}
    assert by_title["a"]["status"] == "failed" and by_title["a"]["attempts"] == 1
    assert by_title["a"]["retryable"] == 0
    assert by_title["b"]["status"] == "pending" and by_title["b"]["attempts"] == 0
    assert by_title["c"]["status"] == "pending" and by_title["c"]["attempts"] == 0

    await runtime._deliver_group(await runtime.repository.pending_deliveries(now_ts() + 1000, 10, 1), semaphore)
    final = await runtime.repository.deliveries(target_id=target.target_id)
    final_by_title = {(await runtime.repository.article(row["article_id"])).title: row for row in final}
    assert sender.calls == ["b", "c"]
    assert final_by_title["b"]["status"] == final_by_title["c"]["status"] == "success"
    await runtime.close()


@pytest.mark.asyncio
async def test_success_card_refresh_failure_recovers_without_resending(tmp_path, monkeypatch):
    class RecordingSender:
        def __init__(self):
            self.keys = []

        async def send(self, target, message, idempotency_key):
            self.keys.append(idempotency_key)
            return SimpleNamespace(platform_message_id="sent")

    sender = RecordingSender()
    runtime = RSSRuntime(
        PluginConfig.from_mapping({"max_delivery_retries": 1}),
        tmp_path,
        fetcher=FakeFetcher(),
        parser=FakeParser(),
        registry=ExplicitCardRegistry([("a", [0])]),
        sender=sender,
    )
    await runtime.initialize()
    feed = await runtime.repository.create_feed("https://example.org/refresh-recovery")
    target = await runtime.repository.create_target("p", "refresh-recovery", "test", "refresh-recovery")
    await runtime.repository.add_direct_subscription(target.target_id, feed.feed_id, 60, send_history=True)
    await runtime.repository.save_articles(feed.feed_id, [Article("", feed.feed_id, "a", "https://example.org/a", "a")])
    await runtime.repository.enqueue_due_paths(now_ts() + 1, 300, 60)
    original_refresh = runtime.repository.refresh_delivery_from_batches
    refresh_failures = 0

    async def fail_refresh_twice(delivery_id):
        nonlocal refresh_failures
        refresh_failures += 1
        if refresh_failures <= 2:
            raise OSError("temporary receipt refresh failure")
        await original_refresh(delivery_id)

    monkeypatch.setattr(runtime.repository, "refresh_delivery_from_batches", fail_refresh_twice)
    semaphore = asyncio.Semaphore(1)
    await runtime._deliver_group(await runtime.repository.pending_deliveries(now_ts() + 1, 10, 2), semaphore)
    delivery = (await runtime.repository.deliveries(target_id=target.target_id))[0]
    assert delivery["status"] == "pending" and delivery["attempts"] == 0
    assert refresh_failures == 2 and len(sender.keys) == 1

    await runtime._deliver_group(await runtime.repository.pending_deliveries(now_ts() + 1000, 10, 2), semaphore)
    delivery = (await runtime.repository.deliveries(target_id=target.target_id))[0]
    assert delivery["status"] == "success"
    assert len(sender.keys) == 1
    await runtime.close()


@pytest.mark.asyncio
async def test_persisted_retry_card_does_not_bypass_backoff_for_unclaimed_article(tmp_path):
    from astrbot_plugin_rss.core.errors import RetryableSendError

    class FailFirstOnce:
        def __init__(self):
            self.calls = []
            self.failed = False

        async def send(self, target, message, idempotency_key):
            part = message.payload["part"]
            self.calls.append((part, idempotency_key))
            if part == "a" and not self.failed:
                self.failed = True
                raise RetryableSendError("retry later")
            return SimpleNamespace(platform_message_id="sent")

    sender = FailFirstOnce()
    registry = ExplicitCardRegistry([("a", [0]), ("b", [1]), ("c", [2])])
    runtime, target = await _make_multicard_list_runtime(tmp_path, registry, sender)
    semaphore = asyncio.Semaphore(1)
    await runtime._deliver_group(await runtime.repository.pending_deliveries(now_ts() + 1, 10, 2), semaphore)
    deliveries = await runtime.repository.deliveries(target_id=target.target_id)
    by_title = {(await runtime.repository.article(row["article_id"])).title: row for row in deliveries}
    async with runtime.database.transaction() as conn:
        await conn.execute(
            "UPDATE deliveries SET next_attempt_at=? WHERE delivery_id=?",
            (now_ts() + 3600, by_title["a"]["delivery_id"]),
        )

    due_others = await runtime.repository.pending_deliveries(now_ts() + 1, 10, 2)
    assert {row["article_id"] for row in due_others} == {
        by_title["b"]["article_id"],
        by_title["c"]["article_id"],
    }
    await runtime._deliver_group(due_others, semaphore)
    assert [part for part, _key in sender.calls] == ["a", "b", "c"]
    assert by_title["a"]["delivery_id"]
    final = await runtime.repository.deliveries(target_id=target.target_id)
    final_by_title = {(await runtime.repository.article(row["article_id"])).title: row for row in final}
    assert final_by_title["a"]["status"] == "failed" and final_by_title["a"]["retryable"] == 1
    assert final_by_title["b"]["status"] == final_by_title["c"]["status"] == "success"
    await runtime.close()


@pytest.mark.asyncio
async def test_delivery_queue_logs_gathered_failures_by_type_only(caplog):
    from astrbot_plugin_rss.delivery.queue import DeliveryQueue

    class RepositoryStub:
        async def pending_deliveries(self, *args):
            return [{"target_id": "target", "source_path": "direct:path", "delivery_id": "delivery"}]

    class RuntimeStub:
        config = SimpleNamespace(delivery_concurrency=1, max_delivery_retries=1)
        repository = RepositoryStub()

        async def _deliver_group(self, rows, semaphore):
            raise RuntimeError("private platform response")

    with caplog.at_level(logging.ERROR):
        count = await DeliveryQueue(RuntimeStub()).run_once()
    assert count == 1
    assert "RSS delivery group failed (RuntimeError)" in caplog.text
    assert "private platform response" not in caplog.text
