import time
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import pytest
from astrbot_plugin_rss.core.config import PluginConfig
from astrbot_plugin_rss.core.models import Feed
from astrbot_plugin_rss.core.security import UnsafeURLError, ValidatedURL
from astrbot_plugin_rss.fetchers.feed_fetcher import FeedFetcher


class Response:
    def __init__(self, status, headers=None, chunks=()):
        self.status_code = status
        self.headers = headers or {}
        self.chunks = chunks
        self.closed = False

    async def aiter_content(self, chunk_size=None):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self):
        self.closed = True


class Session:
    def __init__(self, replies, log):
        self.replies = replies
        self.log = log

    async def request(self, *args, **kwargs):
        self.log.append((args, kwargs))
        return self.replies.pop(0)

    async def close(self):
        return None


@pytest.mark.asyncio
async def test_304_validators_and_no_body(tmp_path, monkeypatch):
    replies = [Response(304, {"etag": '"v2"'})]
    requests = []
    fetcher = FeedFetcher(PluginConfig(), tmp_path, lambda **_: Session(replies, requests))

    async def validate(url, resolve_dns=True):
        return ValidatedURL(url, "example.org", 443, ("93.184.216.34",))

    monkeypatch.setattr("astrbot_plugin_rss.fetchers.feed_fetcher.validate_public_url", validate)
    result = await fetcher.fetch(Feed("f", "https://example.org/feed", etag='"v1"', last_modified="yesterday"))
    assert result.status_code == 304 and result.raw_xml == b"" and result.etag == '"v2"'
    assert requests[0][1]["allow_redirects"] is False
    assert requests[0][1]["discard_cookies"] is True
    assert requests[0][1]["headers"]["If-None-Match"] == '"v1"'
    await fetcher.close()


@pytest.mark.asyncio
async def test_redirect_revalidated_before_second_hop(tmp_path, monkeypatch):
    replies = [Response(302, {"location": "http://127.0.0.1/admin"})]
    requests = []
    fetcher = FeedFetcher(PluginConfig(request_retry_limit=0), tmp_path, lambda **_: Session(replies, requests))

    async def validate(url, resolve_dns=True):
        if "127.0.0.1" in url:
            raise UnsafeURLError("URL resolves to a non-public address")
        return ValidatedURL(url, "example.org", 443, ("93.184.216.34",))

    monkeypatch.setattr("astrbot_plugin_rss.fetchers.feed_fetcher.validate_public_url", validate)
    result = await fetcher.fetch(Feed("f", "https://example.org/feed"))
    assert result.error_type == "unsafe_redirect"
    assert len(requests) == 1
    await fetcher.close()


@pytest.mark.asyncio
async def test_bounded_decompressed_body_and_xml_root_validation(tmp_path, monkeypatch):
    body = b"<rss><channel/></rss>"
    replies = [Response(200, {"content-type": "application/xml"}, [body[:8], body[8:]])]
    requests = []
    fetcher = FeedFetcher(PluginConfig(request_retry_limit=0), tmp_path, lambda **_: Session(replies, requests))

    async def validate(url, resolve_dns=True):
        return ValidatedURL(url, "example.org", 443, ("93.184.216.34",))

    monkeypatch.setattr("astrbot_plugin_rss.fetchers.feed_fetcher.validate_public_url", validate)
    result = await fetcher.fetch(Feed("f", "https://example.org/feed"))
    assert result.error_type is None and result.raw_xml == body
    await fetcher.close()

    small = PluginConfig(max_feed_response_bytes=8, request_retry_limit=0)
    replies = [Response(200, {"content-type": "application/xml"}, [body])]
    fetcher = FeedFetcher(small, tmp_path / "small", lambda **_: Session(replies, []))
    monkeypatch.setattr("astrbot_plugin_rss.fetchers.feed_fetcher.validate_public_url", validate)
    result = await fetcher.fetch(Feed("f", "https://example.org/feed"))
    assert result.error_type == "response_too_large"
    await fetcher.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("retry_after", "expected"),
    [
        ("17", 17.0),
        ("nan", 60.0),
        ("inf", 60.0),
        ("-2", 60.0),
        ("not a date", 60.0),
        (None, 60.0),
        ("0", 0.0),
    ],
)
async def test_429_returns_once_with_cooldown_and_closes_response(tmp_path, monkeypatch, retry_after, expected):
    response_headers = {"content-type": "text/html"}
    if retry_after is not None:
        response_headers["retry-after"] = retry_after
    response = Response(429, response_headers, [b"must not be read"])
    response.read_attempted = False
    original_iter = response.aiter_content

    async def track_read(chunk_size=None):
        response.read_attempted = True
        async for chunk in original_iter(chunk_size):
            yield chunk

    response.aiter_content = track_read
    replies, requests = [response], []
    fetcher = FeedFetcher(PluginConfig(request_retry_limit=3), tmp_path, lambda **_: Session(replies, requests))

    async def validate(url, resolve_dns=True):
        return ValidatedURL(url, "example.org", 443, ("93.184.216.34",))

    monkeypatch.setattr("astrbot_plugin_rss.fetchers.feed_fetcher.validate_public_url", validate)
    before = time.time()
    try:
        result = await fetcher.fetch(Feed("f", "https://example.org/feed"))
        assert result.status_code == 429
        assert result.error_type == "http_429"
        assert result.error_message.startswith("HTTP 429；请求受限，至少等待 ")
        assert "本轮请求尝试=1" in result.error_message
        assert result.browser_attempted is False
        assert result.retry_after_at == pytest.approx(before + expected, abs=1.0)
        assert len(requests) == 1
        assert response.closed
        assert not response.read_attempted
        if retry_after is None:
            assert "Retry-After 未提供，使用默认60秒" in result.error_message
        elif retry_after in {"nan", "inf", "-2", "not a date"}:
            assert f"Retry-After={retry_after}（无效，使用默认60秒）" in result.error_message
    finally:
        await fetcher.close()


@pytest.mark.asyncio
async def test_429_retry_after_http_date_and_long_delta_are_not_capped(tmp_path, monkeypatch):
    future = datetime.now(UTC) + timedelta(seconds=240)
    for header in ("240", format_datetime(future, usegmt=True)):
        response = Response(429, {"retry-after": header})
        requests = []
        fetcher = FeedFetcher(
            PluginConfig(request_retry_limit=3),
            tmp_path / str(len(requests)),
            lambda **_: Session([response], requests),
        )

        async def validate(url, resolve_dns=True):
            return ValidatedURL(url, "example.org", 443, ("93.184.216.34",))

        monkeypatch.setattr("astrbot_plugin_rss.fetchers.feed_fetcher.validate_public_url", validate)
        before = time.time()
        try:
            result = await fetcher.fetch(Feed("f", "https://example.org/feed"))
            assert result.retry_after_at - before > 200
            assert f"Retry-After={header}" in result.error_message
            assert len(requests) == 1 and response.closed
        finally:
            await fetcher.close()


@pytest.mark.asyncio
async def test_429_deadline_overflow_falls_back_to_sixty_seconds(tmp_path, monkeypatch):
    response = Response(429, {"retry-after": "1e308"})
    requests = []
    fetcher = FeedFetcher(PluginConfig(request_retry_limit=3), tmp_path, lambda **_: Session([response], requests))

    async def validate(url, resolve_dns=True):
        return ValidatedURL(url, "example.org", 443, ("93.184.216.34",))

    monkeypatch.setattr("astrbot_plugin_rss.fetchers.feed_fetcher.validate_public_url", validate)
    monkeypatch.setattr("astrbot_plugin_rss.fetchers.feed_fetcher.time.time", lambda: 1e308)
    try:
        result = await fetcher.fetch(Feed("f", "https://example.org/feed"))
        assert "至少等待 60 秒后重试" in result.error_message
        assert result.retry_after_at == 1e308
        assert response.closed
    finally:
        await fetcher.close()


@pytest.mark.asyncio
async def test_429_diagnostic_includes_only_sanitized_allowlisted_headers(tmp_path, monkeypatch):
    headers = {
        "retry-after": "17",
        "server": "nginx/1.25.3",
        "discourse-rate-limit-error-code": "rss_crawler_rate_limit",
        "cf-ray": "abc123-SJC",
        "set-cookie": "secret-cookie-value",
        "cookie": "secret-request-cookie",
        "authorization": "Bearer secret-token",
        "location": "https://example.org/private?token=secret",
        "x-extra": "not-allowlisted",
    }
    response = Response(429, headers)
    fetcher = FeedFetcher(PluginConfig(request_retry_limit=0), tmp_path, lambda **_: Session([response], []))

    async def validate(url, resolve_dns=True):
        return ValidatedURL(url, "example.org", 443, ("93.184.216.34",))

    monkeypatch.setattr("astrbot_plugin_rss.fetchers.feed_fetcher.validate_public_url", validate)
    try:
        result = await fetcher.fetch(Feed("f", "https://example.org/feed"))
        message = result.error_message
        assert "Retry-After=17" in message
        assert "server=nginx/1.25.3" in message
        assert "discourse-rate-limit-error-code=rss_crawler_rate_limit" in message
        assert "cf-ray=abc123-SJC" in message
        assert all(secret not in message for secret in ("secret-cookie", "secret-token", "private", "not-allowlisted"))
    finally:
        await fetcher.close()


@pytest.mark.asyncio
async def test_429_diagnostic_includes_safe_cloudflare_cache_headers_and_preserves_priority(tmp_path, monkeypatch):
    response = Response(
        429,
        {
            "retry-after": "17",
            "discourse-rate-limit-error-code": "rss_crawler_rate_limit",
            "cf-ray": "abc123-SEA",
            "server": "cloudflare",
            "cf-cache-status": "DYNAMIC",
            "age": "42",
            "content-type": "text/html; charset=UTF-8",
            "cf-mitigated": "challenge",
            "set-cookie": "must-not-appear",
            "location": "https://example.org/private",
        },
    )
    fetcher = FeedFetcher(PluginConfig(request_retry_limit=0), tmp_path, lambda **_: Session([response], []))

    async def validate(url, resolve_dns=True):
        return ValidatedURL(url, "example.org", 443, ("93.184.216.34",))

    monkeypatch.setattr("astrbot_plugin_rss.fetchers.feed_fetcher.validate_public_url", validate)
    try:
        result = await fetcher.fetch(Feed("f", "https://example.org/feed"))
        message = result.error_message
        assert "cf-cache-status=DYNAMIC" in message
        assert "age=42" in message
        assert "content-type=text/html; charset=UTF-8" in message
        assert "cf-mitigated=challenge" in message
        assert "discourse-rate-limit-error-code=rss_crawler_rate_limit" in message
        assert "cf-ray=abc123-SEA" in message
        assert "must-not-appear" not in message and "private" not in message
        assert len(message) <= 768
    finally:
        await fetcher.close()


@pytest.mark.asyncio
async def test_429_diagnostics_fit_limit_without_truncating_priority_headers(tmp_path, monkeypatch):
    response = Response(
        429,
        {
            "retry-after": "17",
            "server": "s" * 96,
            "discourse-rate-limit-error-code": "d" * 96,
            "cf-ray": "r" * 96,
            "cf-cache-status": "c" * 96,
            "age": "9" * 96,
            "content-type": "t" * 96,
            "cf-mitigated": "m" * 96,
        },
    )
    fetcher = FeedFetcher(PluginConfig(request_retry_limit=0), tmp_path, lambda **_: Session([response], []))

    async def validate(url, resolve_dns=True):
        return ValidatedURL(url, "example.org", 443, ("93.184.216.34",))

    monkeypatch.setattr("astrbot_plugin_rss.fetchers.feed_fetcher.validate_public_url", validate)
    try:
        message = (await fetcher.fetch(Feed("f", "https://example.org/feed"))).error_message
        assert len(message) <= 768
        assert f"discourse-rate-limit-error-code={'d' * 96}" in message
        assert f"cf-ray={'r' * 96}" in message
    finally:
        await fetcher.close()


@pytest.mark.asyncio
async def test_429_header_sanitization_replaces_controls_and_bounds_length(tmp_path, monkeypatch):
    response = Response(
        429,
        {
            "retry-after": "17",
            "server": "edge/1.2\r\nSet-Cookie: injected",
            "cf-ray": "é" + "a" * 200,
        },
    )
    fetcher = FeedFetcher(PluginConfig(request_retry_limit=0), tmp_path, lambda **_: Session([response], []))

    async def validate(url, resolve_dns=True):
        return ValidatedURL(url, "example.org", 443, ("93.184.216.34",))

    monkeypatch.setattr("astrbot_plugin_rss.fetchers.feed_fetcher.validate_public_url", validate)
    try:
        result = await fetcher.fetch(Feed("f", "https://example.org/feed"))
        message = result.error_message
        assert "\r" not in message and "\n" not in message
        assert "server=edge/1.2" in message
        assert "Set-Cookie" not in message
        assert "injected" not in message
        cf_ray_value = message.split("cf-ray=", 1)[1].split("；", 1)[0]
        assert len(cf_ray_value) <= 96
        assert cf_ray_value.isascii()
        assert all(32 <= ord(char) <= 126 for char in cf_ray_value)
    finally:
        await fetcher.close()


@pytest.mark.asyncio
async def test_500_then_429_reports_two_attempts_and_stops(tmp_path, monkeypatch):
    first = Response(500, {"content-type": "text/plain"}, [b"private error body"])
    second = Response(429, {"retry-after": "0", "discourse-rate-limit-error-code": "rss_crawler_rate_limit"})
    requests = []
    fetcher = FeedFetcher(PluginConfig(request_retry_limit=3), tmp_path, lambda **_: Session([first, second], requests))

    async def validate(url, resolve_dns=True):
        return ValidatedURL(url, "example.org", 443, ("93.184.216.34",))

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr("astrbot_plugin_rss.fetchers.feed_fetcher.validate_public_url", validate)
    monkeypatch.setattr("astrbot_plugin_rss.fetchers.feed_fetcher.asyncio.sleep", no_sleep)
    try:
        result = await fetcher.fetch(Feed("f", "https://example.org/feed"))
        assert result.status_code == 429
        assert "本轮请求尝试=2" in result.error_message
        assert "rss_crawler_rate_limit" in result.error_message
        assert "private error body" not in result.error_message
        assert len(requests) == 2
        assert first.closed and second.closed
    finally:
        await fetcher.close()


@pytest.mark.asyncio
async def test_5xx_retry_behavior_is_unchanged(tmp_path, monkeypatch):
    replies = [Response(503), Response(200, {"content-type": "application/xml"}, [b"<rss><channel/></rss>"])]
    requests = []
    fetcher = FeedFetcher(PluginConfig(request_retry_limit=1), tmp_path, lambda **_: Session(replies, requests))

    async def validate(url, resolve_dns=True):
        return ValidatedURL(url, "example.org", 443, ("93.184.216.34",))

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr("astrbot_plugin_rss.fetchers.feed_fetcher.validate_public_url", validate)
    monkeypatch.setattr("astrbot_plugin_rss.fetchers.feed_fetcher.asyncio.sleep", no_sleep)
    try:
        result = await fetcher.fetch(Feed("f", "https://example.org/feed"))
        assert result.status_code == 200 and result.error_type is None
        assert len(requests) == 2
    finally:
        await fetcher.close()


def test_browser_escalation_is_allowlisted_challenge_only(tmp_path):
    import asyncio

    from astrbot_plugin_rss.core.models import FetchResult

    fetcher = FeedFetcher(PluginConfig(enable_cloakbrowser=True, browser_allowed_domains=("example.org",)), tmp_path)
    challenge = FetchResult("f", "https://example.org/feed", "https://example.org/feed", 403, "text/html", b"cf-chl-")
    plain_forbidden = FetchResult(
        "f", "https://example.org/feed", "https://example.org/feed", 403, "text/html", b"forbidden"
    )
    limited = FetchResult("f", "https://example.org/feed", "https://example.org/feed", 429, "text/html", b"captcha")
    assert asyncio.run(fetcher._should_browser_fallback(challenge))
    assert not asyncio.run(fetcher._should_browser_fallback(plain_forbidden))
    assert not asyncio.run(fetcher._should_browser_fallback(limited))
