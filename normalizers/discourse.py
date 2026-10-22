from __future__ import annotations

import re
from dataclasses import replace
from urllib.parse import urljoin, urlsplit

from ..core.models import Article, ContentBlock
from .media_content import normalize_media_content_blocks

_STATS = re.compile(r"^(?:\d+\s+posts?\s*-\s*\d+\s+participants?|\d+\s*个帖子\s*-\s*\d+\s*位参与者)$", re.I)
_GENERATED_HEADING_FRAGMENT = re.compile(r"^p-\d+-h-\d+$", re.I)
_READ = {"read full topic", "阅读完整话题"}


def _plain(block: ContentBlock) -> str:
    return block.text + "".join(_plain(child) for child in block.children)


def _simple_text(block: ContentBlock) -> str | None:
    if block.type.lower() not in {"paragraph", "text"}:
        return None
    if any(child.type.lower() != "text" or child.children for child in block.children):
        return None
    return block.text + "".join(child.text for child in block.children)


def _links(block: ContentBlock):
    if block.type.lower() == "link" and block.url:
        yield block.url, _plain(block).strip()
    for child in block.children:
        yield from _links(child)


def _footer_link_text(block: ContentBlock) -> str | None:
    if block.type.lower() != "paragraph":
        return None
    if block.text.strip():
        return None
    links = [child for child in block.children if child.type.lower() == "link"]
    if len(links) != 1 or any(
        child.type.lower() != "link" and (child.type.lower() != "text" or child.text.strip() or child.children)
        for child in block.children
    ):
        return None
    link = links[0]
    if any(child.type.lower() != "text" for child in link.children):
        return None
    return _plain(link).strip()


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
    return parsed.hostname.lower(), topic_id


def _direct_image_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return False
    path = parsed.path.lower()
    return bool(
        re.search(r"\.(?:gif|jpeg|jpg|png|webp)$", path)
        or re.search(r"/uploads/[^/]+/(?:original|optimized)/.+$", path)
    )


def _is_linux_do_url(url: str) -> bool:
    try:
        hostname = (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    return hostname in {"linux.do", "www.linux.do"}


def _has_anchor_class(block: ContentBlock) -> bool:
    classes = block.attrs.get("class", [])
    if isinstance(classes, str):
        classes = classes.split()
    return "anchor" in classes


def _anchor_fragment(url: str) -> str:
    try:
        return urlsplit(url).fragment
    except ValueError:
        return ""


def _same_hostname(url: str, canonical_url: str) -> bool:
    try:
        expected = urlsplit(canonical_url).hostname
        actual = urlsplit(urljoin(canonical_url, url)).hostname
    except (TypeError, ValueError):
        return False
    return bool(expected and actual and expected.lower().rstrip(".") == actual.lower().rstrip("."))


def _has_non_text_content(block: ContentBlock) -> bool:
    if block.type.lower() not in {"text", "paragraph"}:
        return True
    return any(_has_non_text_content(child) for child in block.children)


def _is_decorative_anchor(block: ContentBlock) -> bool:
    if any(_has_non_text_content(child) for child in block.children):
        return False
    text = _plain(block).strip()
    return not text or all(char.isspace() or char in "#¶🔗\ufe0f\u200d" for char in text)


def strip_discourse_heading_anchors(
    blocks: list[ContentBlock], canonical_url: str, *, is_discourse: bool = False
) -> list[ContentBlock]:
    """Remove generated Discourse title anchors while preserving their visible text."""
    is_discourse_site = is_discourse or _is_linux_do_url(canonical_url)
    if not is_discourse_site:
        return blocks

    def visit(items: list[ContentBlock], *, in_heading: bool = False) -> list[ContentBlock]:
        result: list[ContentBlock] = []
        for block in items:
            kind = block.type.lower()
            heading_context = in_heading or kind == "heading"
            children = visit(block.children, in_heading=heading_context) if block.children else []
            current = ContentBlock(block.type, block.text, block.url, children, dict(block.attrs))
            fragment = _anchor_fragment(current.url) if kind == "link" and current.url else ""
            same_discourse_site = is_discourse_site and _same_hostname(current.url, canonical_url)
            if fragment and _has_anchor_class(current) and same_discourse_site:
                attrs_match = fragment in {
                    str(current.attrs.get("name") or ""),
                    str(current.attrs.get("id") or ""),
                }
                generated_on_discourse = bool(_GENERATED_HEADING_FRAGMENT.fullmatch(fragment))
                generated_heading_anchor = heading_context and attrs_match
                if generated_on_discourse or generated_heading_anchor:
                    if _is_decorative_anchor(current):
                        continue
                    if current.text:
                        result.append(ContentBlock("text", text=current.text))
                    result.extend(children)
                    continue
            result.append(current)
        return result

    return visit(blocks)


def normalize_image_links(blocks: list[ContentBlock], *, recognize_image_anchors: bool = True) -> list[ContentBlock]:
    """Unwrap links around images and optionally recognize Discourse image labels."""
    result: list[ContentBlock] = []
    for block in blocks:
        children = (
            normalize_image_links(block.children, recognize_image_anchors=recognize_image_anchors)
            if block.children
            else []
        )
        current = ContentBlock(block.type, block.text, block.url, children, dict(block.attrs))
        if current.type.lower() == "link" and current.url:
            image_children = any(_contains_image(child) for child in children)
            label = _plain(current).strip()
            explicit_image_label = label.casefold() in {"image", "图片"} or bool(current.attrs.get("alt"))
            classes = current.attrs.get("class", [])
            if isinstance(classes, str):
                classes = [classes]
            protected_upload_link = "stripped-secure-view-upload" in classes
            if protected_upload_link:
                alt = next(
                    (
                        str(child.attrs.get("alt"))
                        for child in children
                        if _contains_image(child) and child.attrs.get("alt")
                    ),
                    "",
                )
                result.append(
                    ContentBlock(
                        "link", url=current.url, text=label or alt or "查看受保护图片", attrs=dict(current.attrs)
                    )
                )
                continue
            lightbox_class = bool({"lightbox", "lightbox-wrapper"}.intersection(classes))
            if lightbox_class and not image_children and not label and _direct_image_url(current.url):
                alt = str(current.attrs.get("alt") or current.attrs.get("title") or "图片")
                result.append(ContentBlock("image", url=current.url, attrs={"alt": alt}))
                continue
            if image_children:
                if current.text:
                    result.append(ContentBlock("text", text=current.text))
                lightbox_original = lightbox_class and _direct_image_url(current.url)
                result.extend(_replace_image_urls(children, current.url) if lightbox_original else children)
                if not _direct_image_url(current.url):
                    result.append(ContentBlock("link", url=current.url, text="相关链接", attrs=dict(current.attrs)))
                continue
            if recognize_image_anchors and explicit_image_label and _direct_image_url(current.url):
                alt = str(current.attrs.get("alt") or current.attrs.get("title") or label)
                result.append(ContentBlock("image", url=current.url, attrs={"alt": alt}))
                continue
        result.append(current)
    return result


def _contains_image(block: ContentBlock) -> bool:
    return block.type.lower() == "image" or any(_contains_image(child) for child in block.children)


def _replace_image_urls(blocks: list[ContentBlock], url: str) -> list[ContentBlock]:
    result = []
    for block in blocks:
        if block.type.lower() == "image":
            result.append(replace(block, url=url))
        else:
            result.append(
                ContentBlock(
                    block.type,
                    block.text,
                    block.url,
                    _replace_image_urls(block.children, url),
                    dict(block.attrs),
                )
            )
    return result


def strip_discourse_footer(blocks: list[ContentBlock], canonical_url: str) -> list[ContentBlock]:
    """Remove Discourse's generated trailing statistics and same-topic link pair."""
    expected = _topic(canonical_url)
    if not expected:
        return blocks
    result = list(blocks)
    if len(result) >= 2:
        stats = _simple_text(result[-2])
        label = _footer_link_text(result[-1])
        links = list(_links(result[-1]))
        if stats and _STATS.fullmatch(stats.strip()) and label and label.casefold() in _READ:
            if len(links) == 1 and _topic(links[0][0]) == expected:
                return result[:-2]

    # Discourse descriptions can be wrapped in one or more trailing divs.
    # Inspect only the final ordinary container, so earlier body sections are
    # never reinterpreted as generated footer material.
    if not result:
        return blocks
    wrapper = result[-1]
    if (
        wrapper.type.lower() != "paragraph"
        or len(wrapper.children) < 2
        or not any(child.type.lower() == "paragraph" for child in wrapper.children)
    ):
        return blocks
    cleaned_children = strip_discourse_footer(wrapper.children, canonical_url)
    if cleaned_children is wrapper.children:
        return blocks
    result[-1] = ContentBlock(wrapper.type, wrapper.text, wrapper.url, cleaned_children, dict(wrapper.attrs))
    if not cleaned_children and not wrapper.text.strip():
        result.pop()
    return result


def clean_cached_article(article: Article) -> Article:
    cleaned = article.content_blocks
    is_discourse = article.metadata.get("source_adapter") == "discourse"
    if is_discourse:
        cleaned = strip_discourse_footer(cleaned, article.canonical_url)
        cleaned = normalize_image_links(cleaned)
    cleaned = strip_discourse_heading_anchors(cleaned, article.canonical_url, is_discourse=is_discourse)
    cleaned = normalize_media_content_blocks(cleaned, article.metadata.get("media", []))
    if cleaned == article.content_blocks:
        return article
    return replace(article, content_blocks=cleaned)
