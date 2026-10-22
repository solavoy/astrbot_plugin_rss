from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.request
from contextlib import suppress
from dataclasses import replace
from typing import Any
from urllib.parse import urlsplit

from ..core.errors import PlatformUnsupportedError
from ..core.models import Article, ContentBlock, DeliveryTarget, RenderedMessage
from ..core.workers import BoundedThreadWorkers
from ..normalizers.discourse import clean_cached_article
from .blocks import truncate_content_blocks
from .default import DefaultRenderer, _article_meta

TELEGRAPH_CREATE = "https://api.telegra.ph/createPage"
TELEGRAPH_CONTENT_LIMIT = 64 * 1024


class TelegraphPublisher:
    def __init__(self, config: object, repository: object | None = None) -> None:
        self.config = config
        self.token = str(getattr(config, "telegraph_access_token", "") or "")
        self.author = str(getattr(config, "telegraph_author_name", "") or "")[:128]
        self.repository = repository
        self.cache_enabled = bool(getattr(config, "telegraph_cache_enabled", True))
        self._workers = BoundedThreadWorkers(getattr(config, "max_fetch_concurrency", 5))

    @property
    def timezone(self) -> str:
        return str(getattr(self.config, "timezone", "Asia/Shanghai") or "Asia/Shanghai")

    async def publish(self, article: Article, send_as_media: bool = True) -> str:
        if not self.token:
            raise ValueError("Telegraph access token is not configured")
        article = clean_cached_article(article)
        content = _telegraph_nodes(article.content_blocks, send_as_media=send_as_media)
        content.insert(0, {"tag": "p", "children": [_article_meta(article, timezone=self.timezone)]})
        content.append(
            {
                "tag": "p",
                "children": [
                    "原文地址：",
                    {"tag": "a", "attrs": {"href": article.canonical_url}, "children": [article.canonical_url]},
                ],
            }
        )
        publication_key = hashlib.sha256(
            json.dumps(
                {
                    "format": 4,
                    "send_as_media": send_as_media,
                    "title": article.title,
                    "content_author_name": self.author,
                    "timezone": self.timezone,
                    "content": content,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        cached_fn = getattr(self.repository, "get_telegraph_page", None)
        cache_fn = getattr(self.repository, "set_telegraph_page", None)
        if self.cache_enabled and cached_fn and cache_fn:
            cached = await _maybe_await(cached_fn(article.article_id, publication_key))
            if cached:
                return str(cached)
        payload = {
            "access_token": self.token,
            "title": article.title[:256],
            "content": content,
            "return_content": False,
        }
        if self.author:
            payload["author_name"] = self.author
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        # Telegraph limits content to 64 KiB. Trim whole trailing nodes and add
        # an explicit source link, preserving valid JSON and node structure.
        while (
            len(json.dumps(content, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            > TELEGRAPH_CONTENT_LIMIT
        ):
            if len(content) <= 1:
                raise ValueError("Telegraph content cannot fit the 64 KiB limit")
            if len(content) > 1:
                content.pop(-2)
            else:
                raise ValueError("Telegraph source link cannot fit the 64 KiB limit")
            payload["content"] = content
            encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        result = await self._workers.run(_post_json, encoded)
        if not result.get("ok") or not result.get("result", {}).get("url"):
            raise RuntimeError("Telegraph createPage failed")
        url = result["result"]["url"]
        if self.cache_enabled and cache_fn:
            await _maybe_await(cache_fn(article.article_id, publication_key, url, time.time()))
        return str(url)

    async def close(self) -> None:
        await self._workers.close()


class TelegramRenderer:
    def __init__(self, config: object | None = None, repository: object | None = None) -> None:
        self.config = config
        self.repository = repository
        self.fallback = DefaultRenderer(config)
        self.publisher = TelegraphPublisher(config, repository) if config else None

    async def close(self) -> None:
        if self.publisher:
            await self.publisher.close()

    async def render(
        self,
        articles: list[Article],
        target: DeliveryTarget,
        source_name: str,
        is_list: bool = False,
        batch_id: str = "",
        **kwargs: Any,
    ) -> list[RenderedMessage]:
        enabled = bool(getattr(self.config, "telegraph_access_token", ""))
        ordinary_limit = int(getattr(self.config, "content_truncation_length", 0) or 0)
        ordinary_enabled = (
            not is_list and ordinary_limit > 0 and bool(getattr(self.config, "enable_content_truncation", False))
        )
        render_articles = [clean_cached_article(article) for article in articles]
        render_articles = (
            [
                replace(article, content_blocks=truncate_content_blocks(article.content_blocks, ordinary_limit))
                for article in render_articles
            ]
            if ordinary_enabled
            else render_articles
        )
        if not bool(getattr(self.config, "enable_default_renderer", True)) and not enabled:
            raise PlatformUnsupportedError("Telegram Telegraph and default rendering are disabled")
        if enabled and self.publisher:
            messages = []
            send_as_media = bool(getattr(self.config, "send_as_media", True))
            for article in render_articles:
                try:
                    url = await self.publisher.publish(article, send_as_media=send_as_media)
                    text = (
                        f"{article.title}\n{_article_meta(article, source_name, self.publisher.timezone)}"
                        f"\nTelegraph：{url}\n原文地址：{article.canonical_url}"
                    )
                    messages.append(RenderedMessage("telegram", "text", {"text": text}, [article.article_id], batch_id))
                except Exception:
                    if not bool(getattr(self.config, "enable_default_renderer", True)) or not bool(
                        getattr(self.config, "telegraph_fallback_enabled", True)
                    ):
                        raise PlatformUnsupportedError("Telegraph failed and default rendering is disabled")
                    messages.extend(
                        await self.fallback.render([article], target, source_name, is_list=is_list, batch_id=batch_id)
                    )
            return messages
        if not bool(getattr(self.config, "enable_default_renderer", True)):
            raise PlatformUnsupportedError("Telegram default rendering is disabled")
        messages = await self.fallback.render(
            articles, target, source_name, is_list=is_list, batch_id=batch_id, **kwargs
        )
        for message in messages:
            message.renderer = "telegram"
        return messages


def _telegraph_nodes(blocks: list[ContentBlock], send_as_media: bool = True) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    for block in blocks:
        kind = block.type.lower()
        text = block.text
        if kind in {"text", "paragraph"}:
            children = _inline_children(block, send_as_media=send_as_media)
            if children:
                nodes.append({"tag": "p", "children": children})
        elif kind == "heading":
            nodes.append({"tag": "h3", "children": _inline_children(block, send_as_media=send_as_media)})
        elif kind == "quote":
            nodes.append({"tag": "blockquote", "children": _telegraph_nodes(block.children, send_as_media)})
        elif kind == "code":
            nodes.append({"tag": "pre", "children": [{"tag": "code", "children": [text]}]})
        elif kind == "image" and block.url:
            url = _safe_http_url(block.url)
            if url:
                nodes.append(
                    {"tag": "img", "attrs": {"src": url}}
                    if send_as_media
                    else _media_anchor(url, block.attrs.get("alt") or "查看图片")
                )
        elif kind == "video" and block.url:
            url = _safe_http_url(block.url)
            if url:
                nodes.append(
                    {"tag": "video", "attrs": {"src": url}}
                    if send_as_media
                    else _media_anchor(url, block.attrs.get("title") or "查看视频")
                )
        elif kind in {"audio", "attachment", "link"} and block.url:
            url = _safe_http_url(block.url)
            if url:
                label = block.attrs.get("title") or block.attrs.get("alt") or url
                nodes.append({"tag": "p", "children": [{"tag": "a", "attrs": {"href": url}, "children": [str(label)]}]})
        elif kind == "list":
            tag = "ol" if block.attrs.get("ordered") else "ul"
            items = []
            for child in block.children:
                children = (
                    _telegraph_nodes(child.children, send_as_media)
                    if child.children
                    else [{"tag": "p", "children": [child.text]}]
                )
                items.append({"tag": "li", "children": children})
            nodes.append({"tag": tag, "children": items})
        elif kind == "table":
            for row in block.children:
                nodes.append({"tag": "p", "children": [" | ".join(_block_plain(c) for c in row.children)]})
        elif kind in {"list_item", "table_row", "table_cell"}:
            nodes.extend(_telegraph_nodes(block.children, send_as_media))
        elif block.children:
            nodes.extend(_telegraph_nodes(block.children, send_as_media))
    return nodes or [{"tag": "p", "children": ["（无正文）"]}]


def _inline_children(block: ContentBlock, send_as_media: bool = True) -> list[Any]:
    result: list[Any] = []
    if block.text:
        result.append(block.text)
    for child in block.children:
        node = _inline_node(child, send_as_media=send_as_media)
        if node is not None:
            if isinstance(node, list):
                result.extend(node)
            else:
                result.append(node)
    return result


def _inline_node(block: ContentBlock, send_as_media: bool = True) -> Any:
    kind = block.type.lower()
    if kind == "text":
        return block.text
    if kind == "image" and block.url:
        url = _safe_http_url(block.url)
        if not url:
            return None
        return {"tag": "img", "attrs": {"src": url}} if send_as_media else _media_anchor(url, "查看图片")
    if kind == "link" and block.url:
        url = _safe_http_url(block.url)
        if not url:
            return block.text or None
        return {
            "tag": "a",
            "attrs": {"href": url},
            "children": _inline_children(block, send_as_media=send_as_media) or [block.text or url],
        }
    if kind in {"video", "audio"} and block.url:
        url = _safe_http_url(block.url)
        if not url:
            return None
        return (
            (
                {"tag": "video", "attrs": {"src": url}}
                if send_as_media
                else _media_anchor(url, block.attrs.get("title") or "查看视频")
            )
            if kind == "video"
            else ({"tag": "a", "attrs": {"href": url}, "children": [block.attrs.get("title") or url]})
        )
    if block.children:
        return _inline_children(block, send_as_media=send_as_media)
    return block.text or None


def _block_plain(block: ContentBlock) -> str:
    return block.text + "".join(_block_plain(child) for child in block.children)


def _media_anchor(url: str, label: str) -> dict[str, Any]:
    return {"tag": "a", "attrs": {"href": url}, "children": [str(label or url)]}


def _safe_http_url(url: str) -> str:
    try:
        parsed = urlsplit(url)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            return ""
        return url
    except ValueError:
        return ""


def _post_json(data: bytes) -> dict[str, Any]:
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    request = urllib.request.Request(
        TELEGRAPH_CREATE, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        response = urllib.request.build_opener(NoRedirect).open(request, timeout=15)
    except urllib.error.HTTPError as exc:
        with suppress(OSError):
            exc.close()
        raise
    with response:
        return json.loads(response.read(256_000).decode("utf-8"))


async def _maybe_await(value: Any) -> Any:
    return await value if hasattr(value, "__await__") else value
