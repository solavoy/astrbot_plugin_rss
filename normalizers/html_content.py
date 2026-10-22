from __future__ import annotations

from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, NavigableString, Tag

from ..core.models import ContentBlock

_BLOCK_TAGS = {
    "p",
    "div",
    "section",
    "article",
    "header",
    "footer",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "blockquote",
    "pre",
    "ul",
    "ol",
    "li",
    "table",
    "hr",
}
_DROP_TAGS = {"script", "style", "noscript", "template", "svg", "form", "input", "button"}
_MAX_CONTENT_DEPTH = 64


def _safe_url(value: str | None, base_url: str) -> str:
    if not value:
        return ""
    try:
        url = urljoin(base_url, value.strip())
        scheme = urlsplit(url).scheme.lower()
        return url if scheme in {"http", "https"} else ""
    except (AttributeError, ValueError):
        return ""


def _attrs(tag: Tag) -> dict:
    allowed = {
        "id",
        "class",
        "name",
        "title",
        "alt",
        "width",
        "height",
        "colspan",
        "rowspan",
        "start",
        "type",
        "lang",
        "dir",
    }
    result = {key: tag.get(key) for key in allowed if tag.has_attr(key)}
    for dimension in ("width", "height", "colspan", "rowspan", "start"):
        value = result.get(dimension)
        if value is not None:
            try:
                result[dimension] = int(str(value).replace("px", ""))
            except ValueError:
                result.pop(dimension, None)
    if "class" in result:
        result["class"] = list(result["class"]) if isinstance(result["class"], list) else [str(result["class"])]
    return result


def _flatten_deep_content(node: Tag, base_url: str) -> list[ContentBlock]:
    """Flatten a deeply nested HTML subtree without recursing or retaining deep blocks."""
    result: list[ContentBlock] = []
    pending: list[object] = [node]
    while pending:
        current = pending.pop()
        if isinstance(current, NavigableString):
            text = str(current)
            if text:
                result.append(ContentBlock("text", text=text))
            continue
        if not isinstance(current, Tag):
            continue
        name = (current.name or "").lower()
        if name in _DROP_TAGS or name == "source":
            continue
        if name == "img":
            url = _safe_url(current.get("src") or current.get("data-src") or current.get("data-original"), base_url)
            if url:
                result.append(
                    ContentBlock("image", url=url, attrs={"alt": str(current.get("alt") or ""), **_attrs(current)})
                )
            elif current.get("alt"):
                result.append(ContentBlock("text", text=str(current.get("alt"))))
            continue
        if name in {"video", "audio"}:
            source = current.find("source")
            url = _safe_url(current.get("src") or (source.get("src") if source else None), base_url)
            if url:
                result.append(
                    ContentBlock(
                        name,
                        url=url,
                        attrs={
                            "poster": _safe_url(current.get("poster"), base_url) if name == "video" else "",
                            "mime_type": (source.get("type") if source else current.get("type", "")) or "",
                            "title": str(current.get("title") or ""),
                            **_attrs(current),
                        },
                    )
                )
                continue
        elif name in {"iframe", "embed"}:
            url = _safe_url(current.get("src") or current.get("data"), base_url)
            if url:
                result.append(
                    ContentBlock("video", url=url, attrs={"title": str(current.get("title") or ""), **_attrs(current)})
                )
            continue
        elif name in {"br", "wbr", "hr"}:
            result.append(ContentBlock("text", text="\n"))
            continue
        pending.extend(reversed(list(current.children)))
    return result


def _inline_children(tag: Tag, base_url: str, depth: int = 0) -> list[ContentBlock]:
    if depth >= _MAX_CONTENT_DEPTH:
        return _flatten_deep_content(tag, base_url)
    nodes: list[ContentBlock] = []
    for child in tag.children:
        if isinstance(child, NavigableString):
            text = str(child)
            if text:
                nodes.append(ContentBlock("text", text=text))
            continue
        if not isinstance(child, Tag) or child.name in _DROP_TAGS:
            continue
        name = child.name.lower()
        if name == "img":
            url = _safe_url(child.get("src") or child.get("data-src") or child.get("data-original"), base_url)
            if url:
                nodes.append(
                    ContentBlock("image", url=url, attrs={"alt": str(child.get("alt") or ""), **_attrs(child)})
                )
        elif name in {"video", "audio"}:
            src = child.get("src")
            poster = _safe_url(child.get("poster"), base_url) if name == "video" else ""
            source = child.find("source")
            url = _safe_url(src or (source.get("src") if source else None), base_url)
            if url:
                nodes.append(
                    ContentBlock(
                        name,
                        url=url,
                        attrs={
                            "poster": poster,
                            "mime_type": (source.get("type") if source else child.get("type", "")) or "",
                            "title": str(child.get("title") or ""),
                            **_attrs(child),
                        },
                    )
                )
        elif name == "iframe" or name == "embed":
            url = _safe_url(child.get("src") or child.get("data"), base_url)
            if url:
                nodes.append(
                    ContentBlock("video", url=url, attrs={"title": str(child.get("title") or ""), **_attrs(child)})
                )
        elif name == "a":
            url = _safe_url(child.get("href"), base_url)
            children = _inline_children(child, base_url, depth + 1)
            nodes.append(
                ContentBlock(
                    "link" if url else "text",
                    url=url,
                    text="" if children else child.get_text("", strip=False),
                    children=children,
                    attrs=_attrs(child),
                )
            )
        elif name in {"br", "wbr"}:
            nodes.append(ContentBlock("text", text="\n"))
        else:
            nested = _inline_children(child, base_url, depth + 1)
            nodes.extend(nested)
    return nodes


def _block(tag: Tag, base_url: str, depth: int = 0) -> ContentBlock | None:
    name = tag.name.lower()
    if name in _DROP_TAGS:
        return None
    if name == "img":
        url = _safe_url(tag.get("src") or tag.get("data-src") or tag.get("data-original"), base_url)
        return ContentBlock("image", url=url, attrs={"alt": str(tag.get("alt") or ""), **_attrs(tag)}) if url else None
    if name in {"video", "audio", "iframe", "embed"}:
        src = tag.get("src") or tag.get("data")
        source = tag.find("source")
        url = _safe_url(src or (source.get("src") if source else None), base_url)
        if not url:
            return None
        kind = "audio" if name == "audio" else "video"
        return ContentBlock(
            kind,
            url=url,
            attrs={
                "poster": _safe_url(tag.get("poster"), base_url),
                "title": str(tag.get("title") or ""),
                "mime_type": (source.get("type") if source else "") or "",
                **_attrs(tag),
            },
        )
    if name == "a":
        url = _safe_url(tag.get("href"), base_url)
        children = _inline_children(tag, base_url, depth + 1)
        return ContentBlock(
            "link" if url else "paragraph",
            url=url,
            text="" if children else tag.get_text(" ", strip=True),
            children=children,
            attrs=_attrs(tag),
        )
    if name == "pre":
        code = tag.find("code")
        language = ""
        if code:
            language = next(
                (c.removeprefix("language-") for c in code.get("class", []) if c.startswith("language-")), ""
            )
        return ContentBlock("code", text=tag.get_text("", strip=False), attrs={"language": language, **_attrs(tag)})
    if name == "blockquote":
        return ContentBlock("quote", children=_children(tag, base_url, depth + 1), attrs=_attrs(tag))
    if name in {"ul", "ol"}:
        items = [
            b
            for li in tag.find_all("li", recursive=False)
            if (b := ContentBlock("list_item", children=_children(li, base_url, depth + 1), attrs=_attrs(li)))
        ]
        return ContentBlock("list", children=items, attrs={"ordered": name == "ol", **_attrs(tag)})
    if name == "table":
        rows = []
        for row in tag.find_all("tr"):
            cells = [
                ContentBlock(
                    "table_cell",
                    children=_inline_children(cell, base_url, depth + 1),
                    attrs={"header": cell.name.lower() == "th", **_attrs(cell)},
                )
                for cell in row.find_all(["th", "td"], recursive=False)
            ]
            rows.append(ContentBlock("table_row", children=cells))
        return ContentBlock("table", children=rows, attrs=_attrs(tag))
    if name.startswith("h") and len(name) == 2 and name[1].isdigit():
        return ContentBlock(
            "heading", children=_inline_children(tag, base_url, depth + 1), attrs={"level": int(name[1]), **_attrs(tag)}
        )
    if name == "hr":
        return ContentBlock("text", text="\n")
    if name == "p":
        return ContentBlock("paragraph", children=_inline_children(tag, base_url, depth + 1), attrs=_attrs(tag))
    children = _children(tag, base_url, depth + 1)
    if name in {"div", "section", "article", "header", "footer"}:
        return ContentBlock("paragraph", children=children, attrs=_attrs(tag)) if children else None
    if name in _BLOCK_TAGS or children:
        return children[0] if len(children) == 1 else ContentBlock("paragraph", children=children)
    text = tag.get_text(" ", strip=True)
    return ContentBlock("text", text=text) if text else None


def _children(node: Tag, base_url: str, depth: int = 0) -> list[ContentBlock]:
    if depth >= _MAX_CONTENT_DEPTH:
        return _flatten_deep_content(node, base_url)
    result: list[ContentBlock] = []
    pending_text = ""
    for child in node.children:
        if isinstance(child, NavigableString):
            text = str(child)
            if text.strip():
                pending_text += text
            continue
        if not isinstance(child, Tag):
            continue
        if pending_text:
            result.append(ContentBlock("paragraph", text=pending_text.strip()))
            pending_text = ""
        if depth + 1 >= _MAX_CONTENT_DEPTH:
            result.extend(_flatten_deep_content(child, base_url))
            continue
        block = _block(child, base_url, depth + 1)
        if block:
            result.append(block)
    if pending_text:
        result.append(ContentBlock("paragraph", text=pending_text.strip()))
    return result


def _flatten_block_subtree(blocks: list[ContentBlock]) -> list[ContentBlock]:
    result: list[ContentBlock] = []
    pending = list(reversed(blocks))
    while pending:
        block = pending.pop()
        kind = block.type.lower()
        if kind in {"image", "video", "audio", "attachment"} and block.url:
            result.append(ContentBlock(block.type, block.text, block.url, attrs=dict(block.attrs)))
        elif kind == "link" and block.url and (block.text or not block.children):
            result.append(ContentBlock("link", text=block.text or block.url, url=block.url, attrs=dict(block.attrs)))
        elif block.text:
            result.append(ContentBlock("text", text=block.text))
        pending.extend(reversed(block.children))
    return result


def _bound_block_depth(blocks: list[ContentBlock]) -> list[ContentBlock]:
    pending = [(block, 1) for block in blocks]
    while pending:
        block, depth = pending.pop()
        if depth >= _MAX_CONTENT_DEPTH - 1 and block.children:
            block.children = _flatten_block_subtree(block.children)
        else:
            pending.extend((child, depth + 1) for child in block.children)
    return blocks


def normalize_html(value: str | bytes | None, base_url: str = "") -> list[ContentBlock]:
    if not value:
        return []
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    soup = BeautifulSoup(value, "html.parser")
    root = soup.body or soup
    return _bound_block_depth(_children(root, base_url))
