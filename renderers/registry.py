from __future__ import annotations

from typing import Any

from ..core.errors import PlatformUnsupportedError, RenderingError
from ..core.models import Article, DeliveryTarget, RenderedMessage
from ..media import FeishuMediaUploader, MediaDownloader
from ..normalizers.discourse import clean_cached_article
from ..normalizers.media_content import is_pure_video_article
from .default import DefaultRenderer
from .feishu import FeishuRenderer
from .telegram import TelegramRenderer


class RendererRegistry:
    def __init__(
        self,
        config: object | None = None,
        repository: object | None = None,
        data_dir: str | None = None,
        context: object | None = None,
        media_downloader: MediaDownloader | None = None,
    ) -> None:
        self.config = config
        self.repository = repository
        self.context = context
        self._owns_downloader = media_downloader is None and data_dir is not None
        self.downloader = media_downloader or (MediaDownloader(data_dir, config) if data_dir else None)
        self.default = DefaultRenderer(config)
        self.telegram = TelegramRenderer(config, repository)
        self.feishu_uploader = (
            FeishuMediaUploader(context, repository, self.downloader, config)
            if context is not None and self.downloader is not None
            else None
        )

    async def close(self) -> None:
        try:
            await self.telegram.close()
        finally:
            if self._owns_downloader and self.downloader is not None:
                await self.downloader.close()

    async def render(
        self,
        articles: list[Article],
        target: DeliveryTarget,
        source_name: str,
        is_list: bool = False,
        batch_id: str = "",
        **kwargs: Any,
    ) -> list[RenderedMessage]:
        platform = target.platform.lower()
        if "feishu" in platform or "lark" in platform:
            if not bool(getattr(self.config, "enable_feishu_card_v2", True)):
                return await self._fallback(articles, target, source_name, is_list, batch_id, kwargs)
            image_key = self.feishu_uploader.upload_image if self.feishu_uploader else None
            image_size = self.feishu_uploader.image_size if self.feishu_uploader else None
            video_file = self.feishu_uploader.upload_video if self.feishu_uploader else None
            renderer = FeishuRenderer(
                self.config,
                image_key=image_key,
                image_size=image_size,
                video_file=video_file,
            )
        elif "telegram" in platform:
            renderer = self.telegram
        else:
            return await self._fallback(articles, target, source_name, is_list, batch_id, kwargs)
        try:
            if isinstance(renderer, FeishuRenderer):
                messages: list[RenderedMessage] = []
                send_as_media = bool(getattr(self.config, "send_as_media", True))
                default_enabled = bool(getattr(self.config, "enable_default_renderer", True))
                for original in articles:
                    article = clean_cached_article(original)
                    if send_as_media and default_enabled and is_pure_video_article(article):
                        messages.extend(
                            await self.default.render(
                                [article], target, source_name, is_list=is_list, batch_id=batch_id, **kwargs
                            )
                        )
                    else:
                        messages.extend(
                            await renderer.render(
                                [article], target, source_name, is_list=is_list, batch_id=batch_id, **kwargs
                            )
                        )
                return messages
            return await renderer.render(articles, target, source_name, is_list=is_list, batch_id=batch_id, **kwargs)
        except (RenderingError, PlatformUnsupportedError):
            return await self._fallback(articles, target, source_name, is_list, batch_id, kwargs)

    async def _fallback(
        self,
        articles: list[Article],
        target: DeliveryTarget,
        source_name: str,
        is_list: bool,
        batch_id: str,
        kwargs: dict[str, Any],
    ) -> list[RenderedMessage]:
        if not bool(getattr(self.config, "enable_default_renderer", True)):
            raise PlatformUnsupportedError("specialized rendering failed and default renderer is disabled")
        return await self.default.render(articles, target, source_name, is_list=is_list, batch_id=batch_id, **kwargs)
