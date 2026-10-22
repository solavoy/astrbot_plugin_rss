import pytest
from astrbot_plugin_rss.core.config import PluginConfig
from astrbot_plugin_rss.core.models import FetchResult
from astrbot_plugin_rss.core.runtime import RSSRuntime
from astrbot_plugin_rss.scheduler import FeedScheduler
from astrbot_plugin_rss.services.management import ManagementService


class RateLimitedFetcher:
    def __init__(self, retry_after_at):
        self.retry_after_at = retry_after_at
        self.calls = 0

    async def fetch(self, feed, force=False):
        self.calls += 1
        server = "edge-" + "s" * 91
        discourse_code = "rss_" + "d" * 92
        cf_ray = "ray-" + "c" * 92
        message = (
            "HTTP 429；请求受限，至少等待 120 秒后重试；本轮请求尝试=1；Retry-After=120"
            f"；server={server}；discourse-rate-limit-error-code={discourse_code}；cf-ray={cf_ray}"
        )
        return FetchResult(
            feed.feed_id,
            feed.url,
            status_code=429,
            fetched_at=self.retry_after_at - 120,
            error_type="http_429",
            error_message=message,
            retry_after_at=self.retry_after_at,
        )

    async def close(self):
        return None


@pytest.mark.asyncio
async def test_429_fetch_log_keeps_safe_diagnostics_through_management_detail(tmp_path):
    from astrbot_plugin_rss.core.models import now_ts

    start = now_ts()
    fetcher = RateLimitedFetcher(start + 120)
    runtime = RSSRuntime(PluginConfig.from_mapping({"global_poll_interval": 1}), tmp_path, fetcher=fetcher)
    await runtime.initialize()
    try:
        feed = await runtime.repository.create_feed("https://example.org/ratelimit")
        target = await runtime.repository.create_target("instance", "target", "test", "target")
        await runtime.repository.add_direct_subscription(target.target_id, feed.feed_id, interval=1)

        result = await FeedScheduler(runtime).run_once()
        assert result["errors"] == 1
        assert fetcher.calls == 1

        detail = await ManagementService(runtime).get(f"feeds/{feed.public_id}", {})
        logged = detail["fetch_logs"][0]
        assert logged["error_type"] == "http_429"
        assert len(logged["error_message"]) > 300
        assert len(logged["error_message"]) <= 768
        assert "discourse-rate-limit-error-code=rss_" in logged["error_message"]
        assert logged["error_message"].endswith("cf-ray=ray-" + "c" * 92)

        await runtime.repository.log_fetch(
            feed.feed_id,
            start + 1,
            500,
            "curl",
            1,
            "http_500",
            "x" * 900,
        )
        other = await runtime.database.fetchone(
            "SELECT error_message FROM fetch_logs WHERE error_type='http_500' ORDER BY id DESC LIMIT 1"
        )
        assert len(other["error_message"]) == 300
    finally:
        await runtime.close()
