from __future__ import annotations

import re
from urllib.parse import quote, urlsplit, urlunsplit

from ..core.models import Article

_IMAGE_EXTENSIONS = (".gif", ".jpeg", ".jpg", ".png", ".webp", ".avif", ".bmp", ".svg")
_VIDEO_EXTENSIONS = (".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi", ".mpeg", ".mpg", ".ogv")
_AUDIO_EXTENSIONS = (".mp3", ".m4a", ".aac", ".wav", ".ogg", ".opus", ".flac", ".oga", ".weba")


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


def media_kind(item: dict) -> str:
    medium = str(item.get("medium") or "").strip().lower()
    media_type = str(item.get("type") or item.get("mime_type") or "").strip().lower()
    if medium in {"image", "video", "audio"}:
        return medium
    if media_type.startswith("image/"):
        return "image"
    if media_type.startswith("video/"):
        return "video"
    if media_type.startswith("audio/"):
        return "audio"
    try:
        path = urlsplit(str(item.get("url") or "")).path.lower()
    except ValueError:
        path = ""
    if path.endswith(_IMAGE_EXTENSIONS):
        return "image"
    if path.endswith(_VIDEO_EXTENSIONS):
        return "video"
    if path.endswith(_AUDIO_EXTENSIONS):
        return "audio"
    return ""


def associate_media_thumbnails(
    media_items: list[dict],
    associations: list[tuple[str, str]] | None = None,
    *,
    allow_single_pair_fallback: bool = True,
) -> list[dict]:
    """Mark only unambiguous thumbnail-to-original relationships from feed XML."""
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
        if media_kind(original) in {"image", "video"}:
            thumbnail = unassociated[0]
            if media_kind({**thumbnail, "medium": "image"}) == "image":
                thumbnail["associated_url"] = original.get("url", "")
    return result


def is_pure_video_article(article: Article) -> bool:
    """Compatibility export; the decision now comes from Markdown tokens."""
    from .markdown_content import is_pure_video_article as markdown_is_pure_video_article

    return markdown_is_pure_video_article(article)
