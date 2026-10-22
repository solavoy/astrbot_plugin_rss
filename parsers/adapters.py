from __future__ import annotations

from typing import Any, Protocol

from ..normalizers.discourse import strip_discourse_footer
from ..normalizers.html_content import normalize_html


class SourceAdapter(Protocol):
    name: str

    def matches(self, parsed: Any, entries: list[Any]) -> bool: ...
    def normalize_entry(self, entry: Any) -> dict[str, Any]: ...


class GenericAdapter:
    name = "generic"

    def matches(self, parsed: Any, entries: list[Any]) -> bool:
        return True

    def normalize_entry(self, entry: Any) -> dict[str, Any]:
        return {}


class DiscourseAdapter(GenericAdapter):
    name = "discourse"

    def matches(self, parsed: Any, entries: list[Any]) -> bool:
        generator = str(getattr(parsed.feed, "generator", "")).lower()
        if "discourse" in generator or any(
            any(key.startswith("discourse_") for key in item.keys()) for item in entries
        ):
            return True
        # Some Discourse mirrors strip the generator and namespace declarations.
        # The generated, paired trailing footer is a reliable structural marker.
        for entry in entries:
            canonical = str(entry.get("link") or "")
            values = [value.get("value", "") for value in entry.get("content", []) or []]
            values.extend(str(entry.get(key) or "") for key in ("summary", "description"))
            for value in values:
                text = str(value)
                folded = text.casefold()
                has_read_label = "read full topic" in folded or "阅读完整话题" in text
                has_stats_terms = ("posts" in folded and "participants" in folded) or (
                    "个帖子" in text and "位参与者" in text
                )
                if not has_read_label or not has_stats_terms:
                    continue
                blocks = normalize_html(text, canonical)
                if strip_discourse_footer(blocks, canonical) is not blocks:
                    return True
        return False

    def normalize_entry(self, entry: Any) -> dict[str, Any]:
        value = {}
        for key in (
            "discourse_topic_id",
            "discourse_post_number",
            "discourse_username",
            "discourse_user_title",
            "discourse_avatar",
        ):
            if key in entry:
                value[key] = entry.get(key)
        return value


class RSSHubAdapter(GenericAdapter):
    name = "rsshub"

    def matches(self, parsed: Any, entries: list[Any]) -> bool:
        generator = str(getattr(parsed.feed, "generator", "")).lower()
        return "rsshub" in generator or "docs.rsshub.app" in str(getattr(parsed.feed, "link", "")).lower()


DEFAULT_ADAPTERS: tuple[SourceAdapter, ...] = (DiscourseAdapter(), RSSHubAdapter(), GenericAdapter())
