from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import shutil
import sqlite3
import time
import uuid
from contextlib import closing
from datetime import date, datetime, timedelta
from datetime import time as datetime_time
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..core.feed_urls import build_feed_request, is_relative_feed_reference, normalize_relative_feed_reference
from ..core.security import validate_public_url
from ..storage.database import Database
from ..storage.management_repository import ManagementRepository


class ManagementService:
    def __init__(self, runtime):
        self.runtime = runtime
        self.services = runtime.services
        self.read = ManagementRepository(runtime)
        self._restore_confirmations = {}
        self._restore_lock = asyncio.Lock()

    @property
    def timezone(self) -> str:
        return getattr(self.runtime.config, "timezone", "Asia/Shanghai")

    def _date_filter(self, key: str, value):
        raw = str(value)
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            try:
                selected_day = date.fromisoformat(raw)
                zone = ZoneInfo(self.timezone)
            except (ValueError, ZoneInfoNotFoundError):
                raise ValueError(f"{key} must be a date in the configured time zone") from None
            if key == "date_to":
                selected_day += timedelta(days=1)
                operator = "<"
            else:
                operator = ">="
            boundary = datetime.combine(selected_day, datetime_time.min, tzinfo=zone)
            return operator, boundary.timestamp()
        try:
            stamp = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{key} must be a timestamp or YYYY-MM-DD date") from None
        return (">=" if key == "date_from" else "<="), stamp

    async def _validated_feed_reference(self, reference: str) -> str:
        request = build_feed_request(reference, self.runtime.config)
        if is_relative_feed_reference(reference):
            route = normalize_relative_feed_reference(reference)
            await validate_public_url(request.request_url)
            return route
        return (await validate_public_url(request.request_url)).url

    @staticmethod
    def pagination(query):
        try:
            page = max(1, int(query.get("page", 1)))
            size = min(100, max(1, int(query.get("page_size", 20))))
        except (TypeError, ValueError):
            raise ValueError("page and page_size must be integers") from None
        return page, size

    @staticmethod
    def _public_ref(value, entity: str) -> str:
        if type(value) not in (int, str) or not re.fullmatch(r"[0-9]{1,19}", str(value)):
            raise ValueError(f"{entity} ID 必须是正整数公开号")
        public_id = int(value)
        if not 0 < public_id <= 9_223_372_036_854_775_807:
            raise ValueError(f"{entity} ID 必须是正整数公开号")
        return str(public_id)

    async def _feed_by_public_id(self, value):
        reference = self._public_ref(value, "订阅源")
        feed = await self.runtime.repository.get_feed_by_public_id(int(reference))
        if feed is None:
            raise KeyError(reference)
        return feed

    async def _list_by_public_id(self, value):
        reference = self._public_ref(value, "列表")
        row = await self.runtime.repository.get_list_by_public_id(int(reference))
        if row is None:
            raise KeyError(reference)
        return row

    async def get(self, endpoint: str, query: dict):
        page, size = self.pagination(query)
        if endpoint == "dashboard":
            result = await self.read.dashboard()
            result["scheduler"] = await self.runtime.scheduler_status()
            result["timezone"] = self.timezone
            return result
        if endpoint == "feeds":
            scope = query.get("scope", "all")
            if scope not in {"direct", "all"}:
                raise ValueError("scope must be direct or all")
            result = await self.read.feed_page(str(query.get("q", ""))[:200], page, size, scope)
            result["timezone"] = self.timezone
            return result
        if endpoint.startswith("feeds/") and endpoint.count("/") == 1:
            feed_id = (await self._feed_by_public_id(endpoint.split("/", 1)[1])).feed_id
            result = await self.read.feed_detail(feed_id)
            if result is None:
                raise KeyError(feed_id)
            for article in result["articles"]:
                try:
                    article["content_blocks"] = json.loads(article["content_blocks"])
                except (TypeError, ValueError):
                    article["content_blocks"] = []
            for row in result.get("fetch_logs", []):
                row["feed_public_id"] = result["feed"]["public_id"]
            manager = getattr(self.runtime.fetcher, "cookies", None)
            if manager:
                origin = manager.stored_origin_for_url(result["feed"]["url"])
                result["cookie_status"] = manager.status(origin)
            else:
                result["cookie_status"] = {"validation_status": "unavailable"}
            result["timezone"] = self.timezone
            return result
        if endpoint == "lists":
            q = str(query.get("q", ""))[:200]
            result = await self.read.page(
                "SELECT l.*,(SELECT COUNT(*) FROM list_feeds m WHERE m.list_id=l.list_id) member_count,(SELECT COUNT(*) FROM list_subscriptions s WHERE s.list_id=l.list_id AND s.enabled=1) subscriber_count FROM subscription_lists l WHERE l.name LIKE ? ORDER BY l.name",
                (f"%{q}%",),
                page,
                size,
            )
            result["timezone"] = self.timezone
            return result
        if endpoint.startswith("lists/") and endpoint.count("/") == 1:
            list_ref = endpoint.split("/", 1)[1]
            resolved = await self._list_by_public_id(list_ref)
            result = await self.read.list_detail(resolved["list_id"])
            if result is None:
                raise KeyError(endpoint)
            for article in result["articles"]:
                try:
                    article["content_blocks"] = json.loads(article["content_blocks"])
                except (TypeError, ValueError):
                    article["content_blocks"] = []
            result["timezone"] = self.timezone
            return result
        if endpoint == "targets":
            return [
                dict(row)
                for row in await self.runtime.database.fetchall("SELECT * FROM targets ORDER BY platform,label")
            ]
        if endpoint == "deliveries":
            allowed = {
                "status": "d.status",
                "platform": "t.platform",
                "feed_id": "a.feed_id",
                "target_id": "d.target_id",
                "list_id": "ls.list_id",
            }
            where, args = [], []
            for key, col in allowed.items():
                value = query.get(key)
                if value:
                    if key == "feed_id":
                        value = (await self._feed_by_public_id(value)).feed_id
                    elif key == "list_id":
                        value = (await self._list_by_public_id(value))["list_id"]
                    where.append(f"{col}=?")
                    args.append(str(value)[:200])
            for key, op in (("date_from", ">="), ("date_to", "<=")):
                value = query.get(key)
                if value:
                    op, stamp = self._date_filter(key, value)
                    where.append(f"d.created_at{op}?")
                    args.append(stamp)
            clause = " WHERE " + " AND ".join(where) if where else ""
            sql = (
                "SELECT DISTINCT d.*,t.platform,t.label target_label,a.title,a.feed_id,f.public_id feed_public_id,l.public_id list_public_id FROM deliveries d JOIN targets t USING(target_id) JOIN articles a USING(article_id) JOIN feeds f USING(feed_id) LEFT JOIN list_subscriptions ls ON ls.target_id=d.target_id AND d.source_path='list:'||ls.subscription_id LEFT JOIN subscription_lists l ON l.list_id=ls.list_id"
                + clause
                + " ORDER BY d.created_at DESC,d.rowid DESC"
            )
            result = await self.read.page(sql, tuple(args), page, size)
            result["timezone"] = self.timezone
            return result
        if endpoint.startswith("deliveries/") and endpoint.count("/") == 1:
            result = await self.read.delivery_detail(endpoint.split("/", 1)[1])
            if result is None:
                raise KeyError(endpoint)
            try:
                result["article"]["content_blocks"] = json.loads(result["article"]["content_blocks"])
            except (TypeError, ValueError):
                result["article"]["content_blocks"] = []
            result["timezone"] = self.timezone
            return result
        if endpoint == "data/status":
            counts = await self.runtime.repository.status()
            user_version = await self.runtime.database.fetchone("PRAGMA user_version")
            integrity = await self.runtime.database.fetchone("PRAGMA quick_check")
            media_dir = self.runtime.data_dir / "media-cache"
            media_count = sum(1 for _ in media_dir.glob("*.bin")) if media_dir.exists() else 0
            sessions = self._session_status()
            return {
                "database": {
                    "path": self.runtime.database.path.name,
                    "size_bytes": self.runtime.database.path.stat().st_size
                    if self.runtime.database.path.exists()
                    else 0,
                    "user_version": user_version[0],
                    "integrity": integrity[0],
                },
                "counts": counts,
                "cache": {"sessions": len(sessions), "media": media_count},
                "scheduler": await self.runtime.scheduler_status(),
                "config": self.runtime.config.public_dict(),
                "browser_compatibility": self._browser_status(),
                "session_statuses": sessions,
            }
        if endpoint == "data/export":
            return await self._business_export()
        raise KeyError(endpoint)

    def _browser_status(self):
        try:
            from ..fetchers.compatibility import BrowserCompatibilityManager

            return BrowserCompatibilityManager(self.runtime.config.cloakbrowser_version).inspect()
        except Exception as exc:
            return {"status": "unavailable", "error": type(exc).__name__}

    def _session_status(self):
        manager = getattr(self.runtime.fetcher, "cookies", None)
        return manager.list_statuses() if manager and hasattr(manager, "list_statuses") else []

    async def mine(self, target_id: str) -> list[str]:
        direct = await self.services.subscriptions.mine(target_id)
        lists = await self.runtime.database.fetchall(
            "SELECT l.name,l.public_id FROM list_subscriptions s JOIN subscription_lists l USING(list_id) WHERE s.target_id=? ORDER BY l.name",
            (target_id,),
        )
        rows = [f"{row['title'] or row['url']} (订阅源 ID: {row['public_id']})" for row in direct]
        rows.extend(f"列表：{row['name']} ({row['public_id']})" for row in lists)
        return rows

    async def _preview_target(self, target_id):
        if target_id:
            target = await self.services.targets.known(target_id)
            if target is None:
                raise ValueError("target is not registered")
            return target
        from ..core.models import DeliveryTarget

        return DeliveryTarget("preview", "preview", "preview", "preview", "preview", "private", "Preview")

    async def _preview_feed(self, feed_id, target_id):
        feed = await self.runtime.repository.get_feed(feed_id)
        if feed is None:
            raise KeyError(feed_id)
        articles = await self.runtime.list_feed_articles(feed_id, limit=20)
        target = await self._preview_target(target_id)
        messages = (
            await self.runtime.registry.render(articles, target, feed.title or feed.url)
            if self.runtime.registry
            else []
        )
        return {
            "feed": feed,
            "article_count": len(articles),
            "messages": messages,
            "preview_only": True,
            "timezone": self.timezone,
        }

    async def _preview_list(self, list_id, target_id):
        detail = await self.read.list_detail(list_id)
        if detail is None:
            raise KeyError(list_id)
        rows = await self.runtime.database.fetchall(
            "SELECT article_id FROM articles WHERE feed_id IN "
            "(SELECT feed_id FROM list_feeds WHERE list_id=?) "
            "ORDER BY COALESCE(published_at,updated_at,first_seen_at) DESC,seq DESC,article_id DESC LIMIT 20",
            (list_id,),
        )
        articles = await asyncio.gather(*(self.runtime.repository.article(row["article_id"]) for row in rows))
        articles = [article for article in articles if article is not None]
        target = await self._preview_target(target_id)
        list_data = detail["list"]
        messages = (
            await self.runtime.registry.render(articles, target, list_data["name"], is_list=True)
            if self.runtime.registry
            else []
        )
        return {
            "list": list_data,
            "article_count": len(articles),
            "messages": messages,
            "preview_only": True,
            "timezone": self.timezone,
        }

    async def post(self, endpoint: str, body: dict):
        if endpoint == "scheduler":
            action = body.get("action")
            if action == "pause":
                await self.runtime.pause()
            elif action == "resume":
                await self.runtime.resume()
            elif action == "check":
                force = body.get("force", False)
                if type(force) is not bool:
                    raise ValueError("force must be a boolean")
                return await self.runtime.check_now(force=force)
            else:
                raise ValueError("action must be pause, resume, or check")
            return await self.runtime.scheduler_status()
        if endpoint == "feeds/create":
            target_id = self._required(body, "target_id")
            await self._target(target_id)
            url = self._required(body, "url")
            translation_enabled = body.get("translation_enabled", False)
            if type(translation_enabled) is not bool:
                raise ValueError("translation_enabled must be boolean")
            feed = await self.services.feeds.create(url, translation_enabled=translation_enabled)
            await self.services.subscriptions.subscribe(target_id, feed.feed_id)
            return {"feed": feed}
        if endpoint.startswith("feeds/"):
            parts = endpoint.split("/")
            feed_id = (await self._feed_by_public_id(parts[1])).feed_id
            if len(parts) == 3 and parts[2] == "update":
                values = {}
                if "enabled" in body:
                    if type(body["enabled"]) is not bool:
                        raise ValueError("enabled must be boolean")
                    values["enabled"] = body["enabled"]
                if "translation_enabled" in body:
                    if type(body["translation_enabled"]) is not bool:
                        raise ValueError("translation_enabled must be boolean")
                    values["translation_enabled"] = body["translation_enabled"]
                if "poll_interval" in body:
                    values["poll_interval"] = self._interval(body["poll_interval"])
                return await self.services.feeds.update(feed_id, **values)
            if len(parts) == 3 and parts[2] == "refresh":
                return await self.services.feeds.refresh(feed_id)
            if len(parts) == 3 and parts[2] == "unsubscribe":
                target = self._required(body, "target_id")
                await self._target(target)
                return {"removed": await self.services.subscriptions.unsubscribe(target, feed_id)}
            if len(parts) == 3 and parts[2] in {"preview", "test"}:
                target_id = body.get("target_id")
                if target_id:
                    await self._target(target_id)
                if parts[2] == "preview":
                    return await self._preview_feed(feed_id, target_id)
                return await self.runtime.test_push(feed_id, self._required(body, "target_id"))
        if endpoint == "lists/create":
            name = self._text(body, "name", 1, 100)
            list_uuid = await self.services.lists.create(
                name, str(body.get("description", ""))[:1000], self._interval(body.get("poll_interval"))
            )
            created = await self.runtime.repository.get_list(list_uuid)
            return {
                "list_id": created["public_id"],
                "public_id": created["public_id"],
            }
        parts = endpoint.split("/")
        if parts[0] == "lists" and len(parts) >= 3:
            list_id = (await self._list_by_public_id(parts[1]))["list_id"]
            action = parts[2]
            if action == "update":
                values = {k: body[k] for k in ("name", "description", "enabled", "poll_interval") if k in body}
                await self.services.lists.update(list_id, **values)
                return await self.read.list_detail(list_id)
            if action == "delete":
                if body.get("confirm") is not True:
                    raise ValueError("confirm=true required")
                if not await self.services.lists.delete(list_id):
                    raise KeyError(list_id)
                return {"deleted": True}
            if action == "members" and len(parts) == 4:
                if parts[3] == "add":
                    entries = body.get("items", [body])
                    if not isinstance(entries, list) or len(entries) > 100:
                        raise ValueError("items must contain at most 100 feeds")
                    references = []
                    for item in entries:
                        if not isinstance(item, dict):
                            raise ValueError("each item must be an object")
                        if "feed_id" in item:
                            references.append(("feed_id", self._public_ref(item["feed_id"], "订阅源")))
                        else:
                            url = self._required(item, "url")
                            references.append(("url", url))

                    feeds = []
                    for reference_type, reference in references:
                        if reference_type == "feed_id":
                            feed = await self._feed_by_public_id(reference)
                        else:
                            feed = await self.services.feeds.create(reference)
                        feeds.append(feed)

                    for feed in feeds:
                        await self.services.lists.add_feed(list_id, feed.feed_id)
                    return {"feed_ids": [feed.public_id for feed in feeds]}
                if parts[3] == "remove":
                    feed_value = body.get("feed_id")
                    feed = await self._feed_by_public_id(feed_value)
                    return {"removed": await self.services.lists.remove_feed(list_id, feed.feed_id)}
            if action in {"subscribe", "unsubscribe"}:
                target = self._required(body, "target_id")
                await self._target(target)
                if action == "subscribe":
                    await self.services.lists.subscribe(target, list_id)
                else:
                    await self.services.lists.unsubscribe(target, list_id)
                return {"ok": True}
            if action == "preview":
                return await self._preview_list(list_id, body.get("target_id"))
        if endpoint.startswith("deliveries/"):
            did = endpoint.split("/")[1]
            if endpoint.endswith("/retry"):
                if not await self.services.delivery.retry(did):
                    raise ValueError("only failed deliveries can be retried")
                return {"queued": True}
            if endpoint.endswith("/resolve"):
                if body.get("confirm") is not True:
                    raise ValueError("confirm=true required")
                resolution = body.get("resolution")
                if resolution not in {"delivered", "retry"}:
                    raise ValueError("resolution must be delivered or retry")
                if resolution == "delivered":
                    resolved = await self.services.delivery.resolve_unknown(did, confirmed_sent=True)
                else:
                    detail = await self.read.delivery_detail(did)
                    if not detail or detail["delivery"]["status"] != "unknown":
                        raise ValueError("only unknown deliveries can be retried")
                    resolved = await self.services.delivery.resolve_unknown(did, confirmed_sent=False)
                if not resolved:
                    raise ValueError("delivery could not be resolved in its current state")
                return {"resolved": resolution}
        if endpoint.startswith("sessions/"):
            if body.get("confirm") is not True:
                raise ValueError("confirm=true required")
            manager = getattr(self.runtime.fetcher, "cookies", None)
            if not manager:
                raise ValueError("cookie persistence is unavailable")
            session_id = endpoint.split("/")[1]
            origin = manager.origin_for_session(session_id) if hasattr(manager, "origin_for_session") else None
            if not origin or urlsplit(origin).scheme not in {"http", "https"} or not urlsplit(origin).hostname:
                raise ValueError("session not found")
            if endpoint.endswith("/clear"):
                manager.clear(origin)
                return {"cleared": True}
            if endpoint.endswith("/refresh"):
                manager.clear(origin)
                feeds = await self.runtime.repository.list_feeds()
                host = (urlsplit(origin).hostname or "").lower()
                refreshed = 0
                for feed in feeds:
                    feed_host = (urlsplit(feed["url"]).hostname or "").lower()
                    if host and (feed_host == host or feed_host.endswith("." + host)):
                        await self.runtime.refresh_feed(feed["feed_id"])
                        refreshed += 1
                return {"cleared": True, "feeds_refreshed": refreshed}
        if endpoint == "data/cleanup":
            if body.get("confirm") is not True:
                raise ValueError("confirm=true required")
            kind = body.get("kind")
            if kind not in {"articles", "history", "cookies", "media", "all"}:
                raise ValueError("unsupported cleanup kind")
            if kind in {"articles", "history", "all"}:
                return await self.services.data.cleanup(kind)
            if kind == "cookies":
                manager = getattr(self.runtime.fetcher, "cookies", None)
                return {"removed": manager.clear_expired() if manager else 0}
            files = self.runtime.media_downloader.cleanup() if self.runtime.media_downloader else 0
            platform_keys = await self.runtime.repository.cleanup_platform_media_cache()
            return {"removed": files + platform_keys, "media_files": files, "platform_media_keys": platform_keys}
        if endpoint in {"data/restore/confirm", "data/import/confirm"}:
            if body.get("confirm") is not True:
                raise ValueError("confirm=true required")
            filename = self._text(body, "filename", 1, 255)
            size = body.get("size")
            limit = 20_000_000 if endpoint.startswith("data/import") else 100_000_000
            if type(size) is not int or not 0 < size <= limit:
                raise ValueError(f"size must be between 1 and {limit} bytes")
            token = secrets.token_urlsafe(32)
            kind = "import" if endpoint.startswith("data/import") else "restore"
            self._restore_confirmations = {
                key: value for key, value in self._restore_confirmations.items() if value["expires"] >= time.time()
            }
            if len(self._restore_confirmations) >= 1000:
                raise ValueError("too many pending upload confirmations")
            self._restore_confirmations[token] = {
                "kind": kind,
                "username": body.get("_username", ""),
                "filename": Path(filename).name,
                "size": size,
                "expires": time.time() + 300,
            }
            return {"confirmation": token, "expires_in": 300}
        raise KeyError(endpoint)

    async def upload(self, endpoint: str, file_path: str | Path, filename: str, username: str):
        kind = "import" if endpoint == "data/import" else "restore"
        path = Path(file_path)
        size = path.stat().st_size
        matches = [
            token
            for token, item in self._restore_confirmations.items()
            if item["kind"] == kind
            and item["username"] == username
            and item["filename"] == Path(filename).name
            and item["size"] == size
            and item["expires"] >= time.time()
        ]
        if endpoint not in {"data/import", "data/restore"} or len(matches) != 1:
            raise ValueError("a matching unexpired confirmation for this user and file is required")
        self._restore_confirmations.pop(matches[0])
        if kind == "import":
            return await self._import_json(path)
        return await self._restore_sqlite(path)

    async def _business_export(self):
        raw = await self.services.data.export()
        return {
            "feeds": [
                {
                    **{
                        key: row[key]
                        for key in (
                            "feed_id",
                            "public_id",
                            "url",
                            "title",
                            "description",
                            "poll_interval",
                        )
                    },
                    "enabled": bool(row["enabled"]),
                    "translation_enabled": bool(row["translation_enabled"]),
                }
                for row in raw["feeds"]
            ],
            "subscription_lists": [
                {
                    **{key: row[key] for key in ("list_id", "public_id", "name", "description", "poll_interval")},
                    "enabled": bool(row["enabled"]),
                }
                for row in raw["subscription_lists"]
            ],
            "list_feeds": [{"list_id": row["list_id"], "feed_id": row["feed_id"]} for row in raw["list_feeds"]],
            "targets": [{"target_id": row["target_id"]} for row in raw["targets"]],
            "direct_subscriptions": [
                {
                    "target_id": row["target_id"],
                    "feed_id": row["feed_id"],
                    "enabled": bool(row["enabled"]),
                    "poll_interval": row["poll_interval"],
                }
                for row in raw["direct_subscriptions"]
            ],
            "list_subscriptions": [
                {
                    "target_id": row["target_id"],
                    "list_id": row["list_id"],
                    "enabled": bool(row["enabled"]),
                    "poll_interval": row["poll_interval"],
                }
                for row in raw["list_subscriptions"]
            ],
        }

    async def _import_json(self, path: Path):
        try:
            payload = json.loads(path.read_text("utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise ValueError("invalid JSON import") from exc
        allowed = {"feeds", "subscription_lists", "list_feeds", "targets", "direct_subscriptions", "list_subscriptions"}
        if not isinstance(payload, dict) or set(payload) - allowed:
            raise ValueError("unsupported import schema")
        if (
            any(not isinstance(rows, list) for rows in payload.values())
            or sum(len(rows) for rows in payload.values()) > 10000
        ):
            raise ValueError("import lists must be valid and contain at most 10000 rows")
        feed_rows, list_rows = payload.get("feeds", []), payload.get("subscription_lists", [])
        if any(
            not isinstance(row, dict) or not isinstance(row.get("feed_id"), str) or not isinstance(row.get("url"), str)
            for row in feed_rows
        ):
            raise ValueError("each feed requires feed_id and URL")
        if any(
            not isinstance(row, dict)
            or not isinstance(row.get("list_id"), str)
            or not isinstance(row.get("name"), str)
            or not 1 <= len(row["name"].strip()) <= 100
            for row in list_rows
        ):
            raise ValueError("each list requires list_id and a name of 1..100 characters")
        feed_ids = {row["feed_id"] for row in feed_rows}
        list_ids = {row["list_id"] for row in list_rows}
        if len(feed_ids) != len(feed_rows) or len(list_ids) != len(list_rows):
            raise ValueError("feed and list identifiers must be unique")
        for key in ("feeds", "subscription_lists", "direct_subscriptions", "list_subscriptions"):
            for row in payload.get(key, []):
                if not isinstance(row, dict):
                    raise ValueError(f"{key} rows must be objects")
                interval = row.get("poll_interval")
                if interval == 0 and type(interval) is int:
                    row["poll_interval"] = None
                elif interval is not None and (
                    type(interval) is not int or interval < self.runtime.config.min_poll_interval
                ):
                    raise ValueError(f"{key} poll_interval must be a non-negative integer")
                if "enabled" in row and type(row["enabled"]) is not bool:
                    raise ValueError(f"{key} enabled must be boolean")
                if key == "feeds" and "translation_enabled" in row and type(row["translation_enabled"]) is not bool:
                    raise ValueError("feeds translation_enabled must be boolean")
        normalized_urls = {}
        for row in feed_rows:
            normalized_urls[row["feed_id"]] = await self._validated_feed_reference(row["url"])
        for key in ("list_feeds", "direct_subscriptions", "list_subscriptions"):
            for row in payload.get(key, []):
                if not isinstance(row, dict):
                    raise ValueError(f"{key} rows must be objects")
        if any(
            row.get("feed_id") not in feed_ids or row.get("list_id") not in list_ids
            for row in payload.get("list_feeds", [])
        ):
            raise ValueError("list_feeds contains an unknown feed or list")
        if any(row.get("feed_id") not in feed_ids for row in payload.get("direct_subscriptions", [])):
            raise ValueError("direct_subscriptions contains an unknown feed")
        if any(row.get("list_id") not in list_ids for row in payload.get("list_subscriptions", [])):
            raise ValueError("list_subscriptions contains an unknown list")
        for key, left, right in (
            ("list_feeds", "list_id", "feed_id"),
            ("direct_subscriptions", "target_id", "feed_id"),
            ("list_subscriptions", "target_id", "list_id"),
        ):
            seen = set()
            for row in payload.get(key, []):
                if not isinstance(row.get(left), str) or not isinstance(row.get(right), str):
                    raise ValueError(f"{key} requires {left} and {right}")
                relation = (row[left], row[right])
                if relation in seen:
                    raise ValueError(f"{key} contains duplicate relationships")
                seen.add(relation)

        feed_map, list_map = {}, {}
        async with self.runtime.database.transaction() as conn:
            now = time.time()
            for row in feed_rows:
                normalized_url = normalized_urls[row["feed_id"]]
                proposed_id = str(uuid.uuid5(uuid.NAMESPACE_URL, normalized_url))
                mapped = await (
                    await conn.execute("SELECT feed_id FROM feeds WHERE url=?", (normalized_url,))
                ).fetchone()
                if mapped is None:
                    public_id = await self.runtime.database.next_public_id(conn, "feed")
                    await conn.execute(
                        "INSERT INTO feeds(feed_id,public_id,url,title,description,enabled,poll_interval,translation_enabled,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (
                            proposed_id,
                            public_id,
                            normalized_url,
                            str(row.get("title", ""))[:500],
                            str(row.get("description", ""))[:2000],
                            int(row.get("enabled", True)),
                            row.get("poll_interval"),
                            int(row.get("translation_enabled", False)),
                            now,
                        ),
                    )
                    mapped = (proposed_id,)
                else:
                    await conn.execute(
                        "UPDATE feeds SET title=CASE WHEN ?='' THEN title ELSE ? END,description=CASE WHEN ?='' THEN description ELSE ? END,enabled=?,poll_interval=?,translation_enabled=? WHERE feed_id=?",
                        (
                            str(row.get("title", ""))[:500],
                            str(row.get("title", ""))[:500],
                            str(row.get("description", ""))[:2000],
                            str(row.get("description", ""))[:2000],
                            int(row.get("enabled", True)),
                            row.get("poll_interval"),
                            int(row.get("translation_enabled", False)),
                            mapped[0],
                        ),
                    )
                feed_map[row["feed_id"]] = mapped[0]
            for row in list_rows:
                normalized_name = row["name"].strip()
                existing = await (
                    await conn.execute("SELECT list_id FROM subscription_lists WHERE name=?", (normalized_name,))
                ).fetchone()
                if existing is not None:
                    list_map[row["list_id"]] = existing[0]
                    continue
                proposed_list_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"import-list:{row['list_id']}"))
                existing_id = await (
                    await conn.execute("SELECT list_id FROM subscription_lists WHERE list_id=?", (proposed_list_id,))
                ).fetchone()
                if existing_id:
                    list_map[row["list_id"]] = proposed_list_id
                    continue
                public_id = await self.runtime.database.next_public_id(conn, "list")
                await conn.execute(
                    "INSERT INTO subscription_lists(list_id,public_id,name,description,enabled,poll_interval,created_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        proposed_list_id,
                        public_id,
                        normalized_name,
                        str(row.get("description", ""))[:1000],
                        int(row.get("enabled", True)),
                        row.get("poll_interval"),
                        now,
                    ),
                )
                list_map[row["list_id"]] = proposed_list_id
            targets = {row[0] for row in await (await conn.execute("SELECT target_id FROM targets")).fetchall()}
            for row in payload.get("list_feeds", []):
                fid, lid = feed_map[row["feed_id"]], list_map[row["list_id"]]
                baseline = await (
                    await conn.execute("SELECT COALESCE(MAX(seq),0) FROM articles WHERE feed_id=?", (fid,))
                ).fetchone()
                await conn.execute(
                    "INSERT OR IGNORE INTO list_feeds(list_id,feed_id,baseline_seq) VALUES(?,?,?)",
                    (lid, fid, baseline[0]),
                )
            direct_imported = 0
            for row in payload.get("direct_subscriptions", []):
                target, fid = row.get("target_id"), feed_map.get(row.get("feed_id"))
                if target not in targets or not fid:
                    continue
                baseline = await (
                    await conn.execute("SELECT COALESCE(MAX(seq),0) FROM articles WHERE feed_id=?", (fid,))
                ).fetchone()
                await conn.execute(
                    "INSERT INTO direct_subscriptions(subscription_id,target_id,feed_id,enabled,poll_interval,baseline_seq,last_seq,next_check_at,created_at) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(target_id,feed_id) DO UPDATE SET enabled=excluded.enabled,poll_interval=excluded.poll_interval",
                    (
                        str(uuid.uuid4()),
                        target,
                        fid,
                        int(row.get("enabled", True)),
                        row.get("poll_interval"),
                        baseline[0],
                        baseline[0],
                        now,
                        now,
                    ),
                )
                direct_imported += 1
            list_imported = 0
            for row in payload.get("list_subscriptions", []):
                target, lid = row.get("target_id"), list_map.get(row.get("list_id"))
                if target not in targets or not lid:
                    continue
                baseline = await (
                    await conn.execute(
                        "SELECT COALESCE(MAX(a.seq),0) FROM list_feeds lf JOIN articles a USING(feed_id) WHERE lf.list_id=?",
                        (lid,),
                    )
                ).fetchone()
                await conn.execute(
                    "INSERT INTO list_subscriptions(subscription_id,target_id,list_id,enabled,poll_interval,baseline_seq,last_seq,next_check_at,created_at) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(target_id,list_id) DO UPDATE SET enabled=excluded.enabled,poll_interval=excluded.poll_interval",
                    (
                        str(uuid.uuid4()),
                        target,
                        lid,
                        int(row.get("enabled", True)),
                        row.get("poll_interval"),
                        baseline[0],
                        baseline[0],
                        now,
                        now,
                    ),
                )
                list_imported += 1
        skipped_targets = sum(
            1
            for key in ("direct_subscriptions", "list_subscriptions")
            for row in payload.get(key, [])
            if row.get("target_id") not in targets
        )
        return {
            "feeds_imported": len(feed_map),
            "lists_imported": len(list_map),
            "list_memberships_imported": len(payload.get("list_feeds", [])),
            "direct_subscriptions_imported": direct_imported,
            "list_subscriptions_imported": list_imported,
            "skipped_unregistered_targets": skipped_targets,
        }

    async def _restore_sqlite(self, source: Path):
        async with self._restore_lock:
            return await self._restore_sqlite_locked(source)

    @staticmethod
    def _validate_legacy_v1_staging_source(source: Path) -> None:
        required = {
            "feeds",
            "articles",
            "targets",
            "subscription_lists",
            "list_feeds",
            "direct_subscriptions",
            "list_subscriptions",
            "deliveries",
            "delivery_attempts",
            "delivery_batches",
            "batch_articles",
            "fetch_logs",
            "settings",
            "platform_media_cache",
            "telegraph_pages",
            "test_operations",
        }
        with closing(sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)) as check:
            if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("backup integrity check failed")
            if check.execute("PRAGMA user_version").fetchone()[0] != 1:
                raise ValueError("unsupported legacy database schema version")
            tables = {r[0] for r in check.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if (
                not required <= tables
                or check.execute("SELECT COUNT(*) FROM sqlite_master WHERE type IN ('trigger','view')").fetchone()[0]
            ):
                raise ValueError("legacy backup schema is incomplete or contains unsupported objects")
            if check.execute("PRAGMA foreign_key_check").fetchone():
                raise ValueError("backup foreign key check failed")
            for table, keys in {
                "feeds": {"feed_id", "url", "created_at"},
                "subscription_lists": {"list_id", "name", "created_at"},
                "list_feeds": {"list_id", "feed_id"},
                "articles": {"article_id", "feed_id"},
            }.items():
                columns = {row[1] for row in check.execute(f"PRAGMA table_info({table})")}
                if not keys <= columns:
                    raise ValueError("legacy backup is missing required columns")

    @staticmethod
    def _validate_legacy_v2_staging_source(source: Path) -> None:
        required = {
            "feeds",
            "articles",
            "targets",
            "subscription_lists",
            "list_feeds",
            "direct_subscriptions",
            "list_subscriptions",
            "deliveries",
            "delivery_attempts",
            "delivery_batches",
            "batch_articles",
            "fetch_logs",
            "settings",
            "platform_media_cache",
            "telegraph_pages",
            "test_operations",
            "entity_counters",
        }
        with closing(sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)) as check:
            if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("backup integrity check failed")
            if check.execute("PRAGMA user_version").fetchone()[0] != 2:
                raise ValueError("unsupported legacy database schema version")
            tables = {
                row[0]
                for row in check.execute("SELECT name FROM sqlite_master WHERE type='table'")
                if not row[0].startswith("sqlite_")
            }
            if (
                tables != required
                or check.execute("SELECT COUNT(*) FROM sqlite_master WHERE type IN ('trigger','view')").fetchone()[0]
            ):
                raise ValueError("legacy backup schema is incomplete or contains unsupported objects")
            if check.execute("PRAGMA foreign_key_check").fetchone():
                raise ValueError("backup foreign key check failed")

    @staticmethod
    def _validate_backup_sqlite(source: Path, canonical_path: Path) -> bool:
        restore_paused = False
        try:
            with closing(sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)) as check:
                if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("backup integrity check failed")
                if check.execute("PRAGMA user_version").fetchone()[0] != 3:
                    raise ValueError("unsupported database schema version")
                tables = {r[0] for r in check.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                required = {
                    "feeds",
                    "articles",
                    "targets",
                    "subscription_lists",
                    "direct_subscriptions",
                    "list_subscriptions",
                    "deliveries",
                    "delivery_attempts",
                    "delivery_batches",
                    "batch_articles",
                    "fetch_logs",
                    "settings",
                    "platform_media_cache",
                    "telegraph_pages",
                    "test_operations",
                    "entity_counters",
                }
                if (
                    not required <= tables
                    or check.execute("SELECT count(*) FROM sqlite_master WHERE type IN ('trigger','view')").fetchone()[
                        0
                    ]
                ):
                    raise ValueError("backup schema is incomplete or contains unsupported objects")
                if check.execute("PRAGMA foreign_key_check").fetchone():
                    raise ValueError("backup foreign key check failed")
                paused_row = check.execute("SELECT value FROM settings WHERE key='paused'").fetchone()
                restore_paused = bool(paused_row and paused_row[0] == "1")
                expected_columns = {
                    "feeds": {
                        "feed_id",
                        "public_id",
                        "url",
                        "title",
                        "description",
                        "enabled",
                        "translation_enabled",
                        "poll_interval",
                        "etag",
                        "last_modified",
                        "initialized",
                        "metadata",
                        "next_fetch_at",
                        "last_fetch_at",
                        "status",
                        "created_at",
                    },
                    "articles": {
                        "seq",
                        "article_id",
                        "feed_id",
                        "entry_id",
                        "canonical_url",
                        "title",
                        "author",
                        "published_at",
                        "updated_at",
                        "first_seen_at",
                        "categories",
                        "content_hash",
                        "content_blocks",
                        "metadata",
                    },
                    "targets": {
                        "target_id",
                        "platform_instance",
                        "unified_origin",
                        "platform",
                        "session_id",
                        "session_type",
                        "label",
                        "routing_key",
                    },
                    "subscription_lists": {
                        "list_id",
                        "public_id",
                        "name",
                        "description",
                        "enabled",
                        "poll_interval",
                        "created_at",
                    },
                    "list_feeds": {"list_id", "feed_id", "baseline_seq"},
                    "direct_subscriptions": {
                        "subscription_id",
                        "target_id",
                        "feed_id",
                        "enabled",
                        "poll_interval",
                        "baseline_seq",
                        "last_seq",
                        "next_check_at",
                        "created_at",
                    },
                    "list_subscriptions": {
                        "subscription_id",
                        "target_id",
                        "list_id",
                        "enabled",
                        "poll_interval",
                        "baseline_seq",
                        "last_seq",
                        "last_check_at",
                        "next_check_at",
                        "created_at",
                    },
                    "deliveries": {
                        "delivery_id",
                        "target_id",
                        "article_id",
                        "status",
                        "source_path",
                        "attempts",
                        "next_attempt_at",
                        "idempotency_key",
                        "platform_message_id",
                        "error",
                        "created_at",
                        "updated_at",
                        "retryable",
                    },
                    "delivery_attempts": {
                        "attempt_id",
                        "delivery_id",
                        "attempted_at",
                        "result",
                        "error",
                        "platform_message_id",
                    },
                    "delivery_batches": {
                        "batch_id",
                        "group_id",
                        "target_id",
                        "status",
                        "card_index",
                        "card_count",
                        "payload",
                        "idempotency_key",
                        "platform_message_id",
                        "error",
                        "created_at",
                        "updated_at",
                    },
                    "batch_articles": {"batch_id", "delivery_id", "card_index"},
                    "fetch_logs": {
                        "id",
                        "feed_id",
                        "fetched_at",
                        "status_code",
                        "method",
                        "elapsed_ms",
                        "error_type",
                        "error_message",
                        "cookie_cache_hit",
                        "browser_attempted",
                    },
                    "settings": {"key", "value"},
                    "platform_media_cache": {
                        "platform_instance",
                        "media_url",
                        "media_kind",
                        "media_key",
                        "app_id",
                        "expires_at",
                    },
                    "telegraph_pages": {"article_id", "content_hash", "url", "created_at"},
                    "test_operations": {
                        "operation_id",
                        "target_id",
                        "feed_id",
                        "article_ids",
                        "status",
                        "error",
                        "created_at",
                        "updated_at",
                    },
                    "entity_counters": {"entity", "next_id"},
                }
                for table, expected in expected_columns.items():
                    actual = {row[1] for row in check.execute(f"PRAGMA table_info({table})")}
                    if not expected <= actual:
                        raise ValueError(f"backup table {table} is missing required columns")
                invalid_translation_flags = check.execute(
                    "SELECT COUNT(*) FROM feeds WHERE typeof(translation_enabled)!='integer' "
                    "OR translation_enabled NOT IN (0,1)"
                ).fetchone()[0]
                if invalid_translation_flags:
                    raise ValueError("backup feed translation_enabled values are invalid")
                unique_requirements = {
                    "feeds": ({"url"}, {"public_id"}),
                    "subscription_lists": ({"public_id"}, {"name"}),
                    "articles": ({"article_id"}, {"feed_id", "entry_id"}),
                    "targets": ({"target_id"}, {"platform_instance", "routing_key"}),
                    "direct_subscriptions": ({"subscription_id"}, {"target_id", "feed_id"}),
                    "list_subscriptions": ({"subscription_id"}, {"target_id", "list_id"}),
                    "deliveries": ({"delivery_id"}, {"target_id", "article_id"}, {"idempotency_key"}),
                    "delivery_batches": ({"batch_id"}, {"idempotency_key"}, {"group_id", "card_index"}),
                }
                for table, indexes in unique_requirements.items():
                    actual_indexes = []
                    for index in check.execute(f"PRAGMA index_list({table})"):
                        if index[2]:
                            actual_indexes.append({row[2] for row in check.execute(f"PRAGMA index_info({index[1]})")})
                    if any(required_index not in actual_indexes for required_index in indexes):
                        raise ValueError(f"backup table {table} is missing a required unique index")
                for table in ("feeds", "subscription_lists"):
                    invalid_ids = check.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE typeof(public_id)!='integer' OR public_id<=0"
                    ).fetchone()[0]
                    if invalid_ids:
                        raise ValueError(f"backup {table} public IDs are invalid")
                counters = {row[0]: row[1] for row in check.execute("SELECT entity,next_id FROM entity_counters")}
                if set(counters) != {"feed", "list"}:
                    raise ValueError("backup public ID counters are incomplete")
                for entity, table in (("feed", "feeds"), ("list", "subscription_lists")):
                    maximum = check.execute(f"SELECT COALESCE(MAX(public_id),0) FROM {table}").fetchone()[0]
                    if type(counters[entity]) is not int or counters[entity] < maximum + 1:
                        raise ValueError("backup public ID counter is invalid")
                # Compare every table, primary key, index and foreign key to the running
                # canonical schema, so an otherwise-integral lookalike cannot be swapped in.
                with closing(sqlite3.connect(f"file:{canonical_path.as_posix()}?mode=ro", uri=True)) as canonical:
                    canonical_tables = {
                        row[0]
                        for row in canonical.execute("SELECT name FROM sqlite_master WHERE type='table'")
                        if not row[0].startswith("sqlite_")
                    }
                    candidate_tables = {table for table in tables if not table.startswith("sqlite_")}
                    if canonical_tables != candidate_tables:
                        raise ValueError("backup table set does not match the current schema")

                    def signature(connection, table):
                        columns = tuple(
                            sorted(
                                (row[1], row[2], row[3], row[4], row[5])
                                for row in connection.execute(f"PRAGMA table_info({table})")
                            )
                        )
                        indexes = tuple(
                            sorted(
                                (
                                    int(index[2]),
                                    int(index[4] or 0),
                                    tuple(row[2] for row in connection.execute(f"PRAGMA index_info({index[1]})")),
                                )
                                for index in connection.execute(f"PRAGMA index_list({table})")
                            )
                        )
                        foreign_keys = tuple(
                            sorted(
                                (row[2], row[3], row[4], row[5], row[6])
                                for row in connection.execute(f"PRAGMA foreign_key_list({table})")
                            )
                        )
                        return columns, indexes, foreign_keys

                    for table in canonical_tables:
                        if signature(check, table) != signature(canonical, table):
                            raise ValueError(f"backup table {table} has incompatible keys or indexes")
        except sqlite3.DatabaseError as exc:
            raise ValueError("file is not a supported SQLite backup") from exc
        return restore_paused

    async def _restore_sqlite_locked(self, source: Path):
        if not source.is_file() or source.stat().st_size > 100_000_000:
            raise ValueError("invalid or oversized SQLite backup")
        candidate = source
        staged = None
        with closing(sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)) as probe:
            source_version = probe.execute("PRAGMA user_version").fetchone()[0]
        if source_version in {1, 2}:
            staged = self.runtime.data_dir / f".restore-v{source_version}-{uuid.uuid4().hex}.sqlite3"
            try:
                if source_version == 1:
                    self._validate_legacy_v1_staging_source(source)
                else:
                    self._validate_legacy_v2_staging_source(source)
                await asyncio.to_thread(shutil.copyfile, source, staged)
                migrator = Database(staged)
                await migrator.initialize()
                await migrator.close()
                candidate = staged
            except Exception:
                if staged.exists():
                    staged.unlink(missing_ok=True)
                self._remove_sqlite_sidecars(staged, staged.parent) if staged else None
                raise
        try:
            restore_paused = await asyncio.to_thread(
                self._validate_backup_sqlite, candidate, self.runtime.database.path
            )
        except Exception:
            if staged is not None:
                staged.unlink(missing_ok=True)
                self._remove_sqlite_sidecars(staged, staged.parent)
            raise
        data_dir, db_path = self.runtime.data_dir.resolve(), self.runtime.database.path.resolve()
        if db_path.parent != data_dir:
            raise ValueError("database path is outside plugin data directory")
        backup = data_dir / f"restore-safety-{uuid.uuid4().hex}.sqlite3"
        was_paused = await self.runtime.scheduler_status()
        await self.runtime.backup(backup)
        config, context = self.runtime.config, self.runtime.context
        await self.runtime.pause()
        await self.runtime.close()
        incoming = data_dir / f".restore-{uuid.uuid4().hex}.sqlite3"
        from ..core.runtime import RSSRuntime

        replacement = None
        try:
            shutil.copyfile(candidate, incoming)
            if staged is not None:
                staged.unlink(missing_ok=True)
                self._remove_sqlite_sidecars(staged, staged.parent)
            self._remove_sqlite_sidecars(db_path, data_dir)
            os.replace(incoming, db_path)
            replacement = RSSRuntime(config, data_dir, context)
            await replacement.initialize()
            await replacement.repository.set_setting("paused", "1" if restore_paused else "0")
            await replacement.start()
        except Exception as restore_error:
            if replacement is not None:
                try:
                    await replacement.close()
                except BaseException as close_error:
                    restore_error.add_note(f"Replacement cleanup also failed ({type(close_error).__name__})")
            recovery = None
            try:
                self._remove_sqlite_sidecars(db_path, data_dir)
                rollback_file = data_dir / f".rollback-{uuid.uuid4().hex}.sqlite3"
                shutil.copyfile(backup, rollback_file)
                os.replace(rollback_file, db_path)
                recovery = RSSRuntime(config, data_dir, context)
                await recovery.initialize()
                await recovery.repository.set_setting("paused", "1" if was_paused["paused"] else "0")
                await recovery.start()
                self._adopt_runtime(recovery)
            except BaseException as recovery_error:
                if recovery is not None:
                    try:
                        await recovery.close()
                    except BaseException as close_error:
                        restore_error.add_note(f"Recovery cleanup also failed ({type(close_error).__name__})")
                restore_error.add_note(f"Database rollback or recovery also failed ({type(recovery_error).__name__})")
            raise
        self._adopt_runtime(replacement)
        return {"restored": True, "safety_backup": backup.name}

    def _adopt_runtime(self, runtime):
        self.runtime, self.services = runtime, runtime.services
        self.read = ManagementRepository(runtime)
        callback = getattr(self, "on_runtime_replaced", None)
        if callback:
            callback(runtime)

    @staticmethod
    def _remove_sqlite_sidecars(db_path: Path, data_dir: Path) -> None:
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(db_path) + suffix)
            if sidecar.parent.resolve() == data_dir.resolve():
                try:
                    sidecar.unlink(missing_ok=True)
                except PermissionError:
                    # Never delete a path outside the verified plugin data directory.
                    raise

    async def _target(self, target_id):
        if not await self.services.targets.known(target_id):
            raise ValueError("target is not registered")

    async def _feed(self, feed_id):
        if not await self.runtime.repository.get_feed(feed_id):
            raise KeyError(feed_id)

    @staticmethod
    def _required(body, key):
        value = body.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{key} is required")
        return value.strip()

    @classmethod
    def _text(cls, body, key, minimum, maximum):
        value = cls._required(body, key)
        if not minimum <= len(value) <= maximum:
            raise ValueError(f"{key} length must be {minimum}..{maximum}")
        return value

    def _interval(self, value):
        if value is None:
            return None
        if type(value) is not int or value < 0:
            raise ValueError("poll_interval must be a non-negative integer")
        return value or None
