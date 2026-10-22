import asyncio
import ssl

import pytest
from astrbot_plugin_rss.core.config import PluginConfig
from astrbot_plugin_rss.core.models import Feed
from astrbot_plugin_rss.core.security import ValidatedURL
from astrbot_plugin_rss.fetchers.feed_fetcher import FeedFetcher
from curl_cffi.requests import AsyncSession

_CERT = """-----BEGIN CERTIFICATE-----
MIIBSzCB8aADAgECAhQD07wic+LlX53p+2sd4c8IYYTg1TAKBggqhkjOPQQDAjAX
MRUwEwYDVQQDDAxmZWVkLmV4YW1wbGUwHhcNMjYxMDIwMDI0MzM5WhcNMzYxMDE4
MDI0MzM5WjAXMRUwEwYDVQQDDAxmZWVkLmV4YW1wbGUwWTATBgcqhkjOPQIBBggq
hkjOPQMBBwNCAAQjiYIoqUsgG3JeSyflg3PuclcBmovnbKyOZXPe94TenK9sY0HQ
88ULi0YDPDWEyroHwQx0QjOzbMw05m88McrXoxswGTAXBgNVHREEEDAOggxmZWVk
LmV4YW1wbGUwCgYIKoZIzj0EAwIDSQAwRgIhAJ6z/lmxRkLMeTmnELlVr2hVIYv4
CAQ/TZnQ/EKI5BjQAiEA9ZX5ssN8cdDygggKFBn/9zi0olW/eTH+j8wlu9TQ8jY=
-----END CERTIFICATE-----
"""
_PRIVATE_KEY = """-----BEGIN PRIVATE KEY-----
MIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQgAUi5Mnmrr2NeEqqZ
5XYwrNNON7uooacRg3Ks6Ff+EaGhRANCAAQjiYIoqUsgG3JeSyflg3PuclcBmovn
bKyOZXPe94TenK9sY0HQ88ULi0YDPDWEyroHwQx0QjOzbMw05m88McrX
-----END PRIVATE KEY-----
"""


@pytest.mark.asyncio
async def test_explicit_proxy_connect_to_pin_and_session_reuse(tmp_path, monkeypatch):
    cert_file = tmp_path / "feed-cert.pem"
    key_file = tmp_path / "feed-key.pem"
    cert_file.write_text(_CERT, encoding="ascii")
    key_file.write_text(_PRIVATE_KEY, encoding="ascii")

    tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls_context.load_cert_chain(cert_file, key_file)
    feed_requests = []
    proxy_connects = []
    proxy_tasks = set()

    async def feed_handler(reader, writer):
        try:
            request = await reader.readuntil(b"\r\n\r\n")
            feed_requests.append(request)
            body = b"<rss><channel><title>Loopback</title></channel></rss>"
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/rss+xml\r\n"
                + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode("ascii")
                + body
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async def relay(reader, writer):
        try:
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass

    async def proxy_handler(reader, writer):
        upstream_writer = None
        try:
            header = await reader.readuntil(b"\r\n\r\n")
            request_line = header.split(b"\r\n", 1)[0].decode("ascii")
            proxy_connects.append(request_line)
            method, authority, _protocol = request_line.split(" ", 2)
            assert method == "CONNECT"
            upstream_reader, upstream_writer = await asyncio.open_connection("127.0.0.1", tls_port)
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()
            forward = asyncio.create_task(relay(reader, upstream_writer))
            backward = asyncio.create_task(relay(upstream_reader, writer))
            done, pending = await asyncio.wait({forward, backward}, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*done, *pending, return_exceptions=True)
            assert authority.endswith(f":{tls_port}")
        finally:
            if upstream_writer is not None:
                upstream_writer.close()
                await upstream_writer.wait_closed()
            writer.close()
            await writer.wait_closed()

    def track_proxy(reader, writer):
        task = asyncio.create_task(proxy_handler(reader, writer))
        proxy_tasks.add(task)
        task.add_done_callback(proxy_tasks.discard)

    tls_server = await asyncio.start_server(feed_handler, "127.0.0.1", 0, ssl=tls_context)
    tls_port = tls_server.sockets[0].getsockname()[1]
    proxy_server = await asyncio.start_server(track_proxy, "127.0.0.1", 0)
    proxy_port = proxy_server.sockets[0].getsockname()[1]

    sessions = []

    def session_factory(**options):
        # Trust only the local fixture certificate; retain hostname verification.
        options["verify"] = str(cert_file)
        session = AsyncSession(**options)
        sessions.append(session)
        return session

    feed = Feed("f", f"https://feed.example:{tls_port}/rss")

    async def validate(url, resolve_dns=True):
        return ValidatedURL(url, "feed.example", tls_port, ("127.0.0.1",))

    monkeypatch.setattr("astrbot_plugin_rss.fetchers.feed_fetcher.validate_public_url", validate)
    fetcher = FeedFetcher(
        PluginConfig(
            proxy_profile=f"http://127.0.0.1:{proxy_port}",
            enable_cookie_persistence=False,
            request_retry_limit=0,
            request_timeout=3,
        ),
        tmp_path,
        session_factory,
    )
    try:
        first = await fetcher.fetch(feed)
        assert first.status_code == 200 and first.error_type is None
        assert first.raw_xml.startswith(b"<rss>")
        assert len(sessions) == 1

        first_session = sessions[0]
        first_key = next(iter(fetcher._sessions))
        second = await fetcher.fetch(feed)
        assert second.status_code == 200 and second.error_type is None
        assert len(sessions) == 1
        assert fetcher._sessions[first_key]["session"] is first_session
        assert len(proxy_connects) == 2
        assert all(line.startswith("CONNECT 127.0.0.1:") for line in proxy_connects)
        assert len(feed_requests) == 2
    finally:
        await fetcher.close()
        proxy_server.close()
        tls_server.close()
        await proxy_server.wait_closed()
        await tls_server.wait_closed()
        if proxy_tasks:
            await asyncio.gather(*list(proxy_tasks), return_exceptions=True)
            await asyncio.sleep(0)
    assert not fetcher._sessions
    assert first_session._closed
    assert not proxy_tasks


@pytest.mark.asyncio
async def test_proxy_environment_fallback_override_and_no_proxy_are_loopback_only(tmp_path, monkeypatch):
    # Set synthetic values in this test process only; never inspect or print the host's proxy values.
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
    ):
        monkeypatch.delenv(name, raising=False)

    hits = {"ambient": [], "explicit": [], "target": []}

    def response(label):
        body = f"<rss><channel><title>{label}</title></channel></rss>".encode()
        return (
            b"HTTP/1.1 200 OK\r\nContent-Type: application/rss+xml\r\n"
            + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
            + body
        )

    async def relay(reader, writer):
        try:
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass

    async def target_handler(reader, writer):
        try:
            request = await reader.readuntil(b"\r\n\r\n")
            hits["target"].append(request.split(b"\r\n", 1)[0].decode("latin1"))
            writer.write(response("target"))
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    target_server = await asyncio.start_server(target_handler, "127.0.0.1", 0)
    target_port = target_server.sockets[0].getsockname()[1]

    async def proxy_handler(label, reader, writer):
        upstream_writer = None
        relay_tasks = []
        try:
            request = await reader.readuntil(b"\r\n\r\n")
            line = request.split(b"\r\n", 1)[0].decode("latin1")
            hits[label].append(line)
            if line.startswith("CONNECT "):
                authority = line.split()[1]
                host, port = authority.rsplit(":", 1)
                upstream_reader, upstream_writer = await asyncio.open_connection(host.strip("[]"), int(port))
                writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                await writer.drain()
                relay_tasks = [
                    asyncio.create_task(relay(reader, upstream_writer)),
                    asyncio.create_task(relay(upstream_reader, writer)),
                ]
                done, pending = await asyncio.wait(relay_tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*done, *pending, return_exceptions=True)
            else:
                writer.write(response(label))
                await writer.drain()
        finally:
            for task in relay_tasks:
                if not task.done():
                    task.cancel()
            if relay_tasks:
                await asyncio.gather(*relay_tasks, return_exceptions=True)
            if upstream_writer is not None:
                upstream_writer.close()
                await upstream_writer.wait_closed()
            writer.close()
            await writer.wait_closed()

    ambient_server = await asyncio.start_server(
        lambda reader, writer: proxy_handler("ambient", reader, writer), "127.0.0.1", 0
    )
    explicit_server = await asyncio.start_server(
        lambda reader, writer: proxy_handler("explicit", reader, writer), "127.0.0.1", 0
    )
    ambient_port = ambient_server.sockets[0].getsockname()[1]
    explicit_port = explicit_server.sockets[0].getsockname()[1]
    synthetic_proxy = f"http://127.0.0.1:{ambient_port}"
    monkeypatch.setenv("http_proxy", synthetic_proxy)
    monkeypatch.setenv("HTTP_PROXY", synthetic_proxy)
    monkeypatch.setenv("https_proxy", synthetic_proxy)
    monkeypatch.setenv("HTTPS_PROXY", synthetic_proxy)

    async def run(label, *, proxy_profile="", no_proxy=""):
        monkeypatch.setenv("no_proxy", no_proxy)
        monkeypatch.setenv("NO_PROXY", no_proxy)
        fetcher = FeedFetcher(
            PluginConfig(
                proxy_profile=proxy_profile,
                enable_cookie_persistence=False,
                request_retry_limit=0,
                request_timeout=3,
            ),
            tmp_path / label,
        )

        async def validate(url, resolve_dns=True):
            return ValidatedURL(url, "feed.test", target_port, ("127.0.0.1",))

        monkeypatch.setattr("astrbot_plugin_rss.fetchers.feed_fetcher.validate_public_url", validate)
        try:
            return await fetcher.fetch(Feed(label, f"http://feed.test:{target_port}/rss"))
        finally:
            await fetcher.close()

    try:
        ambient = await run("ambient")
        explicit = await run("explicit", proxy_profile=f"http://127.0.0.1:{explicit_port}")
        bypass = await run("bypass", proxy_profile=f"http://127.0.0.1:{explicit_port}", no_proxy="feed.test")

        assert [ambient.status_code, explicit.status_code, bypass.status_code] == [200, 200, 200]
        assert len(hits["ambient"]) == 1
        assert len(hits["explicit"]) == 1 and hits["explicit"][0].startswith("CONNECT 127.0.0.1:")
        assert len(hits["target"]) == 2
    finally:
        for server in (ambient_server, explicit_server, target_server):
            server.close()
        await asyncio.gather(*(server.wait_closed() for server in (ambient_server, explicit_server, target_server)))
