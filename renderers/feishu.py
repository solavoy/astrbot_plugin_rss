from __future__ import annotations

import json
import math
import re
from typing import Any, Awaitable, Callable

from ..core.errors import RenderingError
from ..core.models import Article, ContentBlock, DeliveryTarget, RenderedMessage
from ..normalizers.markdown_content import (
    article_markdown,
    article_media,
    clean_cached_article,
    html_to_markdown,
    markdown_tokens,
)
from .default import _article_meta, _clip_markdown

HARD_LIMIT = 30 * 1024
MAX_ELEMENTS = 200
_IMAGE_PLACEHOLDER = re.compile(r"\ufff0FEISHUIMG[0-9]+\ufff1")
_HTML_TAG = re.compile(r"</?[A-Za-z][A-Za-z0-9:-]*\b[^>]*>")


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
    return f"{max(1, math.floor(width * scale))}px {max(1, math.floor(height * scale))}px"


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


def _token_children(tokens: list[Any]):
    for token in tokens:
        if token.type == "inline":
            yield from token.children or []


def _count_strong_close(value: str) -> int:
    return sum(token.type == "strong_close" for token in _token_children(markdown_tokens(value)))


def _is_escaped(value: str, index: int) -> bool:
    backslashes = 0
    index -= 1
    while index >= 0 and value[index] == "\\":
        backslashes += 1
        index -= 1
    return backslashes % 2 == 1


def _autolink_ranges(value: str, children: list[Any]) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for child in children:
        if child.type != "link_open":
            continue
        href = str((child.attrs or {}).get("href") or "")
        candidates = [href]
        if href.lower().startswith("mailto:"):
            candidates.append(href[7:])
        for candidate in candidates:
            if not candidate:
                continue
            literal = f"<{candidate}>"
            start = value.find(literal)
            if start >= 0:
                ranges.append((start, start + len(literal)))
                break
    return _merge_ranges(ranges)


def _contains_raw_html(value: str, children: list[Any]) -> bool:
    protected = _autolink_ranges(value, children)
    return any(
        not any(left <= match.start() and match.end() <= right for left, right in protected)
        for match in _HTML_TAG.finditer(value)
    )


def _fix_strong_boundaries(value: str, children: list[Any] | None) -> str:
    """Add Feishu-friendly spaces after strong closes outside protected syntax.

    Parsed code, link, image and URL source ranges are masked before examining
    delimiter pairs, so their contents cannot be mistaken for emphasis.
    """
    children = children or []
    protected_ranges = _protected_inline_ranges(value, children)
    replacements: dict[str, str] = {}
    masked = value
    for index, (start, end) in reversed(list(enumerate(protected_ranges))):
        marker = f"\ue000FEISHUPROTECTED{index}\ue001"
        replacements[marker] = value[start:end]
        masked = masked[:start] + marker + masked[end:]
    marker_positions = [
        match.start()
        for match in re.finditer(r"\*+", masked)
        if len(match.group(0)) == 2 and not _is_escaped(masked, match.start())
    ]
    pairs = list(zip(marker_positions[::2], marker_positions[1::2]))
    if pairs:
        baseline_count = _count_strong_close(masked)
        add_spaces: list[int] = []
        for opening, closing in pairs:
            after = closing + 2
            if after >= len(masked) or masked[after].isspace():
                continue
            recognized = baseline_count == len(pairs)
            if not recognized:
                # Validate only this short candidate with markdown-it. This
                # keeps CJK flanking repair linear in a long Markdown line.
                left = max(0, opening - 2)
                end = min(len(masked), after + 2)
                local = masked[left:after] + " " + masked[after:end]
                recognized = _count_strong_close(local) > 0
            if recognized:
                add_spaces.append(after)
        for position in reversed(add_spaces):
            masked = masked[:position] + " " + masked[position:]
    current = masked
    for marker, original in replacements.items():
        current = current.replace(marker, original)
    return current


def _protected_inline_ranges(value: str, children: list[Any]) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    cursor = 0
    for child in children:
        if child.type == "code_inline" and child.markup:
            delimiter = child.markup
            opening = value.find(delimiter, cursor)
            closing = value.find(delimiter, opening + len(delimiter)) if opening >= 0 else -1
            if opening >= 0 and closing >= 0:
                ranges.append((opening, closing + len(delimiter)))
                cursor = closing + len(delimiter)
        elif child.type == "image":
            url = str((child.attrs or {}).get("src") or "")
            start = value.find("![", cursor)
            while url and start >= 0:
                found = _source_image_end(value, start, url)
                if found:
                    ranges.append((start, found[0]))
                    cursor = found[0]
                    break
                start = value.find("![", start + 2)
        elif child.type == "link_open":
            href = str((child.attrs or {}).get("href") or "")
            link_range = _link_destination_range(value, href, cursor)
            if link_range:
                ranges.append(link_range)
                cursor = link_range[1]
    for match in re.finditer(r"\bhttps?://[^\s<>()]+", value, flags=re.I):
        if not any(left <= match.start() < right for left, right in ranges):
            ranges.append((match.start(), match.end()))
    return _merge_ranges(ranges)


def _merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    result: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if result and start <= result[-1][1]:
            result[-1] = (result[-1][0], max(result[-1][1], end))
        else:
            result.append((start, end))
    return result


def _link_destination_range(value: str, expected_url: str, search_from: int) -> tuple[int, int] | None:
    if not expected_url:
        return None
    for match in re.finditer(r"\]\(", value[search_from:]):
        opening = search_from + match.start()
        url_start = opening + 2
        depth = 1
        cursor = url_start
        while cursor < len(value):
            character = value[cursor]
            if character == "\\":
                cursor += 2
                continue
            if character == "(":
                depth += 1
            elif character == ")":
                depth -= 1
                if depth == 0:
                    left = value.rfind("[", search_from, opening)
                    if left >= 0:
                        link_tokens = [
                            token
                            for token in _token_children(markdown_tokens(value[left : cursor + 1]))
                            if token.type == "link_open"
                        ]
                        if link_tokens and str((link_tokens[0].attrs or {}).get("href") or "") == expected_url:
                            destination_start = url_start + (1 if value[url_start : url_start + 1] == "<" else 0)
                            destination_end = cursor - (1 if value[cursor - 1 : cursor] == ">" else 0)
                            return destination_start, destination_end
                    break
            cursor += 1
    # Autolinks have no closing parenthesis; protect their tokenized URL text.
    position = value.find(expected_url, search_from)
    if position >= 0:
        return position, position + len(expected_url)
    return None


def _markdown_line_offsets(value: str) -> list[int]:
    offsets = [0]
    for line in value.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))
    return offsets


def _token_source_span(value: str, token: Any, offsets: list[int], search_from: int = 0) -> tuple[int, int] | None:
    if token.type == "inline" and token.content:
        if token.map:
            start_line, end_line = token.map
            lower = offsets[min(start_line, len(offsets) - 1)]
            upper = offsets[min(end_line, len(offsets) - 1)]
        else:
            lower, upper = search_from, len(value)
        position = value.find(token.content, max(search_from, lower), upper)
        if position >= 0:
            return position, position + len(token.content)
    if not token.map:
        return None
    start_line, end_line = token.map
    lower = offsets[min(start_line, len(offsets) - 1)]
    upper = offsets[min(end_line, len(offsets) - 1)]
    return lower, upper


def _convert_feishu_markdown(value: str, base_url: str = "") -> str:
    """Normalize Markdown variants accepted by the RSS pipeline for Card 2.0."""
    if not value:
        return ""
    tokens = markdown_tokens(value)
    offsets = _markdown_line_offsets(value)
    edits: list[tuple[int, int, str]] = []
    lines = value.splitlines(keepends=True)
    inline_search = 0
    for token in tokens:
        if token.type == "inline":
            span = _token_source_span(value, token, offsets, inline_search)
            if not span:
                continue
            start, end = span
            inline_search = end
            original_inline = value[start:end]
            inline_source = original_inline
            if any(child.type == "html_inline" for child in token.children or []) or _contains_raw_html(
                inline_source, token.children or []
            ):
                inline_source = _sanitize_inline_html(inline_source, token.children or [], base_url)
                updated = _fix_strong_boundaries(inline_source, _token_children(markdown_tokens(inline_source)))
            else:
                updated = _fix_strong_boundaries(inline_source, token.children)
            if updated != original_inline:
                edits.append((start, end, updated))
            continue

        if not token.map:
            continue
        start_line, end_line = token.map
        start = offsets[min(start_line, len(offsets) - 1)]
        end = offsets[min(end_line, len(offsets) - 1)]
        if token.type == "heading_open" and token.markup in {"=", "-"}:
            heading_line = lines[start_line].strip()
            if heading_line:
                level = 1 if token.tag == "h1" else 2
                replacement = f"{'#' * level} {heading_line}\n"
                edits.append((start, end, replacement))
        elif token.type == "code_block":
            code = token.content
            backticks = max((len(run) for run in re.findall(r"`+", code)), default=0)
            tildes = max((len(run) for run in re.findall(r"~+", code)), default=0)
            fence_char = "~" if tildes < backticks else "`"
            fence_length = max(3, (tildes if fence_char == "~" else backticks) + 1)
            fence = fence_char * fence_length
            code = code.rstrip("\n")
            replacement = f"{fence}\n{code}\n{fence}\n"
            edits.append((start, end, replacement))
        elif token.type == "html_block":
            converted = html_to_markdown(token.content, base_url).strip()
            if converted:
                edits.append((start, end, converted + "\n"))
            else:
                edits.append((start, end, ""))

    for start, end, replacement in sorted(edits, reverse=True):
        value = value[:start] + replacement + value[end:]
    return value.strip()


def _sanitize_inline_html(value: str, children: list[Any], base_url: str) -> str:
    """Convert raw inline HTML with markdownify while leaving code literals intact."""
    protected: list[tuple[int, int]] = []
    for child in children:
        if child.type != "code_inline" or not child.markup:
            continue
        delimiter = child.markup
        opening = value.find(delimiter)
        closing = value.find(delimiter, opening + len(delimiter)) if opening >= 0 else -1
        if opening >= 0 and closing >= 0:
            protected.append((opening, closing + len(delimiter)))
    if not protected:
        has_markdown_syntax = any(
            child.type not in {"text", "html_inline", "link_open", "link_close"} for child in children
        ) or bool(re.search(r"(?<!\\)(?:\*\*|__|`|!?\[|\]\()", value))
        if has_markdown_syntax:
            return _strip_raw_html(value, children)
        return _html_to_markdown_preserving_autolinks(value, base_url, children).strip()
    protected.sort()
    output: list[str] = []
    cursor = 0
    for start, end in protected:
        chunk = value[cursor:start]
        output.append(
            _sanitize_html_fragment(chunk, base_url)
            if _contains_raw_html(chunk, _token_children(markdown_tokens(chunk)))
            else chunk
        )
        output.append(value[start:end])
        cursor = end
    tail = value[cursor:]
    output.append(
        _sanitize_html_fragment(tail, base_url)
        if _contains_raw_html(tail, _token_children(markdown_tokens(tail)))
        else tail
    )
    return "".join(output)


def _sanitize_html_fragment(value: str, base_url: str) -> str:
    children = _token_children(markdown_tokens(value))
    if not _contains_raw_html(value, children):
        return value
    if any(child.type not in {"text", "html_inline", "link_open", "link_close"} for child in children) or re.search(
        r"(?<!\\)(?:\*\*|__|`|!?\[|\]\()", value
    ):
        return _strip_raw_html(value, children)
    return _html_to_markdown_preserving_autolinks(value, base_url, children).strip()


def _mask_ranges(value: str, ranges: list[tuple[int, int]], prefix: str) -> tuple[str, dict[str, str]]:
    replacements: dict[str, str] = {}
    for index, (start, end) in reversed(list(enumerate(ranges))):
        marker = f"\ue000{prefix}{index}\ue001"
        replacements[marker] = value[start:end]
        value = value[:start] + marker + value[end:]
    return value, replacements


def _restore_ranges(value: str, replacements: dict[str, str]) -> str:
    for marker, original in replacements.items():
        value = value.replace(marker, original)
    return value


def _html_to_markdown_preserving_autolinks(value: str, base_url: str, children: list[Any]) -> str:
    masked, replacements = _mask_ranges(value, _autolink_ranges(value, children), "FEISHUAUTOLINK")
    return _restore_ranges(html_to_markdown(masked, base_url), replacements)


def _strip_raw_html(value: str, children: list[Any]) -> str:
    masked, replacements = _mask_ranges(value, _autolink_ranges(value, children), "FEISHUAUTOLINK")
    masked = re.sub(r"<(script|style|noscript|template|svg)\b[^>]*>[\s\S]*?</\1\s*>", "", masked, flags=re.I)
    masked = _HTML_TAG.sub(lambda match: "\n" if re.match(r"<\s*br\b", match.group(0), flags=re.I) else "", masked)
    return _restore_ranges(masked, replacements)


def _source_image_end(value: str, start: int, expected_url: str) -> tuple[int, str] | None:
    if start > 0 and value[start - 1] == "!":
        return None
    if _is_escaped(value, start):
        return None
    label_end = value.find("](", start + 2)
    if label_end < 0:
        return None
    depth = 1
    cursor = label_end + 2
    quote = ""
    while cursor < len(value):
        character = value[cursor]
        if character == "\\":
            cursor += 2
            continue
        if quote:
            if character == quote:
                quote = ""
        elif character in {'"', "'"}:
            quote = character
        elif character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth == 0:
                candidate = value[start : cursor + 1]
                found = [
                    token
                    for token in _token_children(markdown_tokens(candidate))
                    if token.type == "image" and str((token.attrs or {}).get("src") or "") == expected_url
                ]
                if found:
                    return cursor + 1, str(found[0].content or "")
                return None
        cursor += 1
    return None


def _inline_image_spans(value: str) -> list[tuple[int, int, str, str, tuple[str, ...]]]:
    """Locate recognized Markdown image tokens in their original inline source."""
    spans: list[tuple[int, int, str, str, tuple[str, ...]]] = []
    search_from = 0
    for block in markdown_tokens(value):
        if block.type != "inline":
            continue
        children = block.children or []
        if not any(child.type == "image" for child in children):
            continue
        inline_source = block.content
        inline_position = value.find(inline_source, search_from) if inline_source else -1
        if inline_position < 0:
            continue
        search_from = inline_position + len(inline_source)
        code_ranges: list[tuple[int, int]] = []
        for child in children:
            if child.type != "code_inline" or not child.markup:
                continue
            delimiter = child.markup
            opening = inline_source.find(delimiter)
            closing = inline_source.find(delimiter, opening + len(delimiter)) if opening >= 0 else -1
            if opening >= 0 and closing >= 0:
                code_ranges.append((opening, closing + len(delimiter)))
        active: list[tuple[str, str]] = []
        image_cursor = 0
        for child in children:
            if child.type in {"strong_open", "em_open"}:
                active.append((child.type, child.markup or ("**" if child.type == "strong_open" else "*")))
            elif child.type in {"strong_close", "em_close"}:
                expected = "strong_open" if child.type == "strong_close" else "em_open"
                for index in range(len(active) - 1, -1, -1):
                    if active[index][0] == expected:
                        active.pop(index)
                        break
            elif child.type == "image":
                url = str((child.attrs or {}).get("src") or "")
                if not url:
                    continue
                candidate_start = inline_source.find("![", image_cursor)
                found = None
                while candidate_start >= 0:
                    end = _source_image_end(inline_source, candidate_start, url)
                    in_code = any(left <= candidate_start < right for left, right in code_ranges)
                    if end and not in_code:
                        found = (candidate_start, end[0], end[1])
                        break
                    candidate_start = inline_source.find("![", candidate_start + 2)
                if not found:
                    continue
                left, right, alt = found
                spans.append(
                    (inline_position + left, inline_position + right, url, alt, tuple(mark for _, mark in active))
                )
                image_cursor = right
    return spans


def _matching_parenthesis(value: str, opening: int) -> int | None:
    depth = 0
    escaped = False
    for index in range(opening, len(value)):
        if escaped:
            escaped = False
            continue
        if value[index] == "\\":
            escaped = True
            continue
        if value[index] == "(":
            depth += 1
        elif value[index] == ")":
            depth -= 1
            if depth == 0:
                return index
    return None


def _split_links_around_placeholders(value: str) -> str:
    """Keep a Markdown anchor's visible text as links around native images."""
    for match in reversed(list(_IMAGE_PLACEHOLDER.finditer(value))):
        placeholder = match.group(0)
        start = value.rfind("[", 0, match.start())
        label_end = value.find("](", match.end())
        if start < 0 or label_end < 0:
            continue
        parenthesis_open = label_end + 1
        close = _matching_parenthesis(value, parenthesis_open)
        if close is None:
            continue
        candidate = value[start : close + 1]
        link = next((token for token in _token_children(markdown_tokens(candidate)) if token.type == "link_open"), None)
        href = str((link.attrs or {}).get("href") or "") if link else ""
        if not href:
            continue
        prefix = value[start + 1 : match.start()]
        suffix = value[match.end() : label_end]
        before = f"[{prefix}]({href})" if prefix.strip() else ""
        after = f"[{suffix}]({href})" if suffix.strip() else ""
        if before or after:
            replacement = before + placeholder + after
        else:
            replacement = placeholder + f"[打开链接]({href})"
        value = value[:start] + replacement + value[close + 1 :]
    return value


def _styled_image_replacement(
    source: str, start: int, end: int, placeholder: str, styles: tuple[str, ...]
) -> tuple[int, int, str]:
    if not styles:
        return start, end, placeholder
    left = start
    while left > 0 and source[left - 1].isspace() and source[left - 1] != "\n":
        left -= 1
    right = end
    while right < len(source) and source[right].isspace() and source[right] != "\n":
        right += 1
    before_space = source[left:start]
    after_space = source[end:right]
    close = "".join(reversed(styles))
    reopen = "".join(styles)
    return left, right, f"{close}{before_space}{placeholder}{after_space}{reopen}"


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
        messages: list[RenderedMessage] = []
        ordinary_limit = int(getattr(self.config, "content_truncation_length", 0) or 0)
        ordinary_enabled = (
            not is_list and ordinary_limit > 0 and bool(getattr(self.config, "enable_content_truncation", False))
        )
        send_as_media = bool(getattr(self.config, "send_as_media", True))
        max_images = max(0, int(getattr(self.config, "max_images_per_message", 8)))
        for original in articles:
            article = clean_cached_article(original)
            body = article_markdown(article).strip()
            if ordinary_enabled:
                body = _clip_markdown(body, ordinary_limit)
            media = article_media(article)
            self._image_count = 0
            self._image_tokens = {}
            self._image_keys = {}
            self._uploaded_video_urls = set()
            video_messages: list[RenderedMessage] = []

            if send_as_media and self.video_file:
                for item in media:
                    if item.type.lower() != "video" or not item.url:
                        continue
                    attachment = await self.video_file(target, item.url)
                    file_key = attachment.get("file_key") if attachment else None
                    if isinstance(file_key, str) and file_key.strip():
                        self._uploaded_video_urls.add(item.url)
                        video_messages.append(
                            RenderedMessage(
                                "feishu",
                                "media",
                                {"file_key": file_key.strip(), "source_url": item.url},
                                [article.article_id],
                                batch_id,
                            )
                        )

            elements = await self._article_elements(article, body, media, target, send_as_media, max_images)
            elements = await self._fit_article(article, source_name, target, elements, batch_id)
            messages.append(self._message(article, source_name, elements, target, batch_id))
            messages.extend(video_messages)
        return messages

    async def _article_elements(
        self,
        article: Article,
        body: str,
        media: list[ContentBlock],
        target: DeliveryTarget,
        send_as_media: bool,
        max_images: int,
        include_body: bool = True,
    ) -> list[dict[str, Any]]:
        elements: list[dict[str, Any]] = []
        used_urls: set[str] = set()
        if include_body and body:
            markdown = _convert_feishu_markdown(body, article.canonical_url)
            spans = _inline_image_spans(markdown)
            replacements: list[tuple[int, int, str]] = []
            cursor = 0
            media_by_url = {item.url: item for item in media if item.url}
            for start, end, url, alt, styles in spans:
                item = media_by_url.get(url, ContentBlock("image", url=url, attrs={"alt": alt}))
                used_urls.add(url)
                label = str(item.attrs.get("alt") or alt or "查看原图")
                replacement = f"[{label}]({url})"
                if send_as_media:
                    if url in self._image_keys:
                        key = self._image_keys[url]
                    elif self._image_count < max_images:
                        key = await self.image_key(target, url, label) if self.image_key else None
                        self._image_keys[url] = key
                        if key:
                            self._image_count += 1
                    else:
                        key = None
                    if key:
                        token = f"\ufff0FEISHUIMG{len(self._image_tokens)}\ufff1"
                        self._image_tokens[token] = await self._image_element(target, item, key)
                        replace_start, replace_end, replacement = _styled_image_replacement(
                            markdown, start, end, token, styles
                        )
                        start, end = replace_start, replace_end
                    else:
                        replacement = f"[图片：{label}]({url})"
                if end > cursor:
                    replacements.append((start, end, replacement))
                    cursor = end

            for start, end, replacement in reversed(replacements):
                markdown = markdown[:start] + replacement + markdown[end:]
            markdown = _split_links_around_placeholders(markdown)
            elements.extend(_expand_image_tokens(markdown, self._image_tokens))

        # Metadata-only media follows body media in the canonical order from
        # article_media. This also preserves links when native upload is off.
        for item in media:
            kind = item.type.lower()
            url = item.url
            if not url or url in used_urls:
                continue
            label = str(item.attrs.get("alt") or item.attrs.get("title") or kind.capitalize())
            if kind == "image" and send_as_media:
                if url in self._image_keys:
                    key = self._image_keys[url]
                elif self._image_count < max_images:
                    key = await self.image_key(target, url, label) if self.image_key else None
                    self._image_keys[url] = key
                    if key:
                        self._image_count += 1
                else:
                    key = None
                if key:
                    elements.append(await self._image_element(target, item, key))
                    continue
            if kind == "video" and url in self._uploaded_video_urls:
                continue
            text_label = f"图片：{label}" if kind == "image" else label
            elements.extend(_expand_image_tokens(f"[{text_label}]({url})", self._image_tokens))

        elements.append({"tag": "markdown", "content": f"原文地址：[{article.canonical_url}]({article.canonical_url})"})
        return elements

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

        footer = elements[-1:]
        body_elements = elements[:-1]
        kept: list[dict[str, Any]] = []
        for element in body_elements:
            trial_elements = kept + [element] + footer
            trial = RenderedMessage(
                "feishu",
                "interactive",
                {"card": self._card(article.title, subtitle, trial_elements)},
                [article.article_id],
                batch_id,
            )
            if len(trial_elements) <= MAX_ELEMENTS and self.request_size(trial, target) <= self.hard_limit:
                kept.append(element)
                continue
            if element.get("tag") == "markdown":
                source = str(element.get("content") or "")
                low, high = 0, len(source)
                best = ""
                while low <= high:
                    middle = (low + high) // 2
                    clipped = _clip_markdown(source, middle)
                    if clipped:
                        partial = {"tag": "markdown", "content": clipped}
                        partial_elements = kept + [partial] + footer
                    else:
                        partial_elements = kept + footer
                    probe = RenderedMessage(
                        "feishu",
                        "interactive",
                        {"card": self._card(article.title, subtitle, partial_elements)},
                        [article.article_id],
                        batch_id,
                    )
                    if len(partial_elements) <= MAX_ELEMENTS and self.request_size(probe, target) <= self.hard_limit:
                        best = clipped
                        low = middle + 1
                    else:
                        high = middle - 1
                if best:
                    kept.append({"tag": "markdown", "content": best})
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

    async def _image_element(self, target: DeliveryTarget, block: ContentBlock, key: str) -> dict[str, Any]:
        attrs = block.attrs
        classes = attrs.get("class", [])
        if isinstance(classes, str):
            classes = [classes]
        is_emoji = "emoji" in classes
        width_hint = _positive_dimension(attrs.get("width"))
        height_hint = _positive_dimension(attrs.get("height"))
        dimensions = await self.image_size(target, block.url) if self.image_size else None
        if not dimensions and width_hint and height_hint:
            dimensions = (width_hint, height_hint)
        size = _display_size(dimensions, emoji=is_emoji, width_hint=width_hint, height_hint=height_hint)
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
        message = RenderedMessage(
            "feishu",
            "interactive",
            {"card": self._card(article.title, subtitle, elements)},
            [article.article_id],
            batch_id,
        )
        size = self.request_size(message, target)
        if size > self.hard_limit or len(elements) > MAX_ELEMENTS:
            raise RenderingError(f"飞书卡片超过硬限制：{size} bytes / {len(elements)} elements")
        return message

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
