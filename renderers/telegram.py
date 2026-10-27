from __future__ import annotations

import hashlib
import json
import re
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
from ..normalizers.markdown_content import (
    article_markdown,
    article_media,
    clean_cached_article,
    markdown_tokens,
)
from .default import DefaultRenderer, _article_meta, _clip_markdown

TELEGRAPH_CREATE = "https://api.telegra.ph/createPage"
TELEGRAPH_CONTENT_LIMIT = 64 * 1024
_TELEGRAPH_TAGS = frozenset(
    {
        "a",
        "aside",
        "b",
        "blockquote",
        "br",
        "code",
        "em",
        "figcaption",
        "figure",
        "h3",
        "h4",
        "hr",
        "i",
        "iframe",
        "img",
        "li",
        "ol",
        "p",
        "pre",
        "s",
        "strong",
        "u",
        "ul",
        "video",
    }
)
_TELEGRAPH_BLOCK_TAGS = {
    "paragraph_open": "p",
    "blockquote_open": "blockquote",
    "bullet_list_open": "ul",
    "ordered_list_open": "ol",
    "list_item_open": "li",
    "tr_open": "p",
}
_TELEGRAPH_INLINE_TAGS = {
    "strong_open": "strong",
    "em_open": "em",
    "s_open": "s",
    "link_open": "a",
}
_CJK_STRONG_BOUNDARY = re.compile(r"(?<!\\)\*\*(?P<body>[^*\n]+?[：:])\*\*(?P<suffix>[\u3400-\u9fff])")


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
        content = _telegraph_markdown_nodes(
            article_markdown(article), article_media(article), send_as_media=send_as_media
        )
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
                    "format": 5,
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
        encoded = _fit_telegraph_content(payload, content)
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
                replace(
                    article,
                    content_blocks=[
                        ContentBlock("markdown", text=_clip_markdown(article_markdown(article), ordinary_limit))
                    ],
                )
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


def _telegraph_markdown_nodes(
    markdown: str, media: list[ContentBlock] | None = None, *, send_as_media: bool = True
) -> list[dict[str, Any]]:
    tokens = _telegraph_tokens(markdown)
    media_by_url = {
        _safe_http_url(block.url): block
        for block in media or []
        if block.type.lower() in {"image", "video", "audio", "attachment"} and _safe_http_url(block.url)
    }
    used_media: set[str] = set()
    root: list[Any] = []
    stack: list[tuple[str, list[Any] | None, dict[str, Any] | None, int]] = []

    def current() -> list[Any]:
        for _open_type, children, _node, _cells in reversed(stack):
            if children is not None:
                return children
        return root

    def append_inline(inline_tokens: list[Any]) -> None:
        inline_stack: list[tuple[str, list[Any] | None, dict[str, Any] | None]] = []

        def inline_current() -> list[Any]:
            for _open_type, children, _node in reversed(inline_stack):
                if children is not None:
                    return children
            return current()

        for token in inline_tokens:
            token_type = token.type
            if token_type in _TELEGRAPH_INLINE_TAGS:
                tag = _TELEGRAPH_INLINE_TAGS[token_type]
                attrs: dict[str, str] = {}
                if token_type == "link_open":
                    href = _safe_http_url(str((token.attrs or {}).get("href", "")))
                    if href:
                        media_block = media_by_url.get(href)
                        if media_block and media_block.type.lower() == "video":
                            used_media.add(href)
                            if send_as_media:
                                append_node = {"tag": "video", "attrs": {"src": href}}
                                inline_current().append(append_node)
                                inline_stack.append(("link_open", None, None))
                                continue
                            attrs = {"href": href}
                        elif media_block and media_block.type.lower() == "image":
                            used_media.add(href)
                            if send_as_media:
                                inline_current().append({"tag": "img", "attrs": {"src": href}})
                                inline_stack.append(("link_open", None, None))
                                continue
                            attrs = {"href": href}
                        else:
                            attrs = {"href": href}
                    else:
                        # Preserve link labels while discarding unsafe targets.
                        inline_stack.append(("link_open", None, None))
                        continue
                node = {"tag": tag, "children": []}
                if attrs:
                    node["attrs"] = attrs
                inline_current().append(node)
                inline_stack.append((token_type, node["children"], node))
            elif token_type in {"strong_close", "em_close", "s_close", "link_close"}:
                expected = token_type.removesuffix("_close") + "_open"
                for index in range(len(inline_stack) - 1, -1, -1):
                    if inline_stack[index][0] == expected:
                        del inline_stack[index:]
                        break
            elif token_type == "image":
                src = _safe_http_url(str((token.attrs or {}).get("src", "")))
                if not src:
                    if token.content:
                        inline_current().append(token.content)
                    continue
                used_media.add(src)
                if send_as_media:
                    inline_current().append({"tag": "img", "attrs": {"src": src}})
                else:
                    inline_current().append(_media_anchor(src, token.content or "查看图片"))
            elif token_type == "code_inline":
                inline_current().append({"tag": "code", "children": [token.content]})
            elif token_type in {"hardbreak", "softbreak"}:
                inline_current().append({"tag": "br"})
            elif token_type == "text":
                if token.content:
                    inline_current().append(token.content)
            elif token_type not in {"html_inline", "html_block"} and token.content:
                inline_current().append(token.content)

    for token in tokens:
        token_type = token.type
        if token_type == "inline":
            append_inline(token.children or [])
            continue
        if token_type in {"fence", "code_block"}:
            current().append({"tag": "pre", "children": [{"tag": "code", "children": [token.content]}]})
            continue
        if token_type == "hr":
            current().append({"tag": "hr"})
            continue
        if token_type.endswith("_open") and token.nesting == 1:
            if token_type == "heading_open":
                level = int(token.tag[1:]) if token.tag.startswith("h") and token.tag[1:].isdigit() else 3
                tag = "h3" if level <= 2 else "h4"
            elif token_type in _TELEGRAPH_BLOCK_TAGS:
                tag = _TELEGRAPH_BLOCK_TAGS[token_type]
            else:
                tag = ""
            if token_type in {"th_open", "td_open"}:
                for index in range(len(stack) - 1, -1, -1):
                    open_type, children, node, cells = stack[index]
                    if open_type == "tr_open":
                        if cells:
                            current().append(" | ")
                        stack[index] = (open_type, children, node, cells + 1)
                        break
            if tag:
                node: dict[str, Any] = {"tag": tag, "children": []}
                current().append(node)
                stack.append((token_type, node["children"], node, 0))
            else:
                stack.append((token_type, None, None, 0))
            continue
        if token_type.endswith("_close") and token.nesting == -1:
            expected = token_type.removesuffix("_close") + "_open"
            for index in range(len(stack) - 1, -1, -1):
                if stack[index][0] == expected:
                    del stack[index:]
                    break

    for block in media or []:
        kind = block.type.lower()
        url = _safe_http_url(block.url)
        if not url or url in used_media or kind not in {"image", "video", "audio", "attachment"}:
            continue
        used_media.add(url)
        if kind == "image":
            root.append(
                {"tag": "img", "attrs": {"src": url}}
                if send_as_media
                else _media_anchor(url, block.attrs.get("alt") or "查看图片")
            )
        elif kind == "video" and send_as_media:
            root.append({"tag": "video", "attrs": {"src": url}})
        else:
            root.append(_media_anchor(url, block.attrs.get("title") or block.attrs.get("alt") or url))
    nodes = _as_node_list(root)
    return nodes or [{"tag": "p", "children": ["（无正文）"]}]


def _as_node_list(values: list[Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for value in values:
        if isinstance(value, str):
            if value:
                result.append({"tag": "p", "children": [value]})
        elif isinstance(value, dict):
            node = _sanitize_node(value)
            if node:
                result.append(node)
    return result


def _sanitize_node(value: dict[str, Any]) -> dict[str, Any] | None:
    tag = value.get("tag")
    if tag not in _TELEGRAPH_TAGS:
        return None
    node: dict[str, Any] = {"tag": tag}
    attrs = value.get("attrs") or {}
    if tag == "a":
        href = _safe_http_url(str(attrs.get("href", "")))
        if href:
            node["attrs"] = {"href": href}
    elif tag in {"img", "video", "iframe"}:
        src = _safe_http_url(str(attrs.get("src", "")))
        if not src:
            return None
        node["attrs"] = {"src": src}
    if tag not in {"hr", "br", "img", "video", "iframe"}:
        children = []
        for child in value.get("children", []):
            if isinstance(child, str):
                if child:
                    children.append(child)
            elif isinstance(child, dict):
                clean = _sanitize_node(child)
                if clean:
                    children.append(clean)
        node["children"] = children
    if tag == "a" and "attrs" not in node:
        return None
    return node


def _fit_telegraph_content(payload: dict[str, Any], content: list[dict[str, Any]]) -> bytes:
    def encoded_content_size() -> int:
        return len(json.dumps(content, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

    while encoded_content_size() > TELEGRAPH_CONTENT_LIMIT:
        # Keep the article heading and the original-source footer in place.
        if len(content) > 3:
            content.pop(-2)
            continue
        if len(content) != 3:
            raise ValueError("Telegraph source link cannot fit the 64 KiB limit")
        body = content[1]
        if not _trim_node_to_fit(content, 1, TELEGRAPH_CONTENT_LIMIT):
            content.pop(1)
        if len(content) == 2:
            raise ValueError("Telegraph source link cannot fit the 64 KiB limit")
        if body is content[1] and encoded_content_size() > TELEGRAPH_CONTENT_LIMIT:
            raise ValueError("Telegraph content cannot fit the 64 KiB limit")
    payload["content"] = content
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _trim_node_to_fit(container: list[dict[str, Any]], node_index: int, limit: int) -> bool:
    root = container[node_index]
    while len(json.dumps(container, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > limit:
        path = _last_text_path(root)
        if path is None:
            return False
        parent, child_index = path
        value = parent["children"][child_index]
        if not value:
            parent["children"].pop(child_index)
            continue
        low, high, best = 0, len(value) - 1, -1
        while low <= high:
            middle = (low + high) // 2
            parent["children"][child_index] = value[:middle]
            size = len(json.dumps(container, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            if size <= limit:
                best = middle
                low = middle + 1
            else:
                high = middle - 1
        if best > 0:
            parent["children"][child_index] = value[:best]
            return True
        parent["children"].pop(child_index)
    return True


def _last_text_path(node: dict[str, Any]) -> tuple[dict[str, Any], int] | None:
    children = node.get("children", [])
    for index in range(len(children) - 1, -1, -1):
        child = children[index]
        if isinstance(child, str):
            return node, index
        if isinstance(child, dict):
            path = _last_text_path(child)
            if path:
                return path
    return None


def _telegraph_tokens(markdown: str) -> list[Any]:
    """Apply a narrow CJK delimiter repair through markdown-it's token parser.

    CommonMark treats `**word：**是` as literal text because its delimiter
    flanking rules reject this punctuation boundary. HTML-to-Markdown output
    uses that form for adjacent strong text. We add a temporary separator only
    while markdown-it tokenizes the affected plain-text token, then remove that
    separator from the resulting AST. The stored Markdown and code tokens are
    untouched.
    """
    tokens = markdown_tokens(markdown)
    for inline in (token for token in tokens if token.type == "inline"):
        raw = inline.content or ""
        code_ranges = _inline_code_ranges(raw, inline.children or [])
        url_ranges = [match.span() for match in re.finditer(r"(?i)(?:https?://|www\.)[^\s<>\"']+", raw)]
        new_children: list[Any] = []
        source_cursor = 0
        for child in inline.children or []:
            if child.type != "text" or not child.content:
                new_children.append(child)
                continue
            candidate_start = raw.find(child.content, source_cursor)
            if candidate_start < 0:
                new_children.append(child)
                continue
            candidate_end = candidate_start + len(child.content)
            source_cursor = candidate_end
            if _CJK_STRONG_BOUNDARY.search(child.content):
                repaired = _repair_cjk_strong_text(child.content, candidate_start, raw, (*code_ranges, *url_ranges))
                new_children.extend(repaired if repaired else [child])
            else:
                new_children.append(child)
        inline.children = new_children
    return tokens


def _inline_code_ranges(raw: str, tokens: list[Any]) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    cursor = 0
    for token in tokens:
        if token.type != "code_inline":
            continue
        markup = token.markup or "`"
        snippet = f"{markup}{token.content}{markup}"
        start = raw.find(snippet, cursor)
        if start >= 0:
            end = start + len(snippet)
            ranges.append((start, end))
            cursor = end
    return ranges


def _repair_cjk_strong_text(
    value: str, raw_offset: int, raw: str, excluded_ranges: tuple[tuple[int, int], ...] | list[tuple[int, int]]
) -> list[Any]:
    parts: list[str] = []
    cursor = 0
    count = 0
    for match in _CJK_STRONG_BOUNDARY.finditer(value):
        source_start = raw_offset + match.start()
        source_end = raw_offset + match.end()
        if raw[source_start:source_end] != match.group(0) or any(
            start < source_end and end > source_start for start, end in excluded_ranges
        ):
            continue
        parts.append(value[cursor : match.start()])
        parts.append(match.group(0)[:-1] + " " + match.group("suffix"))
        cursor = match.end()
        count += 1
    if not count:
        return []
    parts.append(value[cursor:])
    repaired = "".join(parts)
    inline = next((token for token in markdown_tokens(repaired) if token.type == "inline"), None)
    if inline is None:
        return []
    children = list(inline.children or [])
    repaired_close_count = count
    for index, token in enumerate(children):
        if repaired_close_count and token.type == "strong_close" and token.markup == "**":
            next_index = index + 1
            if next_index < len(children) and children[next_index].type == "text":
                following = children[next_index]
                if following.content.startswith(" ") and len(following.content) > 1:
                    following.content = following.content[1:]
                elif following.content == " ":
                    following.content = ""
            repaired_close_count -= 1
    return [token for token in children if token.type != "text" or token.content]


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
