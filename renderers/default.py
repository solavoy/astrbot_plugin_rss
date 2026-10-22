from __future__ import annotations

from typing import Any

from ..core.errors import RenderingError
from ..core.models import Article, ContentBlock, DeliveryTarget, RenderedMessage
from ..normalizers.discourse import clean_cached_article
from .blocks import truncate_content_blocks
from .time_format import format_published


def block_text(block: ContentBlock) -> str:
    kind = block.type.lower()
    if kind in {"text", "paragraph", "heading", "quote", "code"}:
        text = block.text or "".join(block_text(c) for c in block.children)
        if kind == "heading":
            return f"\n{text}\n"
        if kind == "quote":
            return "\n".join(f"> {line}" for line in text.splitlines()) + "\n"
        if kind == "code":
            lang = str(block.attrs.get("language", ""))
            return f"\n```{lang}\n{text}\n```\n"
        return text + ("\n\n" if kind == "paragraph" else "")
    if kind in {"image", "video", "audio", "attachment", "link"}:
        label = (
            "".join(block_text(c) for c in block.children).strip()
            or block.text
            or block.attrs.get("alt")
            or block.attrs.get("title")
            or kind.capitalize()
        )
        url = block.url
        if not url:
            return ""
        return f"[{label}]({url})"
    if kind == "list":
        ordered = bool(block.attrs.get("ordered"))
        return (
            "\n".join(
                f"{i}. {block_text(c)}" if ordered else f"• {block_text(c)}" for i, c in enumerate(block.children, 1)
            )
            + "\n"
        )
    if kind == "table":
        return "\n".join(" | ".join(block_text(c) for c in row.children) for row in block.children) + "\n"
    return block.text + "".join(block_text(c) for c in block.children)


def article_text(article: Article, timezone: str = "Asia/Shanghai") -> str:
    article = clean_cached_article(article)
    chunks = [f"**{article.title}**", _article_meta(article, timezone=timezone)]
    chunks.extend(block_text(b) for b in article.content_blocks)
    chunks.extend([f"\n原文地址：[{article.canonical_url}]({article.canonical_url})"])
    return "\n\n".join(x for x in chunks if x)


def _published(article: Article, timezone: str = "Asia/Shanghai") -> str:
    timestamp = article.published_at
    if timestamp is None:
        timestamp = article.updated_at
    if timestamp is None:
        timestamp = article.first_seen_at
    return format_published(timestamp, timezone)


class DefaultRenderer:
    """Ordered plain-text/Markdown fallback with a configurable hard text cap."""

    def __init__(self, config: object | None = None) -> None:
        self.config = config

    async def render(
        self,
        articles: list[Article],
        target: DeliveryTarget,
        source_name: str,
        is_list: bool = False,
        batch_id: str = "",
        **kwargs: object,
    ) -> list[RenderedMessage]:
        # Telegram is the only platform for which this fallback uses a known
        # text hard cap. Unknown adapters own their transport limit.
        hard_limit = 4096 if "telegram" in target.platform.lower() else None
        ordinary_truncate = not is_list and bool(getattr(self.config, "enable_content_truncation", False))
        ordinary_length = int(getattr(self.config, "content_truncation_length", 0) or 0)
        timezone = str(getattr(self.config, "timezone", "Asia/Shanghai") or "Asia/Shanghai")
        send_as_media = bool(getattr(self.config, "send_as_media", True))
        payloads: list[RenderedMessage] = []
        current: list[dict[str, Any]] = []
        ids: list[str] = []
        for article in articles:
            article = clean_cached_article(article)
            blocks = article.content_blocks
            if ordinary_truncate and ordinary_length > 0:
                blocks = _truncate_blocks(blocks, ordinary_length)
            parts: list[dict[str, Any]] = [{"type": "text", "text": _article_heading(article, source_name, timezone)}]
            for block in blocks:
                parts.extend(_block_parts_many(block, send_as_media=send_as_media))
            parts.append({"type": "text", "text": f"\n原文地址：[{article.canonical_url}]({article.canonical_url})"})
            parts = [p for p in parts if p.get("text") or p.get("url")]
            article_has_video = send_as_media and _contains_type(blocks, "video")
            if article_has_video and ids:
                payloads.append(RenderedMessage("default", "parts", {"parts": current}, ids, batch_id))
                current, ids = [], []
            candidate = current + parts
            if hard_limit and _visible_length(candidate) > hard_limit and ids:
                payloads.append(RenderedMessage("default", "parts", {"parts": current}, ids, batch_id))
                current, ids = [], []
                candidate = current + parts
            if hard_limit and _visible_length(candidate) > hard_limit:
                parts = _hard_fit_article(
                    article,
                    source_name,
                    hard_limit - _visible_length(current),
                    send_as_media=send_as_media,
                    timezone=timezone,
                )
            current.extend(parts)
            ids.append(article.article_id)
            if article_has_video:
                payloads.append(RenderedMessage("default", "parts", {"parts": current}, ids, batch_id))
                current, ids = [], []
        if ids:
            payloads.append(RenderedMessage("default", "parts", {"parts": current}, ids, batch_id))
        return _split_video_operations(payloads)


def _contains_type(blocks: list[ContentBlock], wanted: str) -> bool:
    return any(block.type.lower() == wanted or _contains_type(block.children, wanted) for block in blocks)


def _split_video_operations(messages: list[RenderedMessage]) -> list[RenderedMessage]:
    result: list[RenderedMessage] = []
    for message in messages:
        if message.message_type != "parts":
            result.append(message)
            continue
        current: list[dict[str, Any]] = []
        videos: list[dict[str, Any]] = []
        parts = message.payload.get("parts", [])
        for part in parts:
            if part.get("type") == "video" and part.get("url"):
                videos.append(part)
            else:
                current.append(part)
        if current:
            result.append(
                RenderedMessage(message.renderer, "parts", {"parts": current}, message.article_ids, message.batch_id)
            )
        result.extend(
            RenderedMessage(
                message.renderer,
                "video",
                {"url": part["url"], "alt": part.get("alt", "")},
                message.article_ids,
                message.batch_id,
            )
            for part in videos
        )
    return result


def _article_meta(article: Article, source_name: str = "", timezone: str = "Asia/Shanghai") -> str:
    feed_title = str(article.metadata.get("feed_title") or source_name).strip()
    values = [f"作者：{article.author.strip() or '未知作者'}"]
    if feed_title:
        values.append(feed_title)
    published = _published(article, timezone)
    if published:
        values.append(published)
    return " | ".join(values)


def _article_heading(article: Article, source_name: str = "", timezone: str = "Asia/Shanghai") -> str:
    return f"\n\n**{article.title}**\n{_article_meta(article, source_name, timezone)}\n\n"


def _block_parts(block: ContentBlock, send_as_media: bool = True) -> dict[str, Any]:
    if block.type.lower() in {"image", "video", "audio", "attachment"}:
        if not send_as_media:
            label = block.attrs.get("alt") or block.attrs.get("title") or block.type.capitalize()
            return (
                _text_part(f"[{label}]({block.url})", block.type.lower()) if block.url else _text_part("", block.type)
            )
        return {
            "type": block.type.lower(),
            "url": block.url,
            "alt": block.attrs.get("alt") or block.attrs.get("title") or "",
        }
    return {"type": "text", "text": block_text(block), "block_type": block.type, "structured": bool(block.children)}


def _block_parts_many(block: ContentBlock, send_as_media: bool = True) -> list[dict[str, Any]]:
    if block.type.lower() in {"image", "video", "audio", "attachment"}:
        return [_block_parts(block, send_as_media=send_as_media)]
    if block.type.lower() == "link":
        return [
            {
                "type": "link",
                "text": "".join(block_text(c) for c in block.children)
                or block.text
                or block.attrs.get("title")
                or "Link",
                "url": block.url,
                "block_type": "link",
                "structured": True,
            }
        ]
    if block.children:
        kind = block.type.lower()
        if kind == "list":
            result: list[dict[str, Any]] = []
            ordered = bool(block.attrs.get("ordered"))
            for index, child in enumerate(block.children, 1):
                result.append(_text_part(f"{index}. " if ordered else "• ", "list"))
                result.extend(_block_parts_many(child, send_as_media=send_as_media))
                result.append(_text_part("\n", "list"))
            return result
        if kind == "table":
            result = []
            for row_index, row in enumerate(block.children):
                if row_index:
                    result.append(_text_part("\n", "table"))
                for cell_index, cell in enumerate(row.children):
                    if cell_index:
                        result.append(_text_part(" | ", "table"))
                    result.extend(_block_parts_many(cell, send_as_media=send_as_media))
            return result
        result: list[dict[str, Any]] = []
        if block.text:
            result.append(_text_part(block.text, kind, structured=True))
        for child in block.children:
            child_parts = _block_parts_many(child, send_as_media=send_as_media)
            if kind == "quote":
                child_parts = [_quote_part(part) for part in child_parts]
            result.extend(child_parts)
        if kind == "paragraph":
            result.append(_text_part("\n\n", kind, structured=True))
        elif kind == "heading":
            level = min(6, max(1, int(block.attrs.get("level", 2))))
            result.insert(0, _text_part("#" * level + " ", kind, structured=True))
            result.append(_text_part("\n", kind, structured=True))
        elif kind == "quote":
            result.append(_text_part("\n", kind, structured=True))
        elif kind == "code":
            language = str(block.attrs.get("language", ""))
            result.insert(0, _text_part(f"```{language}\n", kind, structured=True))
            result.append(_text_part("\n```\n", kind, structured=True))
        return result
    return [_block_parts(block)]


def _text_part(text: str, kind: str, structured: bool = False) -> dict[str, Any]:
    return {"type": "text", "text": text, "block_type": kind, "structured": structured}


def _quote_part(part: dict[str, Any]) -> dict[str, Any]:
    if part.get("type") != "text":
        return part
    text = str(part.get("text", ""))
    prefix = "> "
    lines = text.splitlines(keepends=True)
    quoted = "".join(prefix + line if line.strip() else line for line in lines)
    if text and not lines:
        quoted = prefix + text
    return {**part, "text": quoted}


def _utf16_length(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _visible_length(parts: list[dict[str, Any]]) -> int:
    total = 0
    for part in parts:
        text = str(part.get("text", ""))
        url = str(part.get("url", ""))
        if part.get("type") == "text":
            total += _utf16_length(text)
        elif part.get("type") in {"image", "video"}:
            continue
        elif url:
            label = part.get("alt") or text or str(part.get("type", "media")).capitalize()
            total += _utf16_length(f"[{label}]({url})")
    return total


def _clip_utf16(value: str, limit: int) -> str:
    if limit <= 0:
        return ""
    used = 0
    result = []
    for character in value:
        width = 2 if ord(character) > 0xFFFF else 1
        if used + width > limit:
            break
        result.append(character)
        used += width
    return "".join(result)


def _truncate_blocks(blocks: list[ContentBlock], limit: int) -> list[ContentBlock]:
    return truncate_content_blocks(blocks, limit)


def _hard_fit_article(
    article: Article, source_name: str, budget: int, send_as_media: bool = True, timezone: str = "Asia/Shanghai"
) -> list[dict[str, Any]]:
    heading = {"type": "text", "text": _article_heading(article, source_name, timezone)}
    link = {"type": "text", "text": f"\n原文地址：[{article.canonical_url}]({article.canonical_url})"}
    fixed = _utf16_length(heading["text"]) + _utf16_length(link["text"])
    if fixed >= budget:
        # Preserve source/title/link; if those alone exceed known transport limit,
        # surface a rendering failure rather than silently lose the source URL.
        minimal = (
            f"{article.title}\n{_article_meta(article, source_name, timezone)}"
            f"\n原文地址：[{article.canonical_url}]({article.canonical_url})"
        )
        if _utf16_length(minimal) > budget:
            raise RenderingError("标题和原文链接超过平台硬限制")
        return [{"type": "text", "text": minimal}]
    remaining = max(0, budget - fixed)
    pieces: list[dict[str, Any]] = [heading]
    for block in article.content_blocks:
        for part in _block_parts_many(block, send_as_media=send_as_media):
            if part["type"] in {"image", "video"}:
                pieces.append(part)
                continue
            if part["type"] == "text":
                text = str(part.get("text", ""))
                text_size = _utf16_length(text)
                if text_size <= remaining:
                    pieces.append(part)
                    remaining -= text_size
                    continue
                if remaining and part.get("block_type") in {"text", "paragraph"} and not part.get("structured"):
                    clipped = _clip_utf16(text, max(0, remaining - 1)).rstrip()
                    if " " in clipped:
                        clipped = clipped.rsplit(" ", 1)[0]
                    if clipped:
                        pieces.append({**part, "text": clipped + "…"})
                remaining = 0
                continue
            # Other media types fall back to links and count toward the text cap.
            url = str(part.get("url", ""))
            label = str(part.get("alt") or part.get("text") or "媒体")
            media_link = f"[{label}]({url})" if url else ""
            media_size = _utf16_length(media_link)
            if media_size <= remaining:
                pieces.append({"type": "text", "text": media_link})
                remaining -= media_size
            else:
                remaining = 0
                break
    pieces.append(link)
    return pieces
