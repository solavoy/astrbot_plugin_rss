from __future__ import annotations

import asyncio
import re
import uuid

from ..core.errors import FeedFetchError
from ..core.feed_urls import build_feed_request, is_relative_feed_reference, normalize_relative_feed_reference
from ..core.intervals import parse_interval_seconds
from ..core.models import Feed
from ..core.security import validate_public_url
from ..storage.interval_inputs import interval_input_text


def normalize_poll_interval(value):
    if value is None:
        return None
    return parse_interval_seconds(value) or None


def _public_id_ref(value: str) -> int | None:
    if not re.fullmatch(r"[0-9]+", value) or len(value) > 19:
        return None
    try:
        number = int(value)
    except ValueError:
        return None
    return number if 0 < number <= 9_223_372_036_854_775_807 else None


class ServiceFacade:
    """Business API shared by commands, WebAPI, and runtime integrations."""

    def __init__(self, runtime):
        self.runtime = runtime
        self.repo = runtime.repository
        self.feeds = _Feeds(self)
        self.subscriptions = _Subscriptions(self)
        self.lists = _Lists(self)
        self.targets = _Targets(self)
        self.delivery = _Delivery(self)
        self.data = _Data(self)


class _Feeds:
    def __init__(self, service):
        self.s = service
        self._create_locks: dict[str, asyncio.Lock] = {}

    async def list(self):
        return await self.s.repo.list_feeds()

    async def resolve(self, ref: str) -> Feed:
        if not isinstance(ref, str) or not ref.strip():
            raise ValueError("feed reference is required")
        ref = ref.strip()
        if is_relative_feed_reference(ref):
            route = normalize_relative_feed_reference(ref)
            feed = await self.s.repo.get_feed_by_url(route)
            if feed is None:
                raise KeyError(route)
            return feed
        feed = None
        public_id = _public_id_ref(ref)
        if public_id is not None:
            feed = await self.s.repo.get_feed_by_public_id(public_id)
            if feed is not None:
                return feed
        if feed is None:
            feed = await self.s.repo.get_feed_by_url(ref)
        if feed is not None:
            return feed
        matches = await self.s.repo.get_feeds_by_title(ref)
        if len(matches) > 1:
            raise ValueError(f"订阅源标题“{ref}”不唯一，请使用数字ID或URL")
        if not matches:
            raise KeyError(ref)
        return matches[0]

    async def create(
        self,
        url: str,
        title: str = "",
        description: str = "",
        poll_interval: int | str | None = None,
        translation_enabled: bool = False,
    ):
        if type(translation_enabled) is not bool:
            raise ValueError("translation_enabled must be a boolean")
        raw_interval = poll_interval
        poll_interval = normalize_poll_interval(poll_interval)
        poll_interval_input = interval_input_text(raw_interval, poll_interval)
        relative = is_relative_feed_reference(url)
        if relative:
            normalized_url = normalize_relative_feed_reference(url)
        else:
            request = build_feed_request(url, self.s.runtime.config)
            normalized_url = (await validate_public_url(request.request_url)).url
        lock = self._create_locks.setdefault(normalized_url, asyncio.Lock())
        async with lock:
            existing = await self.s.repo.get_feed_by_url(normalized_url)
            if existing is not None:
                return existing
            if relative:
                request = build_feed_request(normalized_url, self.s.runtime.config)
                await validate_public_url(request.request_url)
            feed_id = str(uuid.uuid5(uuid.NAMESPACE_URL, normalized_url))
            provisional = Feed(
                feed_id,
                normalized_url,
                title,
                description,
                poll_interval=poll_interval,
                translation_enabled=translation_enabled,
            )
            result = await self.s.runtime.fetcher.fetch(provisional, force=True)
            if result.error_type or result.status_code < 200 or result.status_code >= 300:
                await self.s.repo.log_fetch(
                    feed_id,
                    result.fetched_at,
                    result.status_code,
                    result.fetch_method,
                    result.elapsed_ms,
                    result.error_type,
                    result.error_message,
                    result.cookie_cache_hit,
                    result.browser_attempted,
                )
                raise FeedFetchError(
                    result.error_message or result.error_type or f"feed returned HTTP {result.status_code}",
                    status_code=result.status_code if 400 <= result.status_code <= 599 else None,
                )
            try:
                parsed = self.s.runtime.parser.parse(result)
            except Exception:
                await self.s.repo.log_fetch(
                    feed_id,
                    result.fetched_at,
                    result.status_code,
                    result.fetch_method,
                    result.elapsed_ms,
                    "parse_error",
                    "feed response is not a valid feed",
                    result.cookie_cache_hit,
                    result.browser_attempted,
                )
                raise FeedFetchError("feed response is not a valid feed") from None
            await self.s.repo.log_fetch(
                feed_id,
                result.fetched_at,
                result.status_code,
                result.fetch_method,
                result.elapsed_ms,
                result.error_type,
                result.error_message,
                result.cookie_cache_hit,
                result.browser_attempted,
            )
            if parsed.metadata.get("not_modified"):
                raise FeedFetchError("cannot initialize a new feed from a not-modified response")
            return await self.s.repo.create_initialized_feed(
                feed_id,
                normalized_url,
                parsed.title or title,
                parsed.description or description,
                poll_interval,
                result.etag,
                result.last_modified,
                parsed.articles,
                result.fetched_at
                + max(
                    self.s.runtime.config.min_poll_interval, poll_interval or self.s.runtime.config.global_poll_interval
                ),
                metadata={
                    "request_url_fingerprint": getattr(result, "request_url_fingerprint", None) or request.fingerprint
                }
                if request.fingerprint
                else None,
                translation_enabled=translation_enabled,
                poll_interval_input=poll_interval_input,
            )

    async def update(self, feed_id: str, **values):
        if "translation_enabled" in values and type(values["translation_enabled"]) is not bool:
            raise ValueError("translation_enabled must be a boolean")
        if "url" in values:
            reference = values["url"]
            request = build_feed_request(reference, self.s.runtime.config)
            if is_relative_feed_reference(reference):
                route = normalize_relative_feed_reference(reference)
                request = build_feed_request(route, self.s.runtime.config)
                await validate_public_url(request.request_url)
                values["url"] = route
            else:
                await validate_public_url(request.request_url)
        if "poll_interval" in values:
            raw_interval = values["poll_interval"]
            values["poll_interval"] = normalize_poll_interval(raw_interval)
            values["poll_interval_input"] = interval_input_text(raw_interval, values["poll_interval"])
        return await self.s.repo.update_feed(feed_id, **values)

    async def detail(self, feed_id: str):
        feed = await self.s.repo.get_feed(feed_id)
        if feed is None:
            raise KeyError(feed_id)
        return {"feed": feed, "articles": await self.s.runtime.list_feed_articles(feed_id)}

    async def refresh(self, feed_id: str):
        return await self.s.runtime.refresh_feed(feed_id)


class _Subscriptions:
    def __init__(self, service):
        self.s = service

    async def subscribe(self, target_id: str, feed_id: str, interval: int | str | None = None):
        interval = normalize_poll_interval(interval)
        return await self.s.repo.add_direct_subscription(
            target_id, feed_id, interval, self.s.runtime.config.first_subscription_send_history
        )

    async def unsubscribe(self, target_id: str, feed_id: str):
        return await self.s.repo.remove_direct_subscription(target_id, feed_id)

    async def mine(self, target_id: str):
        return await self.s.repo.list_direct_subscriptions(target_id)


class _Lists:
    def __init__(self, service):
        self.s = service

    async def list(self):
        return await self.s.repo.list_lists()

    async def resolve(self, ref: str) -> dict:
        if not isinstance(ref, str) or not ref.strip():
            raise ValueError("list reference is required")
        ref = ref.strip()
        by_id = None
        public_id = _public_id_ref(ref)
        if public_id is not None:
            by_id = await self.s.repo.get_list_by_public_id(public_id)
            if by_id is not None:
                return by_id
        matches = await self.s.repo.get_lists_by_name(ref)
        if len(matches) > 1:
            raise ValueError(f"列表名称“{ref}”不唯一")
        if not matches:
            raise KeyError(ref)
        return matches[0]

    async def create(self, name: str, description: str = "", interval: int | str | None = None):
        raw_interval = interval
        interval = normalize_poll_interval(interval)
        if not isinstance(name, str):
            raise ValueError("list name must be a string")
        name = name.strip()
        if not 1 <= len(name) <= 100:
            raise ValueError("list name must contain 1..100 characters")
        return await self.s.repo.create_list(name, description, interval, interval_input_text(raw_interval, interval))

    async def update(self, list_id: str, **values):
        updates = {}
        if "name" in values:
            name = values["name"].strip() if isinstance(values["name"], str) else ""
            if not 1 <= len(name) <= 100:
                raise ValueError("list name must contain 1..100 characters")
            updates["name"] = name
        if "description" in values:
            description = values["description"]
            if not isinstance(description, str):
                raise ValueError("list description must be a string")
            updates["description"] = description[:1000]
        if "enabled" in values:
            if type(values["enabled"]) is not bool:
                raise ValueError("enabled must be boolean")
            updates["enabled"] = values["enabled"]
        if "poll_interval" in values:
            updates["poll_interval"] = normalize_poll_interval(values["poll_interval"])
            updates["poll_interval_input"] = interval_input_text(values["poll_interval"], updates["poll_interval"])
        return await self.s.repo.update_list(list_id, **updates)

    async def delete(self, list_id: str):
        return await self.s.repo.delete_list(list_id)

    async def add_feed(self, list_id: str, feed_id: str):
        return await self.s.repo.add_list_feed(list_id, feed_id, self.s.runtime.config.first_subscription_send_history)

    async def remove_feed(self, list_id: str, feed_id: str):
        return await self.s.repo.remove_list_feed(list_id, feed_id)

    async def subscribe(self, target_id: str, list_id: str, interval: int | str | None = None):
        interval = normalize_poll_interval(interval)
        return await self.s.repo.subscribe_list(
            target_id, list_id, interval, self.s.runtime.config.first_subscription_send_history
        )

    async def unsubscribe(self, target_id: str, list_id: str):
        return await self.s.repo.unsubscribe_list(target_id, list_id)


class _Targets:
    def __init__(self, service):
        self.s = service

    async def known(self, target_id: str):
        return await self.s.repo.get_target(target_id)

    async def from_event(self, event, label: str = ""):
        origin = event.unified_msg_origin
        platform_instance = event.get_platform_id()
        platform = event.get_platform_name()
        session_id = event.get_session_id()
        if not platform_instance or not origin or not platform or not session_id:
            raise ValueError("event is missing AstrBot platform target identity")
        message_type = event.get_message_type()
        session_type = getattr(message_type, "value", str(message_type))
        routing_session_id = session_id
        group_getter = getattr(event, "get_group_id", None)
        if session_type == "GroupMessage" and callable(group_getter):
            group_id = group_getter()
            if group_id:
                routing_session_id = str(group_id)
        return await self.s.repo.create_target(
            platform_instance,
            origin,
            platform,
            routing_session_id,
            session_type,
            label,
            routing_key=f"{session_type}:{routing_session_id}",
        )


class _Delivery:
    def __init__(self, service):
        self.s = service

    async def status(self, **filters):
        return await self.s.repo.deliveries(**filters)

    async def retry(self, delivery_id: str):
        return await self.s.repo.retry_delivery(delivery_id)

    async def resolve_unknown(self, delivery_id: str, *, confirmed_sent: bool):
        return await self.s.repo.resolve_unknown(delivery_id, confirmed_sent)


class _Data:
    def __init__(self, service):
        self.s = service

    async def export(self):
        return await self.s.repo.get_all_data()

    async def backup(self, destination):
        return await self.s.runtime.backup(destination)

    async def cleanup(self, kind="articles"):
        return await self.s.runtime.cleanup(kind)
