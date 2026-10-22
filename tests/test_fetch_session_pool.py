import asyncio
from urllib.parse import urlsplit

import astrbot_plugin_rss.fetchers.feed_fetcher as feed_fetcher_module
import pytest
from astrbot_plugin_rss.core.config import PluginConfig
from astrbot_plugin_rss.core.models import Feed
from astrbot_plugin_rss.core.security import ValidatedURL
from astrbot_plugin_rss.fetchers.feed_fetcher import FeedFetcher


class Session:
    def __init__(self, **options):
        self.options = options
        self.closed = False
        self.close_count = 0

    async def close(self):
        self.closed = True
        self.close_count += 1


def make_fetcher(tmp_path, limit=2):
    created = []

    def factory(**options):
        session = Session(**options)
        created.append(session)
        return session

    fetcher = FeedFetcher(PluginConfig(max_fetch_concurrency=limit), tmp_path, factory)
    return fetcher, created


def validated(host, ip="93.184.216.34"):
    return ValidatedURL(f"https://{host}/feed", host, 443, (ip,))


@pytest.mark.asyncio
async def test_session_pool_reuses_key_and_preserves_dns_pin_isolation(tmp_path):
    fetcher, created = make_fetcher(tmp_path)
    key, entry, first = await fetcher._lease_session(validated("same.example"))
    await fetcher._release_session(key, entry)
    key_again, entry_again, reused = await fetcher._lease_session(validated("same.example"))
    assert reused is first
    assert len(created) == 1
    await fetcher._release_session(key_again, entry_again)

    pin_key, pin_entry, pinned = await fetcher._lease_session(validated("same.example", "93.184.216.35"))
    try:
        assert pinned is not first
        assert len(created) == 2
        assert created[1].options["curl_options"]
    finally:
        await fetcher._release_session(pin_key, pin_entry)
        await fetcher.close()


@pytest.mark.asyncio
async def test_full_pool_waits_then_evicts_only_lru_idle_session(tmp_path):
    fetcher, created = make_fetcher(tmp_path)
    key_a, entry_a, session_a = await fetcher._lease_session(validated("a.example"))
    key_b, entry_b, session_b = await fetcher._lease_session(validated("b.example"))
    waiting = asyncio.create_task(fetcher._lease_session(validated("c.example")))
    await asyncio.sleep(0)
    assert not waiting.done()
    assert len(created) == 2
    assert not session_a.closed and not session_b.closed

    await fetcher._release_session(key_a, entry_a)
    key_c, entry_c, session_c = await asyncio.wait_for(waiting, timeout=1)
    assert session_a.closed
    assert not session_b.closed
    assert len(fetcher._sessions) == 2
    assert session_c is created[2]
    await fetcher._release_session(key_c, entry_c)
    await fetcher._release_session(key_b, entry_b)
    await fetcher.close()


@pytest.mark.asyncio
async def test_waiting_lease_cancellation_and_close_wake_waiters(tmp_path):
    fetcher, _ = make_fetcher(tmp_path)
    leases = [await fetcher._lease_session(validated(f"{i}.example")) for i in range(2)]
    blocked = asyncio.create_task(fetcher._lease_session(validated("blocked.example")))
    await asyncio.sleep(0)
    blocked.cancel()
    with pytest.raises(asyncio.CancelledError):
        await blocked

    waiter = asyncio.create_task(fetcher._lease_session(validated("closing.example")))
    await asyncio.sleep(0)
    closing = asyncio.create_task(fetcher.close())
    with pytest.raises(RuntimeError, match="closed"):
        await asyncio.wait_for(waiter, timeout=1)
    await asyncio.sleep(0)
    for key, entry, _session in leases:
        await fetcher._release_session(key, entry)
    await asyncio.wait_for(closing, timeout=1)
    assert not fetcher._sessions


@pytest.mark.asyncio
async def test_cancelled_headers_invalidates_session_and_closes_it(tmp_path):
    fetcher, created = make_fetcher(tmp_path, limit=1)
    waiting = asyncio.Event()

    class SlowSession(Session):
        async def request(self, *_args, **_kwargs):
            waiting.set()
            await asyncio.Event().wait()

    fetcher.session_factory = lambda **options: created.append(SlowSession(**options)) or created[-1]
    request = asyncio.create_task(
        fetcher._request_with_lease(validated("slow.example"), proxy_url=None, target_ip=None, impersonate=None)
    )
    await asyncio.wait_for(waiting.wait(), timeout=1)
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    assert created[0].closed
    assert not fetcher._sessions
    await fetcher.close()


@pytest.mark.asyncio
async def test_early_response_close_signals_curl_stream_to_stop():
    from astrbot_plugin_rss.fetchers.feed_fetcher import _LeasedResponse

    stopped = asyncio.Event()
    releases = []

    class StreamingResponse:
        quit_now = stopped

        async def aclose(self):
            assert stopped.is_set()
            await asyncio.sleep(0)

    response = _LeasedResponse(StreamingResponse(), lambda: _append_release(releases))
    await response.aclose()
    assert stopped.is_set()
    assert releases == [True]


async def _append_release(values):
    values.append(True)


@pytest.mark.asyncio
async def test_loopback_requests_keep_real_curl_sessions_bounded(tmp_path, monkeypatch):
    body = b"<rss><channel><title>local pool check</title></channel></rss>"
    writers = set()

    async def serve(reader, writer):
        writers.add(writer)
        try:
            while True:
                await reader.readuntil(b"\r\n\r\n")
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/rss+xml\r\nContent-Length: "
                    + str(len(body)).encode()
                    + b"\r\nConnection: keep-alive\r\n\r\n"
                    + body
                )
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writers.discard(writer)
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    async def local_validate(url, resolve_dns=True):
        host = urlsplit(url).hostname
        return ValidatedURL(url, host, port, ("127.0.0.1",))

    monkeypatch.setattr(feed_fetcher_module, "validate_public_url", local_validate)
    fetcher = FeedFetcher(
        PluginConfig(max_fetch_concurrency=4, request_retry_limit=0, enable_cookie_persistence=False), tmp_path
    )
    fetcher.compatibility.inspect = lambda: {"compatible": False}
    try:
        for index in range(20):
            result = await fetcher.fetch(Feed(str(index), f"http://rss-{index}.test:{port}/feed"))
            assert result.error_type is None
            assert len(fetcher._sessions) <= 4
            assert len(writers) <= 4
    finally:
        await fetcher.close()
        server.close()
        await server.wait_closed()
        await asyncio.sleep(0)
    assert not fetcher._sessions
    assert not writers


@pytest.mark.asyncio
async def test_cancelled_real_curl_headers_close_connection_and_session(tmp_path):
    received = asyncio.Event()
    disconnected = asyncio.Event()

    async def slow_headers(reader, writer):
        try:
            await reader.readuntil(b"\r\n\r\n")
            received.set()
            await reader.read()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            disconnected.set()
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(slow_headers, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    fetcher = FeedFetcher(PluginConfig(max_fetch_concurrency=1), tmp_path)
    validated_url = ValidatedURL(f"http://slow-header.test:{port}/feed", "slow-header.test", port, ("127.0.0.1",))
    request = asyncio.create_task(
        fetcher._request_with_lease(
            validated_url,
            proxy_url=None,
            target_ip=None,
            impersonate=None,
            headers={},
            allow_redirects=False,
            timeout=10,
            stream=True,
            max_redirects=0,
            discard_cookies=True,
        )
    )
    try:
        await asyncio.wait_for(received.wait(), timeout=2)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(request, timeout=2)
        await asyncio.wait_for(disconnected.wait(), timeout=2)
        assert not fetcher._sessions
    finally:
        await fetcher.close()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_early_close_of_real_curl_stream_stops_large_body(tmp_path, monkeypatch):
    started = asyncio.Event()
    disconnected = asyncio.Event()

    async def slow_body(reader, writer):
        try:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/rss+xml\r\n"
                b"Content-Length: 100000000\r\n\r\n" + b"x" * 1024
            )
            await writer.drain()
            started.set()
            await reader.read()
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        finally:
            disconnected.set()
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(slow_body, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    async def local_validate(url, resolve_dns=True):
        host = urlsplit(url).hostname
        return ValidatedURL(url, host, port, ("127.0.0.1",))

    monkeypatch.setattr(feed_fetcher_module, "validate_public_url", local_validate)
    fetcher = FeedFetcher(
        PluginConfig(
            max_fetch_concurrency=1,
            max_feed_response_bytes=64,
            request_timeout=10,
            request_retry_limit=0,
            enable_cookie_persistence=False,
        ),
        tmp_path,
    )
    try:
        fetcher.compatibility.inspect = lambda: {"compatible": False}
        started_at = asyncio.get_running_loop().time()
        result = await asyncio.wait_for(
            fetcher.fetch(Feed("stalled-large-body", f"http://slow-body.test:{port}/feed")), timeout=2
        )
        await asyncio.wait_for(started.wait(), timeout=2)
        await asyncio.wait_for(disconnected.wait(), timeout=2)
        assert result.error_type == "response_too_large"
        assert asyncio.get_running_loop().time() - started_at < 1
    finally:
        await fetcher.close()
        server.close()
        await server.wait_closed()
