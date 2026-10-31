from __future__ import annotations

import json
import uuid
from typing import Any

from ..core.models import Article, ContentBlock, DeliveryTarget, Feed, now_ts
from .database import Database
from .interval_inputs import interval_input_key, save_interval_input


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _poll_interval(value):
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise ValueError("poll_interval must be a non-negative integer")
    return value or None


_PATH_INTERVALS_CTE = """WITH path_intervals AS (
    SELECT s.feed_id, MAX(?,COALESCE(NULLIF(s.poll_interval,0),NULLIF(f.poll_interval,0),?)) interval
      FROM direct_subscriptions s JOIN feeds f USING(feed_id) WHERE s.enabled=1 AND f.enabled=1
    UNION ALL
    SELECT lf.feed_id, MAX(?,COALESCE(NULLIF(ls.poll_interval,0),NULLIF(l.poll_interval,0),?)) interval
      FROM list_subscriptions ls JOIN subscription_lists l USING(list_id) JOIN list_feeds lf USING(list_id)
      JOIN feeds f USING(feed_id) WHERE ls.enabled=1 AND l.enabled=1 AND f.enabled=1
) """


class Repository:
    def __init__(self, db: Database):
        self.db = db

    async def create_feed(
        self,
        url: str,
        title: str = "",
        description: str = "",
        poll_interval: int | None = None,
        translation_enabled: bool = False,
        poll_interval_input: str | None = None,
    ) -> Feed:
        if type(translation_enabled) is not bool:
            raise ValueError("translation_enabled must be a boolean")
        poll_interval = _poll_interval(poll_interval)
        feed_id = str(uuid.uuid5(uuid.NAMESPACE_URL, url))
        async with self.db.transaction() as conn:
            row = await (await conn.execute("SELECT * FROM feeds WHERE url=?", (url,))).fetchone()
            if row is not None:
                if title:
                    await conn.execute("UPDATE feeds SET title=? WHERE feed_id=?", (title, row["feed_id"]))
                row = await (await conn.execute("SELECT * FROM feeds WHERE feed_id=?", (row["feed_id"],))).fetchone()
            else:
                public_id = await self.db.next_public_id(conn, "feed")
                await conn.execute(
                    "INSERT INTO feeds(feed_id,public_id,url,title,description,poll_interval,translation_enabled,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (feed_id, public_id, url, title, description, poll_interval, int(translation_enabled), now_ts()),
                )
                await save_interval_input(conn, "feed", feed_id, poll_interval, poll_interval_input)
                row = await (await conn.execute("SELECT * FROM feeds WHERE feed_id=?", (feed_id,))).fetchone()
        return self._feed(row)

    async def get_feed_by_url(self, url: str) -> Feed | None:
        row = await self.db.fetchone("SELECT * FROM feeds WHERE url=?", (url,))
        return self._feed(row) if row else None

    async def get_feed_by_public_id(self, public_id: int) -> Feed | None:
        row = await self.db.fetchone("SELECT * FROM feeds WHERE public_id=?", (public_id,))
        return self._feed(row) if row else None

    async def get_feeds_by_title(self, title: str) -> list[Feed]:
        rows = await self.db.fetchall("SELECT * FROM feeds WHERE title=? ORDER BY feed_id", (title,))
        return [self._feed(row) for row in rows]

    async def create_initialized_feed(
        self,
        feed_id: str,
        url: str,
        title: str,
        description: str,
        poll_interval: int | None,
        etag: str | None,
        last_modified: str | None,
        articles: list[Article],
        next_fetch_at: float,
        metadata: dict[str, Any] | None = None,
        translation_enabled: bool = False,
        poll_interval_input: str | None = None,
    ) -> Feed:
        """Atomically persist a validated initial fetch and its no-history baseline."""
        if type(translation_enabled) is not bool:
            raise ValueError("translation_enabled must be a boolean")
        poll_interval = _poll_interval(poll_interval)
        now = now_ts()
        async with self.db.transaction() as conn:
            public_id = await self.db.next_public_id(conn, "feed")
            await conn.execute(
                "INSERT INTO feeds(feed_id,public_id,url,title,description,poll_interval,etag,last_modified,initialized,last_fetch_at,next_fetch_at,created_at,metadata,translation_enabled) VALUES(?,?,?,?,?,?,?,?,1,?,?,?,?,?)",
                (
                    feed_id,
                    public_id,
                    url,
                    title,
                    description,
                    poll_interval,
                    etag,
                    last_modified,
                    now,
                    next_fetch_at,
                    now,
                    _json(metadata or {}),
                    int(translation_enabled),
                ),
            )
            await save_interval_input(conn, "feed", feed_id, poll_interval, poll_interval_input)
            for article in articles:
                await conn.execute(
                    "INSERT OR IGNORE INTO articles(article_id,feed_id,entry_id,canonical_url,title,author,published_at,updated_at,first_seen_at,categories,content_hash,content_blocks,metadata) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        article.article_id,
                        feed_id,
                        article.entry_id,
                        article.canonical_url,
                        article.title,
                        article.author,
                        article.published_at,
                        article.updated_at,
                        article.first_seen_at,
                        _json(article.categories),
                        article.content_hash,
                        _json([block.to_dict() for block in article.content_blocks]),
                        _json(article.metadata),
                    ),
                )
            row = await (await conn.execute("SELECT * FROM feeds WHERE feed_id=?", (feed_id,))).fetchone()
        return self._feed(row)

    @staticmethod
    def _feed(row) -> Feed:
        return Feed(
            row["feed_id"],
            row["url"],
            row["title"],
            row["description"],
            bool(row["enabled"]),
            row["poll_interval"],
            row["etag"],
            row["last_modified"],
            bool(row["initialized"]),
            json.loads(row["metadata"]),
            row["public_id"],
            bool(row["translation_enabled"]),
        )

    async def get_feed(self, feed_id: str) -> Feed | None:
        row = await self.db.fetchone("SELECT * FROM feeds WHERE feed_id=?", (feed_id,))
        return self._feed(row) if row else None

    async def list_feeds(self) -> list[dict[str, Any]]:
        rows = [
            dict(row)
            for row in await self.db.fetchall(
                "SELECT f.*, (SELECT COUNT(*) FROM articles a WHERE a.feed_id=f.feed_id) article_count FROM feeds f ORDER BY f.created_at DESC"
            )
        ]
        for row in rows:
            row["translation_enabled"] = bool(row["translation_enabled"])
        return rows

    async def update_feed(self, feed_id: str, **values: Any) -> Feed:
        allowed = {"title", "description", "enabled", "poll_interval", "url", "translation_enabled"}
        if "poll_interval" in values:
            values["poll_interval"] = _poll_interval(values["poll_interval"])
        if "translation_enabled" in values and type(values["translation_enabled"]) is not bool:
            raise ValueError("translation_enabled must be a boolean")
        updates = {
            k: int(v) if k in {"enabled", "translation_enabled"} else v for k, v in values.items() if k in allowed
        }
        if not updates:
            feed = await self.get_feed(feed_id)
            if feed is None:
                raise KeyError(feed_id)
            return feed
        async with self.db.transaction() as conn:
            await conn.execute(
                f"UPDATE feeds SET {','.join(f'{k}=?' for k in updates)} WHERE feed_id=?", (*updates.values(), feed_id)
            )
            row = await (await conn.execute("SELECT * FROM feeds WHERE feed_id=?", (feed_id,))).fetchone()
            if row is not None and "poll_interval" in updates:
                await save_interval_input(
                    conn, "feed", feed_id, updates["poll_interval"], values.get("poll_interval_input")
                )
        if row is None:
            raise KeyError(feed_id)
        return self._feed(row)

    async def create_target(
        self,
        platform_instance: str,
        unified_origin: str,
        platform: str,
        session_id: str,
        session_type: str = "",
        label: str = "",
        routing_key: str | None = None,
    ) -> DeliveryTarget:
        routing_key = routing_key or unified_origin
        target_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{platform_instance}:{routing_key}"))
        async with self.db.transaction() as conn:
            await conn.execute(
                "INSERT INTO targets(target_id,platform_instance,unified_origin,platform,session_id,session_type,label,routing_key) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(platform_instance,routing_key) DO UPDATE SET unified_origin=excluded.unified_origin,platform=excluded.platform,session_id=excluded.session_id,session_type=excluded.session_type,label=excluded.label",
                (target_id, platform_instance, unified_origin, platform, session_id, session_type, label, routing_key),
            )
            row = await (
                await conn.execute(
                    "SELECT * FROM targets WHERE platform_instance=? AND routing_key=?",
                    (platform_instance, routing_key),
                )
            ).fetchone()
        return DeliveryTarget(
            *(
                row[k]
                for k in (
                    "target_id",
                    "platform_instance",
                    "unified_origin",
                    "platform",
                    "session_id",
                    "session_type",
                    "label",
                )
            )
        )

    async def get_target(self, target_id: str) -> DeliveryTarget | None:
        row = await self.db.fetchone("SELECT * FROM targets WHERE target_id=?", (target_id,))
        return (
            DeliveryTarget(
                *(
                    row[k]
                    for k in (
                        "target_id",
                        "platform_instance",
                        "unified_origin",
                        "platform",
                        "session_id",
                        "session_type",
                        "label",
                    )
                )
            )
            if row
            else None
        )

    async def add_direct_subscription(
        self, target_id: str, feed_id: str, interval: int | None = None, send_history: bool = False
    ) -> str:
        interval = _poll_interval(interval)
        sub_id, now = str(uuid.uuid4()), now_ts()
        async with self.db.transaction() as conn:
            max_seq = await (
                await conn.execute("SELECT COALESCE(MAX(seq),0) FROM articles WHERE feed_id=?", (feed_id,))
            ).fetchone()
            baseline = 0 if send_history else max_seq[0]
            await conn.execute(
                "INSERT INTO direct_subscriptions(subscription_id,target_id,feed_id,poll_interval,baseline_seq,last_seq,next_check_at,created_at) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(target_id,feed_id) DO UPDATE SET enabled=1,poll_interval=excluded.poll_interval",
                (sub_id, target_id, feed_id, interval, baseline, baseline, now, now),
            )
            row = await (
                await conn.execute(
                    "SELECT subscription_id FROM direct_subscriptions WHERE target_id=? AND feed_id=?",
                    (target_id, feed_id),
                )
            ).fetchone()
        return row[0]

    async def remove_direct_subscription(self, target_id: str, feed_id: str) -> bool:
        async with self.db.transaction() as conn:
            cur = await conn.execute(
                "DELETE FROM direct_subscriptions WHERE target_id=? AND feed_id=?", (target_id, feed_id)
            )
            return cur.rowcount > 0

    async def list_direct_subscriptions(self, target_id: str) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in await self.db.fetchall(
                "SELECT s.*,f.url,f.title,f.public_id FROM direct_subscriptions s JOIN feeds f USING(feed_id) WHERE target_id=? ORDER BY f.title",
                (target_id,),
            )
        ]

    async def create_list(
        self, name: str, description: str = "", interval: int | None = None, poll_interval_input: str | None = None
    ) -> str:
        name = self._list_name(name)
        interval = _poll_interval(interval)
        list_id, now = str(uuid.uuid4()), now_ts()
        async with self.db.transaction() as conn:
            duplicate = await (
                await conn.execute("SELECT 1 FROM subscription_lists WHERE name=? LIMIT 1", (name,))
            ).fetchone()
            if duplicate:
                raise ValueError("列表名称已存在，请使用其他名称")
            public_id = await self.db.next_public_id(conn, "list")
            await conn.execute(
                "INSERT INTO subscription_lists(list_id,public_id,name,description,poll_interval,created_at) VALUES(?,?,?,?,?,?)",
                (list_id, public_id, name, description, interval, now),
            )
            await save_interval_input(conn, "list", list_id, interval, poll_interval_input)
        return list_id

    @staticmethod
    def _list_name(name: str) -> str:
        if not isinstance(name, str):
            raise ValueError("list name must be a string")
        normalized = name.strip()
        if not 1 <= len(normalized) <= 100:
            raise ValueError("list name must contain 1..100 characters")
        return normalized

    async def get_list(self, list_id: str) -> dict[str, Any] | None:
        row = await self.db.fetchone("SELECT * FROM subscription_lists WHERE list_id=?", (list_id,))
        return dict(row) if row else None

    async def get_list_by_public_id(self, public_id: int) -> dict[str, Any] | None:
        row = await self.db.fetchone("SELECT * FROM subscription_lists WHERE public_id=?", (public_id,))
        return dict(row) if row else None

    async def get_lists_by_name(self, name: str) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in await self.db.fetchall("SELECT * FROM subscription_lists WHERE name=? ORDER BY list_id", (name,))
        ]

    async def update_list(self, list_id: str, **values: Any) -> dict[str, Any]:
        allowed = {"name", "description", "enabled", "poll_interval"}
        if "name" in values:
            values["name"] = self._list_name(values["name"])
        if "poll_interval" in values:
            values["poll_interval"] = _poll_interval(values["poll_interval"])
        updates = {key: int(value) if key == "enabled" else value for key, value in values.items() if key in allowed}
        async with self.db.transaction() as conn:
            row = await (
                await conn.execute("SELECT list_id,name FROM subscription_lists WHERE list_id=?", (list_id,))
            ).fetchone()
            if row is None:
                raise KeyError(list_id)
            if updates:
                if "name" in updates and updates["name"] != row["name"]:
                    duplicate = await (
                        await conn.execute(
                            "SELECT 1 FROM subscription_lists WHERE name=? AND list_id<>? LIMIT 1",
                            (updates["name"], list_id),
                        )
                    ).fetchone()
                    if duplicate:
                        raise ValueError("列表名称已存在，请使用其他名称")
                await conn.execute(
                    "UPDATE subscription_lists SET " + ",".join(f"{key}=?" for key in updates) + " WHERE list_id=?",
                    (*updates.values(), list_id),
                )
                if "poll_interval" in updates:
                    await save_interval_input(
                        conn, "list", list_id, updates["poll_interval"], values.get("poll_interval_input")
                    )
            result = await (
                await conn.execute("SELECT * FROM subscription_lists WHERE list_id=?", (list_id,))
            ).fetchone()
        return dict(result)

    async def delete_list(self, list_id: str) -> bool:
        async with self.db.transaction() as conn:
            cur = await conn.execute("DELETE FROM subscription_lists WHERE list_id=?", (list_id,))
            await conn.execute("DELETE FROM settings WHERE key=?", (interval_input_key("list", list_id),))
            return cur.rowcount > 0

    async def list_lists(self) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in await self.db.fetchall(
                "SELECT l.*,(SELECT COUNT(*) FROM list_feeds f WHERE f.list_id=l.list_id) feed_count FROM subscription_lists l ORDER BY name"
            )
        ]

    async def add_list_feed(self, list_id: str, feed_id: str, send_history: bool = False) -> None:
        async with self.db.transaction() as conn:
            maximum = await (
                await conn.execute("SELECT COALESCE(MAX(seq),0) FROM articles WHERE feed_id=?", (feed_id,))
            ).fetchone()
            baseline = 0 if send_history else maximum[0]
            await conn.execute(
                "INSERT OR IGNORE INTO list_feeds(list_id,feed_id,baseline_seq) VALUES(?,?,?)",
                (list_id, feed_id, baseline),
            )

    async def remove_list_feed(self, list_id: str, feed_id: str) -> bool:
        async with self.db.transaction() as conn:
            cur = await conn.execute("DELETE FROM list_feeds WHERE list_id=? AND feed_id=?", (list_id, feed_id))
            return cur.rowcount > 0

    async def subscribe_list(
        self, target_id: str, list_id: str, interval: int | None = None, send_history: bool = False
    ) -> str:
        interval = _poll_interval(interval)
        sub, now = str(uuid.uuid4()), now_ts()
        async with self.db.transaction() as conn:
            maximum = await (
                await conn.execute(
                    "SELECT COALESCE(MAX(a.seq),0) FROM list_feeds lf JOIN articles a USING(feed_id) WHERE lf.list_id=?",
                    (list_id,),
                )
            ).fetchone()
            baseline = 0 if send_history else maximum[0]
            await conn.execute(
                "INSERT INTO list_subscriptions(subscription_id,target_id,list_id,poll_interval,baseline_seq,last_seq,next_check_at,created_at) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(target_id,list_id) DO UPDATE SET enabled=1,poll_interval=excluded.poll_interval",
                (sub, target_id, list_id, interval, baseline, baseline, now, now),
            )
            row = await (
                await conn.execute(
                    "SELECT subscription_id FROM list_subscriptions WHERE target_id=? AND list_id=?",
                    (target_id, list_id),
                )
            ).fetchone()
        return row[0]

    async def unsubscribe_list(self, target_id: str, list_id: str) -> bool:
        async with self.db.transaction() as conn:
            cur = await conn.execute(
                "DELETE FROM list_subscriptions WHERE target_id=? AND list_id=?", (target_id, list_id)
            )
            return cur.rowcount > 0

    async def save_articles(self, feed_id: str, articles: list[Article]) -> tuple[int, int]:
        inserted = 0
        async with self.db.transaction() as conn:
            for article in articles:
                cur = await conn.execute(
                    "INSERT OR IGNORE INTO articles(article_id,feed_id,entry_id,canonical_url,title,author,published_at,updated_at,first_seen_at,categories,content_hash,content_blocks,metadata) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        article.article_id,
                        feed_id,
                        article.entry_id,
                        article.canonical_url,
                        article.title,
                        article.author,
                        article.published_at,
                        article.updated_at,
                        article.first_seen_at,
                        _json(article.categories),
                        article.content_hash,
                        _json([b.to_dict() for b in article.content_blocks]),
                        _json(article.metadata),
                    ),
                )
                inserted += cur.rowcount
            cursor = await (
                await conn.execute("SELECT COALESCE(MAX(seq),0) FROM articles WHERE feed_id=?", (feed_id,))
            ).fetchone()
        return inserted, cursor[0]

    async def article(self, article_id: str) -> Article | None:
        row = await self.db.fetchone(
            "SELECT a.*,COALESCE(NULLIF(f.title,''),f.url) feed_title FROM articles a JOIN feeds f USING(feed_id) WHERE a.article_id=?",
            (article_id,),
        )
        return self._article(row) if row else None

    async def articles_for_feed(self, feed_id: str, limit: int = 100) -> list[Article]:
        rows = await self.db.fetchall(
            "SELECT a.*,COALESCE(NULLIF(f.title,''),f.url) feed_title "
            "FROM articles a JOIN feeds f USING(feed_id) WHERE a.feed_id=? "
            "ORDER BY COALESCE(a.published_at,a.updated_at,a.first_seen_at) DESC,"
            "a.seq DESC,a.article_id DESC LIMIT ?",
            (feed_id, limit),
        )
        return [self._article(row) for row in rows]

    async def articles_for_list(self, list_id: str, limit: int = 100) -> list[Article]:
        rows = await self.db.fetchall(
            "SELECT a.*,COALESCE(NULLIF(f.title,''),f.url) feed_title "
            "FROM articles a JOIN feeds f USING(feed_id) "
            "JOIN list_feeds lf USING(feed_id) WHERE lf.list_id=? "
            "ORDER BY COALESCE(a.published_at,a.updated_at,a.first_seen_at) DESC,"
            "a.seq DESC,a.article_id DESC LIMIT ?",
            (list_id, limit),
        )
        return [self._article(row) for row in rows]

    @staticmethod
    def _article(row) -> Article:
        return Article(
            row["article_id"],
            row["feed_id"],
            row["entry_id"],
            row["canonical_url"],
            row["title"],
            row["author"],
            row["published_at"],
            row["updated_at"],
            row["first_seen_at"],
            json.loads(row["categories"]),
            row["content_hash"],
            [ContentBlock.from_dict(b) for b in json.loads(row["content_blocks"])],
            {**json.loads(row["metadata"]), "feed_title": row["feed_title"]},
        )

    async def due_feeds(self, now: float, default_interval: int = 300, min_interval: int = 1) -> list[dict[str, Any]]:
        interval_params = (min_interval, default_interval, min_interval, default_interval)
        async with self.db.transaction() as conn:
            await conn.execute(
                _PATH_INTERVALS_CTE
                + """UPDATE feeds SET next_fetch_at=MIN(next_fetch_at,last_fetch_at+
                (SELECT MIN(interval) FROM path_intervals p WHERE p.feed_id=feeds.feed_id))
                WHERE last_fetch_at IS NOT NULL AND status!='http_429'
                AND EXISTS(SELECT 1 FROM path_intervals p WHERE p.feed_id=feeds.feed_id)""",
                interval_params,
            )
        sql = (
            _PATH_INTERVALS_CTE
            + """SELECT f.*,MIN(p.interval) required_interval FROM feeds f JOIN path_intervals p USING(feed_id)
          WHERE f.enabled=1 AND f.next_fetch_at<=? GROUP BY f.feed_id"""
        )
        params = (*interval_params, now)
        return [dict(r) for r in await self.db.fetchall(sql, params)]

    async def active_feeds(
        self, now: float | None = None, default_interval: int = 300, min_interval: int = 1
    ) -> list[dict[str, Any]]:
        now = now_ts() if now is None else now
        params = (min_interval, default_interval, min_interval, default_interval, now)
        rows = await self.db.fetchall(
            _PATH_INTERVALS_CTE
            + """SELECT f.*,MIN(p.interval) required_interval FROM feeds f JOIN path_intervals p USING(feed_id)
            WHERE f.enabled=1 AND (f.status!='http_429' OR f.next_fetch_at<=?) GROUP BY f.feed_id""",
            params,
        )
        return [dict(row) for row in rows]

    async def feed_retry_deadline(self, feed_id: str) -> float | None:
        row = await self.db.fetchone("SELECT status,next_fetch_at FROM feeds WHERE feed_id=?", (feed_id,))
        if row is not None and row["status"] == "http_429":
            return float(row["next_fetch_at"])
        return None

    async def required_fetch_interval(self, feed_id: str, default_interval: int = 300, min_interval: int = 1) -> int:
        params = (min_interval, default_interval, min_interval, default_interval, feed_id)
        row = await self.db.fetchone(
            _PATH_INTERVALS_CTE + "SELECT MIN(interval) interval FROM path_intervals WHERE feed_id=?", params
        )
        return int(row["interval"]) if row and row["interval"] is not None else default_interval

    async def update_fetch_state(
        self,
        feed_id: str,
        *,
        etag: str | None,
        last_modified: str | None,
        initialized: bool,
        next_fetch_at: float,
        status: str = "active",
    ) -> None:
        async with self.db.transaction() as conn:
            await conn.execute(
                "UPDATE feeds SET etag=?,last_modified=?,initialized=?,last_fetch_at=?,next_fetch_at=?,status=? WHERE feed_id=?",
                (etag, last_modified, int(initialized), now_ts(), next_fetch_at, status, feed_id),
            )

    async def merge_feed_metadata(self, feed_id: str, values: dict[str, Any]) -> Feed:
        """Merge safe opaque metadata without replacing unrelated feed metadata."""
        if not isinstance(values, dict):
            raise TypeError("feed metadata update must be a dictionary")
        async with self.db.transaction() as conn:
            row = await (await conn.execute("SELECT metadata FROM feeds WHERE feed_id=?", (feed_id,))).fetchone()
            if row is None:
                raise KeyError(feed_id)
            metadata = json.loads(row["metadata"] or "{}")
            metadata.update(values)
            await conn.execute("UPDATE feeds SET metadata=? WHERE feed_id=?", (_json(metadata), feed_id))
            updated = await (await conn.execute("SELECT * FROM feeds WHERE feed_id=?", (feed_id,))).fetchone()
        return self._feed(updated)

    async def enqueue_due_paths(self, now: float, default_interval: int, min_interval: int, force: bool = False) -> int:
        """Advance path cursors and create target/article unique jobs atomically."""
        count = 0
        async with self.db.transaction() as conn:
            direct = await (
                await conn.execute(
                    "SELECT s.*,f.poll_interval feed_interval FROM direct_subscriptions s JOIN feeds f USING(feed_id) WHERE s.enabled=1 AND f.enabled=1 AND (?=1 OR s.next_check_at<=?)",
                    (int(force), now),
                )
            ).fetchall()
            for path in direct:
                interval = max(min_interval, path["poll_interval"] or path["feed_interval"] or default_interval)
                rows = await (
                    await conn.execute(
                        "SELECT a.article_id,a.seq FROM articles a WHERE a.feed_id=? AND a.seq>? AND a.seq>? ORDER BY a.seq",
                        (path["feed_id"], path["last_seq"], path["baseline_seq"]),
                    )
                ).fetchall()
                for row in rows:
                    count += await self._insert_delivery(
                        conn, path["target_id"], row["article_id"], f"direct:{path['subscription_id']}", now
                    )
                maximum = rows[-1]["seq"] if rows else path["last_seq"]
                await conn.execute(
                    "UPDATE direct_subscriptions SET last_seq=?,next_check_at=? WHERE subscription_id=?",
                    (maximum, now + interval, path["subscription_id"]),
                )
            lists = await (
                await conn.execute(
                    "SELECT s.*,l.poll_interval list_interval FROM list_subscriptions s JOIN subscription_lists l USING(list_id) WHERE s.enabled=1 AND l.enabled=1 AND (?=1 OR s.next_check_at<=?)",
                    (int(force), now),
                )
            ).fetchall()
            for path in lists:
                interval = max(min_interval, path["poll_interval"] or path["list_interval"] or default_interval)
                rows = await (
                    await conn.execute(
                        "SELECT a.article_id,a.seq FROM list_feeds lf JOIN articles a ON a.feed_id=lf.feed_id JOIN feeds f ON f.feed_id=a.feed_id WHERE lf.list_id=? AND f.enabled=1 AND a.seq>lf.baseline_seq AND a.seq>? AND a.seq>? ORDER BY a.seq",
                        (path["list_id"], path["last_seq"], path["baseline_seq"]),
                    )
                ).fetchall()
                for row in rows:
                    count += await self._insert_delivery(
                        conn, path["target_id"], row["article_id"], f"list:{path['subscription_id']}", now
                    )
                maximum = max((row["seq"] for row in rows), default=path["last_seq"])
                await conn.execute(
                    "UPDATE list_subscriptions SET last_seq=?,last_check_at=?,next_check_at=? WHERE subscription_id=?",
                    (maximum, now, now + interval, path["subscription_id"]),
                )
        return count

    @staticmethod
    async def _insert_delivery(conn, target_id: str, article_id: str, path: str, now: float) -> int:
        delivery_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{target_id}:{article_id}"))
        cur = await conn.execute(
            "INSERT OR IGNORE INTO deliveries(delivery_id,target_id,article_id,source_path,idempotency_key,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            (delivery_id, target_id, article_id, path, delivery_id, now, now),
        )
        return cur.rowcount

    async def pending_deliveries(self, now: float, limit: int = 100, max_attempts: int = 5) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in await self.db.fetchall(
                "SELECT d.*,t.platform_instance,t.unified_origin,t.platform,t.session_id,t.session_type,t.label FROM deliveries d JOIN targets t USING(target_id) WHERE ((d.status='pending' OR d.status='failed') AND d.retryable=1 AND d.attempts<?) AND d.next_attempt_at<=? ORDER BY d.created_at,d.rowid LIMIT ?",
                (max_attempts, now, limit),
            )
        ]

    async def claim_deliveries(self, delivery_ids: list[str], max_attempts: int) -> list[dict[str, Any]]:
        if not delivery_ids:
            return []
        placeholders = ",".join("?" for _ in delivery_ids)
        async with self.db.transaction() as conn:
            cursor = await conn.execute(
                f"UPDATE deliveries SET status='rendering',attempts=attempts+1,updated_at=? WHERE delivery_id IN ({placeholders}) AND retryable=1 AND (status='pending' OR (status='failed' AND attempts<?)) RETURNING delivery_id",
                (now_ts(), *delivery_ids, max_attempts),
            )
            claimed = [r[0] for r in await cursor.fetchall()]
            if not claimed:
                return []
            claim_placeholders = ",".join("?" for _ in claimed)
            rows = await (
                await conn.execute(
                    f"SELECT d.*,t.platform_instance,t.unified_origin,t.platform,t.session_id,t.session_type,t.label FROM deliveries d JOIN targets t USING(target_id) WHERE d.delivery_id IN ({claim_placeholders}) AND d.status='rendering' ORDER BY d.created_at,d.rowid",
                    claimed,
                )
            ).fetchall()
        return [dict(r) for r in rows]

    async def release_unattempted_deliveries(self, delivery_ids: list[str]) -> None:
        """Requeue unfinished claims without charging for cards that were not processed."""
        if not delivery_ids:
            return
        placeholders = ",".join("?" for _ in delivery_ids)
        async with self.db.transaction() as conn:
            await conn.execute(
                f"UPDATE deliveries SET status='pending',attempts=MAX(attempts-1,0),next_attempt_at=0,error=NULL,updated_at=? "
                f"WHERE delivery_id IN ({placeholders}) AND status IN ('rendering','sending','pending')",
                (now_ts(), *delivery_ids),
            )

    async def fail_claimed_delivery(self, delivery_id: str, error: str, *, max_attempts: int, retry_at: float) -> bool:
        """Record a render/pre-send failure and stop retries once its claim budget is exhausted."""
        now = now_ts()
        async with self.db.transaction() as conn:
            cursor = await conn.execute(
                "UPDATE deliveries SET status='failed',retryable=CASE WHEN attempts>=? THEN 0 ELSE 1 END,"
                "error=?,next_attempt_at=CASE WHEN attempts>=? THEN 0 ELSE ? END,updated_at=? "
                "WHERE delivery_id=? AND status IN ('rendering','sending','pending')",
                (max_attempts, error[:500], max_attempts, retry_at, now, delivery_id),
            )
            if cursor.rowcount:
                await conn.execute(
                    "INSERT INTO delivery_attempts(delivery_id,attempted_at,result,error,platform_message_id) "
                    "VALUES(?,?,?,?,NULL)",
                    (delivery_id, now, "failed", error[:500]),
                )
        return cursor.rowcount == 1

    async def delivery(self, delivery_id: str) -> dict[str, Any] | None:
        row = await self.db.fetchone("SELECT * FROM deliveries WHERE delivery_id=?", (delivery_id,))
        return dict(row) if row else None

    async def delivery_has_active_path(self, target_id: str, article_id: str) -> bool:
        row = await self.db.fetchone(
            """SELECT 1 FROM articles a JOIN feeds f USING(feed_id) WHERE a.article_id=? AND f.enabled=1 AND (
            EXISTS(SELECT 1 FROM direct_subscriptions s WHERE s.target_id=? AND s.feed_id=a.feed_id AND s.enabled=1 AND a.seq>s.baseline_seq) OR
            EXISTS(SELECT 1 FROM list_subscriptions s JOIN subscription_lists l USING(list_id) JOIN list_feeds lf USING(list_id)
                   WHERE s.target_id=? AND s.enabled=1 AND l.enabled=1 AND lf.feed_id=a.feed_id
                     AND a.seq>MAX(s.baseline_seq,lf.baseline_seq))) LIMIT 1""",
            (article_id, target_id, target_id),
        )
        return row is not None

    async def delivery_has_paused_path(self, target_id: str, article_id: str) -> bool:
        """True when a qualifying subscription still exists but its feed/list is paused."""
        row = await self.db.fetchone(
            """SELECT 1 FROM articles a WHERE a.article_id=? AND (
            EXISTS(SELECT 1 FROM direct_subscriptions s WHERE s.target_id=? AND s.feed_id=a.feed_id
                   AND a.seq>s.baseline_seq AND (s.enabled=0 OR EXISTS(SELECT 1 FROM feeds f WHERE f.feed_id=s.feed_id AND f.enabled=0))) OR
            EXISTS(SELECT 1 FROM list_subscriptions s JOIN list_feeds lf USING(list_id)
                   WHERE s.target_id=? AND lf.feed_id=a.feed_id AND a.seq>MAX(s.baseline_seq,lf.baseline_seq)
                     AND (s.enabled=0 OR EXISTS(SELECT 1 FROM subscription_lists l WHERE l.list_id=s.list_id AND l.enabled=0)
                          OR EXISTS(SELECT 1 FROM feeds f WHERE f.feed_id=lf.feed_id AND f.enabled=0)))
            ) LIMIT 1""",
            (article_id, target_id, target_id),
        )
        return row is not None

    async def postpone_delivery(self, delivery_id: str, delay: float) -> None:
        async with self.db.transaction() as conn:
            await conn.execute(
                "UPDATE deliveries SET next_attempt_at=?,updated_at=? WHERE delivery_id=? AND status IN ('pending','failed')",
                (now_ts() + max(1, delay), now_ts(), delivery_id),
            )

    async def delivery_source(self, target_id: str, article_id: str, preferred_path: str = "") -> dict[str, Any] | None:
        article = await self.db.fetchone("SELECT feed_id,title FROM articles WHERE article_id=?", (article_id,))
        if article is None:
            return None
        feed_id = article["feed_id"]
        if preferred_path.startswith("direct:"):
            row = await self.db.fetchone(
                "SELECT f.title source_name,0 is_list FROM direct_subscriptions s JOIN feeds f USING(feed_id) WHERE s.subscription_id=? AND s.target_id=? AND s.feed_id=? AND s.enabled=1 AND f.enabled=1 AND ? > s.baseline_seq",
                (
                    preferred_path[7:],
                    target_id,
                    feed_id,
                    (await self.db.fetchone("SELECT seq FROM articles WHERE article_id=?", (article_id,)))[0],
                ),
            )
            if row:
                return dict(row)
        if preferred_path.startswith("list:"):
            row = await self.db.fetchone(
                "SELECT l.name source_name,1 is_list FROM list_subscriptions s JOIN subscription_lists l USING(list_id) JOIN list_feeds lf USING(list_id) JOIN feeds f USING(feed_id) JOIN articles a USING(feed_id) WHERE s.subscription_id=? AND s.target_id=? AND s.enabled=1 AND l.enabled=1 AND lf.feed_id=? AND f.enabled=1 AND a.article_id=? AND a.seq>MAX(s.baseline_seq,lf.baseline_seq)",
                (preferred_path[5:], target_id, feed_id, article_id),
            )
            if row:
                return dict(row)
        row = await self.db.fetchone(
            "SELECT f.title source_name,0 is_list FROM direct_subscriptions s JOIN feeds f USING(feed_id) JOIN articles a USING(feed_id) WHERE s.target_id=? AND s.feed_id=? AND s.enabled=1 AND f.enabled=1 AND a.article_id=? AND a.seq>s.baseline_seq ORDER BY s.created_at LIMIT 1",
            (target_id, feed_id, article_id),
        )
        if row:
            return dict(row)
        row = await self.db.fetchone(
            "SELECT l.name source_name,1 is_list FROM list_subscriptions s JOIN subscription_lists l USING(list_id) JOIN list_feeds lf USING(list_id) JOIN feeds f USING(feed_id) JOIN articles a USING(feed_id) WHERE s.target_id=? AND s.enabled=1 AND l.enabled=1 AND lf.feed_id=? AND f.enabled=1 AND a.article_id=? AND a.seq>MAX(s.baseline_seq,lf.baseline_seq) ORDER BY s.created_at LIMIT 1",
            (target_id, feed_id, article_id),
        )
        return dict(row) if row else None

    async def deliveries(self, **filters: Any) -> list[dict[str, Any]]:
        clauses, values = [], []
        for key in ("status", "target_id", "article_id"):
            if filters.get(key) is not None:
                clauses.append(f"{key}=?")
                values.append(filters[key])
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        limit = max(1, min(int(filters.get("limit", 100)), 1000))
        return [
            dict(row)
            for row in await self.db.fetchall(
                f"SELECT * FROM deliveries{where} ORDER BY created_at DESC,rowid DESC LIMIT ?", (*values, limit)
            )
        ]

    async def retry_delivery(self, delivery_id: str) -> bool:
        async with self.db.transaction() as conn:
            cur = await conn.execute(
                "UPDATE deliveries SET status='pending',attempts=0,retryable=1,next_attempt_at=0,error=NULL,updated_at=? WHERE delivery_id=? AND status='failed'",
                (now_ts(), delivery_id),
            )
            return cur.rowcount == 1

    async def get_all_data(self) -> dict[str, list[dict[str, Any]]]:
        tables = (
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
            "test_operations",
        )
        data = {}
        for table in tables:
            data[table] = [dict(r) for r in await self.db.fetchall(f"SELECT * FROM {table}")]
        return data

    async def set_delivery_translation_diagnostics(self, delivery_id: str, diagnostics: dict[str, Any]) -> None:
        async with self.db.transaction() as conn:
            await conn.execute(
                "UPDATE deliveries SET translation_diagnostics=? WHERE delivery_id=?",
                (_json([diagnostics]), delivery_id),
            )

    async def mark_delivery(
        self,
        delivery_id: str,
        status: str,
        *,
        error: str | None = None,
        platform_message_id: str | None = None,
        retry_at: float = 0,
    ) -> None:
        now = now_ts()
        async with self.db.transaction() as conn:
            await conn.execute(
                "UPDATE deliveries SET status=?,error=?,platform_message_id=COALESCE(?,platform_message_id),next_attempt_at=?,updated_at=? WHERE delivery_id=?",
                (status, error, platform_message_id, retry_at, now, delivery_id),
            )
            if status not in {"sending", "rendering"}:
                await conn.execute(
                    "INSERT INTO delivery_attempts(delivery_id,attempted_at,result,error,platform_message_id) VALUES(?,?,?,?,?)",
                    (delivery_id, now, status, error, platform_message_id),
                )

    async def create_batch_cards(self, cards: list[dict[str, Any]]) -> None:
        now = now_ts()
        async with self.db.transaction() as conn:
            for card in cards:
                await conn.execute(
                    "INSERT OR IGNORE INTO delivery_batches(batch_id,group_id,target_id,status,card_index,card_count,payload,idempotency_key,created_at,updated_at) VALUES(?,?,?,'pending',?,?,?,?,?,?)",
                    (
                        card["batch_id"],
                        card["group_id"],
                        card["target_id"],
                        card["card_index"],
                        card["card_count"],
                        _json(card["payload"]),
                        card["idempotency_key"],
                        now,
                        now,
                    ),
                )
                for did in card["delivery_ids"]:
                    await conn.execute(
                        "INSERT OR IGNORE INTO batch_articles(batch_id,delivery_id,card_index) VALUES(?,?,?)",
                        (card["batch_id"], did, card["card_index"]),
                    )

    async def batch_cards(self, group_id: str) -> list[dict[str, Any]]:
        cards = await self.db.fetchall(
            "SELECT * FROM delivery_batches WHERE group_id=? ORDER BY card_index", (group_id,)
        )
        result = []
        for card in cards:
            item = dict(card)
            item["payload"] = json.loads(item["payload"])
            links = await self.db.fetchall(
                "SELECT delivery_id FROM batch_articles WHERE batch_id=? ORDER BY delivery_id", (item["batch_id"],)
            )
            item["delivery_ids"] = [r[0] for r in links]
            result.append(item)
        return result

    async def batch_group(self, delivery_ids: list[str]) -> str | None:
        if not delivery_ids:
            return None
        placeholders = ",".join("?" for _ in delivery_ids)
        row = await self.db.fetchone(
            f"SELECT b.group_id FROM batch_articles ba JOIN delivery_batches b USING(batch_id) WHERE ba.delivery_id IN ({placeholders}) LIMIT 1",
            delivery_ids,
        )
        return row[0] if row else None

    async def set_batch_card_status(
        self, batch_id: str, status: str, *, platform_message_id: str | None = None, error: str | None = None
    ) -> None:
        async with self.db.transaction() as conn:
            await conn.execute(
                "UPDATE delivery_batches SET status=?,platform_message_id=COALESCE(?,platform_message_id),error=?,updated_at=? WHERE batch_id=?",
                (status, platform_message_id, error, now_ts(), batch_id),
            )

    async def refresh_delivery_from_batches(self, delivery_id: str) -> None:
        rows = await self.db.fetchall(
            "SELECT b.status,b.platform_message_id FROM batch_articles ba "
            "JOIN delivery_batches b USING(batch_id) WHERE ba.delivery_id=?",
            (delivery_id,),
        )
        if not rows:
            return
        states = [row["status"] for row in rows]
        if all(state == "success" for state in states):
            status = "success"
        elif "unknown" in states:
            status = "unknown"
        elif "sending" in states:
            status = "sending"
        elif "failed" in states:
            status = "failed"
        else:
            status = "pending"
        platform_message_id = next((row["platform_message_id"] for row in rows if row["platform_message_id"]), None)
        async with self.db.transaction() as conn:
            current = await (
                await conn.execute("SELECT status FROM deliveries WHERE delivery_id=?", (delivery_id,))
            ).fetchone()
            await conn.execute(
                "UPDATE deliveries SET status=?,next_attempt_at=CASE WHEN ?='pending' THEN 0 ELSE next_attempt_at END,"
                "platform_message_id=COALESCE(?,platform_message_id),updated_at=? WHERE delivery_id=?",
                (status, status, platform_message_id, now_ts(), delivery_id),
            )
            if status == "success" and current is not None and current[0] != "success":
                await conn.execute(
                    "INSERT INTO delivery_attempts(delivery_id,attempted_at,result,error,platform_message_id) "
                    "VALUES(?,?,?,?,?)",
                    (delivery_id, now_ts(), "success", None, platform_message_id),
                )

    async def get_setting(self, key: str, default: str = "") -> str:
        row = await self.db.fetchone("SELECT value FROM settings WHERE key=?", (key,))
        return row[0] if row else default

    async def set_setting(self, key: str, value: str) -> None:
        async with self.db.transaction() as conn:
            await conn.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    async def make_delivery_permanent(self, delivery_id: str, error: str) -> None:
        async with self.db.transaction() as conn:
            current = await (
                await conn.execute("SELECT status,retryable FROM deliveries WHERE delivery_id=?", (delivery_id,))
            ).fetchone()
            await conn.execute(
                "UPDATE deliveries SET status='failed',retryable=0,error=?,next_attempt_at=0,updated_at=? WHERE delivery_id=?",
                (error[:500], now_ts(), delivery_id),
            )
            if current is not None and (current[0] != "failed" or current[1] != 0):
                await conn.execute(
                    "INSERT INTO delivery_attempts(delivery_id,attempted_at,result,error,platform_message_id) "
                    "VALUES(?,?,?,?,NULL)",
                    (delivery_id, now_ts(), "failed", error[:500]),
                )

    async def mark_delivery_permanent_flag(self, delivery_id: str) -> None:
        """Stop automatic retries while preserving the actual attempt count and error."""
        async with self.db.transaction() as conn:
            await conn.execute(
                "UPDATE deliveries SET retryable=0,updated_at=? WHERE delivery_id=?",
                (now_ts(), delivery_id),
            )

    async def resolve_unknown(self, delivery_id: str, confirmed_sent: bool) -> bool:
        delivery = await self.delivery(delivery_id)
        if delivery is None or delivery["status"] != "unknown":
            return False
        cards = await self.db.fetchall(
            "SELECT b.batch_id,b.status FROM batch_articles ba JOIN delivery_batches b USING(batch_id) WHERE ba.delivery_id=? AND b.status='unknown'",
            (delivery_id,),
        )
        async with self.db.transaction() as conn:
            status = "success" if confirmed_sent else "failed"
            for card in cards:
                await conn.execute(
                    "UPDATE delivery_batches SET status=?,updated_at=?,error=NULL WHERE batch_id=?",
                    (status, now_ts(), card["batch_id"]),
                )
                linked = await (
                    await conn.execute("SELECT delivery_id FROM batch_articles WHERE batch_id=?", (card["batch_id"],))
                ).fetchall()
                for link in linked:
                    await conn.execute(
                        "UPDATE deliveries SET attempts=CASE WHEN ?=0 THEN 0 ELSE attempts END,retryable=CASE WHEN ?=0 THEN 1 ELSE retryable END,next_attempt_at=0,error=NULL,updated_at=? WHERE delivery_id=? AND status='unknown'",
                        (int(confirmed_sent), int(confirmed_sent), now_ts(), link[0]),
                    )
            if not cards:
                await conn.execute(
                    "UPDATE deliveries SET status=?,attempts=CASE WHEN ?=0 THEN 0 ELSE attempts END,retryable=CASE WHEN ?=0 THEN 1 ELSE retryable END,next_attempt_at=0,error=NULL,updated_at=? WHERE delivery_id=?",
                    (status, int(confirmed_sent), int(confirmed_sent), now_ts(), delivery_id),
                )
        for card in cards:
            linked = await self.db.fetchall(
                "SELECT delivery_id FROM batch_articles WHERE batch_id=?", (card["batch_id"],)
            )
            for link in linked:
                await self.refresh_delivery_from_batches(link[0])
        return True

    async def log_fetch(
        self,
        feed_id: str,
        fetched_at: float,
        status_code: int,
        method: str,
        elapsed_ms: int,
        error_type: str | None = None,
        error_message: str | None = None,
        cookie_cache_hit: bool = False,
        browser_attempted: bool = False,
    ) -> None:
        message_limit = 768 if error_type == "http_429" else 300
        async with self.db.transaction() as conn:
            await conn.execute(
                "INSERT INTO fetch_logs(feed_id,fetched_at,status_code,method,elapsed_ms,error_type,error_message,cookie_cache_hit,browser_attempted) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    feed_id,
                    fetched_at,
                    status_code,
                    method,
                    elapsed_ms,
                    error_type,
                    (error_message or "")[:message_limit],
                    int(cookie_cache_hit),
                    int(browser_attempted),
                ),
            )

    async def get_platform_media_cache(
        self, platform_instance: str, media_url: str, media_kind: str
    ) -> dict[str, Any] | None:
        row = await self.db.fetchone(
            "SELECT * FROM platform_media_cache WHERE platform_instance=? AND media_url=? AND media_kind=? AND (expires_at IS NULL OR expires_at>?)",
            (platform_instance, media_url, media_kind, now_ts()),
        )
        return dict(row) if row else None

    async def set_platform_media_cache(
        self,
        platform_instance: str,
        media_url: str,
        media_kind: str,
        media_key: str,
        app_id: str,
        expires_at: float | None,
    ) -> None:
        async with self.db.transaction() as conn:
            await conn.execute(
                "INSERT INTO platform_media_cache VALUES(?,?,?,?,?,?) ON CONFLICT(platform_instance,media_url,media_kind) DO UPDATE SET media_key=excluded.media_key,app_id=excluded.app_id,expires_at=excluded.expires_at",
                (platform_instance, media_url, media_kind, media_key, app_id, expires_at),
            )

    async def cleanup_platform_media_cache(self) -> int:
        async with self.db.transaction() as conn:
            cur = await conn.execute(
                "DELETE FROM platform_media_cache WHERE expires_at IS NOT NULL AND expires_at<=?",
                (now_ts(),),
            )
            return cur.rowcount

    async def delete_platform_media_cache(self, platform_instance: str, media_url: str, media_kind: str) -> None:
        async with self.db.transaction() as conn:
            await conn.execute(
                "DELETE FROM platform_media_cache WHERE platform_instance=? AND media_url=? AND media_kind=?",
                (platform_instance, media_url, media_kind),
            )

    async def get_telegraph_page(self, article_id: str, content_hash: str) -> str | None:
        row = await self.db.fetchone(
            "SELECT url FROM telegraph_pages WHERE article_id=? AND content_hash=?", (article_id, content_hash)
        )
        return row[0] if row else None

    async def set_telegraph_page(self, article_id: str, content_hash: str, url: str, created_at: float) -> None:
        async with self.db.transaction() as conn:
            await conn.execute(
                "INSERT OR REPLACE INTO telegraph_pages VALUES(?,?,?,?)", (article_id, content_hash, url, created_at)
            )

    async def status(self) -> dict[str, int]:
        result = {}
        for table in (
            "feeds",
            "articles",
            "targets",
            "direct_subscriptions",
            "subscription_lists",
            "deliveries",
            "delivery_batches",
            "test_operations",
        ):
            row = await self.db.fetchone(f"SELECT COUNT(*) FROM {table}")
            result[table] = row[0]
        return result

    async def create_test_operation(
        self, operation_id: str, target_id: str, feed_id: str, article_ids: list[str]
    ) -> None:
        now = now_ts()
        async with self.db.transaction() as conn:
            await conn.execute(
                "INSERT INTO test_operations(operation_id,target_id,feed_id,article_ids,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (operation_id, target_id, feed_id, _json(article_ids), "pending", now, now),
            )

    async def get_test_operation(self, operation_id: str) -> dict[str, Any] | None:
        row = await self.db.fetchone("SELECT * FROM test_operations WHERE operation_id=?", (operation_id,))
        if row is None:
            return None
        result = dict(row)
        result["article_ids"] = json.loads(result["article_ids"])
        try:
            diagnostics = json.loads(result.get("translation_diagnostics") or "[]")
        except (TypeError, ValueError):
            diagnostics = []
        result["translation_diagnostics"] = diagnostics if isinstance(diagnostics, list) else []
        return result

    async def set_test_operation_translation_diagnostics(
        self, operation_id: str, diagnostics: list[dict[str, Any]]
    ) -> None:
        async with self.db.transaction() as conn:
            await conn.execute(
                "UPDATE test_operations SET translation_diagnostics=? WHERE operation_id=?",
                (_json(diagnostics), operation_id),
            )

    async def update_test_operation(self, operation_id: str, status: str, error: str | None = None) -> None:
        async with self.db.transaction() as conn:
            await conn.execute(
                "UPDATE test_operations SET status=?,error=?,updated_at=? WHERE operation_id=?",
                (status, (error or "")[:500] or None, now_ts(), operation_id),
            )

    async def get_test_target(self, operation_id: str) -> DeliveryTarget | None:
        row = await self.db.fetchone(
            "SELECT t.* FROM targets t JOIN test_operations o USING(target_id) WHERE o.operation_id=?", (operation_id,)
        )
        return (
            DeliveryTarget(
                *(
                    row[k]
                    for k in (
                        "target_id",
                        "platform_instance",
                        "unified_origin",
                        "platform",
                        "session_id",
                        "session_type",
                        "label",
                    )
                )
            )
            if row
            else None
        )
