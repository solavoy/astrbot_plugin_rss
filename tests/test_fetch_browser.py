import asyncio
import sys
import time
import types

import pytest
from astrbot_plugin_rss.core.config import PluginConfig
from astrbot_plugin_rss.core.models import Feed, FetchResult
from astrbot_plugin_rss.core.security import ValidatedURL
from astrbot_plugin_rss.fetchers.feed_fetcher import FeedFetcher


@pytest.mark.asyncio
async def test_first_browser_attempt_allowed_when_monotonic_clock_is_small(tmp_path, monkeypatch):
    fake = types.ModuleType("cloakbrowser")
    fake.binary_info = lambda **_: {"installed": False}
    monkeypatch.setitem(sys.modules, "cloakbrowser", fake)
    fetcher = FeedFetcher(PluginConfig(enable_cloakbrowser=True, browser_retry_cooldown=900), tmp_path)
    monkeypatch.setattr("astrbot_plugin_rss.fetchers.feed_fetcher.time.monotonic", lambda: 1.0)
    feed = Feed("f", "https://example.org/feed")
    prior = FetchResult("f", feed.url, feed.url, 403, "text/html", b"cf-chl-")
    first = await fetcher._browser_fetch(feed, prior, 1.0)
    second = await fetcher._browser_fetch(feed, prior, 1.0)
    assert first.error_type == "browser_unavailable"
    assert second.error_type == "browser_cooldown"
    await fetcher.close()


@pytest.mark.asyncio
async def test_browser_total_timeout_closes_context(tmp_path, monkeypatch):
    fake = types.ModuleType("cloakbrowser")
    fake.binary_info = lambda **_: {"installed": True, "installed_version": "146.0.1"}
    context = _Context()

    async def launch(*args, **kwargs):
        return context

    fake.launch_persistent_context_async = launch
    monkeypatch.setitem(sys.modules, "cloakbrowser", fake)
    monkeypatch.setattr(
        "astrbot_plugin_rss.fetchers.feed_fetcher.BrowserCompatibilityManager.inspect",
        lambda self: {"compatible": True, "impersonate_target": "chrome146"},
    )
    monkeypatch.setattr(
        "astrbot_plugin_rss.fetchers.feed_fetcher.validate_public_url",
        lambda url: _validated(url),
    )
    config = PluginConfig(enable_cloakbrowser=True, browser_timeout=0.02, curl_impersonate_target="chrome")
    fetcher = FeedFetcher(config, tmp_path)
    feed = Feed("f", "https://example.org/feed")
    prior = FetchResult("f", feed.url, feed.url, 403, "text/html", b"cf-chl-")
    result = await fetcher._browser_fetch(feed, prior, 0.0)
    assert result.error_type == "browser_timeout"
    assert context.closed
    await fetcher.close()


@pytest.mark.asyncio
async def test_close_cancels_active_browser_fetch_and_closes_context(tmp_path, monkeypatch):
    fake = types.ModuleType("cloakbrowser")
    fake.binary_info = lambda **_: {"installed": True, "installed_version": "146.0.1"}
    context = _Context()
    fake.launch_persistent_context_async = _launch(context)
    monkeypatch.setitem(sys.modules, "cloakbrowser", fake)
    monkeypatch.setattr(
        "astrbot_plugin_rss.fetchers.feed_fetcher.BrowserCompatibilityManager.inspect",
        lambda self: {"compatible": True, "impersonate_target": "chrome146"},
    )
    monkeypatch.setattr(
        "astrbot_plugin_rss.fetchers.feed_fetcher.validate_public_url",
        lambda url, resolve_dns=True: _validated(url),
    )
    entered = asyncio.Event()

    async def hanging_goto(*args, **kwargs):
        entered.set()
        await asyncio.Future()

    context.pages[0].goto = hanging_goto
    config = PluginConfig(
        enable_cloakbrowser=True,
        browser_timeout=30,
        curl_impersonate_target="chrome",
        browser_allowed_domains=("example.org",),
    )
    fetcher = FeedFetcher(config, tmp_path)

    async def challenge(*args, **kwargs):
        return FetchResult("f", "https://example.org/feed", "https://example.org/feed", 403, "text/html", b"cf-chl-")

    fetcher._request_chain = challenge
    fetch_task = asyncio.create_task(fetcher.fetch(Feed("f", "https://example.org/feed")))
    await asyncio.wait_for(entered.wait(), timeout=2)
    await fetcher.close()
    assert fetch_task.done()
    assert context.closed
    assert not fetcher._browser_contexts
    with pytest.raises(asyncio.CancelledError):
        await fetch_task


@pytest.mark.asyncio
async def test_browser_chunked_feed_uses_bounded_curl_refetch(tmp_path, monkeypatch):
    fake = types.ModuleType("cloakbrowser")
    fake.binary_info = lambda **_: {"installed": True, "installed_version": "146.0.7680.177.5"}
    context = _Context()
    context.browser.version = "146.0.7680.177"

    async def immediate_goto(*args, **kwargs):
        return None

    context.pages[0].goto = immediate_goto
    fake.launch_persistent_context_async = _launch(context)
    monkeypatch.setitem(sys.modules, "cloakbrowser", fake)
    monkeypatch.setattr(
        "astrbot_plugin_rss.fetchers.feed_fetcher.BrowserCompatibilityManager.inspect",
        lambda self: {"compatible": True, "impersonate_target": "chrome146"},
    )
    monkeypatch.setattr(
        "astrbot_plugin_rss.fetchers.feed_fetcher.validate_public_url",
        lambda url, resolve_dns=True: _validated(url),
    )
    config = PluginConfig(enable_cloakbrowser=True, curl_impersonate_target="chrome")
    fetcher = FeedFetcher(config, tmp_path)
    feed = Feed("f", "https://example.org/feed")
    prior = FetchResult("f", feed.url, feed.url, 403, "text/html", b"cf-chl-")
    refetched = FetchResult("f", feed.url, feed.url, 200, "application/rss+xml", b"<rss><channel/></rss>")
    calls = 0

    async def bounded_curl(*args, **kwargs):
        nonlocal calls
        calls += 1
        metadata = fetcher.cookies.client_metadata_for_url(
            feed.url, config.fetch_user_agent, "", None, "chrome146", binary_version="146.0.7680.177.5"
        )
        cookies = fetcher.cookies.get_for_url(
            feed.url, metadata["user_agent"], "", metadata["actual_browser_version"], "chrome146"
        )
        assert cookies and cookies[0]["name"] == "clearance"
        return refetched

    fetcher._request_chain = bounded_curl
    result = await fetcher._browser_fetch(feed, prior, 0.0)
    assert calls == 1
    assert result.raw_xml == refetched.raw_xml
    assert fetcher.cookies.status("https://example.org")["validation_status"] == "browser_obtained"
    await fetcher.close()


@pytest.mark.asyncio
async def test_browser_captured_feed_is_used_when_curl_refetch_fails(tmp_path, monkeypatch):
    fake = types.ModuleType("cloakbrowser")
    fake.binary_info = lambda **_: {"installed": True, "installed_version": "146.0.7680.177.5"}
    context = _Context()
    context.browser.version = "146.0.7680.177"
    page = context.pages[0]
    listeners = []
    feed = Feed("f", "https://example.org/feed")
    body = b"<rss><channel><title>Browser feed</title></channel></rss>"

    class BrowserResponse:
        status = 200
        url = feed.url

        def __init__(self):
            self.headers = {
                "content-type": "application/rss+xml",
                "content-length": str(len(body)),
            }

        async def body(self):
            return body

    def on(event, callback):
        assert event == "response"
        listeners.append(callback)

    async def immediate_goto(*args, **kwargs):
        for callback in listeners:
            callback(BrowserResponse())

    async def evaluate(script, *args):
        if "document.title" in script:
            return "Legitimate article mentions captcha"
        return {
            "platform": "Linux",
            "mobile": False,
            "major_version": "146",
            "user_agent": "Mozilla/5.0 Chrome/146.0.0.0",
        }

    page.evaluate = evaluate
    page.on = on
    page.goto = immediate_goto
    fake.launch_persistent_context_async = _launch(context)
    monkeypatch.setitem(sys.modules, "cloakbrowser", fake)
    monkeypatch.setattr(
        "astrbot_plugin_rss.fetchers.feed_fetcher.BrowserCompatibilityManager.inspect",
        lambda self: {"compatible": True, "impersonate_target": "chrome146"},
    )
    monkeypatch.setattr(
        "astrbot_plugin_rss.fetchers.feed_fetcher.validate_public_url",
        lambda url, resolve_dns=True: _validated(url),
    )
    config = PluginConfig(enable_cloakbrowser=True, curl_impersonate_target="chrome")
    fetcher = FeedFetcher(config, tmp_path)
    prior = FetchResult("f", feed.url, feed.url, 403, "text/html", b"cf-chl-")
    refetch_calls = 0

    async def failing_curl(*args, **kwargs):
        nonlocal refetch_calls
        refetch_calls += 1
        return FetchResult("f", feed.url, feed.url, 403, "text/html", b"forbidden")

    fetcher._request_chain = failing_curl
    result = await fetcher._browser_fetch(feed, prior, 0.0)
    assert refetch_calls == 0
    assert result.raw_xml == body
    assert result.fetch_method == "browser"
    assert result.browser_attempted is True
    assert context.closed
    await fetcher.close()


@pytest.mark.asyncio
async def test_browser_curl_refetch_preserves_429_cooldown(tmp_path, monkeypatch):
    fake = types.ModuleType("cloakbrowser")
    fake.binary_info = lambda **_: {"installed": True, "installed_version": "146.0.7680.177.5"}
    context = _Context()
    context.browser.version = "146.0.7680.177"
    context.pages[0].goto = lambda *args, **kwargs: asyncio.sleep(0)
    fake.launch_persistent_context_async = _launch(context)
    monkeypatch.setitem(sys.modules, "cloakbrowser", fake)
    monkeypatch.setattr(
        "astrbot_plugin_rss.fetchers.feed_fetcher.BrowserCompatibilityManager.inspect",
        lambda self: {"compatible": True, "impersonate_target": "chrome146"},
    )
    monkeypatch.setattr(
        "astrbot_plugin_rss.fetchers.feed_fetcher.validate_public_url",
        lambda url, resolve_dns=True: _validated(url),
    )
    config = PluginConfig(
        enable_cloakbrowser=True,
        enable_cookie_persistence=False,
        curl_impersonate_target="chrome",
        browser_allowed_domains=("example.org",),
    )
    fetcher = FeedFetcher(config, tmp_path)
    feed = Feed("f", "https://example.org/feed")
    cooldown = time.time() + 240
    responses = iter(
        [
            FetchResult("f", feed.url, feed.url, 403, "text/html", b"cf-chl-"),
            FetchResult("f", feed.url, feed.url, 429, "text/html", error_type="http_429", retry_after_at=cooldown),
        ]
    )

    async def fake_request_chain(*args, **kwargs):
        return next(responses)

    fetcher._request_chain = fake_request_chain
    try:
        result = await fetcher.fetch(feed)
        assert result.status_code == 429
        assert result.retry_after_at == cooldown
        assert result.browser_attempted is True
    finally:
        await fetcher.close()


class _Context:
    def __init__(self):
        self.browser = types.SimpleNamespace(version="146.0.1")
        self.pages = [_Page()]
        self.closed = False

    async def route(self, *_):
        return None

    async def route_web_socket(self, *_):
        return None

    async def close(self):
        self.closed = True

    async def cookies(self):
        return [{"name": "clearance", "value": "ok", "domain": ".example.org", "path": "/", "expires": -1}]


class _Page:
    def on(self, *_):
        return None

    async def evaluate(self, *_):
        return {
            "platform": "Linux",
            "mobile": False,
            "major_version": "146",
            "user_agent": "Mozilla/5.0 Chrome/146.0.0.0",
        }

    async def goto(self, *_, **__):
        await asyncio.Future()


async def _validated(url):
    return ValidatedURL(url, "example.org", 443, ("93.184.216.34",))


def _launch(context):
    async def launch(*args, **kwargs):
        return context

    return launch
