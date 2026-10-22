from __future__ import annotations

import asyncio
import http.server
import socket
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1].parent))

from astrbot_plugin_rss.core.workers import BoundedThreadWorkers
from astrbot_plugin_rss.media.downloader import MediaDownloader, _PinnedHTTPSConnection
from astrbot_plugin_rss.renderers import telegram as telegram_module

_PNG = b"\x89PNG\r\n\x1a\nimage-data"


def _mock_media_request(monkeypatch: pytest.MonkeyPatch, downloader: MediaDownloader, get):
    import astrbot_plugin_rss.media.downloader as module
    from astrbot_plugin_rss.core.security import ValidatedURL

    async def validate(url: str) -> ValidatedURL:
        return ValidatedURL(url, "public.example", 443, ("93.184.216.34",))

    monkeypatch.setattr(module, "validate_public_url", validate)
    monkeypatch.setattr(downloader, "_get", get)


async def _wait_thread_event(event: threading.Event) -> None:
    assert await asyncio.wait_for(asyncio.to_thread(event.wait, 2), timeout=3)


@pytest.mark.asyncio
async def test_identical_concurrent_media_downloads_share_one_get(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    downloader = MediaDownloader(tmp_path)
    started = threading.Event()
    release = threading.Event()
    get_count = 0
    guard = threading.Lock()

    def fake_get(_validated, _limit):
        nonlocal get_count
        with guard:
            get_count += 1
        started.set()
        assert release.wait(3)
        return 200, {"content-type": "image/png"}, _PNG

    _mock_media_request(monkeypatch, downloader, fake_get)
    callers = [
        asyncio.create_task(downloader.download("https://public.example/shared.png", "image", 1024)) for _ in range(3)
    ]
    try:
        await _wait_thread_event(started)
        await asyncio.sleep(0.05)
        assert get_count == 1
        release.set()
        results = await asyncio.gather(*callers)
        assert len({result.path for result in results}) == 1
        assert get_count == 1
    finally:
        release.set()
        await asyncio.gather(*callers, return_exceptions=True)
        await downloader.close()


@pytest.mark.asyncio
async def test_different_limits_use_independent_media_requests(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    downloader = MediaDownloader(tmp_path)
    barrier = threading.Barrier(2, timeout=3)
    limits: list[int] = []
    guard = threading.Lock()

    def fake_get(_validated, limit):
        with guard:
            limits.append(limit)
        barrier.wait()
        return 200, {"content-type": "image/png"}, _PNG

    _mock_media_request(monkeypatch, downloader, fake_get)
    try:
        await asyncio.gather(
            downloader.download("https://public.example/limits.png", "image", 1024),
            downloader.download("https://public.example/limits.png", "image", 2048),
        )
        assert sorted(limits) == [1024, 2048]
    finally:
        await downloader.close()


@pytest.mark.asyncio
async def test_cancelling_one_media_waiter_does_not_cancel_shared_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    downloader = MediaDownloader(tmp_path)
    started = threading.Event()
    release = threading.Event()
    get_count = 0

    def fake_get(_validated, _limit):
        nonlocal get_count
        get_count += 1
        started.set()
        assert release.wait(3)
        return 200, {"content-type": "image/png"}, _PNG

    _mock_media_request(monkeypatch, downloader, fake_get)
    first = asyncio.create_task(downloader.download("https://public.example/cancel.png", "image", 1024))
    second = asyncio.create_task(downloader.download("https://public.example/cancel.png", "image", 1024))
    try:
        await _wait_thread_event(started)
        await asyncio.sleep(0.05)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert not second.done()
        release.set()
        result = await second
        assert Path(result.path).read_bytes() == _PNG
        assert get_count == 1
    finally:
        release.set()
        await asyncio.gather(first, second, return_exceptions=True)
        await downloader.close()


@pytest.mark.asyncio
async def test_failed_shared_download_can_be_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    downloader = MediaDownloader(tmp_path)
    started = threading.Event()
    release = threading.Event()
    get_count = 0

    def fake_get(_validated, _limit):
        nonlocal get_count
        get_count += 1
        if get_count == 1:
            started.set()
            assert release.wait(3)
            return 503, {}, b""
        return 200, {"content-type": "image/png"}, _PNG

    _mock_media_request(monkeypatch, downloader, fake_get)
    callers = [
        asyncio.create_task(downloader.download("https://public.example/retry.png", "image", 1024)) for _ in range(2)
    ]
    try:
        await _wait_thread_event(started)
        await asyncio.sleep(0.05)
        release.set()
        results = await asyncio.gather(*callers, return_exceptions=True)
        assert all(isinstance(result, ValueError) and "HTTP 503" in str(result) for result in results)
        result = await downloader.download("https://public.example/retry.png", "image", 1024)
        assert Path(result.path).read_bytes() == _PNG
        assert get_count == 2
    finally:
        release.set()
        await asyncio.gather(*callers, return_exceptions=True)
        await downloader.close()


@pytest.mark.asyncio
async def test_close_rejects_new_media_work_and_drains_shared_get(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    downloader = MediaDownloader(tmp_path)
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def fake_get(_validated, _limit):
        started.set()
        assert release.wait(3)
        finished.set()
        return 200, {"content-type": "image/png"}, _PNG

    _mock_media_request(monkeypatch, downloader, fake_get)
    caller = asyncio.create_task(downloader.download("https://public.example/close.png", "image", 1024))
    close_task: asyncio.Task[None] | None = None
    try:
        await _wait_thread_event(started)
        close_task = asyncio.create_task(downloader.close())
        await asyncio.sleep(0.05)
        assert not close_task.done()
        with pytest.raises(RuntimeError, match="closed"):
            await downloader.download("https://public.example/new.png", "image", 1024)
        release.set()
        await asyncio.wait_for(close_task, timeout=3)
        result = await caller
        assert Path(result.path).read_bytes() == _PNG
        assert finished.is_set()
        assert not downloader._workers._calls
        assert not downloader._workers._workers
    finally:
        release.set()
        if close_task is not None:
            await asyncio.gather(close_task, return_exceptions=True)
        await asyncio.gather(caller, return_exceptions=True)
        await downloader.close()


@pytest.mark.asyncio
async def test_cancelled_close_still_drains_shared_get_before_propagating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    downloader = MediaDownloader(tmp_path)
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def fake_get(_validated, _limit):
        started.set()
        assert release.wait(3)
        finished.set()
        return 200, {"content-type": "image/png"}, _PNG

    _mock_media_request(monkeypatch, downloader, fake_get)
    caller = asyncio.create_task(downloader.download("https://public.example/cancel-close.png", "image", 1024))
    close_task: asyncio.Task[None] | None = None
    try:
        await _wait_thread_event(started)
        close_task = asyncio.create_task(downloader.close())
        await asyncio.sleep(0.05)
        close_task.cancel()
        await asyncio.sleep(0.05)
        assert not close_task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(close_task, timeout=3)
        result = await caller
        assert Path(result.path).read_bytes() == _PNG
        assert finished.is_set()
        assert not downloader._workers._calls
        assert not downloader._workers._workers
    finally:
        release.set()
        if close_task is not None:
            await asyncio.gather(close_task, return_exceptions=True)
        await asyncio.gather(caller, return_exceptions=True)
        await downloader.close()


class _LocalHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Length", "1000")
        self.send_header("Connection", "close")
        self.end_headers()

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.send_response(503)
        self.send_header("Content-Length", "3")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(b"bad")

    def log_message(self, *_args) -> None:
        pass


def _serve_local() -> tuple[http.server.ThreadingHTTPServer, threading.Thread]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _LocalHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def test_downloader_closes_detached_response_on_early_size_error(tmp_path: Path) -> None:
    server, thread = _serve_local()
    try:
        host, port = server.server_address
        downloader = MediaDownloader(tmp_path)
        validated = SimpleNamespace(
            url=f"http://{host}:{port}/oversized",
            hostname=host,
            port=port,
            pinned_ips=(host,),
        )
        with pytest.raises(ValueError, match="exceeds 100 byte limit") as raised:
            downloader._get(validated, 100)

        traceback = raised.value.__traceback__
        while traceback and traceback.tb_frame.f_code.co_name != "_get":
            traceback = traceback.tb_next
        assert traceback is not None
        response = traceback.tb_frame.f_locals["response"]
        assert response.fp is None
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_telegraph_http_error_response_is_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    server, thread = _serve_local()
    try:
        host, port = server.server_address
        monkeypatch.setattr(telegram_module, "TELEGRAPH_CREATE", f"http://{host}:{port}/create")
        with pytest.raises(HTTPError) as raised:
            telegram_module._post_json(b"{}")
        assert raised.value.code == 503
        assert raised.value.fp is not None and raised.value.fp.closed
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_https_wrap_failure_closes_raw_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    class RawSocket:
        closed = False

        def close(self) -> None:
            self.closed = True

    class Context:
        def wrap_socket(self, _raw, server_hostname):
            raise ValueError(f"invalid test hostname: {server_hostname}")

    raw = RawSocket()
    monkeypatch.setattr(socket, "create_connection", lambda *_args, **_kwargs: raw)
    monkeypatch.setattr("ssl.create_default_context", lambda: Context())
    connection = _PinnedHTTPSConnection("example.test", 443, "127.0.0.1", 1)
    with pytest.raises(ValueError, match="invalid test hostname"):
        connection.connect()
    assert raw.closed


@pytest.mark.asyncio
async def test_cancelled_thread_work_stays_bounded_and_close_drains() -> None:
    workers = BoundedThreadWorkers(concurrency=1)
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    queued_started = threading.Event()

    def blocked_work() -> None:
        started.set()
        release.wait(3)
        finished.set()

    def queued_work() -> None:
        queued_started.set()

    first = asyncio.create_task(workers.run(blocked_work))
    close_task: asyncio.Task[None] | None = None
    try:
        assert await asyncio.wait_for(asyncio.to_thread(started.wait, 2), timeout=3)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first

        queued = asyncio.create_task(workers.run(queued_work))
        await asyncio.sleep(0.02)
        close_task = asyncio.create_task(workers.close())
        await asyncio.sleep(0.02)
        assert not close_task.done()
        assert not queued_started.is_set()
        with pytest.raises(RuntimeError, match="closed"):
            await workers.run(queued_work)

        close_task.cancel()
        await asyncio.sleep(0.02)
        assert not close_task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(close_task, timeout=3)
        queued_result = await asyncio.gather(queued, return_exceptions=True)
        assert isinstance(queued_result[0], RuntimeError)
        assert finished.is_set()
        assert not queued_started.is_set()
        assert not workers._calls
        assert not workers._workers
    finally:
        release.set()
        if close_task is not None:
            await asyncio.gather(close_task, return_exceptions=True)
        await asyncio.gather(workers.close(), return_exceptions=True)
        if not first.done():
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelled_queued_call_never_starts_after_slot_frees() -> None:
    workers = BoundedThreadWorkers(concurrency=1)
    first_started = threading.Event()
    release = threading.Event()
    second_started = threading.Event()

    def first_work() -> None:
        first_started.set()
        release.wait(3)

    def second_work() -> None:
        second_started.set()

    first = asyncio.create_task(workers.run(first_work))
    second: asyncio.Task[object] | None = None
    try:
        assert await asyncio.wait_for(asyncio.to_thread(first_started.wait, 2), timeout=3)
        second = asyncio.create_task(workers.run(second_work))
        await asyncio.sleep(0.02)
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second

        release.set()
        await asyncio.wait_for(first, timeout=3)
        await workers.close()
        assert not second_started.is_set()
        assert not workers._calls
        assert not workers._workers
    finally:
        release.set()
        await asyncio.gather(first, return_exceptions=True)
        if second is not None:
            await asyncio.gather(second, return_exceptions=True)
        await asyncio.gather(workers.close(), return_exceptions=True)
