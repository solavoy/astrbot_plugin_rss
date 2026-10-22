import asyncio
from urllib.parse import urlsplit

import pytest
from astrbot_plugin_rss.core.config import PluginConfig
from astrbot_plugin_rss.core.models import Feed
from astrbot_plugin_rss.core.security import ValidatedURL
from astrbot_plugin_rss.fetchers.feed_fetcher import FeedFetcher

FEED_XML = b"<rss><channel><title>Fake</title></channel></rss>"


class Response:
    status_code = 200
    headers = {"content-type": "application/rss+xml"}

    def __init__(self, *, started=None, blocked=None, failure=None):
        self.started = started
        self.blocked = blocked
        self.failure = failure
        self.closed = False

    async def aiter_content(self, chunk_size=None):
        if self.started:
            self.started.set()
        if self.blocked:
            await self.blocked.wait()
        if self.failure:
            raise RuntimeError(self.failure)
        yield FEED_XML

    async def aclose(self):
        self.closed = True


class Session:
    def __init__(self, response=None, *, request_gate=None):
        self.response = response or Response()
        self.request_gate = request_gate
        self.closed = False
        self.request_cancelled = False
        self.calls = 0

    async def request(self, *args, **kwargs):
        self.calls += 1
        if self.request_gate:
            try:
                await self.request_gate.wait()
            except asyncio.CancelledError:
                self.request_cancelled = True
                raise
        return self.response

    async def close(self):
        self.closed = True


def patch_resolution(monkeypatch):
    async def resolve(url, resolve_dns=True):
        host = urlsplit(url).hostname or "example.org"
        return ValidatedURL(url, host, 443, ("93.184.216.34",))

    monkeypatch.setattr("astrbot_plugin_rss.fetchers.feed_fetcher.validate_public_url", resolve)


@pytest.mark.asyncio
async def test_different_hosts_obey_global_fetch_concurrency(tmp_path, monkeypatch):
    patch_resolution(monkeypatch)
    maximum = 2
    entered_limit = asyncio.Event()
    release = asyncio.Event()

    class SharedSession(Session):
        active = 0
        peak = 0

        async def request(self, *args, **kwargs):
            self.active += 1
            self.peak = max(self.peak, self.active)
            if self.active >= maximum:
                entered_limit.set()
            try:
                await release.wait()
                return self.response
            finally:
                self.active -= 1

    session = SharedSession()
    fetcher = FeedFetcher(
        PluginConfig(max_fetch_concurrency=maximum, request_retry_limit=0),
        tmp_path,
        lambda **_: session,
    )
    fetcher.compatibility.inspect = lambda: {"compatible": False}
    tasks = [asyncio.create_task(fetcher.fetch(Feed(f"feed-{i}", f"https://host-{i}.example/rss"))) for i in range(5)]
    await asyncio.wait_for(entered_limit.wait(), timeout=2)
    assert session.peak == maximum
    release.set()
    results = await asyncio.gather(*tasks)
    assert all(result.error_type is None for result in results)
    assert session.peak == maximum
    await fetcher.close()
    assert session.closed


@pytest.mark.asyncio
async def test_stream_failure_closes_response_and_redacts_proxy_credentials(tmp_path, monkeypatch):
    patch_resolution(monkeypatch)
    proxy_secret = "proxy-user:proxy-secret"
    response = Response(failure=f"connection failed through {proxy_secret}")
    session = Session(response)
    fetcher = FeedFetcher(
        PluginConfig(
            proxy_profile=f"http://{proxy_secret}@proxy.example:8080",
            request_retry_limit=0,
        ),
        tmp_path,
        lambda **_: session,
    )
    fetcher.compatibility.inspect = lambda: {"compatible": False}
    result = await fetcher.fetch(Feed("credential-feed", "https://origin.example/rss"))
    assert result.error_type == "RuntimeError"
    assert result.error_message == "upstream request failed; details omitted"
    assert proxy_secret not in repr(result)
    assert response.closed
    await fetcher.close()


@pytest.mark.asyncio
async def test_stream_cancellation_closes_response(tmp_path, monkeypatch):
    patch_resolution(monkeypatch)
    started, blocked = asyncio.Event(), asyncio.Event()
    response = Response(started=started, blocked=blocked)
    session = Session(response)
    fetcher = FeedFetcher(PluginConfig(request_retry_limit=0), tmp_path, lambda **_: session)
    fetcher.compatibility.inspect = lambda: {"compatible": False}
    task = asyncio.create_task(fetcher.fetch(Feed("cancel-stream", "https://cancel.example/rss")))
    await asyncio.wait_for(started.wait(), timeout=2)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert response.closed
    await fetcher.close()


@pytest.mark.asyncio
async def test_close_cancels_and_awaits_external_inflight_fetch(tmp_path, monkeypatch):
    patch_resolution(monkeypatch)
    request_gate = asyncio.Event()
    session = Session(request_gate=request_gate)
    fetcher = FeedFetcher(PluginConfig(request_retry_limit=0), tmp_path, lambda **_: session)
    fetcher.compatibility.inspect = lambda: {"compatible": False}
    task = asyncio.create_task(fetcher.fetch(Feed("external-task", "https://inflight.example/rss")))
    while session.calls == 0:
        await asyncio.sleep(0)
    await fetcher.close()
    result = await asyncio.gather(task, return_exceptions=True)
    assert isinstance(result[0], asyncio.CancelledError)
    assert session.request_cancelled
    assert session.closed
    assert not fetcher._active_fetches
