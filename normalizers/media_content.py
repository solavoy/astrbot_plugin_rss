from __future__ import annotations

from urllib.parse import urlsplit

from ..core.models import Article, ContentBlock

_IMAGE_EXTENSIONS = (".gif", ".jpeg", ".jpg", ".png", ".webp", ".avif")
_VIDEO_EXTENSIONS = (".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi", ".mpeg", ".mpg")


def _url_key(value: object) -> str:
    url = str(value or "").strip()
    try:
        parts = urlsplit(url)
        if parts.scheme and parts.hostname:
            host = parts.hostname.lower()
            port = f":{parts.port}" if parts.port else ""
            return f"{parts.scheme.lower()}://{host}{port}{parts.path}?{parts.query}"
    except ValueError:
        pass
    return url


def media_kind(item: dict) -> str:
    medium = str(item.get("medium") or "").strip().lower()
    media_type = str(item.get("type") or "").strip().lower()
    if medium in {"image", "video"}:
        return medium
    if media_type.startswith("image/"):
        return "image"
    if media_type.startswith("video/"):
        return "video"
    try:
        path = urlsplit(str(item.get("url") or "")).path.lower()
    except ValueError:
        path = ""
    if path.endswith(_IMAGE_EXTENSIONS):
        return "image"
    if path.endswith(_VIDEO_EXTENSIONS):
        return "video"
    return ""


def associate_media_thumbnails(
    media_items: list[dict],
    associations: list[tuple[str, str]] | None = None,
    *,
    allow_single_pair_fallback: bool = True,
) -> list[dict]:
    """Mark only unambiguous thumbnail-to-original relationships."""
    result = [dict(item) for item in media_items if isinstance(item, dict)]
    originals = [item for item in result if item.get("role") != "thumbnail" and media_kind(item)]
    thumbnails = [item for item in result if item.get("role") == "thumbnail"]

    parents_by_thumbnail: dict[str, set[str]] = {}
    for original_url, thumbnail_url in associations or []:
        original_key = _url_key(original_url)
        thumbnail_key = _url_key(thumbnail_url)
        if original_key and thumbnail_key:
            parents_by_thumbnail.setdefault(thumbnail_key, set()).add(original_key)

    originals_by_key = {_url_key(item.get("url")): item for item in originals if _url_key(item.get("url"))}

    for thumbnail in thumbnails:
        thumb_key = _url_key(thumbnail.get("url"))
        explicit_parents = parents_by_thumbnail.get(thumb_key, set())
        if len(explicit_parents) == 1:
            parent = originals_by_key.get(next(iter(explicit_parents)))
            if parent:
                thumbnail["associated_url"] = parent.get("url", "")
                continue
        exact = [item for item in originals if thumb_key and _url_key(item.get("url")) == thumb_key]
        if len(exact) == 1:
            thumbnail["associated_url"] = exact[0].get("url", "")
    unassociated = [item for item in thumbnails if not item.get("associated_url")]
    if allow_single_pair_fallback and len(originals) == 1 and len(thumbnails) == 1 and len(unassociated) == 1:
        original = originals[0]
        thumb_kind = media_kind({**unassociated[0], "medium": "image"})
        if media_kind(original) in {"image", "video"} and thumb_kind == "image":
            unassociated[0]["associated_url"] = original.get("url", "")
    return result


def normalize_media_content_blocks(blocks: list[ContentBlock], media_items: list[dict]) -> list[ContentBlock]:
    """Remove media links that repeat RSS media and fold associated thumbnails into metadata."""
    items = associate_media_thumbnails(media_items if isinstance(media_items, list) else [])
    originals = {
        _url_key(item.get("url")): item
        for item in items
        if item.get("role") != "thumbnail" and _url_key(item.get("url"))
    }
    associated: dict[str, dict] = {}
    associated_thumbnail_keys: set[str] = set()
    for item in items:
        if item.get("role") != "thumbnail" or not item.get("associated_url"):
            continue
        original_key = _url_key(item.get("associated_url"))
        thumbnail_key = _url_key(item.get("url"))
        if original_key in originals and thumbnail_key:
            associated.setdefault(original_key, item)
            associated_thumbnail_keys.add(thumbnail_key)

    def walk(blocks_to_clean: list[ContentBlock]) -> list[ContentBlock]:
        cleaned: list[ContentBlock] = []
        for block in blocks_to_clean:
            kind = block.type.lower()
            url_key = _url_key(block.url)
            label = (block.text + "".join(_plain(child) for child in block.children)).strip().casefold()
            media = originals.get(url_key)
            expected_kind = "image" if label == "view image" else "video" if label == "view video" else ""
            if kind == "link" and media and expected_kind and media_kind(media) == expected_kind:
                continue
            if kind == "image" and block.attrs.get("role") == "thumbnail" and url_key in associated_thumbnail_keys:
                continue

            children = walk(block.children) if block.children else []
            current = ContentBlock(block.type, block.text, block.url, children, dict(block.attrs))
            if kind in {"image", "video"} and media:
                thumbnail = associated.get(url_key)
                if thumbnail:
                    thumb_url = str(thumbnail.get("url") or "")
                    if kind == "video":
                        current.attrs.setdefault("poster", thumb_url)
                    current.attrs.setdefault("thumbnail_url", thumb_url)
            if kind in {"paragraph", "text"} and not current.text.strip() and not current.url and not current.children:
                continue
            cleaned.append(current)
        return cleaned

    return walk(blocks)


def _plain(block: ContentBlock) -> str:
    return block.text + "".join(_plain(child) for child in block.children)


def is_pure_video_article(article: Article) -> bool:
    """Return true when the body contains video media and no other meaningful content."""
    found_video = False

    def only_video(blocks: list[ContentBlock]) -> bool:
        nonlocal found_video
        for block in blocks:
            kind = block.type.lower()
            if kind == "video" and block.url:
                found_video = True
                if block.text.strip() or block.children:
                    return False
                continue
            if block.text.strip() or block.url:
                return False
            if block.children and not only_video(block.children):
                return False
            if kind not in {"paragraph", "text", "div", "section", "article", "quote"} and not block.children:
                return False
        return True

    return only_video(article.content_blocks) and found_video
