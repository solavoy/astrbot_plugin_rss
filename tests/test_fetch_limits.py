import asyncio
from urllib.parse import urlsplit

import pytest
from astrbot_plugin_rss.core.config import PluginConfig
from astrbot_plugin_rss.core.models import Feed
from astrbot_plugin_rss.core.security import ValidatedURL
from astrbot_plugin_rss.fetchers.feed_fetcher import FeedFetcher


@pytest.mark.asyncio
async def test_fetcher_global_concurrency_limit_covers_distinct_hosts(tmp_path, monkeypatch):
    active = maximum = 0

    class Response:
        status_code = 200
        headers = {"content-type": "application/rss+xml"}

        async def aiter_content(self, chunk_size=None):
            yield b"<rss><channel/></rss>"

        async def aclose(self):
            return None

    class Session:
        async def request(self, *args, **kwargs):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0.02)
            active -= 1
            return Response()

        async def close(self):
            return None

    async def validate(url, resolve_dns=True):
        host = urlsplit(url).hostname
        return ValidatedURL(url, host, 443, ("93.184.216.34",))

    monkeypatch.setattr("astrbot_plugin_rss.fetchers.feed_fetcher.validate_public_url", validate)
    config = PluginConfig(max_fetch_concurrency=2, request_retry_limit=0)
    fetcher = FeedFetcher(config, tmp_path, lambda **_: Session())
    results = await asyncio.gather(*(fetcher.fetch(Feed(str(i), f"https://host{i}.example/rss")) for i in range(8)))
    assert maximum == 2
    assert all(result.error_type is None for result in results)
    await fetcher.close()


@pytest.mark.asyncio
async def test_streaming_exception_closes_response_before_return(tmp_path, monkeypatch):
    class Response:
        status_code = 200
        headers = {"content-type": "application/rss+xml"}
        closed = False

        async def aiter_content(self, chunk_size=None):
            yield b"<rss>"
            raise RuntimeError("network error")

        async def aclose(self):
            self.closed = True

    response = Response()

    class Session:
        async def request(self, *args, **kwargs):
            return response

        async def close(self):
            return None

    async def validate(url, resolve_dns=True):
        return ValidatedURL(url, "example.org", 443, ("93.184.216.34",))

    monkeypatch.setattr("astrbot_plugin_rss.fetchers.feed_fetcher.validate_public_url", validate)
    fetcher = FeedFetcher(PluginConfig(request_retry_limit=0), tmp_path, lambda **_: Session())
    result = await fetcher.fetch(Feed("f", "https://example.org/rss"))
    assert result.error_type == "RuntimeError"
    assert response.closed
    assert "network error" not in result.error_message
    await fetcher.close()


@pytest.mark.asyncio
async def test_streaming_byte_limit_works_with_no_argument_aiter_content(tmp_path, monkeypatch):
    body = b"<rss><channel/></rss>"

    class Response:
        status_code = 200
        headers = {"content-type": "application/rss+xml"}
        closed = False

        async def aiter_content(self):
            yield body

        async def aclose(self):
            self.closed = True

    response = Response()

    class Session:
        async def request(self, *args, **kwargs):
            return response

        async def close(self):
            return None

    async def validate(url, resolve_dns=True):
        return ValidatedURL(url, "example.org", 443, ("93.184.216.34",))

    monkeypatch.setattr("astrbot_plugin_rss.fetchers.feed_fetcher.validate_public_url", validate)
    fetcher = FeedFetcher(
        PluginConfig(max_feed_response_bytes=len(body) - 1, request_retry_limit=0),
        tmp_path,
        lambda **_: Session(),
    )
    result = await fetcher.fetch(Feed("f", "https://example.org/rss"))
    assert result.error_type == "response_too_large"
    assert response.closed
    await fetcher.close()
