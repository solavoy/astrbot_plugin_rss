from __future__ import annotations

from ..core.errors import RenderingError
from ..core.models import Article, DeliveryTarget, RenderedMessage
from ..normalizers.markdown_content import article_markdown, clean_cached_article, markdown_tokens
from .time_format import format_published


def _published(article: Article, timezone: str = "Asia/Shanghai") -> str:
    timestamp = article.published_at
    if timestamp is None:
        timestamp = article.updated_at
    if timestamp is None:
        timestamp = article.first_seen_at
    return format_published(timestamp, timezone)


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
    return f"**{article.title}**\n{_article_meta(article, source_name, timezone)}"


def article_text(article: Article, timezone: str = "Asia/Shanghai") -> str:
    """Return the article as Markdown without rebuilding it from content nodes."""
    article = clean_cached_article(article)
    body = article_markdown(article).strip()
    chunks = [f"**{article.title}**", _article_meta(article, timezone=timezone), body]
    chunks.append(f"原文地址：[{article.canonical_url}]({article.canonical_url})")
    return "\n\n".join(chunk for chunk in chunks if chunk)


def _utf16_length(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _clip_markdown(value: str, limit: int, *, utf16: bool = False) -> str:
    """Clip at complete Markdown block boundaries, with safe plain-text clipping.

    Structured blocks (links, images, emphasis and code) are retained whole or
    omitted. This avoids cutting a URL, fence or formatting span in half.
    """
    measure = _utf16_length if utf16 else len
    if limit <= 0 or not value:
        return ""
    if measure(value) <= limit:
        return value
    suffix = "…"
    lines = value.splitlines(keepends=True)
    ranges: list[tuple[int, int]] = []
    level = 0
    for token in markdown_tokens(value):
        mapping = token.map
        if level == 0 and mapping and (token.nesting == 1 or token.type in {"fence", "code_block", "html_block", "hr"}):
            start, end = int(mapping[0]), int(mapping[1])
            if end > start and (not ranges or ranges[-1][:2] != (start, end)):
                ranges.append((start, end))
        level += token.nesting

    if not ranges:
        return ""
    kept: list[str] = []
    used = 0
    for start, end in ranges:
        block = "".join(lines[start:end])
        block_size = measure(block)
        if used + block_size <= limit:
            kept.append(block)
            used += block_size
            continue
        remaining = limit - used
        body_budget = max(0, remaining - measure(suffix))
        plain_prefix = _clip_plain_block(block, body_budget, measure)
        if not plain_prefix:
            plain_prefix = _clip_visible_prose(block, body_budget, measure)
        if plain_prefix:
            kept.append(plain_prefix.rstrip() + suffix)
        elif remaining >= measure(suffix):
            kept.append(suffix)
        break
    result = "".join(kept).rstrip()
    if not result and limit >= measure(suffix):
        return suffix
    return result


def _clip_plain_block(block: str, limit: int, measure) -> str:
    """Return a source prefix only when markdown-it identifies plain text."""
    if limit <= 0:
        return ""
    tokens = markdown_tokens(block)
    inline_tokens = [token for token in tokens if token.type == "inline"]
    if not inline_tokens:
        return ""
    if any(
        child.type not in {"text", "softbreak", "hardbreak"}
        for inline in inline_tokens
        for child in (inline.children or [])
    ):
        return ""
    result: list[str] = []
    used = 0
    for character in block:
        width = measure(character)
        if used + width > limit:
            break
        result.append(character)
        used += width
    return "".join(result)


def _clip_visible_prose(block: str, limit: int, measure) -> str:
    """Safely degrade a clipped structured block to tokenizer-derived prose."""
    if limit <= 0:
        return ""
    pieces: list[str] = []
    for token in markdown_tokens(block):
        if token.type != "inline":
            continue
        for child in token.children or []:
            if child.type in {"text", "softbreak", "hardbreak"}:
                pieces.append(child.content if child.type == "text" else " ")
            elif child.type == "image" and child.content:
                pieces.append(child.content)
            # Inline code and all URL destinations are deliberately omitted.
    visible = "".join(pieces)
    result: list[str] = []
    used = 0
    for character in visible:
        width = measure(character)
        if used + width > limit:
            break
        result.append(character)
        used += width
    return "".join(result)


class DefaultRenderer:
    """Send normalized article Markdown as one plain component per article."""

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
        # Telegram's text limit is measured in UTF-16 units. Other adapters
        # own their text limit and receive the original Markdown unchanged.
        hard_limit = 4096 if "telegram" in target.platform.lower() else None
        ordinary_truncate = not is_list and bool(getattr(self.config, "enable_content_truncation", False))
        ordinary_length = int(getattr(self.config, "content_truncation_length", 0) or 0)
        timezone = str(getattr(self.config, "timezone", "Asia/Shanghai") or "Asia/Shanghai")

        messages: list[RenderedMessage] = []
        current = ""
        ids: list[str] = []
        for original in articles:
            article = clean_cached_article(original)
            body = article_markdown(article).strip()
            if ordinary_truncate and ordinary_length > 0:
                body = _clip_markdown(body, ordinary_length)
            text = _format_article(article, source_name, timezone, body)

            if hard_limit and _utf16_length(text) > hard_limit:
                text = _fit_article(article, source_name, timezone, body, hard_limit)
            candidate = f"{current}\n\n{text}" if current else text
            if hard_limit and ids and _utf16_length(candidate) > hard_limit:
                messages.append(_message(current, ids, batch_id))
                current, ids = "", []
                candidate = text
            if hard_limit and _utf16_length(candidate) > hard_limit:
                candidate = _fit_article(article, source_name, timezone, body, hard_limit)
            current = candidate
            ids.append(article.article_id)

        if ids:
            messages.append(_message(current, ids, batch_id))
        return messages


def _format_article(article: Article, source_name: str, timezone: str, body: str) -> str:
    chunks = [_article_heading(article, source_name, timezone), body]
    chunks.append(f"原文地址：[{article.canonical_url}]({article.canonical_url})")
    return "\n\n".join(chunk for chunk in chunks if chunk)


def _fit_article(article: Article, source_name: str, timezone: str, body: str, limit: int) -> str:
    header = _article_heading(article, source_name, timezone)
    footer = f"原文地址：[{article.canonical_url}]({article.canonical_url})"
    fixed = _utf16_length(header) + _utf16_length(footer) + 4
    if fixed > limit:
        minimal = f"{article.title}\n{_article_meta(article, source_name, timezone)}\n{footer}"
        if _utf16_length(minimal) > limit:
            raise RenderingError("标题和原文链接超过平台硬限制")
        return minimal
    body_budget = max(0, limit - fixed)
    clipped = _clip_markdown(body, body_budget, utf16=True)
    return "\n\n".join(chunk for chunk in (header, clipped, footer) if chunk)


def _message(text: str, article_ids: list[str], batch_id: str) -> RenderedMessage:
    return RenderedMessage("default", "parts", {"parts": [{"type": "text", "text": text}]}, article_ids, batch_id)
