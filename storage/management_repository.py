from __future__ import annotations

import json
import time
from datetime import datetime
from datetime import time as datetime_time
from zoneinfo import ZoneInfo

from .interval_inputs import attach_interval_inputs


def _local_day_start(timestamp: float, timezone: str) -> float:
    zone = ZoneInfo(timezone)
    local_date = datetime.fromtimestamp(timestamp, zone).date()
    return datetime.combine(local_date, datetime_time.min, tzinfo=zone).timestamp()


class ManagementRepository:
    """Read-oriented queries used by the management UI; mutations stay in services/repository."""

    def __init__(self, runtime):
        self.runtime = runtime
        self.db = runtime.database

    async def page(self, sql: str, params: tuple, page: int, size: int):
        count_sql = sql.split(" ORDER BY ", 1)[0]
        count = await self.db.fetchone(f"SELECT COUNT(*) FROM ({count_sql}) q", params)
        rows = await self.db.fetchall(sql + " LIMIT ? OFFSET ?", (*params, size, (page - 1) * size))
        return {"items": [dict(row) for row in rows], "total": int(count[0]), "page": page, "page_size": size}

    async def feed_page(self, q: str, page: int, size: int, scope: str):
        direct = (
            " AND EXISTS(SELECT 1 FROM direct_subscriptions ds WHERE ds.feed_id=f.feed_id)" if scope == "direct" else ""
        )
        sql = (
            "SELECT f.*, (SELECT COUNT(*) FROM articles a WHERE a.feed_id=f.feed_id) article_count FROM feeds f WHERE (f.title LIKE ? OR f.url LIKE ?)"
            + direct
            + " ORDER BY f.created_at DESC"
        )
        result = await self.page(sql, (f"%{q}%", f"%{q}%"), page, size)
        for row in result["items"]:
            row["translation_enabled"] = bool(row["translation_enabled"])
        await attach_interval_inputs(self.db, "feed", result["items"])
        return result

    async def dashboard(self):
        db = self.db
        timezone = getattr(self.runtime.config, "timezone", "Asia/Shanghai")
        today_start = _local_day_start(time.time(), timezone)

        async def scalar(sql, args=()):
            return await db.fetchone(sql, args)

        keys = ("feeds", "active_feeds", "feed_errors", "lists", "targets", "today_success", "failed", "pending")
        queries = (
            "SELECT count(*) FROM feeds",
            "SELECT count(*) FROM feeds WHERE enabled=1",
            "SELECT count(*) FROM feeds WHERE status!='active'",
            "SELECT count(*) FROM subscription_lists",
            "SELECT count(*) FROM targets",
            "SELECT count(*) FROM deliveries WHERE status='success' AND updated_at>=?",
            "SELECT count(*) FROM deliveries WHERE status IN ('failed','unknown')",
            "SELECT count(*) FROM deliveries WHERE status IN ('pending','sending','rendering')",
        )
        counts = {
            key: int((await scalar(query, (today_start,) if key == "today_success" else ()))[0])
            for key, query in zip(keys, queries)
        }
        articles = await db.fetchall(
            "SELECT a.article_id,a.feed_id,f.public_id feed_public_id,a.title,a.published_at,a.updated_at,a.first_seen_at,a.canonical_url "
            "FROM articles a JOIN feeds f USING(feed_id) "
            "ORDER BY COALESCE(a.published_at,a.updated_at,a.first_seen_at) DESC,a.seq DESC,a.article_id DESC LIMIT 10"
        )
        errors = await db.fetchall(
            "WITH error_rows AS ("
            "SELECT f.feed_id,f.public_id feed_public_id,f.url,l.status_code,l.error_type,l.error_message,l.fetched_at,l.id,"
            "CASE WHEN l.status_code BETWEEN 400 AND 599 THEN 'http' ELSE 'error_type' END error_kind,"
            "CASE WHEN l.status_code BETWEEN 400 AND 599 THEN CAST(l.status_code AS TEXT) ELSE l.error_type END error_code "
            "FROM fetch_logs l JOIN feeds f USING(feed_id) WHERE l.error_type IS NOT NULL), "
            "ranked_errors AS (SELECT error_rows.*,ROW_NUMBER() OVER (PARTITION BY feed_id,error_kind,error_code "
            "ORDER BY fetched_at DESC,id DESC) error_rank FROM error_rows) "
            "SELECT feed_id,feed_public_id,url,status_code,error_type,error_message,fetched_at FROM ranked_errors "
            "WHERE error_rank=1 ORDER BY fetched_at DESC,id DESC LIMIT 10"
        )
        stats = await db.fetchone(
            "SELECT count(*),sum(CASE WHEN error_type IS NOT NULL THEN 1 ELSE 0 END),sum(browser_attempted),sum(cookie_cache_hit),sum(CASE WHEN status_code IN (200,304) AND error_type IS NULL THEN 1 ELSE 0 END),sum(CASE WHEN method='curl' AND status_code IN (200,304) AND error_type IS NULL THEN 1 ELSE 0 END) FROM fetch_logs WHERE fetched_at>=strftime('%s','now','-1 hour')"
        )
        paused = await db.fetchone("SELECT value FROM settings WHERE key='paused'")
        return {
            "counts": counts,
            "recent_articles": [dict(r) for r in articles],
            "recent_errors": [dict(r) for r in errors],
            "fetch_stats": {
                "last_hour": stats[0] or 0,
                "failures": stats[1] or 0,
                "browser_fallbacks": stats[2] or 0,
                "browser_attempts": stats[2] or 0,
                "cookie_hits": stats[3] or 0,
                "cookie_cache_hits": stats[3] or 0,
                "success_rate": round((stats[4] or 0) / stats[0], 4) if stats[0] else 0.0,
                "curl_success_rate": round((stats[5] or 0) / stats[0], 4) if stats[0] else 0.0,
            },
            "paused": bool(paused and paused[0] == "1"),
        }

    async def feed_detail(self, feed_id: str):
        feed = await self.db.fetchone("SELECT * FROM feeds WHERE feed_id=?", (feed_id,))
        if not feed:
            return None
        articles = await self.db.fetchall(
            "SELECT article_id,feed_id,? feed_public_id,title,author,published_at,updated_at,first_seen_at,canonical_url,content_blocks "
            "FROM articles WHERE feed_id=? "
            "ORDER BY COALESCE(published_at,updated_at,first_seen_at) DESC,seq DESC,article_id DESC LIMIT 50",
            (feed["public_id"], feed_id),
        )
        subscribers = await self.db.fetchall(
            "SELECT t.*,s.enabled subscription_enabled FROM targets t JOIN direct_subscriptions s USING(target_id) WHERE s.feed_id=?",
            (feed_id,),
        )
        refs = await self.db.fetchall(
            "SELECT l.list_id,l.public_id list_public_id,l.name FROM subscription_lists l JOIN list_feeds f USING(list_id) WHERE f.feed_id=?",
            (feed_id,),
        )
        paths_by_target = {}
        direct_paths = await self.db.fetchall(
            "SELECT t.*,s.enabled AND f.enabled path_enabled,'direct' kind,NULL list_id,NULL list_public_id,NULL list_name "
            "FROM targets t JOIN direct_subscriptions s USING(target_id) JOIN feeds f USING(feed_id) WHERE s.feed_id=?",
            (feed_id,),
        )
        list_paths = await self.db.fetchall(
            "SELECT t.*,s.enabled AND l.enabled AND f.enabled path_enabled,'list' kind,l.list_id,l.public_id list_public_id,l.name list_name "
            "FROM targets t JOIN list_subscriptions s USING(target_id) JOIN subscription_lists l USING(list_id) "
            "JOIN list_feeds lf USING(list_id) JOIN feeds f USING(feed_id) WHERE lf.feed_id=?",
            (feed_id,),
        )
        for path in (*direct_paths, *list_paths):
            target_id = path["target_id"]
            subscriber = paths_by_target.setdefault(
                target_id,
                {key: path[key] for key in ("target_id", "platform", "platform_instance", "unified_origin", "label")}
                | {"enabled": False, "paths": []},
            )
            subscriber["enabled"] = subscriber["enabled"] or bool(path["path_enabled"])
            subscriber["paths"].append(
                {key: path[key] for key in ("kind", "list_id", "list_public_id", "list_name") if path[key] is not None}
                | {"enabled": bool(path["path_enabled"])}
            )
        logs = await self.db.fetchall(
            "SELECT * FROM fetch_logs WHERE feed_id=? ORDER BY fetched_at DESC,id DESC LIMIT 50", (feed_id,)
        )
        feed_data = dict(feed)
        feed_data["translation_enabled"] = bool(feed_data["translation_enabled"])
        await attach_interval_inputs(self.db, "feed", [feed_data])
        return {
            "feed": feed_data,
            "articles": [dict(r) for r in articles],
            "direct_subscribers": [dict(r) for r in subscribers],
            "subscribers": list(paths_by_target.values()),
            "list_refs": [dict(r) for r in refs],
            "fetch_logs": [dict(r) for r in logs],
        }

    async def list_detail(self, list_id: str):
        row = await self.db.fetchone("SELECT * FROM subscription_lists WHERE list_id=?", (list_id,))
        if not row:
            return None
        members = await self.db.fetchall(
            "SELECT f.feed_id,f.public_id feed_public_id,f.url,f.title,f.status,f.last_fetch_at FROM feeds f JOIN list_feeds lf USING(feed_id) WHERE lf.list_id=? ORDER BY f.title",
            (list_id,),
        )
        subs = await self.db.fetchall(
            "SELECT t.*,s.enabled subscription_enabled FROM targets t JOIN list_subscriptions s USING(target_id) WHERE s.list_id=?",
            (list_id,),
        )
        subscribers = [
            {
                **{
                    key: target[key]
                    for key in ("target_id", "platform", "platform_instance", "unified_origin", "label")
                },
                "enabled": bool(target["subscription_enabled"] and row["enabled"]),
                "paths": [
                    {
                        "kind": "list",
                        "list_id": row["list_id"],
                        "list_public_id": row["public_id"],
                        "list_name": row["name"],
                        "enabled": bool(target["subscription_enabled"]),
                    }
                ],
            }
            for target in subs
        ]
        articles = await self.db.fetchall(
            "SELECT a.article_id,a.feed_id,f.public_id feed_public_id,COALESCE(NULLIF(f.title,''),f.url) feed_title,"
            "a.title,a.author,a.published_at,a.updated_at,a.canonical_url,a.content_blocks,a.first_seen_at "
            "FROM articles a JOIN feeds f USING(feed_id) JOIN list_feeds lf USING(feed_id) "
            "WHERE lf.list_id=? "
            "ORDER BY COALESCE(a.published_at,a.updated_at,a.first_seen_at) DESC,a.seq DESC,a.article_id DESC LIMIT 100",
            (list_id,),
        )
        logs = await self.db.fetchall(
            "SELECT l.*,f.public_id feed_public_id,COALESCE(NULLIF(f.title,''),f.url) feed_title "
            "FROM fetch_logs l JOIN feeds f USING(feed_id) JOIN list_feeds lf USING(feed_id) "
            "WHERE lf.list_id=? ORDER BY l.fetched_at DESC,l.id DESC LIMIT 100",
            (list_id,),
        )
        list_data = dict(row)
        await attach_interval_inputs(self.db, "list", [list_data])
        return {
            "list": list_data,
            "members": [dict(r) for r in members],
            "subscribers": subscribers,
            "articles": [dict(r) for r in articles],
            "fetch_logs": [dict(r) for r in logs],
        }

    async def delivery_detail(self, delivery_id: str):
        delivery = await self.db.fetchone(
            "SELECT d.*,t.platform,t.label target_label,a.title,a.canonical_url,a.content_blocks,a.article_id,a.feed_id,f.public_id feed_public_id,ls.list_id,l.public_id list_public_id FROM deliveries d JOIN targets t USING(target_id) JOIN articles a USING(article_id) JOIN feeds f USING(feed_id) LEFT JOIN list_subscriptions ls ON ls.target_id=d.target_id AND d.source_path='list:'||ls.subscription_id LEFT JOIN subscription_lists l ON l.list_id=ls.list_id WHERE delivery_id=?",
            (delivery_id,),
        )
        if not delivery:
            return None
        attempts = await self.db.fetchall(
            "SELECT * FROM delivery_attempts WHERE delivery_id=? ORDER BY attempted_at DESC,attempt_id DESC",
            (delivery_id,),
        )
        batches = await self.db.fetchall(
            "SELECT b.*,ba.card_index FROM delivery_batches b JOIN batch_articles ba USING(batch_id) WHERE ba.delivery_id=? ORDER BY b.card_index,b.rowid",
            (delivery_id,),
        )
        batch_data = []
        for batch in batches:
            item = dict(batch)
            try:
                item["payload"] = json.loads(item["payload"])
            except (TypeError, ValueError):
                item["payload"] = {}
            links = await self.db.fetchall(
                "SELECT ba.delivery_id,d.article_id FROM batch_articles ba JOIN deliveries d USING(delivery_id) WHERE ba.batch_id=? ORDER BY d.created_at,d.rowid",
                (batch["batch_id"],),
            )
            item["delivery_ids"] = [row["delivery_id"] for row in links]
            item["article_ids"] = [row["article_id"] for row in links]
            batch_data.append(item)
        return {
            "delivery": dict(delivery),
            "article": {
                k: delivery[k]
                for k in ("article_id", "feed_id", "feed_public_id", "title", "canonical_url", "content_blocks")
            },
            "attempts": [dict(r) for r in attempts],
            "batches": batch_data,
        }
