from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

import emoji

from ..core.models import ContentBlock


def _safe_url(value: str, base_url: str = "") -> str:
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


def _destination(value: str) -> str:
    """Quote Markdown delimiters while retaining the URL's component structure."""
    try:
        parts = urlsplit(value)
        path = quote(parts.path, safe="/%:@!$&'*+,;=-._~%")
        query = quote(parts.query, safe="/?@!$&'*+,;=-._~%")
        fragment = quote(parts.fragment, safe="/?@!$&'*+,;=-._~%")
        return urlunsplit((parts.scheme, parts.netloc, path, query, fragment))
    except ValueError:
        return ""


def _url_key(value: object) -> str:
    try:
        parts = urlsplit(str(value or "").strip())
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
    return str(value or "").strip()


def _plain(block: ContentBlock) -> str:
    pending = [block]
    output: list[str] = []
    while pending:
        current = pending.pop()
        if current.text:
            output.append(current.text)
        pending.extend(reversed(current.children))
    return "".join(output)


def _attrs_class(block: ContentBlock) -> set[str]:
    classes = block.attrs.get("class", [])
    if isinstance(classes, str):
        classes = classes.split()
    if not isinstance(classes, (list, tuple, set)):
        return set()
    return {str(value).casefold() for value in classes}


def _emoji_image(block: ContentBlock) -> str | None:
    classes = _attrs_class(block)
    try:
        path = urlsplit(block.url).path.casefold()
    except ValueError:
        path = ""
    if classes & {"emoji-custom", "custom-emoji"} or "/emoji/custom/" in path:
        return None
    if "emoji" not in classes and "/emoji/" not in path and "/twemoji/" not in path:
        return None
    for label in (block.attrs.get("alt"), block.attrs.get("title")):
        label = str(label or "").strip()
        if label:
            converted = emoji.emojize(label, language="alias")
            if emoji.is_emoji(converted):
                return converted
    return None


def _all_legacy_media(blocks: list[ContentBlock]) -> set[str]:
    pending = list(blocks)
    urls: set[str] = set()
    while pending:
        block = pending.pop()
        if block.type.casefold() in {"image", "video", "audio", "attachment", "file"} and block.url:
            urls.add(_url_key(block.url))
        pending.extend(block.children)
    return urls


def _render(block: ContentBlock, media: list[dict[str, Any]], skip_links: set[int]) -> str:
    kind = str(block.type or "").casefold()
    text = block.text or ""

    if kind == "markdown":
        return text
    if kind in {"text", "plain"}:
        return text
    if kind in {"inline_code", "code_inline"}:
        longest_run = max((len(run) for run in re.findall(r"`+", text)), default=0)
        fence = "`" * (longest_run + 1)
        padded = bool(text) and (
            text.startswith("`") or text.endswith("`") or (text.startswith(" ") and text.endswith(" "))
        )
        padding = " " if padded else ""
        return f"{fence}{padding}{text}{padding}{fence}"
    if kind in {"code", "pre", "code_block"}:
        language = str(block.attrs.get("language") or "").strip().replace("`", "")
        content = text or "".join(_render(child, media, skip_links) for child in block.children)
        fence = "```"
        while fence in content:
            fence += "`"
        return f"\n\n{fence}{language}\n{content.rstrip()}\n{fence}\n\n"
    if kind in {"hr", "horizontal_rule"}:
        return "\n\n---\n\n"
    if kind in {"break", "line_break", "br"}:
        return "  \n"

    if kind in {"image", "img"}:
        emoji_text = _emoji_image(block)
        if emoji_text:
            return emoji_text
        url = _safe_url(block.url)
        if not url:
            return str(block.attrs.get("alt") or "")
        attrs = dict(block.attrs)
        attrs.update({"url": url, "medium": "image", "type": attrs.get("type") or "image/*"})
        media.append(attrs)
        alt = str(block.attrs.get("alt") or block.text or "")
        alt = alt.replace("[", r"\[").replace("]", r"\]")
        return f"![{alt}]({_destination(url)})"

    if kind in {"video", "audio", "attachment", "file"}:
        url = _safe_url(block.url)
        if not url:
            return _plain(block)
        attrs = dict(block.attrs)
        if kind in {"video", "audio"}:
            attrs["medium"] = kind
        else:
            attrs["role"] = "attachment"
        attrs["url"] = url
        media.append(attrs)
        label = str(
            block.attrs.get("title")
            or _plain(block)
            or ("Video" if kind == "video" else "Audio" if kind == "audio" else "Attachment")
        )
        return f"[{label}]({_destination(url)})"

    if kind in {"link", "a"}:
        if id(block) in skip_links:
            return ""
        url = _safe_url(block.url)
        label = _plain(block) or block.url
        classes = _attrs_class(block)
        pending = list(reversed(block.children))
        image = None
        while pending:
            child = pending.pop()
            if child.type.casefold() == "image":
                image = child
                break
            pending.extend(reversed(child.children))
        if image:
            emoji_text = _emoji_image(image)
            if emoji_text:
                return emoji_text
            lightbox = bool({"lightbox", "lightbox-wrapper"}.intersection(classes))
            image_url = url if lightbox and url else _safe_url(image.url)
            if image_url:
                alt = str(image.attrs.get("alt") or label)
                item: dict[str, Any] = {"url": image_url, "medium": "image", "type": "image/*", "alt": alt}
                if image.attrs.get("class"):
                    item["class"] = image.attrs["class"]
                if lightbox and _url_key(image_url) != _url_key(image.url):
                    item["thumbnail_url"] = _safe_url(image.url)
                    for dimension in ("width", "height"):
                        value = image.attrs.get(dimension)
                        if value is not None:
                            try:
                                item[f"thumbnail_{dimension}"] = int(str(value).removesuffix("px"))
                            except (TypeError, ValueError):
                                pass
                else:
                    for dimension in ("width", "height"):
                        if image.attrs.get(dimension) is not None:
                            item[dimension] = image.attrs[dimension]
                media.append(item)
                alt = alt.replace("[", r"\[").replace("]", r"\]")
                return f"![{alt}]({_destination(image_url)})"
        if not url:
            return label
        return f"[{label}]({_destination(url)})"

    if kind in {"list", "ul", "ol"}:
        ordered = bool(block.attrs.get("ordered")) or kind == "ol"
        rows = []
        for index, child in enumerate(block.children, start=1):
            item = _render(child, media, skip_links).strip()
            if item:
                marker = f"{index}. " if ordered else "- "
                rows.append(marker + item.replace("\n", "\n  "))
        return "\n\n" + "\n".join(rows) + "\n\n" if rows else ""

    if kind in {"table", "table_row", "tr", "table_cell", "td", "th"}:
        if kind in {"table_cell", "td", "th"}:
            content = text + "".join(_render(child, media, skip_links) for child in block.children)
            return content.replace("|", r"\|").replace("\n", " ").strip()
        if kind in {"table_row", "tr"}:
            cells = [text + _render(child, media, skip_links) for child in block.children]
            return "| " + " | ".join(cell.strip() for cell in cells) + " |\n"
        rows = [child for child in block.children if child.type.casefold() in {"table_row", "tr"}]
        if not rows:
            return text
        rendered = [_render(row, media, skip_links) for row in rows]
        header = rows[0].children and any(child.attrs.get("header") for child in rows[0].children)
        if header:
            separator = "| " + " | ".join("---" for _ in rows[0].children) + " |\n"
            rendered.insert(1, separator)
        return "\n" + "".join(rendered) + "\n"

    child_text = "".join(_render(child, media, skip_links) for child in block.children)
    content = text + child_text
    if kind in {"paragraph", "p", "div", "section", "article", "container"}:
        return f"\n\n{content.strip()}\n\n" if content.strip() else ""
    if kind in {"heading", "header"}:
        level = max(1, min(6, int(block.attrs.get("level") or 2)))
        return f"\n\n{'#' * level} {content.strip()}\n\n" if content.strip() else ""
    if kind in {"strong", "bold", "b"}:
        return f"**{content}**" if content else ""
    if kind in {"emphasis", "italic", "i"}:
        return f"*{content}*" if content else ""
    if kind in {"strike", "del", "s", "deleted"}:
        return f"~~{content}~~" if content else ""
    if kind in {"list_item", "li"}:
        return content.strip()
    if kind in {"quote", "blockquote"}:
        quoted = content.strip()
        return (
            "\n\n" + "\n".join(f"> {line}" if line else ">" for line in quoted.splitlines()) + "\n\n" if quoted else ""
        )
    return content


def blocks_to_markdown(blocks: list[ContentBlock], *, media: list[dict[str, Any]] | None = None) -> str:
    """Convert legacy ContentBlock trees into the single Markdown body format."""
    collected = media if media is not None else []
    media_keys = _all_legacy_media(blocks)
    generic_media_labels = {"view image", "view video", "view audio", "image", "video", "audio"}
    skip_links: set[int] = set()
    pending = list(blocks)
    while pending:
        block = pending.pop()
        if block.type.casefold() in {"link", "a"} and _url_key(block.url) in media_keys:
            if _plain(block).strip().casefold() in generic_media_labels:
                skip_links.add(id(block))
        pending.extend(block.children)
    output = "".join(_render(block, collected, skip_links) for block in blocks)
    return output.replace("\r\n", "\n").replace("\r", "\n").strip()
