import asyncio
import hashlib
import sqlite3

import pytest
from astrbot_plugin_rss.core.models import Article
from astrbot_plugin_rss.core.runtime import RSSRuntime
from astrbot_plugin_rss.services.management import ManagementService
from astrbot_plugin_rss.storage.database import _SCHEMA, Database
from astrbot_plugin_rss.storage.repository import Repository


def _create_v1_database(path):
    old_schema = _SCHEMA.replace(
        "translation_enabled INTEGER NOT NULL DEFAULT 0 CHECK(translation_enabled IN (0,1)),\n poll_interval",
        "poll_interval",
    )
    old_schema = old_schema.replace(
        "feed_id TEXT PRIMARY KEY, public_id INTEGER UNIQUE, ", "feed_id TEXT PRIMARY KEY, "
    )
    old_schema = old_schema.replace(
        "list_id TEXT PRIMARY KEY, public_id INTEGER UNIQUE, ", "list_id TEXT PRIMARY KEY, "
    )
    old_schema = old_schema.replace(
        "CREATE TABLE IF NOT EXISTS entity_counters (\n entity TEXT PRIMARY KEY, next_id INTEGER NOT NULL CHECK(next_id>0)\n);\n",
        "",
    )
    old_schema = old_schema.replace(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_subscription_lists_name_unique ON subscription_lists(name);\n", ""
    ).replace("PRAGMA user_version=3;", "PRAGMA user_version=1;")
    with sqlite3.connect(path) as connection:
        for statement in old_schema.split(";"):
            if statement.strip():
                connection.execute(statement)
        connection.commit()


def _create_v2_database(path):
    old_schema = _SCHEMA.replace(
        "translation_enabled INTEGER NOT NULL DEFAULT 0 CHECK(translation_enabled IN (0,1)),\n poll_interval",
        "poll_interval",
    ).replace("PRAGMA user_version=3;", "PRAGMA user_version=2;")
    with sqlite3.connect(path) as connection:
        for statement in old_schema.split(";"):
            if statement.strip():
                connection.execute(statement)
        connection.commit()


def test_public_ids_are_stable_and_reference_resolution_prefers_id(tmp_path):
    async def run():
        runtime = RSSRuntime({}, tmp_path / "runtime")
        await runtime.initialize()
        try:
            first = await runtime.repository.create_feed("https://example.org/one", title="Duplicate")
            second = await runtime.repository.create_feed("https://example.org/two", title="Duplicate")
            assert (first.public_id, second.public_id) == (1, 2)
            with pytest.raises(ValueError, match="不唯一"):
                await runtime.services.feeds.resolve("Duplicate")
            assert (await runtime.services.feeds.resolve(str(first.public_id))).feed_id == first.feed_id
            with pytest.raises(KeyError):
                await runtime.services.feeds.resolve(first.feed_id)

            first_list = await runtime.repository.create_list("数字名冲突")
            first_list_row = await runtime.repository.get_list(first_list)
            second_list = await runtime.repository.create_list("1")
            second_list_row = await runtime.repository.get_list(second_list)
            assert (first_list_row["public_id"], second_list_row["public_id"]) == (1, 2)
            resolved = await runtime.services.lists.resolve("1")
            assert resolved["list_id"] == first_list
            await runtime.repository.delete_list(first_list)
            third_list = await runtime.repository.create_list("after delete")
            assert (await runtime.repository.get_list(third_list))["public_id"] == 3

            async with runtime.database.transaction() as connection:
                await connection.execute("DELETE FROM feeds WHERE feed_id=?", (first.feed_id,))
            third_feed = await runtime.repository.create_feed("https://example.org/three")
            assert third_feed.public_id == 3
        finally:
            await runtime.close()

    asyncio.run(run())


def test_v1_migration_backfills_ids_and_renames_duplicate_lists_without_losing_links(tmp_path):
    async def run():
        path = tmp_path / "legacy.sqlite3"
        _create_v1_database(path)
        with sqlite3.connect(path) as connection:
            connection.execute("INSERT INTO feeds(feed_id,url,created_at) VALUES('feed-a','https://example.org/a',1)")
            connection.execute("INSERT INTO subscription_lists(list_id,name,created_at) VALUES('list-a',' same ',1)")
            connection.execute("INSERT INTO subscription_lists(list_id,name,created_at) VALUES('list-b','same',2)")
            connection.execute("INSERT INTO subscription_lists(list_id,name,created_at) VALUES('list-c','   ',3)")
            connection.executemany(
                "INSERT INTO list_feeds(list_id,feed_id) VALUES(?,?)",
                (("list-a", "feed-a"), ("list-b", "feed-a"), ("list-c", "feed-a")),
            )
            connection.commit()

        database = Database(path)
        try:
            await database.initialize()
            assert (await database.fetchone("PRAGMA user_version"))[0] == 3
            feeds = await database.fetchall("SELECT feed_id,public_id FROM feeds")
            lists = await database.fetchall("SELECT list_id,public_id,name FROM subscription_lists ORDER BY public_id")
            assert [(row["feed_id"], row["public_id"]) for row in feeds] == [("feed-a", 1)]
            assert (await database.fetchone("SELECT translation_enabled FROM feeds WHERE feed_id='feed-a'"))[0] == 0
            assert [row["public_id"] for row in lists] == [1, 2, 3]
            assert [row["name"] for row in lists] == ["same", "same（重复列表 2）", "未命名列表"]
            assert (await database.fetchone("SELECT COUNT(*) FROM list_feeds"))[0] == 3
            assert {
                row["entity"]: row["next_id"] for row in await database.fetchall("SELECT * FROM entity_counters")
            } == {"feed": 2, "list": 4}
        finally:
            await database.close()

    asyncio.run(run())


def test_v2_migration_adds_translation_flag_disabled_for_existing_feeds(tmp_path):
    async def run():
        path = tmp_path / "legacy-v2.sqlite3"
        _create_v2_database(path)
        with sqlite3.connect(path) as connection:
            connection.execute("INSERT INTO feeds(feed_id,url,created_at) VALUES('feed-v2','https://example.org/v2',1)")
            connection.commit()
        database = Database(path)
        try:
            await database.initialize()
            assert (await database.fetchone("PRAGMA user_version"))[0] == 3
            assert (await database.fetchone("SELECT translation_enabled FROM feeds WHERE feed_id='feed-v2'"))[0] == 0
        finally:
            await database.close()

    asyncio.run(run())


def test_v1_backup_restores_through_private_migrated_copy(tmp_path):
    async def run():
        runtime = RSSRuntime({}, tmp_path / "runtime")
        await runtime.initialize()
        service = ManagementService(runtime)
        source = tmp_path / "legacy-v1.sqlite3"
        _create_v1_database(source)
        before = hashlib.sha256(source.read_bytes()).digest()
        try:
            result = await service._restore_sqlite(source)
            assert result["restored"] is True
            assert hashlib.sha256(source.read_bytes()).digest() == before
            assert (await service.runtime.database.fetchone("PRAGMA user_version"))[0] == 3
            assert {
                row["entity"]: row["next_id"]
                for row in await service.runtime.database.fetchall("SELECT * FROM entity_counters")
            } == {"feed": 1, "list": 1}
        finally:
            await service.runtime.close()

    asyncio.run(run())


def test_v2_backup_restores_through_private_migrated_copy_without_mutating_source(tmp_path):
    async def run():
        runtime = RSSRuntime({}, tmp_path / "runtime-v2")
        await runtime.initialize()
        service = ManagementService(runtime)
        source = tmp_path / "legacy-v2.sqlite3"
        _create_v2_database(source)
        with sqlite3.connect(source) as connection:
            connection.execute(
                "INSERT INTO feeds(feed_id,public_id,url,created_at) VALUES('feed-v2',1,'https://example.org/v2',1)"
            )
            connection.commit()
        before = hashlib.sha256(source.read_bytes()).digest()
        try:
            result = await service._restore_sqlite(source)
            assert result["restored"] is True
            assert hashlib.sha256(source.read_bytes()).digest() == before
            with sqlite3.connect(source) as connection:
                assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
                assert connection.execute("SELECT url FROM feeds").fetchone()[0] == "https://example.org/v2"
            assert (await service.runtime.database.fetchone("PRAGMA user_version"))[0] == 3
            restored = await service.runtime.repository.get_feed_by_url("https://example.org/v2")
            assert restored.translation_enabled is False
        finally:
            await service.runtime.close()

    asyncio.run(run())


def test_members_api_uses_public_numbers_and_rejects_bool_or_float(tmp_path):
    async def run():
        runtime = RSSRuntime({}, tmp_path / "members")
        await runtime.initialize()
        try:
            service = ManagementService(runtime)
            feed = await runtime.repository.create_feed("https://example.org/member")
            list_uuid = await runtime.repository.create_list("Membership")
            list_row = await runtime.repository.get_list(list_uuid)
            added = await service.post(f"lists/{list_row['public_id']}/members/add", {"feed_id": feed.public_id})
            assert added["feed_ids"] == [feed.public_id]
            assert (await runtime.database.fetchone("SELECT COUNT(*) FROM list_feeds"))[0] == 1
            removed = await service.post(f"lists/{list_row['public_id']}/members/remove", {"feed_id": feed.public_id})
            assert removed["removed"] is True
            for invalid in (True, 1.0, -1, 9_223_372_036_854_775_808):
                with pytest.raises(ValueError, match="正整数"):
                    await service.post(f"lists/{list_row['public_id']}/members/add", {"feed_id": invalid})
        finally:
            await runtime.close()

    asyncio.run(run())


def test_v2_backup_layouts_validate_across_fresh_and_migrated_column_order(tmp_path):
    async def run():
        migrated_path = tmp_path / "migrated.sqlite3"
        _create_v1_database(migrated_path)
        migrated = Database(migrated_path)
        await migrated.initialize()
        fresh_runtime = RSSRuntime({}, tmp_path / "fresh")
        await fresh_runtime.initialize()
        backup = tmp_path / "fresh.sqlite3"
        try:
            await fresh_runtime.backup(backup)
            assert ManagementService._validate_backup_sqlite(backup, migrated_path) is False
        finally:
            await migrated.close()
            await fresh_runtime.close()

    asyncio.run(run())


def test_list_names_are_trimmed_unique_and_bounded(tmp_path):
    async def run():
        database = Database(tmp_path / "names.sqlite3")
        await database.initialize()
        try:
            repository = Repository(database)
            first = await repository.create_list("  shared  ")
            assert (await repository.get_list(first))["name"] == "shared"
            with pytest.raises(ValueError, match="列表名称已存在"):
                await repository.create_list("shared")
            second = await repository.create_list("other")
            with pytest.raises(ValueError, match="列表名称已存在"):
                await repository.update_list(second, name=" shared ")
            with pytest.raises(ValueError):
                await repository.create_list("x" * 101)
        finally:
            await database.close()

    asyncio.run(run())


def test_management_details_aggregate_subscriber_paths_and_list_articles(tmp_path):
    async def run():
        runtime = RSSRuntime({}, tmp_path / "management")
        await runtime.initialize()
        try:
            feed = await runtime.repository.create_feed("https://example.org/detail", title="Source")
            target = await runtime.repository.create_target(
                "platform-instance", "platform:group:1", "platform", "group:1", "GroupMessage", "News"
            )
            await runtime.repository.add_direct_subscription(target.target_id, feed.feed_id)
            list_uuid = await runtime.repository.create_list("Collection")
            await runtime.repository.add_list_feed(list_uuid, feed.feed_id)
            await runtime.repository.subscribe_list(target.target_id, list_uuid)
            await runtime.repository.save_articles(
                feed.feed_id,
                [Article("article-1", feed.feed_id, "entry-1", "https://example.org/article", "Article")],
            )
            service = ManagementService(runtime)
            feed_detail = await service.get(f"feeds/{feed.public_id}", {})
            assert len(feed_detail["subscribers"]) == 1
            subscriber = feed_detail["subscribers"][0]
            assert subscriber["platform_instance"] == "platform-instance"
            assert subscriber["unified_origin"] == "platform:group:1"
            assert subscriber["enabled"] is True
            assert {path["kind"] for path in subscriber["paths"]} == {"direct", "list"}
            assert feed_detail["list_refs"][0]["list_public_id"] == 1

            list_detail = await service.get(f"lists/{1}", {})
            assert list_detail["members"][0]["feed_public_id"] == feed.public_id
            assert list_detail["articles"][0]["feed_public_id"] == feed.public_id
            assert list_detail["articles"][0]["content_blocks"] == []
            assert list_detail["subscribers"][0]["platform"] == "platform"
        finally:
            await runtime.close()

    asyncio.run(run())
