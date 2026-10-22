import asyncio
import importlib
import sys
import types


def test_command_handlers_are_owned_by_plugin_main_module(monkeypatch):
    class Group:
        def command(self, name, alias=None):
            def decorate(handler):
                handler.astrbot_command = name
                handler.astrbot_alias = alias or set()
                return handler

            return decorate

    class Filter:
        def command_group(self, name, alias=None):
            def decorate(_handler):
                group = Group()
                group.name = name
                group.alias = alias or set()
                group.__doc__ = _handler.__doc__
                return group

            return decorate

    class Star:
        def __init__(self, context):
            self.context = context

    def register(*args):
        def decorate(plugin):
            plugin.astrbot_metadata = args
            return plugin

        return decorate

    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.AstrBotConfig = dict
    event = types.ModuleType("astrbot.api.event")
    event.AstrMessageEvent = object
    event.filter = Filter()
    star = types.ModuleType("astrbot.api.star")
    star.Context = object
    star.Star = Star
    star.StarTools = types.SimpleNamespace(get_data_dir=lambda name: name)
    star.register = register
    web = types.ModuleType("astrbot.api.web")
    web.PluginUploadFile = type("PluginUploadFile", (), {})
    web.request = types.SimpleNamespace()
    web.json_response = lambda value: value
    web.error_response = lambda value, status_code=400: value
    web.file_response = lambda *args, **kwargs: args
    api.web = web
    api.logger = types.SimpleNamespace(
        info=lambda *_: None,
        warning=lambda *_: None,
        error=lambda *_: None,
        exception=lambda *_: None,
    )
    astrbot.api = api
    for module in (astrbot, api, event, star, web):
        monkeypatch.setitem(sys.modules, module.__name__, module)
    sys.modules.pop("astrbot_plugin_rss.main", None)
    main = importlib.import_module("astrbot_plugin_rss.main")
    methods = [
        value for value in vars(main.RSSPlugin).values() if callable(value) and hasattr(value, "astrbot_command")
    ]
    assert {method.astrbot_command for method in methods} == {
        "help",
        "sub",
        "unsub",
        "sublist",
        "unsublist",
        "list",
        "feed",
        "mine",
        "status",
    }
    assert all(method.__module__ == "astrbot_plugin_rss.main" for method in methods)
    assert main.RSSPlugin.rss.alias == {"/rss"}
    assert main.RSSPlugin._rss_sub.astrbot_alias == {"订阅"}
    assert main.RSSPlugin._rss_list.__doc__ == "查看共享订阅列表及其稳定 ID。"
    assert main.RSSPlugin._rss_feed.__doc__ == "查看订阅源及其稳定 ID。"
    import inspect

    assert inspect.signature(main.RSSPlugin._rss_sub).parameters["url"].default == ""
    assert main.RSSPlugin.astrbot_metadata[2] == "RSS 订阅推送"
    assert main.RSSPlugin.astrbot_metadata[3] == "1.0.0"
    assert main.RSSPlugin.rss.__doc__
    assert "/rss sub <源 ID、公开 RSS/Atom 地址或 RSSHub 相对路径>" in main.RSSPlugin._rss_sub.__doc__
    assert "/rss 订阅 <源 ID、公开 RSS/Atom 地址或 RSSHub 相对路径>" in main.RSSPlugin._rss_sub.__doc__
    assert inspect.signature(main.RSSPlugin._rss_unsub).parameters["feed_id"].default == ""
    assert "源数字 ID、HTTP(S) 地址或 RSSHub 相对路径" in main.RSSPlugin._rss_unsub.__doc__
    assert main.RSSPlugin._rss_help.__doc__ == "查看 RSS 订阅命令和用法。"

    class Config(dict):
        saved = 0

        def save_config(self):
            self.saved += 1

    plugin_config = Config(
        fetch={"global_poll_interval": 300, "request_timeout": 15},
        network={"proxy": ""},
        telegram={"telegraph_access_token": ""},
        global_poll_interval=90,
        request_timeout=20,
        proxy="http://proxy.example:8080",
        telegraph_access_token="legacy-token",
    )
    plugin = main.RSSPlugin(types.SimpleNamespace(), plugin_config)
    assert plugin._rss_config is plugin_config
    assert plugin_config["fetch"] == {"global_poll_interval": 90, "request_timeout": 20}
    assert plugin_config["network"]["proxy"] == "http://proxy.example:8080"
    assert plugin_config["telegram"]["telegraph_access_token"] == "legacy-token"
    assert plugin_config["global_poll_interval"] == 0
    assert plugin_config["request_timeout"] == 0.0
    assert plugin_config["proxy"] == ""
    assert plugin_config["telegraph_access_token"] == ""
    assert plugin_config.saved == 1
    main.RSSPlugin(types.SimpleNamespace(), plugin_config)
    assert plugin_config.saved == 1

    from astrbot_plugin_rss.commands.rss import RSSCommands

    class Event:
        def is_private_chat(self):
            return False

        def is_admin(self):
            return False

        def plain_result(self, text):
            return text

    plugin = RSSCommands()
    plugin._rss_runtime = types.SimpleNamespace(
        config=types.SimpleNamespace(group_admin_only=True),
        services=types.SimpleNamespace(
            targets=types.SimpleNamespace(
                from_event=lambda _event: (_ for _ in ()).throw(AssertionError("must not mutate"))
            )
        ),
    )
    messages = asyncio.run(_collect(plugin.rss_sub(Event(), "https://example.org/rss.xml")))
    assert messages == ["群内订阅操作仅限 AstrBot 管理员。"]


async def _collect(items):
    return [item async for item in items]


def test_group_subscription_policy_defaults_open_and_can_require_admin(monkeypatch):
    _install_astrbot_test_stubs(monkeypatch)
    from astrbot_plugin_rss.commands.rss import RSSCommands

    class Event:
        def __init__(self, *, private=False, admin=False):
            self.private = private
            self.admin = admin
            self.unified_msg_origin = "test:group:42"

        def is_private_chat(self):
            return self.private

        def is_admin(self):
            return self.admin

        def plain_result(self, text):
            return text

    async def run():
        operations = []

        async def from_event(_event):
            return types.SimpleNamespace(target_id="target-42", unified_origin="test:group:42")

        async def resolve_feed(_ref):
            return types.SimpleNamespace(feed_id="feed-uuid", public_id=7, title="Feed")

        async def resolve_list(_ref):
            return {"list_id": "list-uuid", "public_id": 8, "name": "List"}

        async def subscribe(*args):
            operations.append(("sub", *args))

        async def unsubscribe(*args):
            operations.append(("unsub", *args))
            return True

        runtime = types.SimpleNamespace(
            config=types.SimpleNamespace(group_admin_only=False),
            repository=types.SimpleNamespace(
                get_feed_by_public_id=lambda _public_id: resolve_feed("7"),
            ),
            services=types.SimpleNamespace(
                targets=types.SimpleNamespace(from_event=from_event),
                feeds=types.SimpleNamespace(resolve=resolve_feed),
                lists=types.SimpleNamespace(resolve=resolve_list, subscribe=subscribe, unsubscribe=unsubscribe),
                subscriptions=types.SimpleNamespace(subscribe=subscribe, unsubscribe=unsubscribe),
            ),
        )
        plugin = RSSCommands()
        plugin._rss_runtime = runtime

        assert await _collect(plugin.rss_sub(Event(), "7")) == [
            "订阅成功！\n源标题：Feed\n订阅 ID: 7\n推送目标：test:group:42"
        ]
        assert await _collect(plugin.rss_unsub(Event(), "7")) == ["已取消订阅。"]
        assert await _collect(plugin.rss_sublist(Event(), "8")) == ["已订阅列表：List（ID：8）"]
        assert await _collect(plugin.rss_unsublist(Event(), "8")) == ["已取消列表订阅：List（ID：8）"]
        assert len(operations) == 4

        runtime.config.group_admin_only = True
        operations.clear()
        denied = []
        denied.append(await _collect(plugin.rss_sub(Event(), "7")))
        denied.append(await _collect(plugin.rss_unsub(Event(), "7")))
        denied.append(await _collect(plugin.rss_sublist(Event(), "8")))
        denied.append(await _collect(plugin.rss_unsublist(Event(), "8")))
        assert denied == [["群内订阅操作仅限 AstrBot 管理员。"]] * 4
        assert operations == []

        # A group administrator is allowed, and private chats ignore the switch.
        assert await _collect(plugin.rss_sublist(Event(admin=True), "8")) == ["已订阅列表：List（ID：8）"]
        assert await _collect(plugin.rss_unsublist(Event(private=True), "8")) == ["已取消列表订阅：List（ID：8）"]
        assert len(operations) == 2

    import asyncio

    asyncio.run(run())


def _install_astrbot_test_stubs(monkeypatch):
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.AstrBotConfig = dict
    event = types.ModuleType("astrbot.api.event")
    event.AstrMessageEvent = object
    star = types.ModuleType("astrbot.api.star")
    web = types.ModuleType("astrbot.api.web")

    class Star:
        def __init__(self, context):
            self.context = context

    class Group:
        def command(self, _name, alias=None):
            def decorate(handler):
                handler.astrbot_command_alias = alias or set()
                return handler

            return decorate

    class Filter:
        def command_group(self, _name, alias=None):
            def decorate(handler):
                handler.command_group_alias = alias
                group = Group()
                group.alias = alias or set()
                group.__doc__ = handler.__doc__
                return group

            return decorate

    star.Context = object
    star.Star = Star
    star.StarTools = types.SimpleNamespace(get_data_dir=lambda name: name)
    star.register = lambda *_args: lambda plugin: plugin
    api.logger = types.SimpleNamespace(
        info=lambda *_args: None,
        warning=lambda *_args: None,
        error=lambda *_args: None,
        exception=lambda *_args: None,
    )
    api.web = web
    api.event = event
    api.star = star
    api.__path__ = []
    event.__path__ = []
    web.PluginUploadFile = object
    web.request = types.SimpleNamespace()
    web.json_response = lambda value: value
    web.error_response = lambda value, status_code=400: value
    web.file_response = lambda *args, **kwargs: args
    event.filter = Filter()
    astrbot.api = api
    for module in (astrbot, api, event, star, web):
        monkeypatch.setitem(sys.modules, module.__name__, module)


def test_sub_commands_reply_for_missing_unready_fail_and_success(monkeypatch):
    _install_astrbot_test_stubs(monkeypatch)

    from astrbot_plugin_rss.commands.rss import RSSCommands

    class Event:
        unified_msg_origin = "test:private:chat"

        def __init__(self, private=True, admin=False):
            self.private = private
            self.admin = admin

        def is_private_chat(self):
            return self.private

        def is_admin(self):
            return self.admin

        def get_platform_id(self):
            return "test-instance"

        def get_platform_name(self):
            return "test"

        def get_session_id(self):
            return "private:chat"

        def get_message_type(self):
            return "FriendMessage"

        def plain_result(self, text):
            return text

    async def run():
        plugin = RSSCommands()
        plugin._rss_runtime = None
        assert await _collect(plugin.rss_sub(Event(), "")) == [
            "请提供源 ID、公开 RSS/Atom 地址或 RSSHub 相对路径。用法：/rss sub <源 ID、地址或 /route>"
        ]
        assert "尚未就绪" in (await _collect(plugin.rss_sub(Event(), "https://example.org/rss")))[0]

        async def target_from_event(_event):
            return types.SimpleNamespace(target_id="target", unified_origin="test:private:chat")

        plugin._rss_runtime = types.SimpleNamespace(
            config=types.SimpleNamespace(group_admin_only=True),
            services=types.SimpleNamespace(
                targets=types.SimpleNamespace(from_event=target_from_event),
                feeds=types.SimpleNamespace(resolve=lambda ref: _fail_resolve(ref), create=lambda _url: _fail_fetch()),
                subscriptions=types.SimpleNamespace(subscribe=lambda *_args: None),
            ),
        )
        denied = await _collect(plugin.rss_sub(Event(private=False), "https://example.org/rss"))
        assert denied == ["群内订阅操作仅限 AstrBot 管理员。"]

        failed = await _collect(plugin.rss_sub(Event(), "https://example.org/rss"))
        assert failed[0].startswith("正在验证 RSS 地址")
        assert failed[1].startswith("订阅失败：")
        assert "测试抓取失败" in failed[1]

        async def subscribe(_target_id, _feed_id):
            return "subscription-42"

        plugin._rss_runtime.services.feeds.create = lambda _url: _feed()
        plugin._rss_runtime.services.subscriptions.subscribe = subscribe
        success = await _collect(plugin.rss_sub(Event(), "https://example.org/rss"))
        assert success[0].startswith("正在验证 RSS 地址")
        assert success[1] == ("订阅成功！\n源标题：示例源\n订阅 ID: 42\n推送目标：test:private:chat")

        key_rejection = await _collect(plugin.rss_sub(Event(), "/route?key=private-token"))
        assert len(key_rejection) == 1
        assert "请在 RSSHub 配置中填写 Access Key" in key_rejection[0]
        assert "private-token" not in key_rejection[0]

    async def _fail_fetch():
        raise RuntimeError("测试抓取失败")

    async def _fail_resolve(_ref):
        raise KeyError("源未找到")

    async def _feed():
        class Feed:
            feed_id = "feed-42"
            public_id = 42
            title = "示例源"
            url = "https://example.org/rss"

        return Feed()

    asyncio.run(run())


def test_public_id_commands_and_full_list_name_parsing(monkeypatch):
    _install_astrbot_test_stubs(monkeypatch)
    from astrbot_plugin_rss.commands.rss import RSSCommands

    class Event:
        message_str = "/rss sublist The Best List"

        def is_private_chat(self):
            return True

        def is_admin(self):
            return False

        def plain_result(self, text):
            return text

    class GroupReader(Event):
        def is_private_chat(self):
            return False

    class AliasWithExtraArgument(Event):
        message_str = "/rss 订阅 1 2"

    async def run():
        seen = []

        async def resolve_list(ref):
            seen.append(ref)
            return {"list_id": "internal-list-uuid", "public_id": 7, "name": "The Best List"}

        async def list_rows():
            return [{"list_id": "internal-list-uuid", "public_id": 7, "name": "The Best List"}]

        async def feed_rows():
            return [{"feed_id": "internal-feed-uuid", "public_id": 12, "title": "Example Feed"}]

        async def update_subscription(*_args):
            return True

        async def from_event(_event):
            return types.SimpleNamespace(target_id="target", unified_origin="origin")

        plugin = RSSCommands()
        assert plugin._raw_argument(AliasWithExtraArgument(), "sub", "1", aliases=("订阅",)) == "1 2"
        plugin._rss_runtime = types.SimpleNamespace(
            services=types.SimpleNamespace(
                lists=types.SimpleNamespace(
                    resolve=resolve_list,
                    subscribe=update_subscription,
                    unsubscribe=update_subscription,
                    list=list_rows,
                ),
                feeds=types.SimpleNamespace(list=feed_rows),
                targets=types.SimpleNamespace(from_event=from_event),
            )
        )
        assert await _collect(plugin.rss_sublist(Event(), "The")) == ["已订阅列表：The Best List（ID：7）"]
        assert seen == ["The Best List"]
        cancel_event = Event()
        cancel_event.message_str = "/rss unsublist The Best List"
        assert await _collect(plugin.rss_unsublist(cancel_event, "The")) == ["已取消列表订阅：The Best List（ID：7）"]
        assert seen == ["The Best List", "The Best List"]
        assert await _collect(plugin.rss_list(GroupReader())) == ["ID  列表名称\n7  The Best List"]
        assert await _collect(plugin.rss_feed(GroupReader())) == ["ID  feed名称\n12  Example Feed"]

        plugin._rss_runtime.services.lists.list = lambda: _empty()
        plugin._rss_runtime.services.feeds.list = lambda: _empty()
        assert await _collect(plugin.rss_list(GroupReader())) == ["ID  列表名称\n（暂无订阅列表）"]
        assert await _collect(plugin.rss_feed(GroupReader())) == ["ID  feed名称\n（暂无订阅源）"]

    async def _empty():
        return []

    import asyncio

    asyncio.run(run())


def test_sub_command_persists_feed_for_management_page_and_failed_fetch_does_not(tmp_path, monkeypatch):
    _install_astrbot_test_stubs(monkeypatch)
    import astrbot_plugin_rss.services.facade as facade_module
    from astrbot_plugin_rss.core.models import FetchResult, ParsedFeed
    from astrbot_plugin_rss.core.runtime import RSSRuntime
    from astrbot_plugin_rss.core.security import ValidatedURL
    from astrbot_plugin_rss.main import RSSPlugin
    from astrbot_plugin_rss.services.management import ManagementService

    async def allow_url(_url):
        return ValidatedURL("https://example.org/rss", "example.org", 443, ())

    monkeypatch.setattr(facade_module, "validate_public_url", allow_url)

    class Fetcher:
        def __init__(self, fail=False):
            self.fail = fail

        async def fetch(self, feed, force=False):
            if self.fail:
                return FetchResult(
                    feed.feed_id,
                    feed.url,
                    feed.url,
                    status_code=403,
                    error_type="http_error",
                    error_message="HTTP 403 Forbidden",
                )
            return FetchResult(feed.feed_id, feed.url, feed.url, status_code=200, raw_xml=b"<rss><channel/></rss>")

    class Parser:
        def parse(self, _result):
            return ParsedFeed(title="测试订阅源")

    class Event:
        def __init__(self, session="private:chat"):
            self.unified_msg_origin = f"test:{session}"
            self.session = session
            self.sent = []

        def is_private_chat(self):
            return True

        def is_admin(self):
            return False

        def get_platform_id(self):
            return "test-instance"

        def get_platform_name(self):
            return "test"

        def get_session_id(self):
            return self.session

        def get_message_type(self):
            return "FriendMessage"

        def plain_result(self, text):
            return text

        async def send(self, result):
            self.sent.append(result)

    async def run():
        runtime = RSSRuntime({}, tmp_path / "success", fetcher=Fetcher(), parser=Parser())
        try:
            await runtime.initialize()
            plugin = object.__new__(RSSPlugin)
            plugin._rss_runtime = runtime
            plugin._rss_management = ManagementService(runtime)
            success_event = Event()
            await plugin._rss_sub(success_event, "https://example.org/rss")
            assert len(success_event.sent) == 2
            assert success_event.sent[1].startswith("订阅成功！\n源标题：测试订阅源\n订阅 ID: ")
            assert success_event.sent[1].endswith("\n推送目标：test:private:chat")
            feed = await runtime.repository.get_feed_by_url("https://example.org/rss")
            assert feed and feed.public_id
            public_id = feed.public_id
            assert f"订阅 ID: {public_id}" in success_event.sent[1]
            page = await plugin._rss_management.get("feeds", {"page": 1, "page_size": 20, "scope": "direct"})
            assert page["total"] == 1
            assert page["items"][0]["feed_id"]
            relation_rows = await runtime.database.fetchall("SELECT subscription_id FROM direct_subscriptions")
            relation_id = relation_rows[0]["subscription_id"]
            assert relation_id not in success_event.sent[1]
            mine = await plugin._rss_management.mine(
                (await runtime.services.targets.from_event(success_event)).target_id
            )
            assert all(relation_id not in row for row in mine)

            second_event = Event("private:other")
            await plugin._rss_sub(second_event, str(public_id))
            assert len(second_event.sent) == 1
            assert f"订阅 ID: {public_id}" in second_event.sent[0]

            title_unsub_event = Event()
            await plugin._rss_unsub(title_unsub_event, "测试订阅源")
            assert title_unsub_event.sent == [
                "取消失败：仅支持源数字 ID、HTTP(S) 地址或 RSSHub 相对路径，不支持源名称或旧 UUID。"
            ]
            current_target = await runtime.services.targets.from_event(success_event)
            other_target = await runtime.services.targets.from_event(second_event)
            assert await runtime.services.subscriptions.mine(current_target.target_id)
            assert await runtime.services.subscriptions.mine(other_target.target_id)

            numeric_title_feed = await runtime.repository.create_feed(
                "https://example.org/numeric-title", title="98765"
            )
            await runtime.services.subscriptions.subscribe(current_target.target_id, numeric_title_feed.feed_id)
            numeric_title_event = Event()
            await plugin._rss_unsub(numeric_title_event, "98765")
            assert numeric_title_event.sent == ["未找到订阅源，请用 /rss feed 查询数字 ID。"]
            assert await runtime.repository.get_feed(numeric_title_feed.feed_id) is not None
            assert any(
                row["feed_id"] == numeric_title_feed.feed_id
                for row in await runtime.services.subscriptions.mine(current_target.target_id)
            )

            unsub_event = Event()
            await plugin._rss_unsub(unsub_event, "HTTPS://EXAMPLE.ORG/rss#ignored")
            assert unsub_event.sent == ["已取消订阅。"]
            assert all(
                row["feed_id"] != feed.feed_id
                for row in await runtime.services.subscriptions.mine(current_target.target_id)
            )
            assert await runtime.services.subscriptions.mine(other_target.target_id)

            await runtime.services.subscriptions.subscribe(current_target.target_id, feed.feed_id)
            numeric_unsub_event = Event()
            await plugin._rss_unsub(numeric_unsub_event, str(public_id))
            assert numeric_unsub_event.sent == ["已取消订阅。"]
            assert all(
                row["feed_id"] != feed.feed_id
                for row in await runtime.services.subscriptions.mine(current_target.target_id)
            )
            assert await runtime.services.subscriptions.mine(other_target.target_id)

            empty_unsub_event = Event()
            await plugin._rss_unsub(empty_unsub_event, "")
            assert empty_unsub_event.sent == [
                "请提供源数字 ID、HTTP(S) 地址或 RSSHub 相对路径。用法：/rss unsub <源 ID、URL 或 /route>"
            ]
        finally:
            await runtime.close()

        failed_runtime = RSSRuntime({}, tmp_path / "failed", fetcher=Fetcher(fail=True), parser=Parser())
        try:
            await failed_runtime.initialize()
            failed_plugin = object.__new__(RSSPlugin)
            failed_plugin._rss_runtime = failed_runtime
            failed_plugin._rss_management = ManagementService(failed_runtime)
            failed_event = Event()
            await failed_plugin._rss_sub(failed_event, "https://example.org/rss")
            assert failed_event.sent == [
                "正在验证 RSS 地址并尝试订阅，请稍候…",
                "订阅失败：status error (403 Forbidden) | url=https://example.org/rss",
            ]
            assert await failed_runtime.repository.list_feeds() == []
            rows = await failed_runtime.database.fetchall("SELECT COUNT(*) AS n FROM direct_subscriptions")
            assert rows[0]["n"] == 0
        finally:
            await failed_runtime.close()

    import asyncio

    asyncio.run(run())


def test_rsshub_relative_route_command_stores_route_and_unsubscribes_without_fetch(tmp_path, monkeypatch):
    _install_astrbot_test_stubs(monkeypatch)
    import astrbot_plugin_rss.services.facade as facade_module
    from astrbot_plugin_rss.core.models import FetchResult, ParsedFeed
    from astrbot_plugin_rss.core.runtime import RSSRuntime
    from astrbot_plugin_rss.core.security import ValidatedURL
    from astrbot_plugin_rss.main import RSSPlugin

    checked = []

    async def allow_url(url):
        checked.append(url)
        return ValidatedURL(url, "rsshub.example", 443, ())

    monkeypatch.setattr(facade_module, "validate_public_url", allow_url)
    route = "/twitter/example"
    effective = "https://rsshub.example" + route + "?key=secret"

    class Fetcher:
        calls = 0

        async def fetch(self, feed, force=False):
            self.calls += 1
            return FetchResult(feed.feed_id, effective, effective, status_code=200, raw_xml=b"<rss/>")

    class Parser:
        def parse(self, _result):
            return ParsedFeed(title="RSSHub 示例源")

    class Event:
        unified_msg_origin = "test:private:chat"

        def __init__(self):
            self.sent = []

        def is_private_chat(self):
            return True

        def is_admin(self):
            return False

        def get_platform_id(self):
            return "test-instance"

        def get_platform_name(self):
            return "test"

        def get_session_id(self):
            return "private:chat"

        def get_message_type(self):
            return "FriendMessage"

        def plain_result(self, text):
            return text

        async def send(self, result):
            self.sent.append(result)

    async def run():
        fetcher = Fetcher()
        runtime = RSSRuntime(
            {"rsshub": {"default_url_prefix": "https://rsshub.example", "rsshub_access_key": "secret"}},
            tmp_path,
            fetcher=fetcher,
            parser=Parser(),
        )
        try:
            await runtime.initialize()
            plugin = object.__new__(RSSPlugin)
            plugin._rss_runtime = runtime
            sub_event = Event()
            await plugin._rss_sub(sub_event, route)
            assert sub_event.sent[0].startswith("正在验证")
            assert sub_event.sent[1].startswith("订阅成功！")
            feed = await runtime.repository.get_feed_by_url(route)
            assert feed is not None and feed.url == route
            assert checked == [effective]
            assert fetcher.calls == 1
            assert "secret" not in sub_event.sent[1]

            unsub_event = Event()
            await plugin._rss_unsub(unsub_event, route)
            assert unsub_event.sent == ["已取消订阅。"]
            assert fetcher.calls == 1
            assert await runtime.repository.get_feed_by_url(route) is not None
        finally:
            await runtime.close()

    import asyncio

    asyncio.run(run())


def test_plugin_initialize_closes_runtime_when_view_registration_fails(monkeypatch):
    class Group:
        def command(self, name, alias=None):
            return lambda handler: handler

    class Filter:
        def command_group(self, name, alias=None):
            return lambda _handler: Group()

    class Star:
        def __init__(self, context):
            self.context = context

    def register(*_args):
        return lambda plugin: plugin

    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.AstrBotConfig = dict
    event = types.ModuleType("astrbot.api.event")
    event.AstrMessageEvent = object
    event.filter = Filter()
    star = types.ModuleType("astrbot.api.star")
    star.Context = object
    star.Star = Star
    star.StarTools = types.SimpleNamespace(get_data_dir=lambda _name: "unused-test-data")
    star.register = register
    web = types.ModuleType("astrbot.api.web")
    web.PluginUploadFile = object
    web.request = types.SimpleNamespace()
    web.json_response = lambda value: value
    web.error_response = lambda value, status_code=400: value
    web.file_response = lambda *args, **kwargs: args
    api.web = web
    api.logger = types.SimpleNamespace(
        info=lambda *_: None,
        warning=lambda *_: None,
        error=lambda *_: None,
        exception=lambda *_: None,
    )
    astrbot.api = api
    for module in (astrbot, api, event, star, web):
        monkeypatch.setitem(sys.modules, module.__name__, module)
    sys.modules.pop("astrbot_plugin_rss.main", None)
    main = importlib.import_module("astrbot_plugin_rss.main")
    created = []

    class Runtime:
        def __init__(self, *_args):
            self.closed = False
            self.services = types.SimpleNamespace()
            created.append(self)

        async def initialize(self):
            return None

        async def start(self):
            return None

        async def close(self):
            self.closed = True
            raise RuntimeError("cleanup secret should not replace startup failure")

    class FailingWebAPI:
        def __init__(self, runtime):
            self.runtime = runtime
            self.management = None

        def register(self, _context):
            raise RuntimeError("view registration failed")

    monkeypatch.setattr("astrbot_plugin_rss.core.runtime.RSSRuntime", Runtime)
    monkeypatch.setattr(main, "ManagementService", lambda runtime: types.SimpleNamespace(runtime=runtime))
    monkeypatch.setattr(main, "WebAPI", FailingWebAPI)
    plugin = main.RSSPlugin(types.SimpleNamespace())

    async def run():
        raised = False
        try:
            await plugin.initialize()
        except RuntimeError as exc:
            raised = str(exc) == "view registration failed"
        assert raised
        assert created and created[0].closed
        assert plugin._rss_runtime is None
        assert plugin._rss_web is None
        assert plugin._rss_management is None

    asyncio.run(run())
