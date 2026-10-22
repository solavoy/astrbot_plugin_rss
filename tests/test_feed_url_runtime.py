from __future__ import annotations

from urllib.parse import urlsplit

import pytest
from astrbot_plugin_rss.core.config import PluginConfig
from astrbot_plugin_rss.core.feed_urls import build_feed_request
from astrbot_plugin_rss.core.models import Feed
from astrbot_plugin_rss.core.runtime import RSSRuntime
from astrbot_plugin_rss.core.security import ValidatedURL
from astrbot_plugin_rss.fetchers.feed_fetcher import FeedFetcher
from astrbot_plugin_rss.parsers.feed_parser import FeedParser
from astrbot_plugin_rss.scheduler import FeedScheduler
from astrbot_plugin_rss.storage import Database, Repository

_RSS = (
    b'<rss version="2.0"><channel><title>RSSHub</title><link>https://rsshub.example.org/</link>'
    b"<description>Fixture feed</description><item><guid>one</guid><title>Article</title>"
    b"<link>../post/one</link><description>Body</description></item></channel></rss>"
)


class _Response:
    def __init__(self, status=200, headers=None, body=_RSS):
        self.status_code = status
        self.headers = headers or {"content-type": "application/rss+xml", "etag": '"v1"'}
        self.body = body
        self.closed = False

    async def aiter_content(self, chunk_size=None):
        yield self.body

    async def aclose(self):
        self.closed = True


class _Session:
    def __init__(self, replies, requests, sessions):
        self.replies = replies
        self.requests = requests
        self.sessions = sessions
        self.closed = False

    async def request(self, *args, **kwargs):
        self.requests.append((args, kwargs))
        return self.replies.pop(0)

    async def close(self):
        self.closed = True
        self.sessions.append(self)


def _stub_validation(monkeypatch):
    async def validate(url, resolve_dns=True):
        parsed = urlsplit(url)
        return ValidatedURL(url, parsed.hostname or "rsshub.example.org", parsed.port or 443, ("93.184.216.34",))

    monkeypatch.setattr("astrbot_plugin_rss.fetchers.feed_fetcher.validate_public_url", validate)


@pytest.mark.asyncio
async def test_scheduler_rebuilds_relative_url_and_invalidates_stale_validators(tmp_path, monkeypatch):
    _stub_validation(monkeypatch)
    replies = [
        _Response(
            headers={
                "content-type": "application/rss+xml",
                "etag": '"v1"',
                "last-modified": "Mon, 01 Jan 2024 00:00:00 GMT",
            }
        ),
        _Response(
            headers={
                "content-type": "application/rss+xml",
                "etag": '"v2"',
                "last-modified": "Tue, 02 Jan 2024 00:00:00 GMT",
            }
        ),
        _Response(304, {"etag": '"v2"'}),
    ]
    response_objects = list(replies)
    requests, closed_sessions = [], []
    config = PluginConfig(
        default_url_prefix="https://rsshub-a.example.org",
        rsshub_access_key="key-a",
        global_poll_interval=5,
        min_poll_interval=1,
    )
    fetcher = FeedFetcher(
        config,
        tmp_path / "fetch",
        lambda **_: _Session(replies, requests, closed_sessions),
    )
    runtime = RSSRuntime(config, tmp_path / "runtime", fetcher=fetcher, parser=FeedParser())
    await runtime.initialize()
    try:
        feed = await runtime.repository.create_feed("/twitter/user/_RSSHub?format=atom")
        target = await runtime.repository.create_target("instance", "target", "test", "target")
        await runtime.repository.add_direct_subscription(target.target_id, feed.feed_id, interval=5)
        await runtime.repository.update_fetch_state(
            feed.feed_id,
            etag='"old"',
            last_modified="Sun, 31 Dec 2023 00:00:00 GMT",
            initialized=True,
            next_fetch_at=0,
        )
        await runtime.repository.merge_feed_metadata(feed.feed_id, {"request_url_fingerprint": "stale", "keep": "yes"})

        first = await FeedScheduler(runtime).run_once()
        assert first["fetched"] == 1
        assert requests[0][0][1] == "https://rsshub-a.example.org/twitter/user/_RSSHub?format=atom&key=key-a"
        assert "If-None-Match" not in requests[0][1]["headers"]
        assert "If-Modified-Since" not in requests[0][1]["headers"]
        saved = await runtime.repository.get_feed(feed.feed_id)
        assert saved.url == "/twitter/user/_RSSHub?format=atom"
        fingerprint_a = saved.metadata["request_url_fingerprint"]
        assert fingerprint_a == build_feed_request(feed.url, config).fingerprint
        assert saved.metadata["keep"] == "yes"
        articles = await runtime.list_feed_articles(feed.feed_id, limit=5)
        assert articles[0].canonical_url == "https://rsshub-a.example.org/twitter/post/one"

        config.default_url_prefix = "https://rsshub-b.example.org/base"
        config.rsshub_access_key = "key-b"
        async with runtime.database.transaction() as conn:
            await conn.execute("UPDATE feeds SET next_fetch_at=0 WHERE feed_id=?", (feed.feed_id,))
        second = await FeedScheduler(runtime).run_once()
        assert second["fetched"] == 1
        assert requests[1][0][1] == "https://rsshub-b.example.org/base/twitter/user/_RSSHub?format=atom&key=key-b"
        assert "If-None-Match" not in requests[1][1]["headers"]
        assert "If-Modified-Since" not in requests[1][1]["headers"]
        fingerprint_b = (await runtime.repository.get_feed(feed.feed_id)).metadata["request_url_fingerprint"]
        assert fingerprint_b == build_feed_request(feed.url, config).fingerprint
        assert fingerprint_a != fingerprint_b

        async with runtime.database.transaction() as conn:
            await conn.execute("UPDATE feeds SET next_fetch_at=0 WHERE feed_id=?", (feed.feed_id,))
        third = await FeedScheduler(runtime).run_once()
        assert third["fetched"] == 1
        assert requests[2][1]["headers"]["If-None-Match"] == '"v2"'
        assert requests[2][1]["headers"]["If-Modified-Since"] == "Tue, 02 Jan 2024 00:00:00 GMT"
        assert (await runtime.repository.get_feed(feed.feed_id)).metadata["request_url_fingerprint"] == fingerprint_b
    finally:
        await runtime.close()
    assert all(reply.closed for reply in response_objects)
    assert closed_sessions and all(session.closed for session in closed_sessions)


@pytest.mark.asyncio
async def test_full_url_does_not_receive_rsshub_key_or_fingerprint(tmp_path, monkeypatch):
    _stub_validation(monkeypatch)
    requests, closed_sessions = [], []
    fetcher = FeedFetcher(
        PluginConfig(default_url_prefix="https://rsshub.example.org", rsshub_access_key="secret"),
        tmp_path,
        lambda **_: _Session([_Response()], requests, closed_sessions),
    )
    try:
        result = await fetcher.fetch(Feed("f", "https://feeds.example.org/feed.xml"))
        assert result.error_type is None
        assert requests[0][0][1] == "https://feeds.example.org/feed.xml"
        assert result.requested_url == "https://feeds.example.org/feed.xml"
        assert result.final_url == "https://feeds.example.org/feed.xml"
        assert result.request_url_fingerprint is None
    finally:
        await fetcher.close()
    assert closed_sessions and all(session.closed for session in closed_sessions)


@pytest.mark.asyncio
async def test_redirect_key_stays_in_scope_and_is_redacted_from_results(tmp_path, monkeypatch):
    _stub_validation(monkeypatch)
    requests, closed_sessions = [], []
    fetcher = FeedFetcher(
        PluginConfig(
            default_url_prefix="https://rsshub.example.org/rsshub",
            rsshub_access_key="private-key",
            request_retry_limit=0,
        ),
        tmp_path,
        lambda **_: _Session(
            [
                _Response(302, {"location": "/rsshub/redirect"}, b""),
                _Response(),
            ],
            requests,
            closed_sessions,
        ),
    )
    try:
        result = await fetcher.fetch(Feed("f", "/rsshub/twitter/user"))
        assert result.error_type is None
        assert requests[0][0][1].endswith("/rsshub/twitter/user?key=private-key")
        assert requests[1][0][1] == "https://rsshub.example.org/rsshub/redirect?key=private-key"
        assert "private-key" not in result.requested_url
        assert "private-key" not in result.final_url
        assert result.request_url_fingerprint
    finally:
        await fetcher.close()
    assert closed_sessions and all(session.closed for session in closed_sessions)


@pytest.mark.asyncio
async def test_external_redirect_never_receives_or_reports_automatic_key(tmp_path, monkeypatch):
    _stub_validation(monkeypatch)
    requests, closed_sessions = [], []
    fetcher = FeedFetcher(
        PluginConfig(
            default_url_prefix="https://rsshub.example.org/rsshub",
            rsshub_access_key="secret-key",
            request_retry_limit=0,
        ),
        tmp_path,
        lambda **_: _Session(
            [
                _Response(302, {"location": "https://other.example.org/feed?key=secret-key"}, b""),
                _Response(),
            ],
            requests,
            closed_sessions,
        ),
    )
    try:
        result = await fetcher.fetch(Feed("f", "/rsshub/twitter/user"))
        assert result.error_type is None
        assert requests[1][0][1] == "https://other.example.org/feed"
        assert "secret-key" not in result.requested_url
        assert "secret-key" not in result.final_url
        assert "secret-key" not in (result.error_message or "")
    finally:
        await fetcher.close()
    assert closed_sessions and all(session.closed for session in closed_sessions)


@pytest.mark.asyncio
async def test_initialized_feed_persists_only_relative_url_and_opaque_metadata(tmp_path):
    database = Database(tmp_path / "initialized.sqlite3")
    await database.initialize()
    repository = Repository(database)
    try:
        feed = await repository.create_initialized_feed(
            "feed-id",
            "/twitter/user/_RSSHub",
            "RSSHub",
            "",
            None,
            '"etag"',
            None,
            [],
            123.0,
            metadata={"request_url_fingerprint": "opaque-digest", "other": "kept"},
        )
        assert feed.url == "/twitter/user/_RSSHub"
        assert feed.metadata == {"request_url_fingerprint": "opaque-digest", "other": "kept"}
        stored = await database.fetchone("SELECT url,metadata FROM feeds WHERE feed_id=?", (feed.feed_id,))
        assert stored["url"] == "/twitter/user/_RSSHub"
        assert "secret" not in stored["metadata"]
    finally:
        await database.close()
