from __future__ import annotations

import base64
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1].parent))

from astrbot_plugin_rss.core.models import Article, ContentBlock, DeliveryTarget
from astrbot_plugin_rss.media.downloader import MediaDownloader
from astrbot_plugin_rss.renderers.registry import RendererRegistry

PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/pT8AAAAASUVORK5CYII="
)
IMAGE_URL = "https://images.example.test/photo.png?token=private-query"


class _Builder:
    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.values: dict[str, object] = {}

    @classmethod
    def builder(cls):
        return cls(cls.__name__)

    def __getattr__(self, name):
        def setter(value):
            self.values[name] = value
            return self

        return setter

    def build(self):
        return SimpleNamespace(kind=self.kind, values=self.values)


class _FakeResponse:
    def __init__(self, success: bool, image_key: object = "img_v2_test") -> None:
        self.data = SimpleNamespace(image_key=image_key) if success else None
        self._success = success
        self.code = 230099 if not success else 0
        self.raw = SimpleNamespace(status_code=403 if not success else 200)

    def success(self):
        return self._success


class _MediaCache:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str, str], dict[str, object]] = {}
        self.deleted: list[tuple[str, str, str]] = []

    async def get_platform_media_cache(self, instance, url, kind):
        return self.rows.get((instance, url, kind))

    async def set_platform_media_cache(self, instance, url, kind, key, app_id, expires_at):
        self.rows[(instance, url, kind)] = {
            "media_key": key,
            "app_id": app_id,
            "expires_at": expires_at,
        }

    async def delete_platform_media_cache(self, instance, url, kind):
        self.deleted.append((instance, url, kind))
        self.rows.pop((instance, url, kind), None)


def _install_lark_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    lark = ModuleType("lark_oapi")
    api = ModuleType("lark_oapi.api")
    im = ModuleType("lark_oapi.api.im")
    v1 = ModuleType("lark_oapi.api.im.v1")
    v1.CreateImageRequest = type("CreateImageRequest", (_Builder,), {})
    v1.CreateImageRequestBody = type("CreateImageRequestBody", (_Builder,), {})
    for name, module in (
        ("lark_oapi", lark),
        ("lark_oapi.api", api),
        ("lark_oapi.api.im", im),
        ("lark_oapi.api.im.v1", v1),
    ):
        monkeypatch.setitem(sys.modules, name, module)


def _make_client(app_id: str, *, accepted: bool = True, image_key: object = "img_v2_test"):
    calls: list[tuple[object, bytes, bool]] = []

    async def acreate(request):
        body = request.values["request_body"]
        image_stream = body.values["image"]
        calls.append((request, image_stream.read(), image_stream.closed))
        return _FakeResponse(accepted, image_key)

    client = SimpleNamespace(
        config=SimpleNamespace(app_id=app_id),
        im=SimpleNamespace(v1=SimpleNamespace(image=SimpleNamespace(acreate=acreate))),
    )
    return client, calls


def _article() -> Article:
    return Article(
        article_id="article-1",
        feed_id="feed-1",
        entry_id="entry-1",
        canonical_url="https://feed.example.test/article",
        title="Example article",
        content_blocks=[ContentBlock("image", url=IMAGE_URL, attrs={"alt": "Photo"})],
    )


def _target() -> DeliveryTarget:
    return DeliveryTarget(
        target_id="target-1",
        platform_instance="lark-instance",
        unified_origin="lark:chat:1",
        platform="feishu",
        session_id="oc_test",
    )


@pytest.mark.asyncio
async def test_registry_uploads_real_png_and_embeds_image_key_in_card(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_lark_sdk(monkeypatch)
    import astrbot_plugin_rss.media.downloader as downloader_module
    from astrbot_plugin_rss.core.security import ValidatedURL

    async def validate(url):
        return ValidatedURL(url, "images.example.test", 443, ("93.184.216.34",))

    monkeypatch.setattr(downloader_module, "validate_public_url", validate)
    config = SimpleNamespace(
        enable_image_upload=True,
        feishu_enable_image_upload=True,
        media_cache_ttl=3600,
        enable_feishu_card_v2=True,
        enable_default_renderer=True,
        max_images_per_message=8,
        feishu_max_card_bytes=30_720,
        enable_content_truncation=False,
        content_truncation_length=0,
    )
    client, calls = _make_client("app-one")
    context = SimpleNamespace(get_platform_inst=lambda instance: SimpleNamespace(lark_api=client))
    repository = _MediaCache()
    downloader = MediaDownloader(tmp_path, config)
    monkeypatch.setattr(
        downloader,
        "_get",
        lambda validated, limit: (200, {"content-type": "image/png"}, PNG_BYTES),
    )
    registry = RendererRegistry(config, repository, str(tmp_path), context, downloader)

    messages = await registry.render([_article()], _target(), "Example feed")

    assert len(calls) == 1
    request, uploaded_bytes, was_closed_during_request = calls[0]
    body = request.values["request_body"]
    assert body.values["image_type"] == "message"
    assert uploaded_bytes == PNG_BYTES
    assert not was_closed_during_request
    assert body.values["image"].closed
    payload = json.dumps(messages[0].payload, ensure_ascii=False)
    elements = messages[0].payload["card"]["body"]["elements"]
    image_element = next(item for item in elements if item.get("tag") == "img")
    assert image_element["img_key"] == "img_v2_test"
    assert image_element["size"] == "1px 1px"
    assert IMAGE_URL not in payload


@pytest.mark.asyncio
async def test_lark_upload_preserves_original_image_bytes_above_old_compression_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_lark_sdk(monkeypatch)
    import astrbot_plugin_rss.media.downloader as downloader_module
    from astrbot_plugin_rss.core.security import ValidatedURL

    async def validate(url):
        return ValidatedURL(url, "images.example.test", 443, ("93.184.216.34",))

    monkeypatch.setattr(downloader_module, "validate_public_url", validate)
    original_bytes = PNG_BYTES + b"x" * (2_500_000 - len(PNG_BYTES))
    observed_limits: list[int] = []

    def fake_get(validated_url, limit):
        observed_limits.append(limit)
        return 200, {"content-type": "image/png"}, original_bytes

    config = SimpleNamespace(
        enable_image_upload=True,
        feishu_enable_image_upload=True,
        feishu_max_image_bytes=1,
        max_media_download_bytes=1,
        media_cache_ttl=3600,
    )
    client, calls = _make_client("app-original")
    context = SimpleNamespace(get_platform_inst=lambda instance: SimpleNamespace(lark_api=client))
    downloader = MediaDownloader(tmp_path, config)
    monkeypatch.setattr(downloader, "_get", fake_get)
    registry = RendererRegistry(config, _MediaCache(), str(tmp_path), context, downloader)

    key = await registry.feishu_uploader.upload_image(_target(), IMAGE_URL, "Photo")

    assert key == "img_v2_test"
    assert observed_limits == [10_000_000]
    assert calls[0][1] == original_bytes
    assert await registry.feishu_uploader.image_size(_target(), IMAGE_URL) == (1, 1)
    assert observed_limits == [10_000_000]


@pytest.mark.asyncio
async def test_lark_image_over_platform_limit_falls_back_without_upload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = SimpleNamespace(enable_image_upload=True, feishu_enable_image_upload=True, media_cache_ttl=3600)
    client, calls = _make_client("app-too-large")
    context = SimpleNamespace(get_platform_inst=lambda instance: SimpleNamespace(lark_api=client))
    downloader = MediaDownloader(tmp_path, config)
    requested_limits: list[int] = []

    async def oversized_image(url, kind, max_bytes):
        requested_limits.append(max_bytes)
        return SimpleNamespace(path="unused", size=10_000_001)

    monkeypatch.setattr(downloader, "download", oversized_image)
    registry = RendererRegistry(config, _MediaCache(), str(tmp_path), context, downloader)

    key = await registry.feishu_uploader.upload_image(_target(), IMAGE_URL, "Photo")

    assert key is None
    assert requested_limits == [10_000_000]
    assert calls == []


@pytest.mark.asyncio
async def test_lark_video_between_ten_and_thirty_megabytes_uses_video_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    video_bytes = b"0000ftypisom" + b"v" * 11_000_000
    video_path = tmp_path / "source.mp4"
    video_path.write_bytes(video_bytes)
    config = SimpleNamespace(send_as_media=True, media_cache_ttl=3600)
    client = SimpleNamespace(config=SimpleNamespace(app_id="app-video"), im=SimpleNamespace())
    context = SimpleNamespace(get_platform_inst=lambda instance: SimpleNamespace(lark_api=client))
    downloader = MediaDownloader(tmp_path, config)
    requested_limits: list[int] = []
    uploaded: list[bytes] = []

    async def download(url, kind, max_bytes):
        assert kind == "video"
        requested_limits.append(max_bytes)
        return SimpleNamespace(path=str(video_path), size=len(video_bytes))

    async def upload_file(client, path):
        uploaded.append(Path(path).read_bytes())
        return "file_key_video"

    monkeypatch.setattr(downloader, "download", download)
    registry = RendererRegistry(config, _MediaCache(), str(tmp_path), context, downloader)
    monkeypatch.setattr(registry.feishu_uploader, "_upload_file", upload_file)

    result = await registry.feishu_uploader.upload_video(_target(), "https://videos.example.test/video.mp4")

    assert requested_limits == [30_000_000]
    assert result == {"file_key": "file_key_video", "file_name": "video.mp4", "file_type": "mp4"}
    assert uploaded == [video_bytes]


@pytest.mark.asyncio
async def test_image_size_does_not_download_when_media_is_disabled(tmp_path: Path) -> None:
    config = SimpleNamespace(send_as_media=False)
    downloader = MediaDownloader(tmp_path, config)

    async def unexpected_download(*args, **kwargs):
        raise AssertionError("disabled media must not trigger a download")

    downloader.download = unexpected_download
    uploader = RendererRegistry(config, _MediaCache(), str(tmp_path), SimpleNamespace(), downloader).feishu_uploader

    assert uploader is not None
    assert await uploader.image_size(_target(), IMAGE_URL) is None


@pytest.mark.asyncio
async def test_upload_cache_is_app_scoped_and_only_reuses_nonempty_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_lark_sdk(monkeypatch)
    import astrbot_plugin_rss.media.downloader as downloader_module
    from astrbot_plugin_rss.core.security import ValidatedURL

    async def validate(url):
        return ValidatedURL(url, "images.example.test", 443, ("93.184.216.34",))

    monkeypatch.setattr(downloader_module, "validate_public_url", validate)
    config = SimpleNamespace(
        enable_image_upload=True,
        feishu_enable_image_upload=True,
        media_cache_ttl=3600,
    )
    repository = _MediaCache()
    client, calls = _make_client("app-one")
    current = {"client": client}
    context = SimpleNamespace(get_platform_inst=lambda instance: SimpleNamespace(lark_api=current["client"]))
    downloader = MediaDownloader(tmp_path, config)
    monkeypatch.setattr(
        downloader,
        "_get",
        lambda validated, limit: (200, {"content-type": "image/png"}, PNG_BYTES),
    )
    registry = RendererRegistry(config, repository, str(tmp_path), context, downloader)

    first = await registry.feishu_uploader.upload_image(_target(), IMAGE_URL, "Photo")
    second = await registry.feishu_uploader.upload_image(_target(), IMAGE_URL, "Photo")
    assert first == second == "img_v2_test"
    assert len(calls) == 1

    changed_client, changed_calls = _make_client("app-two", image_key="img_v2_new_app")
    current["client"] = changed_client
    third = await registry.feishu_uploader.upload_image(_target(), IMAGE_URL, "Photo")
    assert third == "img_v2_new_app"
    assert len(changed_calls) == 1
    assert repository.deleted == [("lark-instance", IMAGE_URL, "image:original-v1")]

    empty_key_client, _ = _make_client("app-three", image_key="  ")
    current["client"] = empty_key_client
    invalid_key = await registry.feishu_uploader.upload_image(_target(), IMAGE_URL, "Photo")
    assert invalid_key is None
    assert ("lark-instance", IMAGE_URL, "image:original-v1") not in repository.rows


@pytest.mark.asyncio
async def test_api_and_download_failures_return_no_key_and_renderer_uses_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _install_lark_sdk(monkeypatch)
    import astrbot_plugin_rss.media.downloader as downloader_module
    from astrbot_plugin_rss.core.security import ValidatedURL

    async def validate(url):
        return ValidatedURL(url, "images.example.test", 443, ("93.184.216.34",))

    monkeypatch.setattr(downloader_module, "validate_public_url", validate)
    config = SimpleNamespace(
        enable_image_upload=True,
        feishu_enable_image_upload=True,
        media_cache_ttl=3600,
        enable_feishu_card_v2=True,
        enable_default_renderer=True,
        max_images_per_message=8,
        feishu_max_card_bytes=30_720,
        enable_content_truncation=False,
        content_truncation_length=0,
    )
    failed_client, calls = _make_client("app-one", accepted=False)
    context = SimpleNamespace(get_platform_inst=lambda instance: SimpleNamespace(lark_api=failed_client))
    downloader = MediaDownloader(tmp_path, config)
    monkeypatch.setattr(
        downloader,
        "_get",
        lambda validated, limit: (200, {"content-type": "image/png"}, PNG_BYTES),
    )
    registry = RendererRegistry(config, _MediaCache(), str(tmp_path), context, downloader)

    messages = await registry.render([_article()], _target(), "Example feed")
    payload = json.dumps(messages[0].payload, ensure_ascii=False)
    assert len(calls) == 1
    assert "![Photo]" not in payload
    assert IMAGE_URL in payload
    assert "接口拒绝请求" in caplog.text
    assert "code=230099" in caplog.text
    assert "HTTP 403" in caplog.text
    assert "private-query" not in caplog.text

    async def download_failure(*args, **kwargs):
        raise ValueError(f"rejected {IMAGE_URL}")

    registry.feishu_uploader.downloader.download = download_failure
    key = await registry.feishu_uploader.upload_image(_target(), IMAGE_URL, "Photo")
    assert key is None
    assert "private-query" not in caplog.text
