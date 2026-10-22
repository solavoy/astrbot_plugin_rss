import asyncio
from types import SimpleNamespace

import pytest
from astrbot_plugin_rss.core.config import PluginConfig
from astrbot_plugin_rss.core.errors import RetryableSendError
from astrbot_plugin_rss.core.models import Article, ContentBlock, now_ts
from astrbot_plugin_rss.core.runtime import RSSRuntime
from astrbot_plugin_rss.renderers.feishu import FeishuRenderer


class FakeFetcher:
    async def close(self):
        pass


class FakeParser:
    def parse(self, _result):
        raise AssertionError("not used")


class FeishuRegistry:
    def __init__(self, config):
        async def video_file(_target, url):
            return {"file_key": f"uploaded:{url.rsplit('/', 1)[-1]}", "file_name": "clip.mp4", "file_type": "mp4"}

        self.renderer = FeishuRenderer(config, video_file=video_file)
        self.render_calls = 0

    async def render(self, *args, **kwargs):
        self.render_calls += 1
        return await self.renderer.render(*args, **kwargs)


class RetryVideoOnceSender:
    def __init__(self):
        self.calls = []
        self.failed_video = False

    async def send(self, _target, message, idempotency_key):
        record = (message.message_type, message.payload, idempotency_key)
        self.calls.append(record)
        if message.message_type in {"file", "media"} and not self.failed_video:
            self.failed_video = True
            raise RetryableSendError("temporary video send failure")
        return SimpleNamespace(platform_message_id=f"receipt-{idempotency_key[:8]}")


@pytest.mark.asyncio
async def test_feishu_video_failure_retries_persisted_video_only_after_card_success(tmp_path):
    config = PluginConfig.from_mapping(
        {
            "retry_backoff_base": 0.01,
            "max_delivery_retries": 3,
        }
    )
    assert config.send_as_media is True
    sender = RetryVideoOnceSender()
    registry = FeishuRegistry(config)
    runtime = RSSRuntime(
        config,
        tmp_path,
        fetcher=FakeFetcher(),
        parser=FakeParser(),
        registry=registry,
        sender=sender,
    )
    await runtime.initialize()
    try:
        feed = await runtime.repository.create_feed("https://example.org/feishu-video", title="Forum")
        target = await runtime.repository.create_target("feishu", "feishu-video", "feishu", "feishu-video")
        await runtime.repository.add_direct_subscription(target.target_id, feed.feed_id, 60, send_history=True)
        article = Article(
            "",
            feed.feed_id,
            "video-entry",
            "https://example.org/video-entry",
            "Video article",
            content_blocks=[
                ContentBlock("paragraph", text="Text before video"),
                ContentBlock("video", url="https://cdn.example.org/clip.mp4"),
            ],
        )
        await runtime.repository.save_articles(feed.feed_id, [article])
        await runtime.repository.enqueue_due_paths(now_ts() + 1, 300, 60)
        semaphore = asyncio.Semaphore(1)

        await runtime._deliver_group(await runtime.repository.pending_deliveries(now_ts() + 1), semaphore)

        assert [call[0] for call in sender.calls] == ["interactive", "media"]
        original_calls = list(sender.calls)
        delivery = (await runtime.repository.deliveries(target_id=target.target_id))[0]
        assert delivery["status"] == "failed"
        assert delivery["retryable"] == 1
        cards = await runtime.repository.batch_cards(await runtime.repository.batch_group([delivery["delivery_id"]]))
        assert [card["status"] for card in cards] == ["success", "failed"]
        assert [card["idempotency_key"] for card in cards] == [call[2] for call in original_calls]

        due = await runtime.repository.pending_deliveries(now_ts() + 1000, 10, 5)
        assert len(due) == 1
        await runtime._deliver_group(due, semaphore)

        assert [call[0] for call in sender.calls] == ["interactive", "media", "media"]
        assert sender.calls[-1] == original_calls[-1]
        assert sender.calls[0] == original_calls[0]
        assert registry.render_calls == 1
        final = (await runtime.repository.deliveries(target_id=target.target_id))[0]
        assert final["status"] == "success"
        assert [
            card["status"]
            for card in await runtime.repository.batch_cards(
                await runtime.repository.batch_group([final["delivery_id"]])
            )
        ] == ["success", "success"]
    finally:
        await runtime.close()
