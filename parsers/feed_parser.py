from __future__ import annotations

import calendar
import datetime
import email.utils
import hashlib
import html
import xml.etree.ElementTree as ET
import xml.parsers.expat
from typing import Any
from urllib.parse import urljoin, urlsplit

import feedparser

from ..core.models import Article, ContentBlock, FetchResult, ParsedFeed
from ..normalizers.markdown_content import clean_markdown, html_to_markdown
from ..normalizers.media_content import associate_media_thumbnails
from .adapters import DEFAULT_ADAPTERS, SourceAdapter


class FeedParseError(ValueError):
    pass


def _date(value: Any) -> float | None:
    if not value:
        return None
    if hasattr(value, "tm_year"):
        return float(calendar.timegm(value))
    if isinstance(value, str):
        try:
            parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=datetime.UTC)
            return parsed.timestamp()
        except ValueError:
            try:
                parsed = email.utils.parsedate_to_datetime(value)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=datetime.UTC)
                return parsed.timestamp()
            except (TypeError, ValueError, OverflowError):
                return None
    return None


def _clean_link(value: Any, base_url: str) -> str:
    value = str(value or "").strip()
    if not value:
        return base_url
    try:
        resolved = urljoin(base_url, value)
        parsed = urlsplit(resolved)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return ""
        if parsed.username or parsed.password:
            return ""
        _ = parsed.port
        return resolved
    except ValueError:
        return ""


def _dimension(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value).strip().removesuffix("px"))
    except (TypeError, ValueError):
        return None


def _nested_media_thumbnails(data: bytes) -> list[list[tuple[str, str]]]:
    """Read media:thumbnail parentage that feedparser flattens into separate arrays."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return []

    def local_name(tag: object) -> str:
        return str(tag).rsplit("}", 1)[-1].rsplit(":", 1)[-1].lower()

    def is_media_tag(tag: object, expected: str) -> bool:
        name = str(tag)
        return local_name(name) == expected and "search.yahoo.com/mrss" in name.lower()

    result: list[list[tuple[str, str]]] = []
    for entry_element in root.iter():
        if local_name(entry_element.tag) not in {"item", "entry"}:
            continue
        relations: list[tuple[str, str]] = []
        for content in entry_element.iter():
            if not is_media_tag(content.tag, "content"):
                continue
            original_url = str(content.attrib.get("url") or "").strip()
            if not original_url:
                continue
            for thumbnail in content.iter():
                if thumbnail is content or not is_media_tag(thumbnail.tag, "thumbnail"):
                    continue
                thumbnail_url = str(thumbnail.attrib.get("url") or "").strip()
                if thumbnail_url:
                    relations.append((original_url, thumbnail_url))
        result.append(relations)
    return result


class FeedParser:
    def __init__(self, adapters: tuple[SourceAdapter, ...] = DEFAULT_ADAPTERS) -> None:
        self.adapters = adapters

    @staticmethod
    def _validate_xml(data: bytes) -> str:
        parser = xml.parsers.expat.ParserCreate()
        root: list[str] = []
        parser.StartElementHandler = lambda name, _attrs: (
            root.append(name.rsplit(":", 1)[-1].lower()) if not root else None
        )

        def reject_doctype(*_args):
            raise FeedParseError("DOCTYPE and entity declarations are disabled")

        parser.StartDoctypeDeclHandler = reject_doctype
        parser.EntityDeclHandler = reject_doctype
        parser.ExternalEntityRefHandler = lambda *_args: 0
        try:
            parser.Parse(data, True)
            if not root or root[0] not in {"rss", "feed", "rdf"}:
                raise FeedParseError("XML root is not RSS, Atom, or RDF")
            return root[0]
        except FeedParseError:
            raise
        except xml.parsers.expat.ExpatError as exc:
            raise FeedParseError(f"invalid feed XML: {exc}") from exc

    def parse(self, result: FetchResult) -> ParsedFeed:
        if result.status_code == 304:
            return ParsedFeed(
                metadata={"not_modified": True, "etag": result.etag, "last_modified": result.last_modified}
            )
        if result.error_type:
            raise FeedParseError(f"cannot parse failed fetch: {result.error_type}")
        if not result.raw_xml:
            raise FeedParseError("empty feed response")
        self._validate_xml(result.raw_xml)
        parsed = feedparser.parse(
            result.raw_xml, response_headers={"content-location": result.final_url or result.requested_url}
        )
        if parsed.bozo and not parsed.entries:
            raise FeedParseError(f"feedparser rejected XML: {parsed.bozo_exception}")
        entries = list(parsed.entries)
        media_relations = _nested_media_thumbnails(result.raw_xml)
        if len(media_relations) != len(entries):
            media_relations = []
        adapter = next(
            (candidate for candidate in self.adapters if candidate.matches(parsed, entries)), self.adapters[-1]
        )
        source_url = result.final_url or result.requested_url
        feed_title = str(getattr(parsed.feed, "title", "") or "")
        feed_description = str(getattr(parsed.feed, "subtitle", "") or getattr(parsed.feed, "description", "") or "")
        articles: list[Article] = []
        for entry_index, entry in enumerate(entries):
            fixes = adapter.normalize_entry(entry)
            title = str(entry.get("title", "") or "").strip()
            link = _clean_link(entry.get("link", ""), source_url)
            entry_id = str(entry.get("id") or entry.get("guid") or entry.get("link") or "").strip()
            if not entry_id:
                body_seed = repr(entry.get("content") or entry.get("summary") or entry.get("description") or "")
                entry_id = hashlib.sha256(
                    f"{title}\0{entry.get('published', '')}\0{link}\0{body_seed}".encode()
                ).hexdigest()
            author = str(entry.get("author") or entry.get("dc_creator") or entry.get("creator") or "")
            categories = [str(tag.get("term") or tag.get("label") or "") for tag in entry.get("tags", [])]
            categories = [category for category in categories if category]
            attachments = []
            media_items = []
            for item in entry.get("media_content", []) or []:
                media_url = _clean_link(item.get("url"), link)
                if media_url:
                    normalized = {
                        "url": media_url,
                        "type": item.get("type", ""),
                        "medium": item.get("medium", ""),
                    }
                    for dimension in ("width", "height"):
                        value = _dimension(item.get(dimension))
                        if value is not None:
                            normalized[dimension] = value
                    media_items.append(normalized)
            for thumb in entry.get("media_thumbnail", []) or []:
                thumb_url = _clean_link(thumb.get("url"), link)
                if thumb_url:
                    normalized = {"url": thumb_url, "role": "thumbnail"}
                    for dimension in ("width", "height"):
                        value = _dimension(thumb.get(dimension))
                        if value is not None:
                            normalized[dimension] = value
                    media_items.append(normalized)
            nested_relations = [
                (_clean_link(original, link), _clean_link(thumbnail, link))
                for original, thumbnail in (media_relations[entry_index] if entry_index < len(media_relations) else [])
            ]
            nested_relations = [(original, thumb) for original, thumb in nested_relations if original and thumb]
            media_items = associate_media_thumbnails(
                media_items,
                nested_relations,
                allow_single_pair_fallback=not nested_relations,
            )
            for enclosure in entry.get("enclosures", []):
                href = _clean_link(enclosure.get("href"), link)
                if href and href != link:
                    attachments.append(
                        {
                            "url": href,
                            "type": enclosure.get("type", ""),
                            "length": enclosure.get("length"),
                            "role": "attachment",
                        }
                    )
            markdown = self._content_markdown(
                entry,
                link,
                media_items,
                attachments=attachments,
                is_discourse=adapter.name == "discourse",
            )
            metadata = {
                "source_adapter": adapter.name,
                "feed_title": feed_title,
                "feed_generator": getattr(parsed.feed, "generator", ""),
                "attachments": attachments,
                "media": media_items,
            }
            metadata.update({k: v for k, v in fixes.items() if v is not None})
            articles.append(
                Article(
                    article_id="",
                    feed_id=result.feed_id,
                    entry_id=entry_id,
                    canonical_url=link,
                    title=title,
                    author=author,
                    published_at=_date(
                        dict.get(entry, "published_parsed")
                        or dict.get(entry, "published")
                        or dict.get(entry, "updated_parsed")
                        or dict.get(entry, "updated")
                    ),
                    updated_at=_date(dict.get(entry, "updated_parsed") or dict.get(entry, "updated")),
                    categories=categories,
                    content_blocks=[ContentBlock("markdown", text=markdown)],
                    metadata=metadata,
                )
            )
        metadata = {
            "version": getattr(parsed.feed, "version", ""),
            "bozo": bool(parsed.bozo),
            "bozo_exception": str(parsed.bozo_exception)[:300] if parsed.bozo else "",
            "source_adapter": adapter.name,
            "etag": result.etag,
            "last_modified": result.last_modified,
        }
        return ParsedFeed(feed_title, feed_description, articles, metadata)

    @staticmethod
    def _content_markdown(
        entry: Any,
        base_url: str,
        media: list[dict],
        *,
        attachments: list[dict] | None = None,
        is_discourse: bool = False,
    ) -> str:
        candidates = []
        # Prefer Atom content / content:encoded, then summary, then description.
        for content in entry.get("content", []) or []:
            candidates.append((content.get("value", ""), content.get("type", "html")))
        for key in ("summary", "description"):
            if entry.get(key):
                candidates.append((entry.get(key), entry.get(f"{key}_type", "html")))
        conversion_errors = []
        for value, content_type in candidates:
            if value:
                candidate_media: list[dict] = []
                try:
                    if "html" in str(content_type).lower() or "xhtml" in str(content_type).lower() or "<" in str(value):
                        markdown = html_to_markdown(
                            str(value), base_url, media=candidate_media, is_discourse=is_discourse
                        )
                    else:
                        escaped = html.escape(str(value), quote=False)
                        markdown = html_to_markdown(
                            f"<p>{escaped}</p>", base_url, media=candidate_media, is_discourse=is_discourse
                        )
                    media.extend(candidate_media)
                    return clean_markdown(
                        markdown,
                        base_url,
                        is_discourse=is_discourse,
                        media=media,
                        attachments=attachments or [],
                    )
                except ValueError as exc:
                    conversion_errors.append(str(exc))
        if conversion_errors:
            raise FeedParseError(f"could not convert feed HTML: {conversion_errors[0]}")
        return ""
