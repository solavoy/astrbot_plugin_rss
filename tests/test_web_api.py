import asyncio
import importlib
import os
import sys
import types
from pathlib import Path

from astrbot_plugin_rss.core.runtime import RSSRuntime


class _Upload:
    def __init__(self, data, filename="input.json"):
        self.filename = filename
        self.content_type = "application/json"
        self.data = data
        self.size = len(data)
        self.saved = False

    async def save(self, path):
        self.saved = True
        path.write_bytes(self.data)


def _install_astrbot_web(monkeypatch):
    state = types.SimpleNamespace(username="admin", query={}, path_params={}, _json={}, _files={})

    async def json(default=None):
        return state._json if state._json else default

    async def files():
        return state._files

    state.json = json
    state.files = files
    web = types.ModuleType("astrbot.api.web")
    web.request = state
    web.PluginUploadFile = _Upload
    web.json_response = lambda value: {"kind": "json", "value": value}
    web.error_response = lambda value, status_code=400: {"kind": "error", "status": status_code, "error": value}
    web.file_response = lambda path, **kwargs: {"kind": "file", "path": str(path), **kwargs}
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    astrbot.api = api
    api.web = web
    monkeypatch.setitem(sys.modules, "astrbot", astrbot)
    monkeypatch.setitem(sys.modules, "astrbot.api", api)
    monkeypatch.setitem(sys.modules, "astrbot.api.web", web)
    return state


class _Context:
    def __init__(self):
        self.handlers = {}

    def register_web_api(self, route, handler, methods, desc):
        self.handlers[(methods[0], route)] = handler


def test_registered_web_handlers_route_to_real_services(tmp_path, monkeypatch):
    state = _install_astrbot_web(monkeypatch)
    sys.modules.pop("astrbot_plugin_rss.web.api", None)
    api_module = importlib.import_module("astrbot_plugin_rss.web.api")

    async def run():
        runtime = RSSRuntime({}, tmp_path)
        await runtime.initialize()
        api = api_module.WebAPI(runtime)
        context = _Context()
        api.register(context)
        assert ("GET", "/astrbot_plugin_rss/dashboard") in context.handlers
        assert ("GET", "/astrbot_plugin_rss/feeds/<feed_id>") in context.handlers
        assert ("POST", "/astrbot_plugin_rss/lists/create") in context.handlers

        dashboard = await context.handlers[("GET", "/astrbot_plugin_rss/dashboard")]()
        assert dashboard["kind"] == "json"
        assert dashboard["value"]["counts"]["feeds"] == 0

        feed = await runtime.repository.create_feed("https://example.org/api-poll-interval")
        assert feed.translation_enabled is False
        state.path_params = {"feed_id": str(feed.public_id)}
        state._json = {"poll_interval": 0}
        inherited = await context.handlers[("POST", "/astrbot_plugin_rss/feeds/<feed_id>/update")]()
        assert inherited["kind"] == "json" and inherited["value"]["poll_interval"] is None
        state._json = {"poll_interval": 1}
        one_second = await context.handlers[("POST", "/astrbot_plugin_rss/feeds/<feed_id>/update")]()
        assert one_second["kind"] == "json" and one_second["value"]["poll_interval"] == 1
        for invalid in (-1, 1.5, True):
            state._json = {"poll_interval": invalid}
            rejected = await context.handlers[("POST", "/astrbot_plugin_rss/feeds/<feed_id>/update")]()
            assert rejected["kind"] == "error" and rejected["status"] == 400

        state._json = {"translation_enabled": True}
        translation_enabled = await context.handlers[("POST", "/astrbot_plugin_rss/feeds/<feed_id>/update")]()
        assert translation_enabled["kind"] == "json"
        assert translation_enabled["value"]["translation_enabled"] is True
        state._json = {"poll_interval": 0}
        unchanged_translation = await context.handlers[("POST", "/astrbot_plugin_rss/feeds/<feed_id>/update")]()
        assert unchanged_translation["value"]["translation_enabled"] is True
        state._json = {"translation_enabled": "true"}
        invalid_translation = await context.handlers[("POST", "/astrbot_plugin_rss/feeds/<feed_id>/update")]()
        assert invalid_translation["kind"] == "error" and invalid_translation["status"] == 400

        state.path_params = {"feed_id": "999"}
        missing = await context.handlers[("GET", "/astrbot_plugin_rss/feeds/<feed_id>")]()
        assert missing["kind"] == "error" and missing["status"] == 404

        state._json = {"name": "Engineering", "poll_interval": 0}
        created = await context.handlers[("POST", "/astrbot_plugin_rss/lists/create")]()
        assert created["kind"] == "json"
        list_id = created["value"]["list_id"]
        list_row = await runtime.repository.get_list_by_public_id(list_id)
        assert (
            await runtime.database.fetchone(
                "SELECT poll_interval FROM subscription_lists WHERE list_id=?", (list_row["list_id"],)
            )
        )[0] is None
        state.path_params = {"list_id": list_id}
        state._json = {"name": "Renamed", "poll_interval": 1}
        updated = await context.handlers[("POST", "/astrbot_plugin_rss/lists/<list_id>/update")]()
        assert updated["kind"] == "json" and updated["value"]["list"]["name"] == "Renamed"
        assert updated["value"]["list"]["poll_interval"] == 1
        for invalid in (-1, 1.5, True):
            state._json = {"poll_interval": invalid}
            rejected = await context.handlers[("POST", "/astrbot_plugin_rss/lists/<list_id>/update")]()
            assert rejected["kind"] == "error" and rejected["status"] == 400
        state._json = {"confirm": True}
        deleted = await context.handlers[("POST", "/astrbot_plugin_rss/lists/<list_id>/delete")]()
        assert deleted["kind"] == "json" and deleted["value"]["deleted"] is True
        assert await runtime.repository.list_lists() == []
        missing = await context.handlers[("POST", "/astrbot_plugin_rss/lists/<list_id>/delete")]()
        assert missing["kind"] == "error" and missing["status"] == 404
        await runtime.close()

    asyncio.run(run())


def test_bulk_list_member_add_validates_and_resolves_before_mutating(tmp_path, monkeypatch):
    state = _install_astrbot_web(monkeypatch)
    sys.modules.pop("astrbot_plugin_rss.web.api", None)
    api_module = importlib.import_module("astrbot_plugin_rss.web.api")

    async def run():
        runtime = RSSRuntime({}, tmp_path)
        await runtime.initialize()
        api = api_module.WebAPI(runtime)
        feed_one = await runtime.repository.create_feed("https://example.org/bulk-one")
        feed_two = await runtime.repository.create_feed("https://example.org/bulk-two")
        list_uuid = await runtime.repository.create_list("Bulk members")
        listing = await runtime.repository.get_list(list_uuid)
        endpoint = f"lists/{listing['public_id']}/members/add"

        for invalid_item in (None, {"feed_id": "not-a-public-id"}):
            state._json = {"items": [{"feed_id": feed_one.public_id}, invalid_item]}
            rejected = await api.post(endpoint, state)
            assert rejected["kind"] == "error" and rejected["status"] == 400
            count = await runtime.database.fetchone("SELECT COUNT(*) FROM list_feeds WHERE list_id=?", (list_uuid,))
            assert count[0] == 0

        state._json = {"items": [{"feed_id": feed_one.public_id}, {"feed_id": feed_two.public_id}]}
        added = await api.post(endpoint, state)
        assert added["kind"] == "json"
        assert added["value"]["feed_ids"] == [feed_one.public_id, feed_two.public_id]
        count = await runtime.database.fetchone("SELECT COUNT(*) FROM list_feeds WHERE list_id=?", (list_uuid,))
        assert count[0] == 2
        await runtime.close()

    asyncio.run(run())


def test_web_requires_authenticated_username(tmp_path, monkeypatch):
    state = _install_astrbot_web(monkeypatch)
    sys.modules.pop("astrbot_plugin_rss.web.api", None)
    api_module = importlib.import_module("astrbot_plugin_rss.web.api")

    async def run():
        runtime = RSSRuntime({}, tmp_path)
        await runtime.initialize()
        state.username = None
        result = await api_module.WebAPI(runtime).get("dashboard", state)
        assert result["status"] == 401
        await runtime.close()

    asyncio.run(run())


def test_import_requires_username_bound_one_time_confirmation(tmp_path, monkeypatch):
    state = _install_astrbot_web(monkeypatch)
    sys.modules.pop("astrbot_plugin_rss.web.api", None)
    api_module = importlib.import_module("astrbot_plugin_rss.web.api")

    async def run():
        runtime = RSSRuntime({}, tmp_path)
        await runtime.initialize()
        api = api_module.WebAPI(runtime)
        context = _Context()
        api.register(context)
        data = b'{"feeds":[],"subscription_lists":[],"list_feeds":[],"targets":[],"direct_subscriptions":[],"list_subscriptions":[]}'
        state._json = {"confirm": True, "filename": "business.json", "size": len(data)}
        confirmed = await context.handlers[("POST", "/astrbot_plugin_rss/data/import/confirm")]()
        assert confirmed["kind"] == "json" and confirmed["value"]["confirmation"]
        state._files = {"file": _Upload(data, "business.json")}
        uploaded = await context.handlers[("POST", "/astrbot_plugin_rss/data/import")]()
        assert uploaded["kind"] == "json" and uploaded["value"]["feeds_imported"] == 0
        rejected = await context.handlers[("POST", "/astrbot_plugin_rss/data/import")]()
        assert rejected["kind"] == "error" and rejected["status"] == 400
        await runtime.close()

    asyncio.run(run())


def test_oversized_declared_upload_is_rejected_before_saving(tmp_path, monkeypatch):
    state = _install_astrbot_web(monkeypatch)
    sys.modules.pop("astrbot_plugin_rss.web.api", None)
    api_module = importlib.import_module("astrbot_plugin_rss.web.api")

    async def run():
        runtime = RSSRuntime({}, tmp_path)
        await runtime.initialize()
        upload = _Upload(b"x")
        upload.size = 20_000_001
        state._files = {"file": upload}
        result = await api_module.WebAPI(runtime).post("data/import", state)
        assert result["kind"] == "error" and result["status"] == 400
        assert upload.saved is False
        await runtime.close()

    asyncio.run(run())


def test_export_cleanup_removes_only_expired_plugin_downloads(tmp_path, monkeypatch):
    _install_astrbot_web(monkeypatch)
    sys.modules.pop("astrbot_plugin_rss.web.api", None)
    api_module = importlib.import_module("astrbot_plugin_rss.web.api")

    export_dir = tmp_path / "exports"
    export_dir.mkdir()
    old_export = export_dir / f"rss-export-{'a' * 32}.json"
    old_backup = export_dir / f"rss-backup-{'b' * 32}.sqlite3"
    recent_export = export_dir / f"rss-export-{'c' * 32}.json"
    unrelated = export_dir / f"restore-safety-{'d' * 32}.sqlite3"
    other_file = export_dir / "manual-export.json"
    safety_backup = tmp_path / f"restore-safety-{'e' * 32}.sqlite3"
    for path in (old_export, old_backup, recent_export, unrelated, other_file, safety_backup):
        path.write_text("keep or remove", encoding="utf-8")
    os.utime(old_export, (1, 1))
    os.utime(old_backup, (1, 1))

    api = api_module.WebAPI.__new__(api_module.WebAPI)
    api.runtime = types.SimpleNamespace(data_dir=tmp_path)
    api._cleanup_expired_exports()

    assert not old_export.exists()
    assert not old_backup.exists()
    assert recent_export.exists()
    assert unrelated.exists()
    assert other_file.exists()
    assert safety_backup.exists()


def test_export_download_continues_when_expired_file_cleanup_fails(tmp_path, monkeypatch):
    state = _install_astrbot_web(monkeypatch)
    sys.modules.pop("astrbot_plugin_rss.web.api", None)
    api_module = importlib.import_module("astrbot_plugin_rss.web.api")

    async def run():
        runtime = RSSRuntime({}, tmp_path)
        await runtime.initialize()
        api = api_module.WebAPI(runtime)
        export_dir = tmp_path / "exports"
        export_dir.mkdir()
        original_iterdir = Path.iterdir

        def fail_export_iteration(path):
            if path == export_dir:
                raise OSError("temporary directory access failure")
            return original_iterdir(path)

        monkeypatch.setattr(Path, "iterdir", fail_export_iteration)
        response = await api.get("data/export", state)

        assert response["kind"] == "file"
        assert Path(response["path"]).is_file()
        await runtime.close()

    asyncio.run(run())
