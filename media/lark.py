from __future__ import annotations

import logging
import time
from typing import Any

from .downloader import MediaDownloader

_LOGGER = logging.getLogger(__name__)
_IMAGE_CACHE_KIND = "image:original-v1"
_LARK_IMAGE_LIMIT = 10_000_000
_LARK_VIDEO_LIMIT = 30_000_000


class _UploadFailure(RuntimeError):
    """Safe, non-sensitive reason suitable for a concise diagnostic log."""


class FeishuMediaUploader:
    """Upload safe local image files through AstrBot's already-authorized Lark client."""

    def __init__(
        self, context: object, repository: object | None, downloader: MediaDownloader, config: object | None = None
    ) -> None:
        self.context = context
        self.repository = repository
        self.downloader = downloader
        self.config = config

    async def upload_image(self, target: object, url: str, alt: str = "") -> str | None:
        if (
            not url
            or not bool(getattr(self.config, "send_as_media", True))
            or not bool(getattr(self.config, "enable_image_upload", True))
            or not bool(getattr(self.config, "feishu_enable_image_upload", True))
        ):
            return None
        client = self._platform_client(target)
        if client is None:
            _LOGGER.warning("飞书图片上传不可用：当前平台实例未提供 Lark API 客户端")
            return None
        app_id = _client_app_id(client)
        get_cache = getattr(self.repository, "get_platform_media_cache", None)
        set_cache = getattr(self.repository, "set_platform_media_cache", None)
        delete_cache = getattr(self.repository, "delete_platform_media_cache", None)
        instance_id = str(getattr(target, "platform_instance", ""))
        if app_id and get_cache and set_cache:
            try:
                cached = await _maybe_await(get_cache(instance_id, url, _IMAGE_CACHE_KIND))
            except Exception as exc:
                _LOGGER.warning("飞书图片缓存读取失败，继续上传（%s）", type(exc).__name__)
                cached = None
            cached_key = cached.get("media_key") if cached else None
            if (
                cached
                and cached.get("app_id") == app_id
                and cached.get("expires_at", 0) > time.time()
                and isinstance(cached_key, str)
                and cached_key.strip()
            ):
                return cached_key.strip()
            if cached and delete_cache:
                try:
                    await _maybe_await(delete_cache(instance_id, url, _IMAGE_CACHE_KIND))
                except Exception as exc:
                    _LOGGER.warning("飞书图片缓存清理失败，继续上传（%s）", type(exc).__name__)
        try:
            limit = _LARK_IMAGE_LIMIT
            try:
                media = await self.downloader.download(url, "image", max_bytes=limit)
            except Exception as exc:
                _LOGGER.warning("飞书图片下载或格式校验失败，已回退为原图链接（%s）", type(exc).__name__)
                return None
            if media.size > limit:
                _LOGGER.warning("飞书图片上传失败：下载文件超过大小限制")
                return None
            key = await self._upload(client, media.path)
            if not isinstance(key, str) or not key.strip():
                _LOGGER.warning("飞书图片上传失败：接口未返回有效 image_key")
                return None
            key = key.strip()
            if app_id and set_cache:
                try:
                    await _maybe_await(
                        set_cache(
                            instance_id,
                            url,
                            _IMAGE_CACHE_KIND,
                            key,
                            app_id,
                            time.time() + int(getattr(self.config, "media_cache_ttl", 86400)),
                        )
                    )
                except Exception as exc:
                    _LOGGER.warning("飞书图片缓存写入失败（%s）", type(exc).__name__)
            return key
        except ImportError:
            _LOGGER.warning("飞书图片上传失败：未安装 lark_oapi SDK")
            return None
        except _UploadFailure as exc:
            _LOGGER.warning("飞书图片上传失败：%s", exc)
            return None
        except Exception as exc:
            # Image upload is optional presentation enhancement: the renderer keeps
            # the original URL when it fails.
            _LOGGER.warning("飞书图片上传失败，已回退为原图链接（%s）", type(exc).__name__)
            return None

    async def image_size(self, target: object, url: str) -> tuple[int, int] | None:
        """Read image dimensions from the bounded source cache without decoding pixels."""
        if (
            not url
            or not bool(getattr(self.config, "send_as_media", True))
            or not bool(getattr(self.config, "enable_image_upload", True))
            or not bool(getattr(self.config, "feishu_enable_image_upload", True))
        ):
            return None
        try:
            media = await self.downloader.download(url, "image", max_bytes=_LARK_IMAGE_LIMIT)
            from PIL import Image

            with Image.open(media.path) as image:
                width, height = image.size
            if type(width) is int and type(height) is int and width > 0 and height > 0:
                return width, height
        except Exception as exc:
            _LOGGER.debug("读取飞书图片尺寸失败（%s）", type(exc).__name__)
        return None

    async def upload_video(self, target: object, url: str) -> dict[str, str] | None:
        if not url or not bool(getattr(self.config, "send_as_media", True)):
            return None
        client = self._platform_client(target)
        if client is None:
            return None
        app_id = _client_app_id(client)
        instance_id = str(getattr(target, "platform_instance", ""))
        get_cache = getattr(self.repository, "get_platform_media_cache", None)
        set_cache = getattr(self.repository, "set_platform_media_cache", None)
        delete_cache = getattr(self.repository, "delete_platform_media_cache", None)
        if app_id and get_cache and set_cache:
            try:
                cached = await _maybe_await(get_cache(instance_id, url, "video"))
            except Exception as exc:
                _LOGGER.warning("飞书视频缓存读取失败，继续上传（%s）", type(exc).__name__)
                cached = None
            cached_key = cached.get("media_key") if cached else None
            if (
                cached
                and cached.get("app_id") == app_id
                and cached.get("expires_at", 0) > time.time()
                and isinstance(cached_key, str)
                and cached_key.strip()
            ):
                return {"file_key": cached_key.strip(), "file_name": "video.mp4", "file_type": "mp4"}
            if cached and delete_cache:
                try:
                    await _maybe_await(delete_cache(instance_id, url, "video"))
                except Exception as exc:
                    _LOGGER.warning("飞书视频缓存清理失败，继续上传（%s）", type(exc).__name__)
        try:
            limit = _LARK_VIDEO_LIMIT
            media = await self.downloader.download(url, "video", max_bytes=limit)
            if media.size > limit:
                _LOGGER.warning("飞书视频下载文件超过大小限制，已回退为原视频链接")
                return None
            key = await self._upload_file(client, media.path)
            if not isinstance(key, str) or not key.strip():
                _LOGGER.warning("飞书视频上传失败：接口未返回有效 file_key，已回退为原视频链接")
                return None
            key = key.strip()
            if app_id and set_cache:
                try:
                    await _maybe_await(
                        set_cache(
                            instance_id,
                            url,
                            "video",
                            key,
                            app_id,
                            time.time() + int(getattr(self.config, "media_cache_ttl", 86400)),
                        )
                    )
                except Exception as exc:
                    _LOGGER.warning("飞书视频缓存写入失败（%s）", type(exc).__name__)
            return {"file_key": key, "file_name": "video.mp4", "file_type": "mp4"}
        except Exception as exc:
            _LOGGER.warning("飞书视频上传失败，已回退为原视频链接（%s）", type(exc).__name__)
            return None

    def _platform_client(self, target: object) -> object | None:
        try:
            platform = self.context.get_platform_inst(target.platform_instance)
        except Exception:
            return None
        return getattr(platform, "lark_api", None)

    async def _upload(self, client: object, path: str) -> str | None:
        """SDK request construction follows lark_oapi's generated v1 models."""
        if client is None or getattr(client, "im", None) is None:
            raise _UploadFailure("当前 Lark API 客户端不可用")
        from lark_oapi.api.im.v1 import CreateImageRequest, CreateImageRequestBody

        image_api = getattr(getattr(client.im, "v1", None), "image", None)
        if image_api is None or not callable(getattr(image_api, "acreate", None)):
            raise _UploadFailure("Lark 图片 API 不兼容")
        with open(path, "rb") as image:
            request = (
                CreateImageRequest.builder()
                .request_body(CreateImageRequestBody.builder().image_type("message").image(image).build())
                .build()
            )
            response = await image_api.acreate(request)
        if not response.success() or response.data is None:
            details = []
            code = getattr(response, "code", None)
            if type(code) is int:
                details.append(f"code={code}")
            raw_status = getattr(getattr(response, "raw", None), "status_code", None)
            if type(raw_status) is not int:
                raw_status = getattr(response, "status_code", None)
            if type(raw_status) is int:
                details.append(f"HTTP {raw_status}")
            suffix = f"（{'，'.join(details)}）" if details else ""
            raise _UploadFailure(f"Lark 图片上传接口拒绝请求{suffix}")
        key = getattr(response.data, "image_key", None)
        if not isinstance(key, str) or not key.strip():
            raise _UploadFailure("接口未返回有效 image_key")
        return key.strip()

    async def _upload_file(self, client: object, path: str) -> str | None:
        if client is None or getattr(client, "im", None) is None:
            return None
        try:
            from lark_oapi.api.im.v1 import CreateFileRequest, CreateFileRequestBody
        except ImportError:
            return None
        with open(path, "rb") as media_file:
            request = (
                CreateFileRequest.builder()
                .request_body(
                    CreateFileRequestBody.builder().file_type("mp4").file_name("video.mp4").file(media_file).build()
                )
                .build()
            )
            response = await client.im.v1.file.acreate(request)
        if not response.success() or response.data is None:
            return None
        return getattr(response.data, "file_key", None)


async def _maybe_await(value: Any) -> Any:
    return await value if hasattr(value, "__await__") else value


def _client_app_id(client: object) -> str:
    config = getattr(client, "config", None) or getattr(client, "_config", None)
    value = getattr(config, "app_id", "") if config else ""
    return str(value or "")
