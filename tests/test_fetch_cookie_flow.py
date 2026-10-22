import pytest
from astrbot_plugin_rss.core.config import PluginConfig
from astrbot_plugin_rss.core.models import Feed
from astrbot_plugin_rss.core.security import ValidatedURL
from astrbot_plugin_rss.fetchers.feed_fetcher import FeedFetcher


class Response:
    status_code = 200
    headers = {"content-type": "application/rss+xml"}

    async def aiter_content(self, chunk_size=None):
        yield b"<rss><channel/></rss>"

    async def aclose(self):
        return None


class Session:
    def __init__(self, kwargs, requests):
        self.init_kwargs = kwargs
        self.requests = requests

    async def request(self, *args, **kwargs):
        self.requests.append(kwargs)
        return Response()

    async def close(self):
        return None


@pytest.mark.asyncio
async def test_fetcher_sends_only_matching_cookie_after_verified_compatibility(tmp_path, monkeypatch):
    config = PluginConfig(curl_impersonate_target="chrome")
    created = []

    def factory(**kwargs):
        session = Session(kwargs, requests)
        created.append(session)
        return session

    requests = []
    fetcher = FeedFetcher(config, tmp_path, factory)
    monkeypatch.setattr(
        "astrbot_plugin_rss.fetchers.feed_fetcher.validate_public_url", lambda url, resolve_dns=True: _validated(url)
    )
    fetcher.compatibility.inspect = lambda: {
        "compatible": True,
        "browser_version": "146.0.7680.177.5",
        "impersonate_target": "chrome146",
    }
    browser_ua = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/146.0.0.0 Safari/537.36"
    fetcher.cookies.save(
        "https://example.org",
        [
            {
                "name": "good",
                "value": "secret",
                "domain": ".example.org",
                "path": "/feed",
                "expires": -1,
                "secure": True,
            },
            {"name": "wrong-path", "value": "no", "domain": ".example.org", "path": "/other", "expires": -1},
            {"name": "wrong-domain", "value": "no", "domain": "badexample.org", "path": "/", "expires": -1},
        ],
        browser_ua,
        "",
        "146.0.7680.177",
        "chrome146",
        browser_metadata={
            "user_agent": browser_ua,
            "major_version": "146",
            "actual_browser_version": "146.0.7680.177",
            "binary_version": "146.0.7680.177.5",
            "platform": "Linux",
            "mobile": False,
        },
    )
    result = await fetcher.fetch(Feed("f", "https://example.org/feed/rss"))
    assert result.status_code == 200
    assert requests[0]["headers"]["Cookie"] == "good=secret"
    assert requests[0]["headers"]["User-Agent"] == browser_ua
    assert created[0].init_kwargs["impersonate"] == "chrome146"
    assert fetcher.cookies.status("https://example.org")["validation_status"] == "validated"
    fetcher.compatibility.inspect = lambda: {
        "compatible": True,
        "browser_version": "147.0.1",
        "impersonate_target": "chrome147",
    }
    await fetcher.fetch(Feed("f", "https://example.org/feed/rss"))
    assert "Cookie" not in requests[1]["headers"]
    await fetcher.close()


@pytest.mark.asyncio
async def test_cookie_persistence_switch_prevents_reads_and_writes(tmp_path, monkeypatch):
    config = PluginConfig(enable_cookie_persistence=False)
    requests = []
    fetcher = FeedFetcher(config, tmp_path, lambda **kwargs: Session(kwargs, requests))
    monkeypatch.setattr(
        "astrbot_plugin_rss.fetchers.feed_fetcher.validate_public_url", lambda url, resolve_dns=True: _validated(url)
    )
    fetcher.compatibility.inspect = lambda: {
        "compatible": True,
        "browser_version": "146.0.1",
        "impersonate_target": "chrome146",
    }
    fetcher.cookies.save(
        "https://example.org",
        [{"name": "c", "value": "v", "domain": ".example.org", "path": "/", "expires": -1}],
        config.fetch_user_agent,
        "",
        "146.0.1",
        "chrome146",
    )
    await fetcher.fetch(Feed("f", "https://example.org/feed"))
    assert "Cookie" not in requests[0]["headers"]
    assert fetcher.cookies.status("https://example.org")["validation_status"] == "browser_obtained"
    await fetcher.close()


@pytest.mark.asyncio
async def test_browser_cookie_uses_browser_bound_ua_and_client_hints(tmp_path, monkeypatch):
    config = PluginConfig(curl_impersonate_target="chrome")
    requests = []
    fetcher = FeedFetcher(config, tmp_path, lambda **kwargs: Session(kwargs, requests))
    monkeypatch.setattr(
        "astrbot_plugin_rss.fetchers.feed_fetcher.validate_public_url",
        lambda url, resolve_dns=True: _validated(url),
    )
    fetcher.compatibility.inspect = lambda: {
        "compatible": True,
        "browser_version": "146.0.1",
        "impersonate_target": "chrome146",
    }
    browser_ua = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/146.0.0.0 Safari/537.36"
    fetcher.cookies.save(
        "https://example.org",
        [{"name": "clearance", "value": "ok", "domain": ".example.org", "path": "/", "expires": -1}],
        browser_ua,
        "",
        "146.0.1",
        "chrome146",
        browser_metadata={
            "user_agent": browser_ua,
            "major_version": "146",
            "platform": "Linux",
            "mobile": False,
            "binary_version": "146.0.1",
            "actual_browser_version": "146.0.1",
        },
    )
    result = await fetcher.fetch(Feed("f", "https://example.org/feed"))
    assert result.status_code == 200
    headers = requests[0]["headers"]
    assert headers["Cookie"] == "clearance=ok"
    assert headers["User-Agent"] == browser_ua
    assert headers["sec-ch-ua-platform"] == '"Linux"'
    assert headers["sec-ch-ua-mobile"] == "?0"
    assert result.cookie_cache_hit is True
    await fetcher.close()


async def _validated(url):
    return ValidatedURL(url, "example.org", 443, ("93.184.216.34",))
