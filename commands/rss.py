from __future__ import annotations

import re
from http import HTTPStatus

from astrbot.api.event import AstrMessageEvent

from ..core.feed_urls import is_relative_feed_reference, normalize_relative_feed_reference
from ..core.security import validate_public_url


class RSSCommands:
    @staticmethod
    def _not_ready(event):
        return event.plain_result("RSS 插件尚未就绪，请稍后重试或联系管理员检查插件状态。")

    @staticmethod
    def _group_admin_denied(event, runtime) -> bool:
        if event.is_private_chat():
            return False
        config = getattr(runtime, "config", None)
        if not getattr(config, "group_admin_only", False):
            return False
        return not event.is_admin()

    @staticmethod
    def _raw_argument(event, command: str, fallback: str, aliases: tuple[str, ...] = ()) -> str:
        """Read the complete command tail so list names may contain spaces."""
        getter = getattr(event, "get_message_str", None)
        message = getter() if callable(getter) else getattr(event, "message_str", "")
        if not isinstance(message, str):
            return fallback.strip()
        commands = "|".join(re.escape(name) for name in (command, *aliases))
        pattern = rf"(?:^|\s)(?:/)?rss(?:@\S+)?\s+(?:{commands})(?:\s+|$)(.*)$"
        match = re.search(pattern, message.strip(), re.IGNORECASE)
        return (match.group(1) if match else fallback).strip()

    @staticmethod
    def _not_found(ref: str, entity: str) -> str:
        if re.fullmatch(r"[0-9a-fA-F-]{32,36}", ref):
            return f"未找到{entity}；命令已改用稳定数字 ID，旧 UUID 不再支持。"
        return f"未找到{entity}，请用 /rss {'feed' if entity == '订阅源' else 'list'} 查询可用 ID。"

    async def rss_help(self, event: AstrMessageEvent):
        yield event.plain_result(
            "订阅命令：\n"
            "/rss sub <源 ID、公开 RSS/Atom 地址或 RSSHub 相对路径>\n"
            "/rss unsub <源数字 ID、HTTP(S) 地址或 RSSHub 相对路径>\n"
            "/rss sublist <列表 ID 或列表名称>\n"
            "/rss unsublist <列表 ID 或列表名称>\n"
            "/rss list：查看订阅列表\n"
            "/rss feed：查看订阅源\n"
            "/rss mine：查看当前会话订阅\n"
            "/rss status：查看调度状态\n"
            "/rss help：查看帮助"
        )

    async def rss_sub(self, event: AstrMessageEvent, url: str):
        ref = self._raw_argument(event, "sub", url, aliases=("订阅",))
        if not ref:
            yield event.plain_result(
                "请提供源 ID、公开 RSS/Atom 地址或 RSSHub 相对路径。用法：/rss sub <源 ID、地址或 /route>"
            )
            return
        runtime = getattr(self, "_rss_runtime", None)
        if runtime is None or getattr(runtime, "services", None) is None:
            yield self._not_ready(event)
            return
        if self._group_admin_denied(event, runtime):
            yield event.plain_result("群内订阅操作仅限 AstrBot 管理员。")
            return
        if is_relative_feed_reference(ref):
            try:
                ref = normalize_relative_feed_reference(ref)
            except ValueError as exc:
                # Do not echo an invalid route: its query may contain a private key.
                yield event.plain_result(f"订阅失败：{exc}")
                return
        try:
            target = await runtime.services.targets.from_event(event)
            try:
                feed = await runtime.services.feeds.resolve(ref)
            except KeyError:
                if not ref.lower().startswith(("http://", "https://")) and not is_relative_feed_reference(ref):
                    raise
                yield event.plain_result("正在验证 RSS 地址并尝试订阅，请稍候…")
                feed = await runtime.services.feeds.create(ref)
            await runtime.services.subscriptions.subscribe(target.target_id, feed.feed_id)
            yield event.plain_result(
                "订阅成功！\n"
                f"源标题：{feed.title or '（未提供标题）'}\n"
                f"订阅 ID: {feed.public_id}\n"
                f"推送目标：{target.unified_origin}"
            )
        except Exception as exc:
            status_code = getattr(exc, "status_code", None)
            if status_code:
                try:
                    phrase = HTTPStatus(status_code).phrase
                except ValueError:
                    phrase = "Unknown Status"
                reason = f"status error ({status_code} {phrase})"
            else:
                reason = str(exc).replace("\n", " ").strip()[:140] or type(exc).__name__
            if isinstance(exc, KeyError):
                yield event.plain_result(f"订阅失败：{self._not_found(ref, '订阅源')}")
                return
            suffix = (
                f" | url={ref}"
                if ref.lower().startswith(("http://", "https://")) or is_relative_feed_reference(ref)
                else ""
            )
            yield event.plain_result(f"订阅失败：{reason}{suffix}")

    async def rss_unsub(self, event: AstrMessageEvent, feed_id: str):
        ref = self._raw_argument(event, "unsub", feed_id)
        if not ref:
            yield event.plain_result(
                "请提供源数字 ID、HTTP(S) 地址或 RSSHub 相对路径。用法：/rss unsub <源 ID、URL 或 /route>"
            )
            return
        is_numeric_id = bool(re.fullmatch(r"[0-9]{1,19}", ref)) and 0 < int(ref) <= 9_223_372_036_854_775_807
        is_url = ref.lower().startswith(("http://", "https://"))
        is_route = is_relative_feed_reference(ref)
        if not is_numeric_id and not is_url and not is_route:
            yield event.plain_result(
                "取消失败：仅支持源数字 ID、HTTP(S) 地址或 RSSHub 相对路径，不支持源名称或旧 UUID。"
            )
            return
        if is_route:
            try:
                ref = normalize_relative_feed_reference(ref)
            except ValueError as exc:
                yield event.plain_result(f"取消失败：{exc}")
                return
        runtime = getattr(self, "_rss_runtime", None)
        if runtime is None or getattr(runtime, "services", None) is None:
            yield self._not_ready(event)
            return
        if self._group_admin_denied(event, runtime):
            yield event.plain_result("群内订阅操作仅限 AstrBot 管理员。")
            return
        try:
            target = await runtime.services.targets.from_event(event)
            if is_route:
                feed = await runtime.repository.get_feed_by_url(ref)
                if feed is None:
                    raise KeyError(ref)
            elif is_url:
                # Reuse the feed URL security/canonicalization rules without DNS or fetching.
                ref = (await validate_public_url(ref, resolve_dns=False)).url
                feed = await runtime.repository.get_feed_by_url(ref)
                if feed is None:
                    raise KeyError(ref)
            else:
                feed = await runtime.repository.get_feed_by_public_id(int(ref))
                if feed is None:
                    raise KeyError(ref)
            removed = await runtime.services.subscriptions.unsubscribe(target.target_id, feed.feed_id)
            yield event.plain_result("已取消订阅。" if removed else "当前目标没有这条直接订阅。")
        except KeyError:
            yield event.plain_result(f"未找到订阅源，请用 /rss feed 查询数字 ID。{f' | url={ref}' if is_url else ''}")
        except Exception as exc:
            yield event.plain_result(f"取消失败：{str(exc)[:180] or type(exc).__name__}")

    async def rss_sublist(self, event: AstrMessageEvent, list_id: str):
        ref = self._raw_argument(event, "sublist", list_id)
        if not ref:
            yield event.plain_result("请提供列表 ID 或完整列表名称。用法：/rss sublist <列表 ID 或列表名称>")
            return
        runtime = getattr(self, "_rss_runtime", None)
        if runtime is None or getattr(runtime, "services", None) is None:
            yield self._not_ready(event)
            return
        if self._group_admin_denied(event, runtime):
            yield event.plain_result("群内订阅操作仅限 AstrBot 管理员。")
            return
        try:
            target = await runtime.services.targets.from_event(event)
            listing = await runtime.services.lists.resolve(ref)
            await runtime.services.lists.subscribe(target.target_id, listing["list_id"])
            yield event.plain_result(f"已订阅列表：{listing['name']}（ID：{listing['public_id']}）")
        except KeyError:
            yield event.plain_result(self._not_found(ref, "列表"))
        except Exception as exc:
            yield event.plain_result(f"操作失败：{str(exc)[:180]}")

    async def rss_unsublist(self, event: AstrMessageEvent, list_id: str):
        ref = self._raw_argument(event, "unsublist", list_id)
        if not ref:
            yield event.plain_result("请提供列表 ID 或完整列表名称。用法：/rss unsublist <列表 ID 或列表名称>")
            return
        runtime = getattr(self, "_rss_runtime", None)
        if runtime is None or getattr(runtime, "services", None) is None:
            yield self._not_ready(event)
            return
        if self._group_admin_denied(event, runtime):
            yield event.plain_result("群内订阅操作仅限 AstrBot 管理员。")
            return
        try:
            target = await runtime.services.targets.from_event(event)
            listing = await runtime.services.lists.resolve(ref)
            removed = await runtime.services.lists.unsubscribe(target.target_id, listing["list_id"])
            yield event.plain_result(
                f"已取消列表订阅：{listing['name']}（ID：{listing['public_id']}）"
                if removed
                else "当前目标没有该列表订阅。"
            )
        except KeyError:
            yield event.plain_result(self._not_found(ref, "列表"))
        except Exception as exc:
            yield event.plain_result(f"操作失败：{str(exc)[:180]}")

    async def rss_list(self, event: AstrMessageEvent):
        runtime = getattr(self, "_rss_runtime", None)
        if runtime is None or getattr(runtime, "services", None) is None:
            yield self._not_ready(event)
            return
        try:
            rows = sorted(await runtime.services.lists.list(), key=lambda row: row["public_id"])
            body = "\n".join(f"{row['public_id']}  {row['name']}" for row in rows) or "（暂无订阅列表）"
            yield event.plain_result(f"ID  列表名称\n{body}")
        except Exception as exc:
            yield event.plain_result(f"查询失败：{str(exc)[:180]}")

    async def rss_feed(self, event: AstrMessageEvent):
        runtime = getattr(self, "_rss_runtime", None)
        if runtime is None or getattr(runtime, "services", None) is None:
            yield self._not_ready(event)
            return
        try:
            rows = sorted(await runtime.services.feeds.list(), key=lambda row: row["public_id"])
            body = (
                "\n".join(f"{row['public_id']}  {row['title'] or '（未提供标题）'}" for row in rows) or "（暂无订阅源）"
            )
            yield event.plain_result(f"ID  feed名称\n{body}")
        except Exception as exc:
            yield event.plain_result(f"查询失败：{str(exc)[:180]}")

    async def rss_mine(self, event: AstrMessageEvent):
        runtime = getattr(self, "_rss_runtime", None)
        management = getattr(self, "_rss_management", None)
        if runtime is None or getattr(runtime, "services", None) is None or management is None:
            yield self._not_ready(event)
            return
        try:
            target = await runtime.services.targets.from_event(event)
            rows = await management.mine(target.target_id)
            yield event.plain_result("\n".join(rows) or "当前没有订阅。")
        except Exception as exc:
            yield event.plain_result(f"查询失败：{str(exc)[:180]}")

    async def rss_status(self, event: AstrMessageEvent):
        runtime = getattr(self, "_rss_runtime", None)
        if runtime is None or getattr(runtime, "services", None) is None:
            yield self._not_ready(event)
            return
        try:
            status = await runtime.scheduler_status()
            yield event.plain_result(
                f"调度：{'暂停' if status['paused'] else '运行'}；待检查 Feed：{len(status['due_feed_ids'])}"
            )
        except Exception as exc:
            yield event.plain_result(f"状态查询失败：{str(exc)[:180]}")
