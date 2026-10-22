from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1].parent))

from astrbot_plugin_rss.media.downloader import MediaDownloader, _magic_mime


def test_magic_signatures_are_required() -> None:
    assert _magic_mime(b"\x89PNG\r\n\x1a\nrest") == "image/png"
    assert _magic_mime(b"GIF89a") == "image/gif"
    assert _magic_mime(b"<html>image/png</html>") is None


def test_disk_media_cache_reuses_content_and_expires(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    downloader = MediaDownloader(tmp_path, type("Config", (), {"media_cache_ttl": 3600})())
    original = b"\x89PNG\r\n\x1a\nimage"
    item = downloader._store("a" * 64, original, "image/png")
    assert Path(item.path).read_bytes() == original
    assert downloader._cached("a" * 64, 10_000) == item
    assert downloader.cleanup() == 0


@pytest.mark.asyncio
async def test_downloader_uses_explicit_caller_limit_without_general_config_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import astrbot_plugin_rss.media.downloader as module
    from astrbot_plugin_rss.core.security import ValidatedURL

    seen = []
    monkeypatch.setattr(module, "validate_public_url", lambda url: _validated(url))

    async def validated(url):
        return ValidatedURL(url, "public.example", 443, ("93.184.216.34",))

    monkeypatch.setattr(module, "validate_public_url", validated)

    def fake_get(validated_url, limit):
        seen.append(limit)
        return 200, {"content-type": "image/png"}, b"\x89PNG\r\n\x1a\nimage"

    config = SimpleNamespace(media_download_timeout=2, media_cache_ttl=60, max_media_download_bytes=1024)
    downloader = MediaDownloader(tmp_path, config)
    monkeypatch.setattr(downloader, "_get", fake_get)
    await downloader.download("https://public.example/image", "image", max_bytes=10_000_000)
    assert seen[-1] == 10_000_000

    large_video = b"0000ftypisom" + b"x" * 11_000_000

    def fake_video(validated_url, limit):
        seen.append(limit)
        return 200, {"content-type": "video/mp4"}, large_video

    monkeypatch.setattr(downloader, "_get", fake_video)
    video = await downloader.download("https://public.example/video", "video", max_bytes=30_000_000)
    assert seen[-1] == 30_000_000
    assert video.size > 10_000_000

    with pytest.raises(ValueError, match="positive byte limit"):
        await downloader.download("https://public.example/unbounded", "image")


async def _validated(url):
    from astrbot_plugin_rss.core.security import ValidatedURL

    return ValidatedURL(url, "public.example", 443, ("93.184.216.34",))
