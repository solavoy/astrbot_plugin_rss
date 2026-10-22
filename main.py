from __future__ import annotations

from contextlib import aclosing

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

from .commands.rss import RSSCommands
from .core.config import migrate_legacy_config
from .services.management import ManagementService
from .web.api import WebAPI


@register("astrbot_plugin_rss", "solavoy", "RSS 订阅推送", "1.1.0")
class RSSPlugin(RSSCommands, Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self._rss_config = migrate_legacy_config(config)
        self._rss_runtime = None
        self._rss_web = None
        self._rss_management = None

    @filter.command_group("rss", alias={"/rss"})
    def rss(self):
        """RSS 订阅管理：订阅源、共享列表、当前会话订阅与调度状态。"""
        pass

    @rss.command("help")
    async def _rss_help(self, event: AstrMessageEvent):
        """查看 RSS 订阅命令和用法。"""
        await self._run_command(event, RSSCommands.rss_help(self, event))

    @rss.command("sub", alias={"订阅"})
    async def _rss_sub(self, event: AstrMessageEvent, url: str = ""):
        """通过稳定源 ID、公开 RSS/Atom 地址或 RSSHub 相对路径订阅当前会话。

        用法：
        - /rss sub <源 ID、公开 RSS/Atom 地址或 RSSHub 相对路径>
        - /rss 订阅 <源 ID、公开 RSS/Atom 地址或 RSSHub 相对路径>
        """
        await self._run_command(event, RSSCommands.rss_sub(self, event, url))

    @rss.command("unsub")
    async def _rss_unsub(self, event: AstrMessageEvent, feed_id: str = ""):
        """通过源数字 ID、HTTP(S) 地址或 RSSHub 相对路径取消当前会话的直接订阅。"""
        await self._run_command(event, RSSCommands.rss_unsub(self, event, feed_id))

    @rss.command("sublist")
    async def _rss_sublist(self, event: AstrMessageEvent, list_id: str = ""):
        """通过稳定列表 ID 或完整名称订阅共享列表。"""
        await self._run_command(event, RSSCommands.rss_sublist(self, event, list_id))

    @rss.command("unsublist")
    async def _rss_unsublist(self, event: AstrMessageEvent, list_id: str = ""):
        """通过稳定列表 ID 或完整名称取消当前会话的共享列表订阅。"""
        await self._run_command(event, RSSCommands.rss_unsublist(self, event, list_id))

    @rss.command("list")
    async def _rss_list(self, event: AstrMessageEvent):
        """查看共享订阅列表及其稳定 ID。"""
        await self._run_command(event, RSSCommands.rss_list(self, event))

    @rss.command("feed")
    async def _rss_feed(self, event: AstrMessageEvent):
        """查看订阅源及其稳定 ID。"""
        await self._run_command(event, RSSCommands.rss_feed(self, event))

    @rss.command("mine")
    async def _rss_mine(self, event: AstrMessageEvent):
        """列出当前会话的直接订阅和共享列表订阅。"""
        await self._run_command(event, RSSCommands.rss_mine(self, event))

    @rss.command("status")
    async def _rss_status(self, event: AstrMessageEvent):
        """查看抓取调度器当前状态。"""
        await self._run_command(event, RSSCommands.rss_status(self, event))

    async def _run_command(self, event: AstrMessageEvent, operation):
        async with aclosing(operation):
            async for result in operation:
                await event.send(result)

    async def initialize(self):
        from .core.runtime import RSSRuntime

        data_dir = StarTools.get_data_dir("astrbot_plugin_rss")
        runtime = RSSRuntime(self._rss_config, data_dir, self.context)
        try:
            await runtime.initialize()
            await runtime.start()
            management = ManagementService(runtime)
            web = WebAPI(runtime)
            web.management = management
            web.management.on_runtime_replaced = self._replace_runtime
            web.register(self.context)
        except BaseException as initialize_error:
            self._rss_runtime = None
            self._rss_management = None
            self._rss_web = None
            try:
                await runtime.close()
            except BaseException as close_error:
                initialize_error.add_note(f"Runtime cleanup also failed ({type(close_error).__name__})")
            raise
        self._rss_runtime = runtime
        self._rss_management = management
        self._rss_web = web
        logger.info("RSS 订阅推送运行时已启动 version=1.1.0；可用命令 /rss help")

    async def terminate(self):
        runtime = self._rss_runtime
        self._rss_runtime = None
        self._rss_management = None
        self._rss_web = None
        if runtime is not None:
            await runtime.close()

    def _replace_runtime(self, runtime):
        self._rss_runtime = runtime
        self._rss_management = self._rss_web.management
        if self._rss_web is not None:
            self._rss_web.runtime = runtime
            self._rss_web.management.runtime = runtime
            self._rss_web.management.services = runtime.services
