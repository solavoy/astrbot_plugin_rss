from __future__ import annotations

import json
import math
import re
from dataclasses import replace
from typing import Any, Awaitable, Callable

from ..core.errors import RenderingError
from ..core.models import Article, ContentBlock, DeliveryTarget, RenderedMessage
from ..normalizers.discourse import clean_cached_article, normalize_image_links
from .blocks import truncate_content_blocks
from .default import _article_meta

HARD_LIMIT = 30 * 1024
MAX_ELEMENTS = 200


def _positive_dimension(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        dimension = int(str(value).replace("px", ""))
    except (TypeError, ValueError):
        return None
    return dimension if dimension > 0 else None


def _display_size(
    dimensions: tuple[int, int] | None,
    *,
    emoji: bool,
    width_hint: int | None = None,
    height_hint: int | None = None,
) -> str | None:
    if not dimensions or len(dimensions) != 2:
        if emoji:
            return "20px 20px"
        if width_hint and height_hint:
            dimensions = (width_hint, height_hint)
        else:
            return None
    width, height = dimensions
    if width <= 0 or height <= 0:
        return "20px 20px" if emoji else None
    scale = 1.0
    if width_hint:
        scale = min(scale, width_hint / width)
    if height_hint:
        scale = min(scale, height_hint / height)
    if emoji:
        scale = min(scale, 20 / width, 20 / height)
    else:
        scale = min(scale, 580 / width, 1000 / height)
    shown_width = max(1, math.floor(width * scale))
    shown_height = max(1, math.floor(height * scale))
    return f"{shown_width}px {shown_height}px"


def _expand_image_tokens(value: str, image_tokens: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    if not image_tokens:
        return [{"tag": "markdown", "content": value}] if value else []
    token_pattern = re.compile("|".join(re.escape(token) for token in sorted(image_tokens, key=len, reverse=True)))
    elements: list[dict[str, Any]] = []
    cursor = 0
    for match in token_pattern.finditer(value):
        text = value[cursor : match.start()]
        if text:
            elements.append({"tag": "markdown", "content": text})
        elements.append(image_tokens[match.group(0)])
        cursor = match.end()
    tail = value[cursor:]
    if tail:
        elements.append({"tag": "markdown", "content": tail})
    return elements


class FeishuRenderer:
    def __init__(
        self,
        config: object | None = None,
        image_key: Callable[[DeliveryTarget, str, str], Awaitable[str | None]] | None = None,
        video_file: Callable[[DeliveryTarget, str], Awaitable[dict[str, str] | None]] | None = None,
        image_size: Callable[[DeliveryTarget, str], Awaitable[tuple[int, int] | None]] | None = None,
    ) -> None:
        self.config = config
        self.image_key = image_key
        self.video_file = video_file
        self.image_size = image_size
        self._image_count = 0
        self._image_tokens: dict[str, dict[str, Any]] = {}
        self._image_keys: dict[str, str | None] = {}
        self._uploaded_video_urls: set[str] = set()
        self.hard_limit = min(HARD_LIMIT, int(getattr(config, "feishu_max_card_bytes", HARD_LIMIT)))

    async def render(
        self,
        articles: list[Article],
        target: DeliveryTarget,
        source_name: str,
        is_list: bool = False,
        batch_id: str = "",
        **kwargs: Any,
    ) -> list[RenderedMessage]:
        if not articles:
            return []
        ordinary_limit = int(getattr(self.config, "content_truncation_length", 0) or 0)
        ordinary_enabled = (
            not is_list and ordinary_limit > 0 and bool(getattr(self.config, "enable_content_truncation", False))
        )
        render_articles = []
        for article in articles:
            article = clean_cached_article(article)
            article = replace(
                article,
                content_blocks=normalize_image_links(
                    article.content_blocks,
                    recognize_image_anchors=article.metadata.get("source_adapter") == "discourse",
                ),
            )
            render_articles.append(article)
        render_articles = (
            [
                replace(article, content_blocks=truncate_content_blocks(article.content_blocks, ordinary_limit))
                for article in render_articles
            ]
            if ordinary_enabled
            else render_articles
        )
        messages: list[RenderedMessage] = []
        for article in render_articles:
            self._image_count = 0
            self._image_tokens = {}
            self._image_keys = {}
            self._uploaded_video_urls = set()
            video_messages: list[RenderedMessage] = []
            if self.video_file and bool(getattr(self.config, "send_as_media", True)):
                for block in _walk_blocks(article.content_blocks):
                    if block.type.lower() != "video" or not block.url:
                        continue
                    attachment = await self.video_file(target, block.url)
                    file_key = attachment.get("file_key") if attachment else None
                    if isinstance(file_key, str) and file_key.strip():
                        self._uploaded_video_urls.add(block.url)
                        video_messages.append(
                            RenderedMessage(
                                "feishu",
                                "media",
                                {"file_key": file_key.strip(), "source_url": block.url},
                                [article.article_id],
                                batch_id,
                            )
                        )
            elements = await self._article_elements(article, target)
            elements = await self._fit_article(article, source_name, target, elements, batch_id)
            messages.append(self._message(article, source_name, elements, target, batch_id))
            messages.extend(video_messages)
        return messages

    async def _fit_article(
        self, article: Article, source_name: str, target: DeliveryTarget, elements: list[dict[str, Any]], batch_id: str
    ) -> list[dict[str, Any]]:
        feed_title = str(article.metadata.get("feed_title") or source_name)
        timezone = str(getattr(self.config, "timezone", "Asia/Shanghai") or "Asia/Shanghai")
        subtitle = _article_meta(article, feed_title, timezone)
        candidate = RenderedMessage(
            "feishu",
            "interactive",
            {"card": self._card(article.title, subtitle, elements)},
            [article.article_id],
            batch_id,
        )
        if self.request_size(candidate, target) <= self.hard_limit and len(elements) <= MAX_ELEMENTS:
            return elements
        # The trial pass may already have uploaded media. Recompute the final
        # card's image allowance so cached keys are retained after trimming.
        self._image_count = 0
        # Keep complete ordered blocks. Only plain text/paragraph blocks can be
        # shortened; structured Markdown and media remain whole or are omitted.
        base = await self._article_elements(article, target, include_body=False)
        footer = base[-1:]
        kept: list[dict[str, Any]] = []
        for block in article.content_blocks:
            value = await self._block_markdown(block, target)
            if not value:
                continue
            block_elements = _expand_image_tokens(value, self._image_tokens)
            trial = kept + block_elements + footer
            trial_message = RenderedMessage(
                "feishu",
                "interactive",
                {"card": self._card(article.title, subtitle, trial)},
                [article.article_id],
                batch_id,
            )
            if len(trial) <= MAX_ELEMENTS and self.request_size(trial_message, target) <= self.hard_limit:
                kept.extend(block_elements)
                continue
            text_size = _block_text_length(block)
            if text_size:
                low, high = 0, text_size
                best = ""
                while low <= high:
                    middle = (low + high) // 2
                    clipped_block = truncate_content_blocks([block], middle)
                    clipped = await self._block_markdown(clipped_block[0], target) if clipped_block else ""
                    clipped = clipped.rstrip() + "…" if clipped else ""
                    clipped_elements = _expand_image_tokens(clipped, self._image_tokens) if clipped else []
                    test_elems = kept + clipped_elements + footer
                    test = RenderedMessage(
                        "feishu",
                        "interactive",
                        {"card": self._card(article.title, subtitle, test_elems)},
                        [article.article_id],
                        batch_id,
                    )
                    if len(test_elems) <= MAX_ELEMENTS and self.request_size(test, target) <= self.hard_limit:
                        best = clipped
                        low = middle + 1
                    else:
                        high = middle - 1
                if best:
                    kept.extend(_expand_image_tokens(best, self._image_tokens))
            break
        result = kept + footer
        final = RenderedMessage(
            "feishu",
            "interactive",
            {"card": self._card(article.title, subtitle, result)},
            [article.article_id],
            batch_id,
        )
        if self.request_size(final, target) > self.hard_limit or len(result) > MAX_ELEMENTS:
            raise RenderingError("最小飞书卡片仍超过配置或平台硬限制")
        return result

    async def _article_elements(
        self, article: Article, target: DeliveryTarget, include_body: bool = True
    ) -> list[dict[str, Any]]:
        elems: list[dict[str, Any]] = []
        if include_body:
            for block in article.content_blocks:
                value = await self._block_markdown(block, target)
                if value:
                    elems.extend(_expand_image_tokens(value, self._image_tokens))
        elems.append({"tag": "markdown", "content": f"原文地址：[{article.canonical_url}]({article.canonical_url})"})
        return elems

    async def _block_markdown(self, block: ContentBlock, target: DeliveryTarget) -> str:
        kind = block.type.lower()
        send_as_media = bool(getattr(self.config, "send_as_media", True))
        if kind == "image":
            if not send_as_media:
                label = block.attrs.get("alt") or "查看原图"
                return f"[{label}]({block.url})" if block.url else ""
            max_images = int(getattr(self.config, "max_images_per_message", 8))
            key = self._image_keys.get(block.url)
            if block.url not in self._image_keys:
                key = None
                if self.image_key and self._image_count < max_images:
                    key = await self.image_key(target, block.url, str(block.attrs.get("alt", "图片")))
                    if key:
                        self._image_count += 1
                self._image_keys[block.url] = key
            if key:
                token = f"\ufff0FEISHUIMG{len(self._image_tokens)}\ufff1"
                self._image_tokens[token] = await self._image_element(target, block, key)
                return token
            return f"[图片：{block.attrs.get('alt') or '查看原图'}]({block.url})" if block.url else ""
        if kind == "video" and send_as_media and block.url in self._uploaded_video_urls:
            return ""
        if kind in {"video", "audio", "attachment", "link"}:
            title = (
                "".join([await self._block_markdown(c, target) for c in block.children]).strip()
                or block.text
                or block.attrs.get("title")
                or block.attrs.get("alt")
                or kind.capitalize()
            )
            return f"[{title}]({block.url})" if block.url else ""
        text = block.text or "".join([await self._block_markdown(c, target) for c in block.children])
        if kind == "heading":
            level = min(6, max(1, int(block.attrs.get("level", 2))))
            return f"{'#' * level} {text}"
        if kind == "quote":
            return "\n".join("> " + line for line in text.splitlines())
        if kind == "code":
            lang = str(block.attrs.get("language", ""))
            return f"```{lang}\n{text}\n```"
        if kind == "list":
            return "\n".join(
                [
                    (f"{i}. " if block.attrs.get("ordered") else "- ") + await self._block_markdown(c, target)
                    for i, c in enumerate(block.children, 1)
                ]
            )
        if kind == "table":
            rows = []
            for row in block.children:
                rows.append(" | ".join([await self._block_markdown(c, target) for c in row.children]))
            return "\n".join(rows)
        return text

    async def _image_element(self, target: DeliveryTarget, block: ContentBlock, key: str) -> dict[str, Any]:
        attrs = block.attrs
        classes = attrs.get("class", [])
        if isinstance(classes, str):
            classes = [classes]
        is_emoji = "emoji" in classes
        width_hint = _positive_dimension(attrs.get("width"))
        height_hint = _positive_dimension(attrs.get("height"))
        dimensions = None
        if self.image_size:
            dimensions = await self.image_size(target, block.url)
        if not dimensions and width_hint and height_hint:
            dimensions = (width_hint, height_hint)
        size = _display_size(
            dimensions,
            emoji=is_emoji,
            width_hint=width_hint,
            height_hint=height_hint,
        )
        element = {
            "tag": "img",
            "img_key": key,
            "alt": {"tag": "plain_text", "content": str(attrs.get("alt") or "")[:1000]},
            "scale_type": "crop_center",
            "preview": True,
            "transparent": True,
        }
        if size:
            element["size"] = size
        return element

    @staticmethod
    def _card(title: str, subtitle: str, elements: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "schema": "2.0",
            "header": {
                "title": {"tag": "plain_text", "content": title[:100]},
                "subtitle": {"tag": "plain_text", "content": subtitle[:500]},
                "template": "blue",
            },
            "body": {"elements": elements},
        }

    def _message(
        self,
        article: Article,
        source_name: str,
        elements: list[dict[str, Any]],
        target: DeliveryTarget,
        batch_id: str,
    ) -> RenderedMessage:
        timezone = str(getattr(self.config, "timezone", "Asia/Shanghai") or "Asia/Shanghai")
        subtitle = _article_meta(article, str(article.metadata.get("feed_title") or source_name), timezone)
        msg = RenderedMessage(
            "feishu",
            "interactive",
            {"card": self._card(article.title, subtitle, elements)},
            [article.article_id],
            batch_id,
        )
        size = self.request_size(msg, target)
        if size > self.hard_limit or len(elements) > MAX_ELEMENTS:
            raise RenderingError(f"飞书卡片超过硬限制：{size} bytes / {len(elements)} elements")
        return msg

    @staticmethod
    def request_size(message: RenderedMessage, target: DeliveryTarget, idempotency_key: str = "") -> int:
        content = json.dumps(message.payload["card"], ensure_ascii=False)
        request = {
            "receive_id": _receive_id(target),
            "msg_type": "interactive",
            "content": content,
            "uuid": idempotency_key or "00000000-0000-0000-0000-000000000000",
        }
        return len(json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _receive_id(target: DeliveryTarget) -> str:
    if _session_kind(target) in {"group", "chat", "group_message", "groupmessage"}:
        return target.session_id.rsplit("%", 1)[-1]
    return target.session_id


def _session_kind(target: DeliveryTarget) -> str:
    value = getattr(target.session_type, "value", target.session_type)
    return str(value).lower().replace("_", "").replace(" ", "")


def _walk_blocks(blocks: list[ContentBlock]):
    for block in blocks:
        yield block
        yield from _walk_blocks(block.children)


def _block_text_length(block: ContentBlock) -> int:
    if block.type.lower() in {"image", "video", "audio", "attachment"}:
        return 0
    return len(block.text) + sum(_block_text_length(child) for child in block.children)
