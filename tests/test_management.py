import asyncio
import json
import shutil
import sqlite3

import pytest
from astrbot_plugin_rss.core.runtime import RSSRuntime
from astrbot_plugin_rss.services.management import ManagementService


@pytest.mark.parametrize(
    ("raw", "seconds"), [("1h", 3600), ("3600", 3600), ("0h", None), (" 2 H ", 7200), (3600, 3600)]
)
def test_interval_input_survives_reload_restart_and_backup(tmp_path, raw, seconds):
    async def run():
        runtime = RSSRuntime({}, tmp_path / "data")
        await runtime.initialize()
        service = ManagementService(runtime)
        try:
            feed = await runtime.repository.create_feed("https://example.org/interval-display")
            changed = await service.post(f"feeds/{feed.public_id}/update", {"poll_interval": raw})
            assert changed["poll_interval"] == seconds
            assert changed["poll_interval_input"] == str(raw)
            created = await service.post("lists/create", {"name": "Display", "poll_interval": raw})
            list_id = created["list_id"]
            assert created["poll_interval_input"] == str(raw)

            async def check_display():
                for endpoint, entity in ((f"feeds/{feed.public_id}", "feed"), (f"lists/{list_id}", "list")):
                    detail = (await service.get(endpoint, {}))[entity]
                    assert detail["poll_interval"] == seconds
                    assert detail["poll_interval_input"] == str(raw)
                for endpoint in ("feeds", "lists"):
                    row = (await service.get(endpoint, {}))["items"][0]
                    assert row["poll_interval"] == seconds
                    assert row["poll_interval_input"] == str(raw)

            await service.post(f"feeds/{feed.public_id}/update", {"translation_enabled": True})
            await service.post(f"lists/{list_id}/update", {"description": "Preserve input"})
            await check_display()
            await runtime.close()
            runtime = RSSRuntime({}, tmp_path / "data")
            await runtime.initialize()
            service = ManagementService(runtime)
            await check_display()

            backup = tmp_path / "with-inputs.sqlite3"
            await runtime.backup(backup)
            await service.post(f"feeds/{feed.public_id}/update", {"poll_interval": "2s"})
            await service.post(f"lists/{list_id}/update", {"poll_interval": "2s"})
            await service._restore_sqlite(backup)
            await check_display()
        finally:
            await service.runtime.close()

    asyncio.run(run())


def test_interval_display_falls_back_and_invalid_metadata_write_rolls_back(tmp_path):
    async def run():
        runtime = RSSRuntime({}, tmp_path)
        await runtime.initialize()
        try:
            feed = await runtime.repository.create_feed(
                "https://example.org/stale-interval", poll_interval=3600, poll_interval_input="1h"
            )
            service = ManagementService(runtime)
            async with runtime.database.transaction() as conn:
                await conn.execute("UPDATE feeds SET poll_interval=60 WHERE feed_id=?", (feed.feed_id,))
            detail = await service.get(f"feeds/{feed.public_id}", {})
            assert detail["feed"]["poll_interval_input"] == "60"
            with pytest.raises(ValueError, match="must match"):
                await runtime.repository.update_feed(feed.feed_id, poll_interval=120, poll_interval_input="1h")
            assert (await runtime.repository.get_feed(feed.feed_id)).poll_interval == 60
            await runtime.repository.set_setting(f"poll_interval_input:feed:{feed.feed_id}", "invalid JSON")
            assert (await service.get(f"feeds/{feed.public_id}", {}))["feed"]["poll_interval_input"] == "60"
            list_uuid = await runtime.repository.create_list("Legacy", interval=120)
            async with runtime.database.transaction() as conn:
                await conn.execute("DELETE FROM settings WHERE key=?", (f"poll_interval_input:list:{list_uuid}",))
            row = await runtime.repository.get_list(list_uuid)
            assert (await service.get(f"lists/{row['public_id']}", {}))["list"]["poll_interval_input"] == "120"
        finally:
            await runtime.close()

    asyncio.run(run())


def test_list_preview_loads_articles_with_one_repository_query(tmp_path, monkeypatch):
    from astrbot_plugin_rss.core.models import Article

    class PreviewRegistry:
        def __init__(self):
            self.titles = []

        async def render(self, articles, target, source_name, is_list=False):
            assert is_list is True
            self.titles = [article.title for article in articles]
            return []

    async def run():
        runtime = RSSRuntime({}, tmp_path / "data")
        await runtime.initialize()
        service = ManagementService(runtime)
        try:
            list_id = await runtime.repository.create_list("Preview")
            for index in range(2):
                feed = await runtime.repository.create_feed(f"https://example.org/preview-{index}")
                await runtime.repository.add_list_feed(list_id, feed.feed_id)
                await runtime.repository.save_articles(
                    feed.feed_id,
                    [
                        Article(
                            "",
                            feed.feed_id,
                            f"entry-{index}",
                            f"https://example.org/article-{index}",
                            f"Article {index}",
                            published_at=float(index),
                        )
                    ],
                )
            registry = PreviewRegistry()
            runtime.registry = registry

            async def unexpected_article_lookup(article_id):
                raise AssertionError("list preview must not load articles one by one")

            monkeypatch.setattr(runtime.repository, "article", unexpected_article_lookup)
            preview = await service._preview_list(list_id, None)
            assert preview["article_count"] == 2
            assert registry.titles == ["Article 1", "Article 0"]
        finally:
            await runtime.close()

    asyncio.run(run())


def test_business_import_export_roundtrip_and_relationship_validation(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import astrbot_plugin_rss.services.management as management_module

    async def validate(url):
        return SimpleNamespace(url=url)

    monkeypatch.setattr(management_module, "validate_public_url", validate)

    async def run():
        runtime = RSSRuntime({}, tmp_path / "data")
        await runtime.initialize()
        service = ManagementService(runtime)
        async with runtime.database.transaction() as conn:
            await conn.execute(
                "INSERT INTO targets(target_id,platform_instance,unified_origin,platform,session_id,session_type,label,routing_key) VALUES(?,?,?,?,?,?,?,?)",
                ("target-1", "test", "origin", "test", "session", "private", "Test", "route"),
            )
        payload = {
            "feeds": [
                {
                    "feed_id": "f-1",
                    "url": "https://example.org/rss.xml",
                    "title": "Example",
                    "description": "feed",
                    "enabled": False,
                    "poll_interval": "0d",
                }
            ],
            "subscription_lists": [{"list_id": "l-1", "name": "List", "enabled": False, "poll_interval": "0h"}],
            "list_feeds": [{"list_id": "l-1", "feed_id": "f-1"}],
            "targets": [{"target_id": "target-1"}],
            "direct_subscriptions": [
                {"target_id": "target-1", "feed_id": "f-1", "enabled": False, "poll_interval": "0m"},
                {"target_id": "missing-target", "feed_id": "f-1", "enabled": True, "poll_interval": None},
            ],
            "list_subscriptions": [{"target_id": "target-1", "list_id": "l-1", "enabled": True, "poll_interval": "0s"}],
        }
        path = tmp_path / "business.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        result = await service._import_json(path)
        assert result["feeds_imported"] == 1
        assert result["list_memberships_imported"] == 1
        assert result["direct_subscriptions_imported"] == 1
        assert result["skipped_unregistered_targets"] == 1
        feed = await runtime.database.fetchone("SELECT enabled,translation_enabled FROM feeds")
        sub = await runtime.database.fetchone("SELECT enabled FROM direct_subscriptions")
        list_enabled = await runtime.database.fetchone("SELECT enabled FROM subscription_lists")
        assert (feed[0], feed[1], sub[0], list_enabled[0]) == (0, 0, 0, 0)
        for table in ("feeds", "direct_subscriptions", "subscription_lists", "list_subscriptions"):
            assert (await runtime.database.fetchone(f"SELECT poll_interval FROM {table}"))[0] is None
        exported = await service._business_export()
        assert exported["feeds"][0]["enabled"] is False
        assert exported["feeds"][0]["translation_enabled"] is False
        assert exported["feeds"][0]["public_id"] == 1
        assert exported["subscription_lists"][0]["public_id"] == 1
        assert exported["subscription_lists"][0]["enabled"] is False
        assert exported["direct_subscriptions"][0]["enabled"] is False
        assert exported["list_subscriptions"][0]["enabled"] is True

        payload["feeds"][0]["translation_enabled"] = True
        path.write_text(json.dumps(payload), encoding="utf-8")
        await service._import_json(path)
        assert (await runtime.repository.get_feed_by_url("https://example.org/rss.xml")).translation_enabled is True
        exported = await service._business_export()
        assert exported["feeds"][0]["translation_enabled"] is True
        assert all(
            type(row["enabled"]) is bool
            for key in ("feeds", "subscription_lists", "direct_subscriptions", "list_subscriptions")
            for row in exported[key]
        )
        path.write_text(json.dumps(exported), encoding="utf-8")
        await service._import_json(path)
        assert (await runtime.repository.get_feed_by_url("https://example.org/rss.xml")).translation_enabled is True
        assert (await runtime.database.fetchone("SELECT enabled FROM direct_subscriptions"))[0] == 0
        assert (await runtime.database.fetchone("SELECT enabled FROM list_subscriptions"))[0] == 1
        payload["feeds"][0]["translation_enabled"] = "true"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(ValueError, match="translation_enabled must be boolean"):
            await service._import_json(path)
        payload["feeds"][0]["translation_enabled"] = False

        existing_public_id = (await runtime.database.fetchone("SELECT public_id FROM subscription_lists"))[0]
        payload["subscription_lists"][0]["list_id"] = "l-2"
        payload["list_feeds"] = [{"list_id": "l-2", "feed_id": "f-1"}]
        payload["list_subscriptions"] = [
            {"target_id": "target-1", "list_id": "l-2", "enabled": True, "poll_interval": 0}
        ]
        path.write_text(json.dumps(payload), encoding="utf-8")
        merged = await service._import_json(path)
        assert merged["lists_imported"] == 1
        assert (await runtime.database.fetchone("SELECT COUNT(*) FROM subscription_lists"))[0] == 1
        assert (await runtime.database.fetchone("SELECT public_id FROM subscription_lists"))[0] == existing_public_id
        assert (await runtime.database.fetchone("SELECT COUNT(*) FROM list_feeds"))[0] == 1

        repeated = await service._import_json(path)
        assert repeated["lists_imported"] == 1
        assert (await runtime.database.fetchone("SELECT count(*) FROM subscription_lists"))[0] == 1
        payload["list_feeds"] = [{"list_id": "l-1", "feed_id": "unknown"}]
        path.write_text(json.dumps(payload), encoding="utf-8")
        before = await runtime.database.fetchone("SELECT count(*) FROM subscription_lists")
        with pytest.raises(ValueError, match="unknown feed or list"):
            await service._import_json(path)
        after = await runtime.database.fetchone("SELECT count(*) FROM subscription_lists")
        assert before[0] == after[0] == 1
        await runtime.close()

    asyncio.run(run())


def test_business_import_normalizes_units_and_validates_before_mutation(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import astrbot_plugin_rss.services.management as management_module

    async def validate(url):
        return SimpleNamespace(url=url)

    monkeypatch.setattr(management_module, "validate_public_url", validate)

    async def run():
        runtime = RSSRuntime({}, tmp_path / "data")
        await runtime.initialize()
        try:
            target = await runtime.repository.create_target("instance", "origin", "test", "session")
            service = ManagementService(runtime)
            payload = {
                "feeds": [{"feed_id": "feed", "url": "https://example.org/units", "poll_interval": "1d"}],
                "subscription_lists": [{"list_id": "list", "name": "Units", "poll_interval": " 2 H "}],
                "direct_subscriptions": [{"target_id": target.target_id, "feed_id": "feed", "poll_interval": "3m"}],
                "list_subscriptions": [{"target_id": target.target_id, "list_id": "list", "poll_interval": "120s"}],
            }
            path = tmp_path / "units.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            await service._import_json(path)
            exported = await service._business_export()
            expected = {
                "feeds": 86400,
                "subscription_lists": 7200,
                "direct_subscriptions": 180,
                "list_subscriptions": 120,
            }
            for key, seconds in expected.items():
                assert exported[key][0]["poll_interval"] == seconds
            assert exported["feeds"][0]["poll_interval_input"] == "1d"
            assert exported["subscription_lists"][0]["poll_interval_input"] == " 2 H "

            # Optional display fields cannot override or bypass the effective interval.
            for key in ("feeds", "subscription_lists"):
                payload[key][0]["poll_interval_input"] = "1s"
                path.write_text(json.dumps(payload), encoding="utf-8")
                with pytest.raises(ValueError, match="must match"):
                    await service._import_json(path)
                assert await service._business_export() == exported
                del payload[key][0]["poll_interval_input"]

            # Seconds remain compatible with older importers while a new importer retains display spelling.
            roundtrip_runtime = RSSRuntime({}, tmp_path / "roundtrip")
            await roundtrip_runtime.initialize()
            try:
                roundtrip_service = ManagementService(roundtrip_runtime)
                path.write_text(json.dumps(exported), encoding="utf-8")
                await roundtrip_service._import_json(path)
                roundtrip = await roundtrip_service._business_export()
                for key in ("feeds", "subscription_lists"):
                    assert roundtrip[key][0]["poll_interval"] == exported[key][0]["poll_interval"]
                    assert roundtrip[key][0]["poll_interval_input"] == exported[key][0]["poll_interval_input"]
            finally:
                await roundtrip_runtime.close()

            for key in expected:
                original = payload[key][0]["poll_interval"]
                for invalid in (True, "1h30m", "1.5d", "1w", "-1s"):
                    payload[key][0]["poll_interval"] = invalid
                    path.write_text(json.dumps(payload), encoding="utf-8")
                    with pytest.raises(ValueError):
                        await service._import_json(path)
                    assert await service._business_export() == exported
                payload[key][0]["poll_interval"] = original
        finally:
            await runtime.close()

    asyncio.run(run())


def test_import_export_keeps_rsshub_route_and_validates_effective_keyed_url(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import astrbot_plugin_rss.services.management as management_module

    checked = []

    async def validate(url):
        checked.append(url)
        return SimpleNamespace(url=url)

    monkeypatch.setattr(management_module, "validate_public_url", validate)

    async def run():
        runtime = RSSRuntime(
            {"rsshub": {"default_url_prefix": "https://rsshub.example", "rsshub_access_key": "secret"}},
            tmp_path / "data",
        )
        await runtime.initialize()
        try:
            service = ManagementService(runtime)
            route = "/twitter/example?foo=bar"
            path = tmp_path / "rsshub-import.json"
            path.write_text(
                json.dumps({"feeds": [{"feed_id": "imported-feed", "url": route, "title": "RSSHub"}]}),
                encoding="utf-8",
            )
            result = await service._import_json(path)
            assert result["feeds_imported"] == 1
            assert checked == ["https://rsshub.example" + route + "&key=secret"]
            feed = await runtime.repository.get_feed_by_url(route)
            assert feed is not None
            exported = await service._business_export()
            assert exported["feeds"][0]["url"] == route
            assert "secret" not in json.dumps(exported)
        finally:
            await runtime.close()

    asyncio.run(run())


def test_management_create_and_list_member_add_accept_rsshub_routes(tmp_path, monkeypatch):
    import astrbot_plugin_rss.services.facade as facade_module
    from astrbot_plugin_rss.core.models import FetchResult, ParsedFeed
    from astrbot_plugin_rss.core.security import ValidatedURL

    checked = []

    async def validate(url):
        checked.append(url)
        return ValidatedURL(url, "rsshub.example", 443, ())

    monkeypatch.setattr(facade_module, "validate_public_url", validate)

    class Fetcher:
        calls = 0

        async def fetch(self, feed, force=False):
            self.calls += 1
            request_url = "https://rsshub.example" + feed.url + "?key=secret"
            return FetchResult(feed.feed_id, request_url, request_url, status_code=200, raw_xml=b"<rss/>")

    class Parser:
        def parse(self, _result):
            return ParsedFeed(title="RSSHub")

    async def run():
        fetcher = Fetcher()
        runtime = RSSRuntime(
            {"rsshub": {"default_url_prefix": "https://rsshub.example", "rsshub_access_key": "secret"}},
            tmp_path,
            fetcher=fetcher,
            parser=Parser(),
        )
        await runtime.initialize()
        try:
            target = await runtime.repository.create_target("test-instance", "origin", "test", "session")
            service = ManagementService(runtime)
            route = "/twitter/example"
            created = await service.post("feeds/create", {"url": route, "target_id": target.target_id})
            feed = created["feed"]
            assert feed["url"] == route
            listing = await service.post("lists/create", {"name": "RSSHub"})
            added = await service.post(f"lists/{listing['list_id']}/members/add", {"url": route})
            assert added["feed_ids"] == [feed["public_id"]]
            assert fetcher.calls == 1
            assert checked == ["https://rsshub.example" + route + "?key=secret"]
            export = await service._business_export()
            assert export["feeds"][0]["url"] == route
            assert "secret" not in json.dumps(export)
        finally:
            await runtime.close()

    asyncio.run(run())


def test_feed_translation_flag_create_and_update_contract(tmp_path, monkeypatch):
    import astrbot_plugin_rss.services.facade as facade_module
    from astrbot_plugin_rss.core.models import FetchResult, ParsedFeed
    from astrbot_plugin_rss.core.security import ValidatedURL

    async def validate(url):
        return ValidatedURL(url, "example.org", 443, ())

    monkeypatch.setattr(facade_module, "validate_public_url", validate)

    class Fetcher:
        async def fetch(self, feed, force=False):
            return FetchResult(feed.feed_id, feed.url, feed.url, status_code=200, raw_xml=b"<rss/>")

    class Parser:
        def parse(self, _result):
            return ParsedFeed(title="Example")

    async def run():
        runtime = RSSRuntime({}, tmp_path, fetcher=Fetcher(), parser=Parser())
        await runtime.initialize()
        try:
            target = await runtime.repository.create_target("instance", "origin", "test", "session")
            service = ManagementService(runtime)

            created = await service.post(
                "feeds/create", {"url": "https://example.org/new", "target_id": target.target_id}
            )
            feed = created["feed"]
            assert feed["translation_enabled"] is False

            enabled = await service.post(
                "feeds/create",
                {"url": "https://example.org/enabled", "target_id": target.target_id, "translation_enabled": True},
            )
            assert enabled["feed"]["translation_enabled"] is True
            repeated = await service.post(
                "feeds/create",
                {
                    "url": "https://example.org/enabled",
                    "target_id": target.target_id,
                    "translation_enabled": False,
                },
            )
            assert repeated["feed"]["translation_enabled"] is True

            unchanged = await service.post(f"feeds/{feed['public_id']}/update", {"enabled": False})
            assert unchanged["translation_enabled"] is False
            changed = await service.post(f"feeds/{feed['public_id']}/update", {"translation_enabled": True})
            assert changed["translation_enabled"] is True
            for endpoint, body in (
                (
                    "feeds/create",
                    {
                        "url": "https://example.org/invalid",
                        "target_id": target.target_id,
                        "translation_enabled": "true",
                    },
                ),
                (f"feeds/{feed['public_id']}/update", {"translation_enabled": "false"}),
            ):
                with pytest.raises(ValueError, match="translation_enabled must be boolean"):
                    await service.post(endpoint, body)
        finally:
            await runtime.close()

    asyncio.run(run())


def test_restore_accepts_real_backup_and_rejects_missing_column_without_closing_runtime(tmp_path):
    async def run():
        data_dir = tmp_path / "data"
        runtime = RSSRuntime({}, data_dir)
        await runtime.initialize()
        service = ManagementService(runtime)
        feed = await runtime.repository.create_feed("https://example.org/backup-feed", translation_enabled=True)
        valid = tmp_path / "valid.sqlite3"
        await runtime.backup(valid)
        result = await service._restore_sqlite(valid)
        assert result["restored"] is True
        assert service.runtime._started
        assert await service.runtime.repository.get_setting("paused", "0") == "0"
        restored_feed = await service.runtime.repository.get_feed(feed.feed_id)
        assert restored_feed is not None and restored_feed.translation_enabled is True

        invalid_flag = tmp_path / "invalid-translation-flag.sqlite3"
        shutil.copyfile(valid, invalid_flag)
        with sqlite3.connect(invalid_flag) as db:
            db.execute("PRAGMA ignore_check_constraints=ON")
            db.execute("UPDATE feeds SET translation_enabled='false'")
            db.commit()
        with pytest.raises(ValueError, match="translation_enabled values are invalid"):
            await service._restore_sqlite(invalid_flag)
        assert service.runtime._started

        malformed = tmp_path / "missing-column.sqlite3"
        shutil.copyfile(valid, malformed)
        with sqlite3.connect(malformed) as db:
            db.execute("ALTER TABLE feeds DROP COLUMN metadata")
            db.commit()
        with pytest.raises(ValueError, match="missing required columns"):
            await service._restore_sqlite(malformed)
        assert service.runtime._started
        assert (await service.runtime.database.fetchone("PRAGMA quick_check"))[0] == "ok"

        missing_index = tmp_path / "missing-index.sqlite3"
        shutil.copyfile(valid, missing_index)
        with sqlite3.connect(missing_index) as db:
            db.execute("DROP INDEX idx_articles_feed_seq")
            db.commit()
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        with pytest.raises(ValueError, match="incompatible keys or indexes"):
            await service._restore_sqlite(missing_index)
        assert service.runtime._started
        await service.runtime.close()

    asyncio.run(run())


def test_restore_rolls_back_if_replacement_start_fails(tmp_path, monkeypatch):
    async def run():
        old_dir = tmp_path / "old"
        runtime = RSSRuntime({}, old_dir)
        source_runtime = None
        service = None
        created = []
        try:
            await runtime.initialize()
            await runtime.repository.set_setting("marker", "old")
            service = ManagementService(runtime)
            source_runtime = RSSRuntime({}, tmp_path / "source")
            await source_runtime.initialize()
            await source_runtime.repository.set_setting("marker", "new")
            backup = tmp_path / "source.sqlite3"
            await source_runtime.backup(backup)

            original_init = RSSRuntime.__init__

            def capture_init(self, *args, **kwargs):
                original_init(self, *args, **kwargs)
                created.append(self)

            monkeypatch.setattr(RSSRuntime, "__init__", capture_init)
            original_start = RSSRuntime.start
            calls = 0

            async def fail_once(self):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise RuntimeError("injected start failure")
                await original_start(self)

            original_close = RSSRuntime.close

            async def fail_replacement_close(self):
                await original_close(self)
                if created and self is created[0]:
                    raise OSError("injected replacement close failure")

            monkeypatch.setattr(RSSRuntime, "start", fail_once)
            monkeypatch.setattr(RSSRuntime, "close", fail_replacement_close)
            with pytest.raises(RuntimeError, match="injected start failure"):
                await service._restore_sqlite(backup)
            assert service.runtime._started
            assert await service.runtime.repository.get_setting("marker") == "old"
            assert created[0].database.connection is None
            assert len(created) == 2
        finally:
            try:
                await (service.runtime if service is not None else runtime).close()
            finally:
                if source_runtime is not None:
                    await source_runtime.close()

    asyncio.run(run())


def test_runtime_close_attempts_all_resources_and_database_after_failure(tmp_path):
    class Resource:
        def __init__(self, label, fail=False):
            self.label = label
            self.fail = fail
            self.closed = False

        async def close(self):
            self.closed = True
            if self.fail:
                raise OSError("sensitive implementation detail")

    async def run():
        fetcher = Resource("fetcher", fail=True)
        registry = Resource("registry")
        sender = Resource("sender")
        media_downloader = Resource("media_downloader")
        runtime = RSSRuntime({}, tmp_path / "runtime", fetcher=fetcher, registry=registry, sender=sender)
        runtime.media_downloader = media_downloader
        await runtime.database.initialize()
        runtime._initialized = True
        runtime._started = True
        try:
            with pytest.raises(RuntimeError, match="OSError") as raised:
                await runtime.close()
            assert "sensitive implementation detail" not in str(raised.value)
            assert fetcher.closed and registry.closed and sender.closed and media_downloader.closed
            assert runtime.database.connection is None
            assert not runtime._started and not runtime._initialized
        finally:
            if runtime.database.connection is not None:
                await runtime.database.close()

    asyncio.run(run())


def test_runtime_close_finishes_remaining_resources_after_cancelled_error(tmp_path):
    class Resource:
        def __init__(self, fail=False):
            self.fail = fail
            self.closed = False

        async def close(self):
            self.closed = True
            if self.fail:
                raise asyncio.CancelledError()

    async def run():
        fetcher = Resource(fail=True)
        registry = Resource()
        sender = Resource()
        media_downloader = Resource()
        runtime = RSSRuntime({}, tmp_path / "runtime", fetcher=fetcher, registry=registry, sender=sender)
        runtime.media_downloader = media_downloader
        await runtime.database.initialize()
        runtime._initialized = True
        runtime._started = True
        try:
            with pytest.raises(asyncio.CancelledError):
                await runtime.close()
            assert fetcher.closed and registry.closed and sender.closed and media_downloader.closed
            assert runtime.database.connection is None
            assert not runtime._started and not runtime._initialized
        finally:
            if runtime.database.connection is not None:
                await runtime.database.close()

    asyncio.run(run())
