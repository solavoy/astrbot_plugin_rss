import asyncio
import inspect

import pytest
from astrbot_plugin_rss.core.config import PluginConfig
from astrbot_plugin_rss.core.models import Feed
from astrbot_plugin_rss.core.security import UnsafeURLError, validate_public_url
from astrbot_plugin_rss.fetchers.feed_fetcher import FeedFetcher
from curl_cffi import CurlOpt
from curl_cffi.requests import AsyncSession


@pytest.mark.asyncio
async def test_real_asyncsession_api_uses_session_level_dns_pin(tmp_path, monkeypatch):
    async def fake_resolve(url, resolve_dns=True):
        from astrbot_plugin_rss.core.security import ValidatedURL

        return ValidatedURL(url, "example.org", 443, ("93.184.216.34",))

    monkeypatch.setattr("astrbot_plugin_rss.fetchers.feed_fetcher.validate_public_url", fake_resolve)
    config = PluginConfig()
    fetcher = FeedFetcher(config, tmp_path)
    validated = await fake_resolve("https://example.org/feed")
    key, entry, session = await fetcher._lease_session(validated)
    try:
        assert isinstance(session, AsyncSession)
        assert session.curl_options[CurlOpt.RESOLVE] == ["+example.org:443:93.184.216.34"]
        assert session.trust_env is False
        assert session.max_clients == 1
        assert "curl_options" not in inspect.signature(AsyncSession.request).parameters
    finally:
        await fetcher._release_session(key, entry)
    await fetcher.close()


def test_url_blocks_local_ip_and_credential_urls(monkeypatch):
    async def run():
        with pytest.raises(UnsafeURLError):
            await validate_public_url("http://127.0.0.1/feed")
        with pytest.raises(UnsafeURLError):
            await validate_public_url("https://user:pass@example.org/rss")

    asyncio.run(run())


@pytest.mark.parametrize("url", ["http://example.org:0/feed", "https://example.org:0/feed"])
def test_url_rejects_explicit_zero_port(url):
    async def run():
        with pytest.raises(UnsafeURLError, match="non-standard ports"):
            await validate_public_url(url, resolve_dns=False)

    asyncio.run(run())


def test_url_rejects_mixed_dns_and_mapped_private_addresses(monkeypatch):
    def mixed(host, port, type=None):
        return [
            (2, 1, 6, "", ("93.184.216.34", port)),
            (2, 1, 6, "", ("10.0.0.1", port)),
        ]

    monkeypatch.setattr("astrbot_plugin_rss.core.security.socket.getaddrinfo", mixed)

    async def run():
        with pytest.raises(UnsafeURLError):
            await validate_public_url("https://example.org/rss")
        with pytest.raises(UnsafeURLError):
            await validate_public_url("http://[::ffff:127.0.0.1]/rss")
        with pytest.raises(UnsafeURLError):
            await validate_public_url("file:///etc/passwd")
        with pytest.raises(UnsafeURLError):
            await validate_public_url("https://example.org/\nheader")

    asyncio.run(run())


def test_url_normalizes_idna_trailing_dot_and_ipv6_without_dns(monkeypatch):
    def public_dns(host, port, type=None):
        return [(2, 1, 6, "", ("93.184.216.34", port))]

    monkeypatch.setattr("astrbot_plugin_rss.core.security.socket.getaddrinfo", public_dns)

    async def run():
        international = await validate_public_url("https://bücher.example./feed")
        assert international.url == "https://xn--bcher-kva.example/feed"
        literal = await validate_public_url("https://[2606:4700:4700::1111]/feed")
        assert literal.hostname == "2606:4700:4700::1111"
        assert literal.url == "https://[2606:4700:4700::1111]/feed"
        assert literal.curl_resolve() == ""

    asyncio.run(run())


def test_feed_request_config_contract():
    config = PluginConfig()
    assert config.fetch_user_agent
    assert config.max_feed_response_bytes > 0
    assert Feed("f", "https://example.org/rss").url.endswith("rss")
