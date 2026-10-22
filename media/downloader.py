from __future__ import annotations

import asyncio
import hashlib
import http.client
import json
import os
import socket
import ssl
import tempfile
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from ..core.security import validate_public_url
from ..core.workers import BoundedThreadWorkers


@dataclass(slots=True)
class DownloadedMedia:
    path: str
    mime_type: str
    size: int
    sha256: str


def _magic_mime(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    if len(data) > 12 and data[4:8] == b"ftyp":
        return "video/mp4"
    if data.startswith(b"ID3") or data[:2] in {b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"}:
        return "audio/mpeg"
    if data.startswith(b"OggS"):
        return "application/ogg"
    return None


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, ip: str, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout)
        self._ip = ip

    def connect(self) -> None:
        self.sock = socket.create_connection((self._ip, self.port), self.timeout)


class _PinnedHTTPSConnection(_PinnedHTTPConnection):
    def connect(self) -> None:
        context = ssl.create_default_context()
        raw = socket.create_connection((self._ip, self.port), self.timeout)
        try:
            self.sock = context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            with suppress(OSError):
                raw.close()
            raise


class MediaDownloader:
    """SSRF-safe bounded downloader; DNS results are pinned into the TCP connection."""

    def __init__(self, data_dir: str | os.PathLike[str], config: object | None = None) -> None:
        self.data_dir = Path(data_dir)
        self.cache_dir = self.data_dir / "media-cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.timeout = float(getattr(config, "media_download_timeout", 30.0))
        self.cache_ttl = int(getattr(config, "media_cache_ttl", 86400))
        self._workers = BoundedThreadWorkers(getattr(config, "max_fetch_concurrency", 5))
        self._inflight_lock = asyncio.Lock()
        self._inflight: dict[tuple[str, str, int], asyncio.Task[DownloadedMedia]] = {}
        self._closed = False

    async def download(self, url: str, kind: str = "image", max_bytes: int | None = None) -> DownloadedMedia:
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError("media download requires a positive byte limit")
        limit = max_bytes
        key = hashlib.sha256(f"{kind}:{url}".encode()).hexdigest()
        inflight_key = (kind, url, limit)
        async with self._inflight_lock:
            if self._closed:
                raise RuntimeError("media downloader is closed")
            cached = self._cached(key, limit)
            if cached:
                return cached
            task = self._inflight.get(inflight_key)
            if task is not None and task.done():
                self._inflight.pop(inflight_key, None)
                task = None
            if task is None:
                task = asyncio.create_task(self._download_and_store(url, kind, limit, key))
                self._inflight[inflight_key] = task
                task.add_done_callback(lambda completed: self._forget_inflight(inflight_key, completed))
        return await asyncio.shield(task)

    async def _download_and_store(self, url: str, kind: str, limit: int, key: str) -> DownloadedMedia:
        current = url
        for _ in range(6):
            validated = await validate_public_url(current)
            status, headers, data = await self._workers.run(self._get, validated, limit)
            if status in {301, 302, 303, 307, 308}:
                location = headers.get("location")
                if not location:
                    raise ValueError("media redirect has no location")
                current = urljoin(validated.url, location)
                continue
            if status < 200 or status >= 300:
                raise ValueError(f"media download returned HTTP {status}")
            if len(data) > limit:
                raise ValueError(f"media exceeds {limit} byte limit")
            content_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
            magic = _magic_mime(data[:32])
            content_type = {"image/jpg": "image/jpeg", "image/x-png": "image/png"}.get(content_type, content_type)
            if (
                not magic
                or content_type
                and content_type != magic
                and not (magic == "application/ogg" and content_type.startswith("audio/"))
            ):
                raise ValueError("media type does not match allowed file signatures")
            if kind == "image" and not magic.startswith("image/"):
                raise ValueError("downloaded resource is not a supported image")
            if kind == "video" and not magic.startswith("video/"):
                raise ValueError("downloaded resource is not a supported video")
            return self._store(key, data, magic)
        raise ValueError("too many media redirects")

    def _forget_inflight(self, key: tuple[str, str, int], task: asyncio.Task[DownloadedMedia]) -> None:
        if self._inflight.get(key) is task:
            self._inflight.pop(key, None)
        if not task.cancelled():
            # Observe failures even when every caller cancelled its wait.
            task.exception()

    async def close(self) -> None:
        async with self._inflight_lock:
            self._closed = True
            tasks = tuple(self._inflight.values())
        cancelled = False
        if tasks:
            drain = asyncio.gather(*(asyncio.shield(task) for task in tasks), return_exceptions=True)
            try:
                await asyncio.shield(drain)
            except asyncio.CancelledError:
                cancelled = True
                await asyncio.shield(drain)
        await self._workers.close()
        if cancelled:
            raise asyncio.CancelledError

    def _get(self, validated: object, limit: int) -> tuple[int, dict[str, str], bytes]:
        parsed = urlsplit(validated.url)
        ip = validated.pinned_ips[0]
        cls = _PinnedHTTPSConnection if parsed.scheme == "https" else _PinnedHTTPConnection
        conn = cls(validated.hostname, validated.port, ip, self.timeout)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        deadline = time.monotonic() + self.timeout
        response = None
        try:
            conn.request(
                "GET",
                path,
                headers={
                    "Host": parsed.netloc,
                    "User-Agent": "astrbot-plugin-rss/1.1.0",
                    "Accept": "image/*,video/*,audio/*,application/pdf,*/*;q=0.1",
                },
            )
            response = conn.getresponse()
            headers = {k.lower(): v for k, v in response.getheaders()}
            if response.status in {301, 302, 303, 307, 308}:
                return response.status, headers, b""
            length = headers.get("content-length")
            if length and int(length) > limit:
                raise ValueError(f"media exceeds {limit} byte limit")
            chunks, total = [], 0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("media download timed out")
                if conn.sock is not None:
                    conn.sock.settimeout(remaining)
                chunk = response.read(min(64 * 1024, limit + 1 - total))
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise ValueError(f"media exceeds {limit} byte limit")
                chunks.append(chunk)
            return response.status, headers, b"".join(chunks)
        finally:
            try:
                if response is not None:
                    with suppress(OSError):
                        response.close()
            finally:
                with suppress(OSError):
                    conn.close()

    def _cached(self, key: str, limit: int) -> DownloadedMedia | None:
        path = self.cache_dir / f"{key}.bin"
        metadata = self.cache_dir / f"{key}.json"
        try:
            info = json.loads(metadata.read_text("utf-8"))
            if time.time() - info["created_at"] > self.cache_ttl or path.stat().st_size > limit:
                return None
            return DownloadedMedia(str(path), info["mime_type"], path.stat().st_size, info["sha256"])
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _store(self, key: str, data: bytes, mime: str) -> DownloadedMedia:
        path = self.cache_dir / f"{key}.bin"
        digest = hashlib.sha256(data).hexdigest()
        fd, temp = tempfile.mkstemp(dir=self.cache_dir, prefix=".media-")
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
            os.replace(temp, path)
            (self.cache_dir / f"{key}.json").write_text(
                json.dumps({"created_at": time.time(), "mime_type": mime, "sha256": digest}), "utf-8"
            )
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
        return DownloadedMedia(str(path), mime, len(data), digest)

    def cleanup(self) -> int:
        removed = 0
        for meta in self.cache_dir.glob("*.json"):
            try:
                if time.time() - json.loads(meta.read_text("utf-8"))["created_at"] > self.cache_ttl:
                    meta.with_suffix(".bin").unlink(missing_ok=True)
                    meta.unlink(missing_ok=True)
                    removed += 1
            except (OSError, ValueError, KeyError, TypeError):
                meta.with_suffix(".bin").unlink(missing_ok=True)
                meta.unlink(missing_ok=True)
        return removed
