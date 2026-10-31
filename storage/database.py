from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import aiosqlite

_LOGGER = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS feeds (
 feed_id TEXT PRIMARY KEY, public_id INTEGER UNIQUE, url TEXT NOT NULL UNIQUE, title TEXT NOT NULL DEFAULT '', description TEXT NOT NULL DEFAULT '',
 enabled INTEGER NOT NULL DEFAULT 1, translation_enabled INTEGER NOT NULL DEFAULT 0 CHECK(translation_enabled IN (0,1)),
 poll_interval INTEGER, etag TEXT, last_modified TEXT,
 initialized INTEGER NOT NULL DEFAULT 0, metadata TEXT NOT NULL DEFAULT '{}', next_fetch_at REAL NOT NULL DEFAULT 0,
 last_fetch_at REAL, status TEXT NOT NULL DEFAULT 'active', created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS articles (
 seq INTEGER PRIMARY KEY AUTOINCREMENT, article_id TEXT NOT NULL UNIQUE, feed_id TEXT NOT NULL REFERENCES feeds(feed_id) ON DELETE CASCADE,
 entry_id TEXT NOT NULL, canonical_url TEXT NOT NULL DEFAULT '', title TEXT NOT NULL DEFAULT '', author TEXT NOT NULL DEFAULT '',
 published_at REAL, updated_at REAL, first_seen_at REAL NOT NULL, categories TEXT NOT NULL DEFAULT '[]',
 content_hash TEXT NOT NULL DEFAULT '', content_blocks TEXT NOT NULL DEFAULT '[]', metadata TEXT NOT NULL DEFAULT '{}',
 UNIQUE(feed_id, entry_id)
);
CREATE INDEX IF NOT EXISTS idx_articles_feed_seq ON articles(feed_id,seq);
CREATE TABLE IF NOT EXISTS targets (
 target_id TEXT PRIMARY KEY, platform_instance TEXT NOT NULL, unified_origin TEXT NOT NULL, platform TEXT NOT NULL,
 session_id TEXT NOT NULL, session_type TEXT NOT NULL DEFAULT '', label TEXT NOT NULL DEFAULT '', routing_key TEXT NOT NULL,
 UNIQUE(platform_instance, routing_key)
);
CREATE TABLE IF NOT EXISTS subscription_lists (
 list_id TEXT PRIMARY KEY, public_id INTEGER UNIQUE, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1,
 poll_interval INTEGER, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS list_feeds (
 list_id TEXT NOT NULL REFERENCES subscription_lists(list_id) ON DELETE CASCADE,
 feed_id TEXT NOT NULL REFERENCES feeds(feed_id) ON DELETE CASCADE, baseline_seq INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(list_id,feed_id)
);
CREATE TABLE IF NOT EXISTS direct_subscriptions (
 subscription_id TEXT PRIMARY KEY, target_id TEXT NOT NULL REFERENCES targets(target_id) ON DELETE CASCADE,
 feed_id TEXT NOT NULL REFERENCES feeds(feed_id) ON DELETE CASCADE, enabled INTEGER NOT NULL DEFAULT 1,
 poll_interval INTEGER, baseline_seq INTEGER NOT NULL DEFAULT 0, last_seq INTEGER NOT NULL DEFAULT 0, next_check_at REAL NOT NULL DEFAULT 0,
 created_at REAL NOT NULL, UNIQUE(target_id,feed_id)
);
CREATE TABLE IF NOT EXISTS list_subscriptions (
 subscription_id TEXT PRIMARY KEY, target_id TEXT NOT NULL REFERENCES targets(target_id) ON DELETE CASCADE,
 list_id TEXT NOT NULL REFERENCES subscription_lists(list_id) ON DELETE CASCADE, enabled INTEGER NOT NULL DEFAULT 1,
 poll_interval INTEGER, baseline_seq INTEGER NOT NULL DEFAULT 0, last_seq INTEGER NOT NULL DEFAULT 0, last_check_at REAL, next_check_at REAL NOT NULL DEFAULT 0, created_at REAL NOT NULL,
 UNIQUE(target_id,list_id)
);
CREATE TABLE IF NOT EXISTS deliveries (
 delivery_id TEXT PRIMARY KEY, target_id TEXT NOT NULL REFERENCES targets(target_id), article_id TEXT NOT NULL REFERENCES articles(article_id) ON DELETE CASCADE,
 status TEXT NOT NULL DEFAULT 'pending', source_path TEXT NOT NULL DEFAULT '', attempts INTEGER NOT NULL DEFAULT 0, retryable INTEGER NOT NULL DEFAULT 1,
 next_attempt_at REAL NOT NULL DEFAULT 0, idempotency_key TEXT NOT NULL, platform_message_id TEXT,
 error TEXT, translation_diagnostics TEXT NOT NULL DEFAULT '[]', created_at REAL NOT NULL, updated_at REAL NOT NULL,
 UNIQUE(target_id,article_id), UNIQUE(idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_deliveries_pending ON deliveries(status,next_attempt_at);
CREATE TABLE IF NOT EXISTS delivery_attempts (
 attempt_id INTEGER PRIMARY KEY AUTOINCREMENT, delivery_id TEXT NOT NULL REFERENCES deliveries(delivery_id) ON DELETE CASCADE,
 attempted_at REAL NOT NULL, result TEXT NOT NULL, error TEXT, platform_message_id TEXT
);
CREATE TABLE IF NOT EXISTS delivery_batches (
 batch_id TEXT PRIMARY KEY, group_id TEXT NOT NULL, target_id TEXT NOT NULL REFERENCES targets(target_id),
 status TEXT NOT NULL, card_index INTEGER NOT NULL, card_count INTEGER NOT NULL,
 payload TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE, platform_message_id TEXT, error TEXT,
 created_at REAL NOT NULL, updated_at REAL NOT NULL, UNIQUE(group_id,card_index)
);
CREATE TABLE IF NOT EXISTS test_operations (
 operation_id TEXT PRIMARY KEY, target_id TEXT NOT NULL REFERENCES targets(target_id), feed_id TEXT NOT NULL REFERENCES feeds(feed_id),
 article_ids TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', error TEXT,
 translation_diagnostics TEXT NOT NULL DEFAULT '[]', created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS batch_articles (
 batch_id TEXT NOT NULL REFERENCES delivery_batches(batch_id) ON DELETE CASCADE,
 delivery_id TEXT NOT NULL REFERENCES deliveries(delivery_id), card_index INTEGER NOT NULL,
 PRIMARY KEY(batch_id,delivery_id)
);
CREATE TABLE IF NOT EXISTS fetch_logs (
 id INTEGER PRIMARY KEY AUTOINCREMENT, feed_id TEXT NOT NULL, fetched_at REAL NOT NULL, status_code INTEGER,
 method TEXT, elapsed_ms INTEGER, error_type TEXT, error_message TEXT,
 cookie_cache_hit INTEGER NOT NULL DEFAULT 0, browser_attempted INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS platform_media_cache (
 platform_instance TEXT NOT NULL, media_url TEXT NOT NULL, media_kind TEXT NOT NULL, media_key TEXT NOT NULL,
 app_id TEXT NOT NULL DEFAULT '', expires_at REAL, PRIMARY KEY(platform_instance,media_url,media_kind)
);
CREATE TABLE IF NOT EXISTS telegraph_pages (
 article_id TEXT NOT NULL, content_hash TEXT NOT NULL, url TEXT NOT NULL, created_at REAL NOT NULL,
 PRIMARY KEY(article_id,content_hash)
);
CREATE TABLE IF NOT EXISTS entity_counters (
 entity TEXT PRIMARY KEY, next_id INTEGER NOT NULL CHECK(next_id>0)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_subscription_lists_name_unique ON subscription_lists(name);
PRAGMA user_version=3;
"""


class Database:
    def __init__(self, path: str | Path, busy_timeout_ms: int = 5000):
        self.path = Path(path)
        self.busy_timeout_ms = busy_timeout_ms
        self.connection: aiosqlite.Connection | None = None
        self.lock = asyncio.Lock()

    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = await aiosqlite.connect(self.path)
        try:
            self.connection.row_factory = aiosqlite.Row
            await self.connection.execute(f"PRAGMA busy_timeout={int(self.busy_timeout_ms)}")
            await self.connection.execute("PRAGMA foreign_keys=ON")
            await self.connection.execute("PRAGMA journal_mode=WAL")
            version = (await (await self.connection.execute("PRAGMA user_version")).fetchone())[0]
            if version > 3:
                raise RuntimeError(f"database schema version {version} is newer than supported version 3")
            if version < 1:
                async with self.lock:
                    await self.connection.execute("BEGIN IMMEDIATE")
                    for statement in _SCHEMA.split(";"):
                        if statement.strip():
                            await self.connection.execute(statement)
                    await self.connection.commit()
                version = 3
            elif version < 2:
                async with self.lock:
                    await self._migrate_v1_to_v2()
                version = 2
            if version < 3:
                async with self.lock:
                    await self._migrate_v2_to_v3()
            # Additive migration for databases created by early P0 builds.
            columns = {
                row[1] for row in await (await self.connection.execute("PRAGMA table_info(deliveries)")).fetchall()
            }
            if "retryable" not in columns:
                await self.connection.execute("ALTER TABLE deliveries ADD COLUMN retryable INTEGER NOT NULL DEFAULT 1")
                await self.connection.commit()
            if "translation_diagnostics" not in columns:
                await self.connection.execute(
                    "ALTER TABLE deliveries ADD COLUMN translation_diagnostics TEXT NOT NULL DEFAULT '[]'"
                )
            test_columns = {
                row[1] for row in await (await self.connection.execute("PRAGMA table_info(test_operations)")).fetchall()
            }
            if "translation_diagnostics" not in test_columns:
                await self.connection.execute(
                    "ALTER TABLE test_operations ADD COLUMN translation_diagnostics TEXT NOT NULL DEFAULT '[]'"
                )
            fetch_columns = {
                row[1] for row in await (await self.connection.execute("PRAGMA table_info(fetch_logs)")).fetchall()
            }
            if "cookie_cache_hit" not in fetch_columns:
                await self.connection.execute(
                    "ALTER TABLE fetch_logs ADD COLUMN cookie_cache_hit INTEGER NOT NULL DEFAULT 0"
                )
            if "browser_attempted" not in fetch_columns:
                await self.connection.execute(
                    "ALTER TABLE fetch_logs ADD COLUMN browser_attempted INTEGER NOT NULL DEFAULT 0"
                )
            for entity, table in (("feed", "feeds"), ("list", "subscription_lists")):
                maximum = await (
                    await self.connection.execute(f"SELECT COALESCE(MAX(public_id),0) FROM {table}")
                ).fetchone()
                await self.connection.execute(
                    "INSERT INTO entity_counters(entity,next_id) VALUES(?,?) "
                    "ON CONFLICT(entity) DO UPDATE SET next_id=MAX(entity_counters.next_id,excluded.next_id)",
                    (entity, int(maximum[0]) + 1),
                )
            await self.connection.commit()
        except Exception:
            await self.close()
            raise

    async def _migrate_v1_to_v2(self) -> None:
        connection = self.connection
        try:
            await connection.execute("BEGIN IMMEDIATE")
            for table in ("feeds", "subscription_lists"):
                columns = {row[1] for row in await (await connection.execute(f"PRAGMA table_info({table})")).fetchall()}
                if "public_id" not in columns:
                    await connection.execute(f"ALTER TABLE {table} ADD COLUMN public_id INTEGER")
            await connection.execute(
                "CREATE TABLE IF NOT EXISTS entity_counters(entity TEXT PRIMARY KEY,next_id INTEGER NOT NULL CHECK(next_id>0))"
            )
            for table, entity, key in (
                ("feeds", "feed", "feed_id"),
                ("subscription_lists", "list", "list_id"),
            ):
                existing = await (
                    await connection.execute(f"SELECT COALESCE(MAX(public_id),0) FROM {table}")
                ).fetchone()
                next_id = int(existing[0]) + 1
                rows = await (
                    await connection.execute(
                        f"SELECT rowid,{key} FROM {table} WHERE public_id IS NULL ORDER BY created_at,rowid"
                    )
                ).fetchall()
                for row in rows:
                    await connection.execute(f"UPDATE {table} SET public_id=? WHERE rowid=?", (next_id, row[0]))
                    next_id += 1
                await connection.execute(
                    "INSERT INTO entity_counters(entity,next_id) VALUES(?,?) "
                    "ON CONFLICT(entity) DO UPDATE SET next_id=MAX(entity_counters.next_id,excluded.next_id)",
                    (entity, next_id),
                )
            renamed = await self._normalize_legacy_list_names(connection)
            if renamed:
                _LOGGER.warning("数据库迁移：为 %s 个重名或空白列表补充唯一名称，原订阅关系保持不变", renamed)
            await connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_feeds_public_id ON feeds(public_id)")
            await connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_subscription_lists_public_id ON subscription_lists(public_id)"
            )
            await connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_subscription_lists_name_unique ON subscription_lists(name)"
            )
            await connection.execute("PRAGMA user_version=2")
            await connection.commit()
        except Exception:
            await connection.rollback()
            raise

    async def _migrate_v2_to_v3(self) -> None:
        connection = self.connection
        try:
            await connection.execute("BEGIN IMMEDIATE")
            columns = {row[1] for row in await (await connection.execute("PRAGMA table_info(feeds)")).fetchall()}
            if "translation_enabled" not in columns:
                await connection.execute(
                    "ALTER TABLE feeds ADD COLUMN translation_enabled INTEGER NOT NULL DEFAULT 0 "
                    "CHECK(translation_enabled IN (0,1))"
                )
            await connection.execute("PRAGMA user_version=3")
            await connection.commit()
        except Exception:
            await connection.rollback()
            raise

    @staticmethod
    async def _normalize_legacy_list_names(connection) -> int:
        rows = await (
            await connection.execute("SELECT rowid,list_id,name FROM subscription_lists ORDER BY created_at,rowid")
        ).fetchall()
        prepared = [
            (row[0], row[1], (str(row[2] or "").strip() or "未命名列表")[:100], str(row[2] or "")) for row in rows
        ]
        reserved = {name for _, _, name, _ in prepared}
        seen: set[str] = set()
        renamed = 0
        for rowid, list_id, name, original in prepared:
            final_name = name
            if final_name in seen:
                suffix_number = 2
                while True:
                    suffix = f"（重复列表 {suffix_number}）"
                    candidate = name[: 100 - len(suffix)] + suffix
                    if candidate not in reserved and candidate not in seen:
                        final_name = candidate
                        break
                    suffix_number += 1
            if final_name != original:
                await connection.execute("UPDATE subscription_lists SET name=? WHERE rowid=?", (final_name, rowid))
                renamed += 1
            seen.add(final_name)
        return renamed

    async def close(self) -> None:
        if self.connection is not None:
            await self.connection.close()
            self.connection = None

    async def fetchone(self, sql: str, parameters: tuple = ()) -> aiosqlite.Row | None:
        if self.connection is None:
            raise RuntimeError("database is not initialized")
        async with self.lock:
            cursor = await self.connection.execute(sql, parameters)
            return await cursor.fetchone()

    async def fetchall(self, sql: str, parameters: tuple = ()) -> list[aiosqlite.Row]:
        if self.connection is None:
            raise RuntimeError("database is not initialized")
        async with self.lock:
            cursor = await self.connection.execute(sql, parameters)
            return await cursor.fetchall()

    def transaction(self):
        return _Transaction(self)

    @staticmethod
    async def next_public_id(connection, entity: str) -> int:
        if entity not in {"feed", "list"}:
            raise ValueError("unknown public ID entity")
        row = await (
            await connection.execute("SELECT next_id FROM entity_counters WHERE entity=?", (entity,))
        ).fetchone()
        table = "feeds" if entity == "feed" else "subscription_lists"
        maximum = await (await connection.execute(f"SELECT COALESCE(MAX(public_id),0) FROM {table}")).fetchone()
        minimum = int(maximum[0]) + 1
        if row is None:
            public_id = minimum
            await connection.execute("INSERT INTO entity_counters(entity,next_id) VALUES(?,?)", (entity, public_id + 1))
            return public_id
        public_id = max(int(row[0]), minimum)
        await connection.execute("UPDATE entity_counters SET next_id=? WHERE entity=?", (public_id + 1, entity))
        return public_id


class _Transaction:
    def __init__(self, database: Database):
        self.database = database

    async def __aenter__(self):
        if self.database.connection is None:
            raise RuntimeError("database is not initialized")
        await self.database.lock.acquire()
        connection = self.database.connection
        try:
            await self._await_operation(connection.execute("BEGIN IMMEDIATE"))
        except BaseException as begin_error:
            try:
                await self._rollback(connection)
            except BaseException as rollback_error:
                begin_error.add_note(
                    f"SQLite rollback after failed transaction entry also failed ({type(rollback_error).__name__})"
                )
            self.database.lock.release()
            raise
        return connection

    async def __aexit__(self, exc_type, exc, tb):
        try:
            connection = self.database.connection
            if connection is None:
                raise RuntimeError("database connection closed during transaction")
            if exc_type:
                try:
                    await self._rollback(connection)
                except BaseException as rollback_error:
                    if exc is not None:
                        exc.add_note(
                            f"SQLite rollback after transaction error also failed ({type(rollback_error).__name__})"
                        )
                    else:
                        raise
            else:
                try:
                    # Drain a queued COMMIT before unlocking; SQLite may finish it during cancellation.
                    await self._await_operation(connection.commit())
                except BaseException as commit_error:
                    try:
                        await self._rollback(connection)
                    except BaseException as rollback_error:
                        commit_error.add_note(
                            f"SQLite rollback after failed commit also failed ({type(rollback_error).__name__})"
                        )
                    raise
        finally:
            self.database.lock.release()

    async def _rollback(self, connection) -> None:
        try:
            await self._await_operation(connection.rollback())
        except BaseException as rollback_error:
            try:
                await self._close_connection(connection)
            except BaseException as close_error:
                rollback_error.add_note(
                    f"SQLite close after failed rollback also failed ({type(close_error).__name__})"
                )
            raise

    async def _close_connection(self, connection) -> None:
        try:
            await self._await_operation(connection.close())
        finally:
            if self.database.connection is connection:
                self.database.connection = None

    @staticmethod
    async def _await_operation(operation):
        """Finish a queued SQLite operation before allowing another transaction."""
        task = asyncio.create_task(operation)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError as cancellation:
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except BaseException:
                    break
            if task.done():
                try:
                    task.result()
                except BaseException as operation_error:
                    if operation_error is not cancellation:
                        cancellation.add_note(
                            f"SQLite operation completed with {type(operation_error).__name__} during cancellation"
                        )
            raise
