from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


def now_ts() -> float:
    return datetime.now(UTC).timestamp()


@dataclass(slots=True)
class ContentBlock:
    type: str
    text: str = ""
    url: str = ""
    children: list[ContentBlock] = field(default_factory=list)
    attrs: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "text": self.text,
            "url": self.url,
            "children": [child.to_dict() for child in self.children],
            "attrs": self.attrs,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ContentBlock:
        return cls(
            type=value["type"],
            text=value.get("text", ""),
            url=value.get("url", ""),
            children=[cls.from_dict(item) for item in value.get("children", [])],
            attrs=dict(value.get("attrs", {})),
        )


@dataclass(slots=True)
class Article:
    article_id: str
    feed_id: str
    entry_id: str
    canonical_url: str
    title: str
    author: str = ""
    published_at: float | None = None
    updated_at: float | None = None
    first_seen_at: float = field(default_factory=now_ts)
    categories: list[str] = field(default_factory=list)
    content_hash: str = ""
    content_blocks: list[ContentBlock] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.article_id:
            self.article_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{self.feed_id}:{self.entry_id}"))
        if not self.content_hash:
            raw = json.dumps([b.to_dict() for b in self.content_blocks], ensure_ascii=False, sort_keys=True)
            self.content_hash = hashlib.sha256(raw.encode()).hexdigest()


@dataclass(slots=True)
class Feed:
    feed_id: str
    url: str
    title: str = ""
    description: str = ""
    enabled: bool = True
    poll_interval: int | None = None
    etag: str | None = None
    last_modified: str | None = None
    initialized: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    public_id: int | None = None
    translation_enabled: bool = False


@dataclass(slots=True)
class DeliveryTarget:
    target_id: str
    platform_instance: str
    unified_origin: str
    platform: str
    session_id: str
    session_type: str = ""
    label: str = ""


@dataclass(slots=True)
class FetchResult:
    feed_id: str
    requested_url: str
    final_url: str = ""
    status_code: int = 0
    content_type: str = ""
    raw_xml: bytes = b""
    etag: str | None = None
    last_modified: str | None = None
    fetched_at: float = field(default_factory=now_ts)
    fetch_method: str = "curl"
    elapsed_ms: int = 0
    error_type: str | None = None
    error_message: str | None = None
    cookie_cache_hit: bool = False
    browser_attempted: bool = False
    retry_after_at: float | None = None
    request_url_fingerprint: str | None = None


@dataclass(slots=True)
class ParsedFeed:
    title: str = ""
    description: str = ""
    articles: list[Article] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RenderedMessage:
    renderer: str
    message_type: str
    payload: dict[str, Any] | list[Any]
    article_ids: list[str]
    batch_id: str = ""


@dataclass(slots=True)
class SendResult:
    platform_message_id: str | None = None
