from __future__ import annotations

import re
from dataclasses import replace
from typing import Any
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

import emoji
from markdown_it import MarkdownIt
from markdown_it.token import Token
from markdownify import MarkdownConverter

from ..core.models import Article, ContentBlock
from .legacy_blocks import blocks_to_markdown as _legacy_blocks_to_markdown
from .media_content import associate_media_thumbnails, media_kind

_MARKDOWN = MarkdownIt("default", options_update={"html": False, "linkify": False, "typographer": False})
_GENERATED_HEADING_FRAGMENT = re.compile(r"^p-\d+-h-\d+$", re.I)
_DISCOURSE_STATS = re.compile(r"^(?:\d+\s+posts?\s*-\s*\d+\s+participants?|\d+\s*个帖子\s*-\s*\d+\s*位参与者)$", re.I)
_DISCOURSE_READ = {"read full topic", "阅读完整话题"}
_IMAGE_EXTENSIONS = (".gif", ".jpeg", ".jpg", ".png", ".webp", ".avif", ".bmp", ".svg")
_VIDEO_EXTENSIONS = (".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi", ".mpeg", ".mpg", ".ogv")
_AUDIO_EXTENSIONS = (".mp3", ".m4a", ".aac", ".wav", ".ogg", ".opus", ".flac", ".oga", ".weba")


def _safe_url(value: object, base_url: str = "") -> str:
    raw = str(value or "").strip()
    if not raw or any(ord(char) < 0x20 for char in raw):
        return ""
    try:
        resolved = urljoin(base_url, raw)
        parsed = urlsplit(resolved)
    except (TypeError, ValueError):
        return ""
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        return ""
    try:
        _ = parsed.port
    except ValueError:
        return ""
    return resolved


def _markdown_destination(url: str) -> str:
    try:
        parts = urlsplit(url)
        path = quote(parts.path, safe="/%:@!$&'*+,;=-._~%")
        query = quote(parts.query, safe="/?@!$&'*+,;=-._~%")
        fragment = quote(parts.fragment, safe="/?@!$&'*+,;=-._~%")
        return urlunsplit((parts.scheme, parts.netloc, path, query, fragment))
    except ValueError:
        return ""


def _escape_markdown_label(value: object) -> str:
    compact = re.sub(r"\s+", " ", str(value or ""))
    return re.sub(r"([\\\[\]])", r"\\\1", compact)


def _markdown_title(value: object) -> str:
    compact = re.sub(r"\s+", " ", str(value or "")).strip()
    return compact.replace("\\", "\\\\").replace('"', "’")


def _classes(tag: Any) -> set[str]:
    value = tag.get("class", [])
    if isinstance(value, str):
        value = value.split()
    if not isinstance(value, (list, tuple, set)):
        return set()
    return {str(item).casefold() for item in value}


def _dimension(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value).strip().removesuffix("px"))
    except (TypeError, ValueError):
        return None


def _url_key(value: object) -> str:
    url = str(value or "").strip()
    try:
        parts = urlsplit(url)
        if parts.scheme and parts.hostname:
            scheme = parts.scheme.lower()
            host = parts.hostname.lower().rstrip(".")
            port = parts.port
            if (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
                port = None
            netloc = f"[{host}]" if ":" in host else host
            if port:
                netloc += f":{port}"
            path = quote(parts.path, safe="/%:@!$&'*+,;=-._~%")
            query = quote(parts.query, safe="/?@!$&'*+,;=-._~%")
            fragment = quote(parts.fragment, safe="/?@!$&'*+,;=-._~%")
            path = re.sub(r"%[0-9a-fA-F]{2}", lambda m: m.group(0).upper(), path)
            query = re.sub(r"%[0-9a-fA-F]{2}", lambda m: m.group(0).upper(), query)
            fragment = re.sub(r"%[0-9a-fA-F]{2}", lambda m: m.group(0).upper(), fragment)
            return urlunsplit((scheme, netloc, path, query, fragment))
    except ValueError:
        pass
    return url


def _topic(url: str) -> tuple[str, str] | None:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    segments = [segment for segment in parsed.path.strip("/").split("/") if segment]
    try:
        t_index = len(segments) - 1 - segments[::-1].index("t")
        route = segments[t_index + 1 :]
    except ValueError:
        return None
    if not parsed.hostname or not route:
        return None
    if route[0].isdigit():
        topic_id = route[0]
        if len(route) > 2 or (len(route) == 2 and not route[1].isdigit()):
            return None
    elif len(route) >= 2 and route[1].isdigit():
        topic_id = route[1]
        if len(route) > 3 or (len(route) == 3 and not route[2].isdigit()):
            return None
    else:
        return None
    return parsed.hostname.lower().rstrip("."), topic_id


def _strip_discourse_footer(markdown: str, canonical_url: str) -> str:
    expected = _topic(canonical_url)
    if not expected or not markdown.strip():
        return markdown
    tokens = _MARKDOWN.parse(markdown)
    paragraphs: list[tuple[Token, Token]] = []
    for index, token in enumerate(tokens):
        if token.type != "paragraph_open" or token.level != 0:
            continue
        inline = tokens[index + 1] if index + 1 < len(tokens) and tokens[index + 1].type == "inline" else None
        if inline is not None:
            paragraphs.append((token, inline))
    if len(paragraphs) < 2:
        return markdown
    stats_open, stats_inline = paragraphs[-2]
    read_open, read_inline = paragraphs[-1]
    stats = (stats_inline.content or "").strip()
    label = "".join(
        child.content for child in (read_inline.children or []) if child.type in {"text", "code_inline"}
    ).strip()
    if not _DISCOURSE_STATS.fullmatch(stats) or label.casefold() not in _DISCOURSE_READ:
        return markdown
    links = [child for child in (read_inline.children or []) if child.type == "link_open"]
    if len(links) != 1:
        return markdown
    href = str(links[0].attrGet("href") or "")
    if _topic(urljoin(canonical_url, href)) != expected:
        return markdown
    if not stats_open.map or not read_open.map:
        return markdown
    source_lines = markdown.splitlines(keepends=True)
    if any(line.strip() for line in source_lines[stats_open.map[1] : read_open.map[0]]):
        return markdown
    if any(line.strip() for line in source_lines[read_open.map[1] :]):
        return markdown
    start_line = stats_open.map[0]
    if start_line >= len(source_lines):
        return markdown
    return "".join(source_lines[:start_line]).rstrip()


def _anchor_is_generated(tag: Any, href: str, parent_tags: set[str], base_url: str, is_discourse: bool = False) -> bool:
    classes = _classes(tag)
    if "anchor" not in classes or not href:
        return False
    try:
        link = urlsplit(urljoin(base_url, href))
        canonical = urlsplit(base_url)
    except ValueError:
        return False
    if not link.fragment or not link.hostname or not canonical.hostname:
        return False
    if link.hostname.casefold().rstrip(".") != canonical.hostname.casefold().rstrip("."):
        return False
    matches_name = link.fragment in {str(tag.get("name") or ""), str(tag.get("id") or "")}
    in_heading = any(re.fullmatch(r"h[1-6]", name, re.I) for name in parent_tags)
    generated = bool(_GENERATED_HEADING_FRAGMENT.fullmatch(link.fragment))
    return generated or bool((is_discourse or _is_discourse_url(base_url)) and in_heading and matches_name)


def _emoji_image(tag: Any, src: str) -> str | None:
    classes = _classes(tag)
    try:
        path = urlsplit(src).path.casefold()
    except ValueError:
        path = ""
    if classes & {"emoji-custom", "custom-emoji"} or "/emoji/custom/" in path:
        return None
    if "emoji" not in classes and "/emoji/" not in path and "/twemoji/" not in path:
        return None
    for label in (tag.get("alt"), tag.get("title")):
        label = str(label or "").strip()
        if not label:
            continue
        converted = emoji.emojize(label, language="alias")
        if emoji.is_emoji(converted):
            return converted
    return None


class _FeedMarkdownConverter(MarkdownConverter):
    def __init__(self, base_url: str, media: list[dict[str, Any]], is_discourse: bool = False) -> None:
        self.base_url = base_url
        self.media = media
        self.is_discourse = is_discourse
        super().__init__(
            heading_style="ATX",
            bullets="-",
            strong_em_symbol="*",
            table_infer_header=True,
            code_language_callback=self._code_language,
            keep_inline_images_in=["h1", "h2", "h3", "h4", "h5", "h6", "th", "td", "a"],
            wrap=False,
        )

    @staticmethod
    def _code_language(element: Any) -> str:
        code = element.find("code") if getattr(element, "name", "") == "pre" else element
        classes = code.get("class", []) if code else []
        if isinstance(classes, str):
            classes = classes.split()
        for value in classes or []:
            if str(value).startswith("language-"):
                return str(value).removeprefix("language-")
        return ""

    def process_tag(self, node, parent_tags=None):
        # markdownify's strip option skips tag conversion but keeps child text.
        # Drop active/hidden subtrees in this narrow converter hook instead.
        if str(getattr(node, "name", "") or "").casefold() in {
            "script",
            "style",
            "noscript",
            "template",
            "svg",
            "form",
            "input",
            "button",
            "source",
        }:
            return ""
        return super().process_tag(node, parent_tags=parent_tags)

    def _record(self, url: str, *, medium: str, tag: Any, mime_type: str = "") -> None:
        if not url:
            return
        item: dict[str, Any] = {"url": url, "medium": medium}
        if mime_type:
            item["type"] = mime_type
        for source, target in (("width", "width"), ("height", "height")):
            dimension = _dimension(tag.get(source))
            if dimension is not None:
                item[target] = dimension
        title = str(tag.get("title") or "")
        alt = str(tag.get("alt") or "")
        classes = _classes(tag)
        if classes:
            item["class"] = sorted(classes)
        for attribute in ("data-emoji", "data-name", "data-emoji-name"):
            value = tag.get(attribute)
            if value is not None:
                item[attribute] = str(value)
        if title:
            item["title"] = title
        if alt:
            item["alt"] = alt
        poster = _safe_url(tag.get("poster"), self.base_url)
        if poster:
            item["poster"] = poster
        self.media.append(item)

    def convert_a(self, el, text, parent_tags):
        href = _safe_url(el.get("href"), self.base_url)
        if not href:
            return text
        if _anchor_is_generated(el, href, parent_tags, self.base_url, self.is_discourse):
            stripped = text.strip()
            if not stripped or all(char in "#¶🔗\ufe0f\u200d" for char in stripped):
                return ""
            return text

        classes = _classes(el)
        images = el.find_all("img")
        if images and classes.intersection({"lightbox", "lightbox-wrapper"}) and _looks_like_image(href):
            image = images[0]
            alt = str(image.get("alt") or el.get("title") or "")
            item: dict[str, Any] = {"url": href, "medium": "image", "type": "image/*"}
            if alt:
                item["alt"] = alt
            thumbnail = _safe_url(
                image.get("src") or image.get("data-src") or image.get("data-original"), self.base_url
            )
            if thumbnail and _url_key(thumbnail) != _url_key(href):
                item["thumbnail_url"] = thumbnail
                width = _dimension(image.get("width"))
                height = _dimension(image.get("height"))
                if width is not None:
                    item["thumbnail_width"] = width
                if height is not None:
                    item["thumbnail_height"] = height
            self.media.append(item)
            return self._library_image(image, href, set())

        label = text.strip()
        known_kind = next(
            (
                _record_kind(item)
                for item in self.media
                if isinstance(item, dict) and _url_key(item.get("url")) == _url_key(href)
            ),
            "",
        )
        if (
            not images
            and label.casefold() in {"image", "图片", "view image"}
            and (known_kind == "image" or _looks_like_image(href))
        ):
            self._record(href, medium="image", tag=el, mime_type="image/*")
            return f"![{_escape_markdown_label(label)}]({_markdown_destination(href)})"

        if not images and _looks_like_supported_media(href):
            medium = _url_medium(href)
            self._record(href, medium=medium, tag=el)

        original = el.get("href")
        original_title = el.get("title")
        el["href"] = _markdown_destination(href)
        if original_title is not None:
            el["title"] = _markdown_title(original_title)
        try:
            return super().convert_a(el, text, parent_tags)
        finally:
            if original is None:
                el.attrs.pop("href", None)
            else:
                el["href"] = original
            if original_title is None:
                el.attrs.pop("title", None)
            else:
                el["title"] = original_title

    def convert_strong(self, el, text, parent_tags):
        if {"strong", "b"}.intersection(parent_tags):
            return text
        return super().convert_strong(el, text, parent_tags)

    def convert_b(self, el, text, parent_tags):
        if {"strong", "b"}.intersection(parent_tags):
            return text
        return super().convert_b(el, text, parent_tags)

    def convert_em(self, el, text, parent_tags):
        if {"em", "i"}.intersection(parent_tags):
            return text
        return super().convert_em(el, text, parent_tags)

    def convert_i(self, el, text, parent_tags):
        if {"em", "i"}.intersection(parent_tags):
            return text
        return super().convert_i(el, text, parent_tags)

    def convert_img(self, el, text, parent_tags):
        raw_src = el.get("src") or el.get("data-src") or el.get("data-original")
        src = _safe_url(raw_src, self.base_url)
        if not src:
            return str(el.get("alt") or "")
        emoji_text = _emoji_image(el, src)
        if emoji_text:
            return emoji_text
        parent_anchor = el.find_parent("a")
        is_lightbox = bool(parent_anchor and _classes(parent_anchor).intersection({"lightbox", "lightbox-wrapper"}))
        if not is_lightbox:
            self._record(src, medium="image", tag=el, mime_type="image/*")
        return self._library_image(el, src, parent_tags)

    def _library_image(self, el, src: str, parent_tags: set[str]) -> str:
        original_src = el.get("src")
        original_alt = el.get("alt")
        original_title = el.get("title")
        el["src"] = _markdown_destination(src)
        if original_alt is not None:
            el["alt"] = _escape_markdown_label(original_alt)
        if original_title is not None:
            el["title"] = _markdown_title(original_title)
        try:
            return super().convert_img(el, "", parent_tags)
        finally:
            if original_src is None:
                el.attrs.pop("src", None)
            else:
                el["src"] = original_src
            if original_alt is None:
                el.attrs.pop("alt", None)
            else:
                el["alt"] = original_alt
            if original_title is None:
                el.attrs.pop("title", None)
            else:
                el["title"] = original_title

    def _convert_media_tag(self, el, kind: str, text: str):
        raw_src = el.get("src") or el.get("data-src") or el.get("data-original")
        source = el.find("source", attrs={"src": True})
        if not raw_src and source:
            raw_src = source.get("src")
        src = _safe_url(raw_src, self.base_url)
        mime_type = str(el.get("type") or (source.get("type") if source else "") or "")
        if not src:
            return text
        self._record(src, medium=kind, tag=el, mime_type=mime_type)
        label = _escape_markdown_label(el.get("title") or text or ("Video" if kind == "video" else "Audio")).strip()
        return f"[{label}]({_markdown_destination(src)})"

    def convert_video(self, el, text, parent_tags):
        return self._convert_media_tag(el, "video", text)

    def convert_audio(self, el, text, parent_tags):
        return self._convert_media_tag(el, "audio", text)

    def convert_iframe(self, el, text, parent_tags):
        url = _safe_url(el.get("src"), self.base_url)
        if not url:
            return ""
        title = _escape_markdown_label(el.get("title") or "Video")
        self._record(url, medium="video", tag=el)
        return f"[{title}]({_markdown_destination(url)})"

    def convert_embed(self, el, text, parent_tags):
        url = _safe_url(el.get("src") or el.get("data"), self.base_url)
        if not url:
            return ""
        title = _escape_markdown_label(el.get("title") or "Media")
        self._record(url, medium=_url_medium(url) or "attachment", tag=el, mime_type=str(el.get("type") or ""))
        return f"[{title}]({_markdown_destination(url)})"


def _looks_like_image(url: str) -> bool:
    try:
        return urlsplit(url).path.casefold().endswith(_IMAGE_EXTENSIONS)
    except ValueError:
        return False


def _looks_like_supported_media(url: str) -> bool:
    return bool(_url_medium(url))


def _url_medium(url: str) -> str:
    try:
        path = urlsplit(url).path.casefold()
    except ValueError:
        return ""
    if path.endswith(_IMAGE_EXTENSIONS):
        return "image"
    if path.endswith(_VIDEO_EXTENSIONS):
        return "video"
    if path.endswith(_AUDIO_EXTENSIONS):
        return "audio"
    return ""


def html_to_markdown(
    value: str | bytes | None,
    base_url: str = "",
    *,
    media: list[dict] | None = None,
    is_discourse: bool = False,
) -> str:
    """Convert feed HTML into sanitized CommonMark using python-markdownify."""
    if not value:
        return ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if not isinstance(value, str):
        value = str(value)
    collected = media if media is not None else []
    try:
        converted = _FeedMarkdownConverter(base_url, collected, is_discourse=is_discourse).convert(value)
    except RecursionError as exc:
        raise ValueError("HTML nesting exceeds markdownify's safe conversion depth") from exc
    converted = converted.replace("\r\n", "\n").replace("\r", "\n").strip()
    if base_url and (_is_discourse_url(base_url)):
        converted = _strip_discourse_footer(converted, base_url)
    return converted.strip()


def _is_discourse_url(url: str) -> bool:
    try:
        host = (urlsplit(url).hostname or "").casefold().rstrip(".")
    except ValueError:
        return False
    return host in {"linux.do", "www.linux.do"}


def blocks_to_markdown(blocks: list[ContentBlock]) -> str:
    return _legacy_blocks_to_markdown(blocks)


def clean_markdown(
    markdown: str,
    canonical_url: str,
    *,
    is_discourse: bool = False,
    media: list[dict[str, Any]] | None = None,
    attachments: list[dict[str, Any]] | None = None,
) -> str:
    """Clean generated footer text and include XML-only resources in Markdown."""
    if is_discourse or _is_discourse_url(canonical_url):
        markdown = _strip_discourse_footer(markdown, canonical_url)
    return _append_unreferenced_media(markdown, canonical_url, media or [], attachments or [])


def _append_unreferenced_media(
    markdown: str,
    canonical_url: str,
    media: list[dict[str, Any]],
    attachments: list[dict[str, Any]],
) -> str:
    candidates = [dict(item) for item in [*media, *attachments] if isinstance(item, dict)]
    resources: list[dict[str, Any]] = []
    by_key: dict[str, dict[str, Any]] = {}
    for item in candidates:
        url = _safe_url(item.get("url"), canonical_url)
        if not url or _url_key(url) == _url_key(canonical_url):
            continue
        item["url"] = url
        key = _url_key(url)
        if key not in by_key:
            by_key[key] = item
            resources.append(item)
        else:
            for name, value in item.items():
                if value not in (None, "", [], {}) and by_key[key].get(name) in (None, "", [], {}):
                    by_key[key][name] = value

    associations: dict[str, str] = {}
    for item in resources:
        if item.get("role") == "thumbnail" and item.get("associated_url"):
            associations[_url_key(item.get("url"))] = _url_key(item.get("associated_url"))
    referenced: set[str] = set()
    for url, _is_image, _attrs in _markdown_media_urls(markdown):
        safe_url = _safe_url(url, canonical_url)
        if not safe_url:
            continue
        key = _url_key(safe_url)
        if key in associations:
            key = associations[key]
        if key in by_key:
            referenced.add(key)

    additions: list[str] = []
    emitted = set(referenced)
    for item in resources:
        key = _url_key(item.get("url"))
        if not key or key in emitted:
            continue
        if item.get("role") == "thumbnail" and item.get("associated_url"):
            continue
        kind = _record_kind(item)
        if not kind:
            continue
        url = _markdown_destination(str(item["url"]))
        if kind == "image":
            label = _escape_markdown_label(item.get("alt") or "Image")
            additions.append(f"![{label}]({url})")
        else:
            label = _escape_markdown_label(
                item.get("title") or ("Video" if kind == "video" else "Audio" if kind == "audio" else "Attachment")
            )
            additions.append(f"[{label}]({url})")
        emitted.add(key)
    return f"{markdown.rstrip()}\n\n" + "\n\n".join(additions) if additions else markdown


def _article_markdown_and_media(article: Article) -> tuple[str, list[dict[str, Any]]]:
    blocks = list(article.content_blocks or [])
    if len(blocks) == 1 and blocks[0].type.casefold() == "markdown" and not blocks[0].children:
        markdown = blocks[0].text or ""
        media: list[dict[str, Any]] = []
    else:
        media = []
        markdown = _legacy_blocks_to_markdown(blocks, media=media)
    markdown = clean_markdown(
        markdown,
        article.canonical_url,
        is_discourse=article.metadata.get("source_adapter") == "discourse",
        media=article.metadata.get("media") if isinstance(article.metadata.get("media"), list) else None,
        attachments=(
            article.metadata.get("attachments") if isinstance(article.metadata.get("attachments"), list) else None
        ),
    )
    return markdown.strip(), media


def article_markdown(article: Article) -> str:
    return _article_markdown_and_media(article)[0]


def clean_cached_article(article: Article) -> Article:
    markdown, legacy_media = _article_markdown_and_media(article)
    existing = article.content_blocks
    already_canonical = (
        len(existing) == 1 and existing[0].type.casefold() == "markdown" and existing[0].text == markdown
    ) or (not existing and not markdown)
    metadata = article.metadata
    updated_metadata = dict(metadata)
    if legacy_media:
        resources = list(updated_metadata.get("media") or [])
        normalized: list[dict[str, Any]] = []
        index_by_key: dict[str, int] = {}
        for raw_item in [*resources, *legacy_media]:
            if not isinstance(raw_item, dict):
                continue
            item = dict(raw_item)
            safe_url = _safe_url(item.get("url"), article.canonical_url)
            if not safe_url:
                continue
            item["url"] = safe_url
            key = _url_key(safe_url)
            index = index_by_key.get(key)
            if index is None:
                index_by_key[key] = len(normalized)
                normalized.append(item)
                continue
            existing_item = normalized[index]
            for name, value in item.items():
                if value not in (None, "", [], {}) and existing_item.get(name) in (None, "", [], {}):
                    existing_item[name] = value
        updated_metadata["media"] = normalized
    if already_canonical and updated_metadata == metadata:
        return article
    return replace(
        article,
        content_blocks=[ContentBlock("markdown", text=markdown)],
        metadata=updated_metadata,
        content_hash=article.content_hash,
    )


def markdown_tokens(text: str) -> list[Token]:
    return _MARKDOWN.parse(text or "")


def _record_kind(item: dict[str, Any]) -> str:
    medium = str(item.get("medium") or "").casefold()
    media_type = str(item.get("type") or item.get("mime_type") or "").casefold()
    if medium in {"image", "video", "audio"}:
        return medium
    if media_type.startswith("image/"):
        return "image"
    if media_type.startswith("video/"):
        return "video"
    if media_type.startswith("audio/"):
        return "audio"
    return media_kind(item) or ("attachment" if item.get("role") == "attachment" else "")


def _metadata_media(article: Article) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    source_items: list[dict[str, Any]] = []
    for source_name in ("media", "attachments"):
        source = article.metadata.get(source_name)
        if isinstance(source, list):
            for raw_item in source:
                if isinstance(raw_item, dict):
                    item = dict(raw_item)
                    if source_name == "attachments":
                        item.setdefault("role", "attachment")
                    source_items.append(item)

    valid: list[dict[str, Any]] = []
    valid_by_url: dict[str, dict[str, Any]] = {}
    for raw_item in source_items:
        item = dict(raw_item)
        url = _safe_url(item.get("url"), article.canonical_url)
        if not url:
            continue
        item["url"] = url
        if item.get("role") == "thumbnail":
            item.setdefault("medium", "image")
        if item.get("type") is None:
            item.pop("type", None)
        key = _url_key(url)
        if key not in valid_by_url:
            valid_by_url[key] = item
            valid.append(item)
        else:
            for name, value in item.items():
                if value not in (None, "", [], {}) and valid_by_url[key].get(name) in (None, "", [], {}):
                    valid_by_url[key][name] = value
    valid = associate_media_thumbnails(valid)
    by_url: dict[str, dict[str, Any]] = {}
    for item in valid:
        key = _url_key(item.get("url"))
        if not key:
            continue
        if key not in by_url:
            by_url[key] = dict(item)
        else:
            for name, value in item.items():
                if value not in (None, "", [], {}) and by_url[key].get(name) in (None, "", [], {}):
                    by_url[key][name] = value
    return valid, by_url


def _markdown_media_urls(text: str) -> list[tuple[str, bool, dict[str, Any]]]:
    result: list[tuple[str, bool, dict[str, Any]]] = []
    for token in markdown_tokens(text):
        if token.type != "inline" or not token.children:
            continue
        children = token.children
        link_stack: list[dict[str, Any]] = []
        events: list[tuple[str, str, dict[str, Any], dict[str, Any] | None]] = []
        for child in children:
            if child.type == "link_open":
                href = str(child.attrGet("href") or "")
                state = {"href": href, "has_image": False}
                link_stack.append(state)
                events.append(("link", href, {}, state))
            elif child.type == "image":
                src = str(child.attrGet("src") or "")
                if src:
                    attrs = {"alt": child.content} if child.content else {}
                    title = child.attrGet("title")
                    if title:
                        attrs["title"] = title
                    events.append(("image", src, attrs, None))
                for state in link_stack:
                    state["has_image"] = True
            elif child.type == "link_close" and link_stack:
                link_stack.pop()
        for kind, url, attrs, state in events:
            if kind == "image" or (url and state is not None and not state["has_image"]):
                result.append((url, kind == "image", attrs))
    return result


def _media_attrs(item: dict[str, Any], kind: str) -> dict[str, Any]:
    attrs = {key: value for key, value in item.items() if key != "url" and value not in (None, "")}
    if kind == "video" and not attrs.get("poster"):
        thumbnail = attrs.get("thumbnail_url")
        if thumbnail:
            attrs["poster"] = thumbnail
    return attrs


def article_media(article: Article) -> list[ContentBlock]:
    """Return ordered, safe media blocks referenced in Markdown or feed metadata."""
    records, by_url = _metadata_media(article)
    original_by_key = {
        _url_key(item.get("url")): item
        for item in records
        if item.get("role") != "thumbnail" and _url_key(item.get("url"))
    }
    thumbnail_parent = {
        _url_key(item.get("url")): _url_key(item.get("associated_url"))
        for item in records
        if item.get("role") == "thumbnail" and item.get("associated_url")
    }

    ordered: list[tuple[str, dict[str, Any], str]] = []
    seen: set[str] = set()
    body = article_markdown(article)
    for raw_url, is_image, token_attrs in _markdown_media_urls(body):
        url = _safe_url(raw_url, article.canonical_url)
        if not url:
            continue
        key = _url_key(url)
        if key in thumbnail_parent:
            key = thumbnail_parent[key]
        item = original_by_key.get(key) or by_url.get(key)
        if item:
            kind = _record_kind(item)
            item = {**token_attrs, **item}
            url = str(item.get("url") or url)
        else:
            kind = _url_medium(url) or ("image" if is_image else "")
            item = {"url": url, **token_attrs}
        if not kind:
            continue
        if kind == "image" and not is_image and item.get("role") == "thumbnail" and item.get("associated_url"):
            continue
        key = _url_key(url)
        if key not in seen:
            ordered.append((url, item, kind))
            seen.add(key)

    associated: dict[str, dict[str, Any]] = {}
    for item in records:
        if item.get("role") == "thumbnail" and item.get("associated_url"):
            associated.setdefault(_url_key(item.get("associated_url")), item)
    for item in records:
        key = _url_key(item.get("url"))
        if not key or key in seen:
            continue
        if item.get("role") == "thumbnail" and item.get("associated_url"):
            continue
        kind = _record_kind(item)
        if not kind:
            continue
        ordered.append((str(item["url"]), item, kind))
        seen.add(key)

    output: list[ContentBlock] = []
    for url, item, kind in ordered:
        attrs = _media_attrs(item, kind)
        thumbnail = associated.get(_url_key(url))
        if thumbnail:
            thumbnail_url = str(thumbnail.get("url") or "")
            if thumbnail_url:
                attrs.setdefault("thumbnail_url", thumbnail_url)
                for dimension in ("width", "height"):
                    value = thumbnail.get(dimension)
                    if value is not None:
                        attrs.setdefault(f"thumbnail_{dimension}", value)
                if kind == "video":
                    attrs.setdefault("poster", thumbnail_url)
        output.append(ContentBlock(kind, url=url, attrs=attrs))
    return output


def is_pure_video_article(article: Article) -> bool:
    media = article_media(article)
    if not media or not any(item.type == "video" for item in media):
        return False
    if any(item.type != "video" for item in media):
        return False
    video_keys = {_url_key(item.url) for item in media if item.type == "video"}
    records, _ = _metadata_media(article)
    poster_parents = {
        _url_key(item.get("url")): _url_key(item.get("associated_url"))
        for item in records
        if item.get("role") == "thumbnail" and item.get("associated_url")
    }
    for token in markdown_tokens(article_markdown(article)):
        if token.type in {"fence", "code_block", "html_block"} and token.content.strip():
            return False
        if token.type != "inline" or not token.children:
            continue
        link_stack: list[str] = []
        for child in token.children:
            if child.type == "link_open":
                url = _safe_url(str(child.attrGet("href") or ""), article.canonical_url)
                link_stack.append(_url_key(url))
            elif child.type == "link_close" and link_stack:
                link_stack.pop()
            elif child.type == "image":
                image_url = _safe_url(str(child.attrGet("src") or ""), article.canonical_url)
                parent = poster_parents.get(_url_key(image_url))
                if parent in video_keys:
                    continue
                return False
            elif child.type in {"text", "code_inline", "html_inline"} and child.content.strip():
                if not link_stack or link_stack[-1] not in video_keys:
                    return False
            elif child.type in {"softbreak", "hardbreak"}:
                continue
    return True
