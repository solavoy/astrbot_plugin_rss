from __future__ import annotations

import asyncio
import logging
import math
import time
import uuid
from collections import OrderedDict, deque
from dataclasses import replace
from pathlib import Path

from ..storage import Database, Repository
from .config import PluginConfig, normalize_translation_prompt
from .models import Article, DeliveryTarget, RenderedMessage, now_ts

logger = logging.getLogger(__name__)


class RSSRuntime:
    """Owns persistence, network resources, scheduling and the shared service facade."""

    def __init__(self, config, data_dir, context=None, fetcher=None, parser=None, registry=None, sender=None):
        self.config = config if isinstance(config, PluginConfig) else PluginConfig.from_mapping(config)
        self.data_dir = Path(data_dir)
        self.context = context
        self.fetcher = fetcher
        self.parser = parser
        self.registry = registry
        self.sender = sender
        self.media_downloader = None
        self.database = Database(self.data_dir / "rss.sqlite3", self.config.database_busy_timeout_ms)
        self.repository = Repository(self.database)
        self.services = None
        self._tasks: set[asyncio.Task] = set()
        self._stop = asyncio.Event()
        self._initialized = False
        self._started = False
        self._send_times: dict[str, deque[float]] = {}
        self._send_rate_lock = asyncio.Lock()
        self._feed_refresh_locks: dict[str, asyncio.Lock] = {}
        self._feed_refresh_generations: dict[str, int] = {}
        self._translation_cache: OrderedDict[tuple, object] = OrderedDict()
        self._translation_tasks: dict[tuple, asyncio.Task] = {}

    async def _articles_for_render(
        self, articles: list[Article], *, diagnostics: list[dict] | None = None
    ) -> list[Article]:
        """Return translated Article copies for renderers, leaving stored Article data untouched."""
        provider_id = self.config.translation_provider_id
        if not provider_id and self.database.connection is None:
            if diagnostics is not None:
                diagnostics.extend(
                    {
                        "article_id": article.article_id,
                        "feed_id": article.feed_id,
                        "status": "provider_missing",
                        "reason": "provider_not_configured",
                        "segments_total": 0,
                        "segments_translated": 0,
                        "segments_skipped": 0,
                        "segments_unchanged": 0,
                        "segments_failed": 0,
                        "reasons": ["provider_not_configured"],
                    }
                    for article in articles
                )
            return articles
        feed_translation_enabled = {}
        feeds = {}
        for article in articles:
            if article.feed_id not in feed_translation_enabled:
                feed = await self.repository.get_feed(article.feed_id)
                feeds[article.feed_id] = feed
                feed_translation_enabled[article.feed_id] = bool(
                    feed is not None and getattr(feed, "translation_enabled", False)
                )
        translation_prompt = normalize_translation_prompt(self.config.translation_prompt)
        translator = None
        if provider_id:
            from ..services.translation import TranslationService

            translator = TranslationService(
                self.context,
                provider_id,
                prompt=translation_prompt,
                timeout_seconds=self.config.translation_timeout_seconds,
                total_timeout_seconds=self.config.translation_total_timeout_seconds,
                max_segment_chars=self.config.translation_max_segment_chars,
                max_requests=self.config.translation_max_requests,
            )
        rendered_articles = []
        for article in articles:
            if not feed_translation_enabled.get(article.feed_id, False):
                rendered_articles.append(article)
                if diagnostics is not None:
                    diagnostics.append(
                        {
                            "article_id": article.article_id,
                            "feed_id": article.feed_id,
                            "status": "disabled",
                            "reason": "feed_unavailable"
                            if feeds.get(article.feed_id) is None
                            else "feed_translation_disabled",
                            "segments_total": 0,
                            "segments_translated": 0,
                            "segments_skipped": 0,
                            "segments_unchanged": 0,
                            "segments_failed": 0,
                            "reasons": [],
                        }
                    )
                continue
            if not provider_id:
                rendered_articles.append(article)
                if diagnostics is not None:
                    diagnostics.append(
                        {
                            "article_id": article.article_id,
                            "feed_id": article.feed_id,
                            "status": "provider_missing",
                            "reason": "provider_not_configured",
                            "segments_total": 0,
                            "segments_translated": 0,
                            "segments_skipped": 0,
                            "segments_unchanged": 0,
                            "segments_failed": 0,
                            "reasons": ["provider_not_configured"],
                        }
                    )
                continue
            key = (
                provider_id,
                self.config.timezone,
                self.config.translation_include_original,
                article.article_id,
                f"{article.content_hash}:{article.title}",
                translation_prompt,
                self.config.translation_timeout_seconds,
                self.config.translation_total_timeout_seconds,
                self.config.translation_max_segment_chars,
                self.config.translation_max_requests,
            )
            cached = self._translation_cache.get(key)
            if cached is not None:
                self._translation_cache.move_to_end(key)
                outcome = cached
            else:
                task = self._translation_tasks.get(key)
                if task is None:
                    task = asyncio.create_task(
                        translator.translate_article(article, include_original=self.config.translation_include_original)
                    )
                    self._translation_tasks[key] = task

                    def forget_translation(completed, *, translation_key=key):
                        if self._translation_tasks.get(translation_key) is completed:
                            self._translation_tasks.pop(translation_key, None)
                        if not completed.cancelled():
                            completed.exception()

                    task.add_done_callback(forget_translation)
                try:
                    outcome = await asyncio.shield(task)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    from ..services.translation import TranslationOutcome

                    logger.warning("AI 翻译任务失败，保留原文（%s）", type(exc).__name__)
                    outcome = TranslationOutcome(article, False, "failed", "translation_task_failed")
                if outcome.succeeded and outcome.status in {"translated", "skipped"}:
                    self._translation_cache[key] = outcome
                    self._translation_cache.move_to_end(key)
                    while len(self._translation_cache) > 256:
                        self._translation_cache.popitem(last=False)
            rendered_articles.append(
                outcome.article
                if outcome.article is article
                else replace(article, title=outcome.article.title, content_blocks=outcome.article.content_blocks)
            )
            if diagnostics is not None:
                diagnostics.append(
                    {
                        "article_id": article.article_id,
                        "feed_id": article.feed_id,
                        **outcome.diagnostic(),
                    }
                )
        return rendered_articles

    def feed_refresh_lock(self, feed_id: str) -> asyncio.Lock:
        """Return the shared refresh lock for one feed."""
        return self._feed_refresh_locks.setdefault(feed_id, asyncio.Lock())

    def feed_refresh_generation(self, feed_id: str) -> int:
        """Return the in-process generation of completed refresh attempts."""
        return self._feed_refresh_generations.get(feed_id, 0)

    def feed_refresh_generations_snapshot(self) -> dict[str, int]:
        return self._feed_refresh_generations.copy()

    def bump_feed_refresh_generation(self, feed_id: str) -> None:
        self._feed_refresh_generations[feed_id] = self.feed_refresh_generation(feed_id) + 1

    async def initialize(self) -> None:
        if self._initialized:
            return
        try:
            await self._initialize()
        except BaseException as initialize_error:
            try:
                await self.close()
            except BaseException as close_error:
                initialize_error.add_note(f"Runtime cleanup also failed ({type(close_error).__name__})")
            raise

    async def _initialize(self) -> None:
        if self._initialized:
            return
        self.data_dir.mkdir(parents=True, exist_ok=True)
        await self.database.initialize()
        if self.fetcher is None:
            from ..fetchers import FeedFetcher

            self.fetcher = FeedFetcher(self.config, self.data_dir / "fetch")
        if self.parser is None:
            from ..parsers import FeedParser

            self.parser = FeedParser()
        from ..media import MediaDownloader

        self.media_downloader = MediaDownloader(self.data_dir, self.config)
        if self.registry is None:
            try:
                from ..renderers import RendererRegistry

                self.registry = RendererRegistry(
                    self.config,
                    repository=self.repository,
                    data_dir=self.data_dir,
                    context=self.context,
                    media_downloader=self.media_downloader,
                )
            except ImportError:
                self.registry = None
        if self.sender is None:
            try:
                from ..senders import PlatformSender

                self.sender = PlatformSender(self.context, self.config)
            except (ImportError, AttributeError):
                self.sender = None
        async with self.database.transaction() as conn:
            await conn.execute(
                "UPDATE deliveries SET status='pending',attempts=MAX(attempts-1,0),updated_at=? WHERE status='rendering'",
                (now_ts(),),
            )
            await conn.execute(
                "UPDATE deliveries SET status='unknown',error='process stopped while send result was pending',updated_at=? WHERE status='sending'",
                (now_ts(),),
            )
            await conn.execute(
                "UPDATE delivery_batches SET status='unknown',error='process stopped while send result was pending',updated_at=? WHERE status='sending'",
                (now_ts(),),
            )
            await conn.execute(
                "UPDATE test_operations SET status='unknown',error='process stopped while send result was pending',updated_at=? WHERE status='pending' AND operation_id IN (SELECT substr(group_id,6) FROM delivery_batches WHERE group_id LIKE 'test:%' AND status='unknown')",
                (now_ts(),),
            )
        from ..services import ServiceFacade

        self.services = ServiceFacade(self)
        self._initialized = True

    async def start(self) -> None:
        if not self._initialized:
            await self.initialize()
        if self._started:
            return
        self._started = True
        self._stop.clear()
        self._spawn(self._maintenance_loop(), "rss-maintenance")
        if self.config.enable_scheduler:
            self._spawn(self._scheduler_loop(), "rss-scheduler")
        if self.registry is not None and self.sender is not None:
            self._spawn(self._delivery_loop(), "rss-delivery")

    async def pause(self) -> None:
        await self.repository.set_setting("paused", "1")

    async def resume(self) -> None:
        await self.repository.set_setting("paused", "0")

    async def scheduler_status(self) -> dict:
        due = await self.repository.due_feeds(now_ts(), self.config.global_poll_interval, self.config.min_poll_interval)
        return {
            "enabled": self.config.enable_scheduler,
            "running": self._started and self.config.enable_scheduler,
            "paused": await self.repository.get_setting("paused", "0") == "1",
            "due_feed_ids": [feed["feed_id"] for feed in due],
            "database": await self.repository.status(),
        }

    async def check_now(self, force: bool = False) -> dict[str, int]:
        if not self._initialized:
            await self.initialize()
        from ..scheduler import FeedScheduler

        return await FeedScheduler(self).run_once(force=force)

    async def run_maintenance_once(self) -> dict:
        """Run the same idempotent cleanup used by the background maintenance task."""
        cookie_manager = getattr(self.fetcher, "cookies", None)
        cookies = cookie_manager.clear_expired() if cookie_manager else 0
        media = self.media_downloader.cleanup() if self.media_downloader else 0
        platform_media = await self.repository.cleanup_platform_media_cache()
        history = await self.cleanup("all")
        return {"cookies": cookies, "media": media, "platform_media": platform_media, **history}

    async def _maintenance_loop(self) -> None:
        while not self._stop.is_set():
            stage = "run_maintenance_once"
            try:
                await self.run_maintenance_once()
            except Exception as exc:
                # A maintenance failure must not terminate scheduling or delivery.
                logger.warning("RSS maintenance failed during %s (%s)", stage, type(exc).__name__)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.config.cookie_cleanup_interval)
            except TimeoutError:
                continue

    def _spawn(self, coroutine, name: str) -> None:
        task = asyncio.create_task(coroutine, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def close(self) -> None:
        self._stop.set()
        tasks, self._tasks = list(self._tasks), set()
        for task in tasks:
            task.cancel()
        cleanup_errors: list[Exception] = []
        cancelled_error: asyncio.CancelledError | None = None
        if tasks:
            try:
                await asyncio.gather(*tasks, return_exceptions=True)
            except asyncio.CancelledError as exc:
                cancelled_error = exc
            except Exception as exc:
                cleanup_errors.append(exc)
        translation_tasks = list(set(self._translation_tasks.values()))
        self._translation_tasks.clear()
        self._translation_cache.clear()
        for task in translation_tasks:
            task.cancel()
        if translation_tasks:
            try:
                await asyncio.gather(*translation_tasks, return_exceptions=True)
            except asyncio.CancelledError as exc:
                if cancelled_error is None:
                    cancelled_error = exc
            except Exception as exc:
                cleanup_errors.append(exc)
        try:
            for resource in (self.fetcher, self.registry, self.sender, self.media_downloader, self.database):
                close = getattr(resource, "close", None)
                if close is None:
                    continue
                try:
                    result = close()
                    if asyncio.iscoroutine(result):
                        await result
                except asyncio.CancelledError as exc:
                    if cancelled_error is None:
                        cancelled_error = exc
                except Exception as exc:
                    cleanup_errors.append(exc)
        finally:
            self._started = False
            self._initialized = False
        if cancelled_error is not None:
            other_errors = [type(error).__name__ for error in cleanup_errors]
            if other_errors:
                cancelled_error.add_note(f"Other runtime cleanup failures ({', '.join(other_errors)})")
            raise cancelled_error
        if cleanup_errors:
            kinds = ", ".join(type(error).__name__ for error in cleanup_errors)
            raise RuntimeError(f"runtime cleanup failed ({kinds})") from None

    async def _respect_send_rate_limit(self, platform_instance: str) -> None:
        limit = self.config.delivery_rate_limit
        while True:
            async with self._send_rate_lock:
                send_times = self._send_times.setdefault(platform_instance, deque())
                now = time.monotonic()
                while send_times and now - send_times[0] >= 60:
                    send_times.popleft()
                if len(send_times) < limit:
                    send_times.append(now)
                    return
                delay = max(0.01, 60 - (now - send_times[0]))
            await asyncio.sleep(delay)

    async def _preflight_rendered(self, articles, target, rendered, *, source_name, is_list, batch_id):
        """Preflight the complete unsent group; only known unsupported capability may fallback."""
        from .errors import PlatformUnsupportedError

        preflight = getattr(self.sender, "preflight", None)
        if preflight is None:
            return rendered
        try:
            for message in rendered:
                await preflight(target, message)
            return rendered
        except PlatformUnsupportedError:
            default_renderer = getattr(self.registry, "default", None)
            if not self.config.enable_default_renderer or default_renderer is None:
                raise
            fallback = await default_renderer.render(
                articles, target, source_name=source_name, is_list=is_list, batch_id=batch_id
            )
            if not fallback:
                raise RuntimeError("default renderer produced no messages")
            for message in fallback:
                await preflight(target, message)
            return fallback

    async def test_push(self, feed_id: str, target_id: str) -> dict:
        """Send a separately tracked manual test operation, outside normal deduplication."""
        if not self._initialized:
            await self.initialize()
        if self.registry is None or self.sender is None:
            raise RuntimeError("rendering and sending integrations are unavailable")
        feed = await self.repository.get_feed(feed_id)
        target = await self.repository.get_target(target_id)
        if feed is None or target is None:
            raise KeyError("feed or target not found")
        articles = await self.list_feed_articles(feed_id, 1)
        if not articles:
            raise ValueError("feed has no stored articles to test")
        operation_id = str(uuid.uuid4())
        await self.repository.create_test_operation(operation_id, target_id, feed_id, [articles[0].article_id])
        await self._run_test_operation(operation_id)
        return await self.repository.get_test_operation(operation_id)

    async def retry_test_push(self, operation_id: str) -> dict:
        operation = await self.repository.get_test_operation(operation_id)
        if operation is None:
            raise KeyError(operation_id)
        if operation["status"] != "failed":
            raise ValueError("only clearly failed test operations can be retried")
        await self.repository.update_test_operation(operation_id, "pending")
        await self._run_test_operation(operation_id)
        return await self.repository.get_test_operation(operation_id)

    async def _run_test_operation(self, operation_id: str) -> None:
        operation = await self.repository.get_test_operation(operation_id)
        target = await self.repository.get_test_target(operation_id)
        if operation is None or target is None:
            raise KeyError(operation_id)
        group_id = f"test:{operation_id}"
        cards = await self.repository.batch_cards(group_id)
        if not cards:
            articles = [await self.repository.article(aid) for aid in operation["article_ids"]]
            articles = [article for article in articles if article is not None]
            feed = await self.repository.get_feed(operation["feed_id"])
            try:
                translation_diagnostics = []
                articles = await self._articles_for_render(articles, diagnostics=translation_diagnostics)
                await self.repository.set_test_operation_translation_diagnostics(operation_id, translation_diagnostics)
                rendered = await self.registry.render(
                    articles, target, source_name=feed.title or feed.url, is_list=False, batch_id=group_id
                )
            except Exception as exc:
                await self.repository.update_test_operation(operation_id, "failed", str(exc))
                return
            if not rendered:
                await self.repository.update_test_operation(operation_id, "failed", "renderer produced no messages")
                return
            try:
                rendered = await self._preflight_rendered(
                    articles, target, rendered, source_name=feed.title or feed.url, is_list=False, batch_id=group_id
                )
            except Exception as exc:
                await self.repository.update_test_operation(operation_id, "failed", str(exc))
                return
            await self.repository.create_batch_cards(
                [
                    {
                        "group_id": group_id,
                        "batch_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{group_id}:{index}")),
                        "target_id": target.target_id,
                        "card_index": index,
                        "card_count": len(rendered),
                        "payload": {
                            "renderer": message.renderer,
                            "message_type": message.message_type,
                            "payload": message.payload,
                            "article_ids": message.article_ids,
                            "batch_id": message.batch_id,
                        },
                        "idempotency_key": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{group_id}:{index}")),
                        "delivery_ids": [],
                    }
                    for index, message in enumerate(rendered)
                ]
            )
            cards = await self.repository.batch_cards(group_id)
        for card in cards:
            if card["status"] == "success":
                continue
            if card["status"] == "unknown":
                await self.repository.update_test_operation(operation_id, "unknown", card.get("error"))
                return
            payload = card["payload"]
            message = RenderedMessage(
                payload["renderer"],
                payload["message_type"],
                payload["payload"],
                payload["article_ids"],
                payload.get("batch_id", group_id),
            )
            await self._respect_send_rate_limit(target.platform_instance)
            await self.repository.set_batch_card_status(card["batch_id"], "sending")
            try:
                result = await self.sender.send(target, message, idempotency_key=card["idempotency_key"])
            except Exception as exc:
                from .errors import PermanentSendError, RetryableSendError

                status = "failed" if isinstance(exc, (PermanentSendError, RetryableSendError)) else "unknown"
                await self.repository.set_batch_card_status(card["batch_id"], status, error=str(exc)[:500])
                await self.repository.update_test_operation(operation_id, status, str(exc))
                return
            await self.repository.set_batch_card_status(
                card["batch_id"], "success", platform_message_id=result.platform_message_id
            )
        await self.repository.update_test_operation(operation_id, "success")

    async def refresh_feed(self, feed_id: str):
        generation = self.feed_refresh_generation(feed_id)
        async with self.feed_refresh_lock(feed_id):
            return await self._refresh_feed_locked(feed_id, expected_generation=generation)

    async def _refresh_feed_locked(self, feed_id: str, *, expected_generation: int | None = None):
        feed = await self.repository.get_feed(feed_id)
        if feed is None:
            raise KeyError(feed_id)
        now = now_ts()
        retry_deadline = await self.repository.feed_retry_deadline(feed_id)
        if retry_deadline is not None and retry_deadline > now:
            remaining = max(1, math.ceil(retry_deadline - now))
            raise ValueError(f"订阅源因 HTTP 429 处于冷却期，请在约 {remaining} 秒后重试")
        if expected_generation is not None and self.feed_refresh_generation(feed_id) != expected_generation:
            return {"feed_id": feed_id, "status": "skipped", "reason": "concurrent_refresh"}
        interval = await self.repository.required_fetch_interval(
            feed_id, self.config.global_poll_interval, self.config.min_poll_interval
        )
        try:
            result = await self.fetcher.fetch(feed, force=True)
            if result.error_type:
                await self.repository.log_fetch(
                    feed_id,
                    result.fetched_at,
                    result.status_code,
                    result.fetch_method,
                    result.elapsed_ms,
                    result.error_type,
                    result.error_message,
                    result.cookie_cache_hit,
                    result.browser_attempted,
                )
                updated_at = now_ts()
                next_fetch_at = updated_at + max(self.config.min_poll_interval, interval)
                if result.status_code == 429 or result.error_type == "http_429":
                    retry_after_at = getattr(result, "retry_after_at", None)
                    if (
                        isinstance(retry_after_at, bool)
                        or not isinstance(retry_after_at, (int, float))
                        or not math.isfinite(retry_after_at)
                    ):
                        retry_after_at = updated_at + 60
                    next_fetch_at = max(next_fetch_at, retry_after_at)
                await self.repository.update_fetch_state(
                    feed_id,
                    etag=feed.etag,
                    last_modified=feed.last_modified,
                    initialized=feed.initialized,
                    next_fetch_at=next_fetch_at,
                    status=result.error_type,
                )
                return {"feed_id": feed_id, "status": "error", "error": result.error_message or result.error_type}
            return await self._persist_fetch(feed, result, interval)
        finally:
            self.bump_feed_refresh_generation(feed_id)

    async def _persist_fetch(self, feed, result, interval: int | None = None):
        next_fetch = now_ts() + max(self.config.min_poll_interval, interval or self.config.global_poll_interval)
        if result.status_code == 304:
            await self.repository.log_fetch(
                feed.feed_id,
                result.fetched_at,
                result.status_code,
                result.fetch_method,
                result.elapsed_ms,
                result.error_type,
                result.error_message,
                result.cookie_cache_hit,
                result.browser_attempted,
            )
            await self.repository.update_fetch_state(
                feed.feed_id,
                etag=result.etag or feed.etag,
                last_modified=result.last_modified or feed.last_modified,
                initialized=feed.initialized,
                next_fetch_at=next_fetch,
            )
            await self._persist_request_url_fingerprint(feed.feed_id, result.request_url_fingerprint)
            return {"feed_id": feed.feed_id, "status": "not_modified", "new_articles": 0}
        try:
            parsed = self.parser.parse(result)
        except Exception as exc:
            error_message = f"feed parsing failed: {type(exc).__name__}"
            await self.repository.log_fetch(
                feed.feed_id,
                result.fetched_at,
                result.status_code,
                result.fetch_method,
                result.elapsed_ms,
                "parse_error",
                error_message,
                result.cookie_cache_hit,
                result.browser_attempted,
            )
            await self.repository.update_fetch_state(
                feed.feed_id,
                etag=feed.etag,
                last_modified=feed.last_modified,
                initialized=feed.initialized,
                next_fetch_at=now_ts() + min(300, self.config.global_poll_interval),
                status="parse_error",
            )
            return {"feed_id": feed.feed_id, "status": "error", "error": error_message}
        await self.repository.log_fetch(
            feed.feed_id,
            result.fetched_at,
            result.status_code,
            result.fetch_method,
            result.elapsed_ms,
            result.error_type,
            result.error_message,
            result.cookie_cache_hit,
            result.browser_attempted,
        )
        if parsed.title or parsed.description:
            await self.repository.update_feed(
                feed.feed_id, title=parsed.title or feed.title, description=parsed.description or feed.description
            )
        inserted, maximum = await self.repository.save_articles(feed.feed_id, parsed.articles)
        # First successful fetch establishes an article-sequence baseline unless history was requested.
        if not feed.initialized and not self.config.first_subscription_send_history:
            async with self.database.transaction() as conn:
                await conn.execute(
                    "UPDATE direct_subscriptions SET baseline_seq=?,last_seq=? WHERE feed_id=?",
                    (maximum, maximum, feed.feed_id),
                )
                await conn.execute("UPDATE list_feeds SET baseline_seq=? WHERE feed_id=?", (maximum, feed.feed_id))
        await self.repository.update_fetch_state(
            feed.feed_id,
            etag=result.etag,
            last_modified=result.last_modified,
            initialized=True,
            next_fetch_at=next_fetch,
        )
        await self._persist_request_url_fingerprint(feed.feed_id, result.request_url_fingerprint)
        return {"feed_id": feed.feed_id, "status": "ok", "new_articles": inserted, "total_sequence": maximum}

    async def _persist_request_url_fingerprint(self, feed_id: str, fingerprint: str | None) -> None:
        if fingerprint:
            await self.repository.merge_feed_metadata(feed_id, {"request_url_fingerprint": fingerprint})

    async def _scheduler_loop(self):
        from ..scheduler import FeedScheduler

        await FeedScheduler(self).run(self._stop)

    async def _delivery_loop(self):
        from ..delivery import DeliveryQueue

        await DeliveryQueue(self).run(self._stop)

    async def _mark_card_unknown_after_send(self, card, error: Exception) -> None:
        """Persist an ambiguous post-send outcome without risking an automatic duplicate."""
        message = str(error)[:500]
        try:
            await self.repository.set_batch_card_status(card["batch_id"], "unknown", error=message)
        except Exception as persist_error:
            logger.error("Could not persist unknown delivery card (%s)", type(persist_error).__name__)
        for delivery_id in card["delivery_ids"]:
            try:
                await self.repository.mark_delivery(delivery_id, "unknown", error=message)
            except Exception as persist_error:
                logger.error("Could not persist unknown delivery (%s)", type(persist_error).__name__)

    async def _release_unattempted_claims(self, claim_ids, *, protected_ids=()) -> None:
        # Pending deliveries can be the result of one successful card plus another
        # card that was never attempted. Roll back their claim so max_retries=0
        # cannot strand the outstanding card.
        releasable = list(set(claim_ids) - set(protected_ids))
        if releasable:
            await self.repository.release_unattempted_deliveries(releasable)

    async def _deliver_group(self, candidates, semaphore):
        if len(candidates) > 1:
            partitions = {}
            for candidate in candidates:
                persisted_group = await self.repository.batch_group([candidate["delivery_id"]])
                key = (
                    (candidate["target_id"], "persisted", persisted_group)
                    if persisted_group
                    else (
                        candidate["target_id"],
                        "new",
                        candidate["source_path"]
                        if candidate["source_path"].startswith("list:")
                        else candidate["delivery_id"],
                    )
                )
                partitions.setdefault(key, []).append(candidate)
            if len(partitions) > 1:
                outcomes = await asyncio.gather(
                    *(self._deliver_group(rows, semaphore) for rows in partitions.values()),
                    return_exceptions=True,
                )
                for outcome in outcomes:
                    if isinstance(outcome, asyncio.CancelledError):
                        raise outcome
                    if isinstance(outcome, BaseException):
                        logger.error("RSS delivery partition failed (%s)", type(outcome).__name__)
                return
        async with semaphore:
            valid = []
            source_info = {}
            for row in candidates:
                info = await self.repository.delivery_source(row["target_id"], row["article_id"], row["source_path"])
                if info is not None:
                    valid.append(row)
                    source_info[row["delivery_id"]] = info
                elif await self.repository.delivery_has_paused_path(row["target_id"], row["article_id"]):
                    await self.repository.postpone_delivery(
                        row["delivery_id"], max(self.config.scheduler_tick_seconds, self.config.min_poll_interval)
                    )
                else:
                    await self.repository.make_delivery_permanent(row["delivery_id"], "no active subscription path")
            candidates = valid
            rows = await self.repository.claim_deliveries(
                [row["delivery_id"] for row in candidates], 1 + self.config.max_delivery_retries
            )
            if not rows:
                return
            claim_ids = {row["delivery_id"] for row in rows}
            active_card = None
            send_started = False
            active_card_success_persisted = False
            max_attempts = 1 + self.config.max_delivery_retries
            try:
                # A retry after a partial batch reuses the persisted group, payloads and UUIDs.
                group_id = await self.repository.batch_group([r["delivery_id"] for r in rows])
                if group_id is None:
                    group_id = str(
                        uuid.uuid5(uuid.NAMESPACE_URL, "group:" + ":".join(sorted(r["delivery_id"] for r in rows)))
                    )
                target = DeliveryTarget(
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
                cards = await self.repository.batch_cards(group_id)
                if not cards:
                    articles = []
                    for row in rows:
                        article = await self.repository.article(row["article_id"])
                        if article is not None:
                            articles.append(article)
                    if not articles:
                        await self._release_unattempted_claims(claim_ids)
                        return
                    ids_by_article = {row["article_id"]: row["delivery_id"] for row in rows}
                    source_path = rows[0]["source_path"]
                    info = source_info.get(rows[0]["delivery_id"], {})
                    translation_diagnostics = []
                    articles = await self._articles_for_render(articles, diagnostics=translation_diagnostics)
                    for diagnostic in translation_diagnostics:
                        delivery_id = ids_by_article.get(diagnostic.get("article_id"))
                        if delivery_id:
                            await self.repository.set_delivery_translation_diagnostics(delivery_id, diagnostic)
                    rendered = await self.registry.render(
                        articles,
                        target,
                        source_name=info.get("source_name") or "RSS",
                        is_list=bool(info.get("is_list", source_path.startswith("list:"))),
                        batch_id=group_id,
                    )
                    if not rendered:
                        raise RuntimeError("renderer produced no messages")
                    rendered = await self._preflight_rendered(
                        articles,
                        target,
                        rendered,
                        source_name=info.get("source_name") or "RSS",
                        is_list=bool(info.get("is_list", source_path.startswith("list:"))),
                        batch_id=group_id,
                    )
                    persisted_cards = []
                    for index, message in enumerate(rendered):
                        card_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{group_id}:{index}"))
                        links = [ids_by_article[a] for a in message.article_ids if a in ids_by_article]
                        persisted = {
                            "renderer": message.renderer,
                            "message_type": message.message_type,
                            "payload": message.payload,
                            "article_ids": message.article_ids,
                            "batch_id": message.batch_id,
                        }
                        persisted_cards.append(
                            {
                                "group_id": group_id,
                                "batch_id": card_id,
                                "target_id": target.target_id,
                                "card_index": index,
                                "card_count": len(rendered),
                                "payload": persisted,
                                "idempotency_key": card_id,
                                "delivery_ids": links,
                            }
                        )
                    await self.repository.create_batch_cards(persisted_cards)
                    cards = await self.repository.batch_cards(group_id)
                for card in cards:
                    if card["status"] == "success":
                        active_card = card
                        active_card_success_persisted = True
                        for did in card["delivery_ids"]:
                            await self.repository.refresh_delivery_from_batches(did)
                        continue
                    if card["status"] == "unknown":
                        continue
                    active_card = card
                    active_card_success_persisted = False
                    links = card["delivery_ids"]
                    linked_rows = []
                    for delivery_id in links:
                        linked = await self.repository.delivery(delivery_id)
                        if linked is not None:
                            linked_rows.append(linked)
                    unknown_links = [row for row in linked_rows if row["status"] == "unknown"]
                    permanent_links = [row for row in linked_rows if row["status"] == "failed" and not row["retryable"]]
                    if unknown_links:
                        await self.repository.set_batch_card_status(
                            card["batch_id"],
                            "failed",
                            error="card is blocked by an article with unknown delivery status",
                        )
                        # This card was never sent. Keep the unknown state scoped to the
                        # actual send attempt so resolving it cannot mark this card successful.
                        blocked_ids = {row["delivery_id"] for row in unknown_links}
                        for linked in linked_rows:
                            if linked["delivery_id"] not in blocked_ids:
                                await self.repository.make_delivery_permanent(
                                    linked["delivery_id"], "card blocked by an article with unknown delivery status"
                                )
                        continue
                    if permanent_links:
                        await self.repository.set_batch_card_status(
                            card["batch_id"], "failed", error="card contains a permanently failed article"
                        )
                        blocked_ids = {row["delivery_id"] for row in permanent_links}
                        for linked in linked_rows:
                            if linked["delivery_id"] not in blocked_ids:
                                await self.repository.make_delivery_permanent(
                                    linked["delivery_id"], "card blocked by a permanently failed article"
                                )
                        continue
                    # A persisted group can be reached through a different eligible row. Do not
                    # bypass another article's backoff or retry limit by replaying its card.
                    if links and not all(delivery_id in claim_ids for delivery_id in links):
                        continue
                    data = card["payload"]
                    from ..core.models import RenderedMessage

                    message = RenderedMessage(
                        data["renderer"],
                        data["message_type"],
                        data["payload"],
                        data["article_ids"],
                        data.get("batch_id", group_id),
                    )
                    await self._respect_send_rate_limit(target.platform_instance)
                    for did in card["delivery_ids"]:
                        await self.repository.mark_delivery(did, "sending")
                    await self.repository.set_batch_card_status(card["batch_id"], "sending")
                    send_started = True
                    try:
                        result = await self.sender.send(target, message, idempotency_key=card["idempotency_key"])
                    except Exception as exc:
                        from .errors import PermanentSendError, PlatformUnsupportedError, RetryableSendError

                        if isinstance(exc, RetryableSendError):
                            await self.repository.set_batch_card_status(
                                card["batch_id"], "failed", error=str(exc)[:500]
                            )
                            for did in card["delivery_ids"]:
                                current = await self.repository.delivery(did)
                                attempt = int(current["attempts"]) if current else 1
                                delay = self.config.retry_backoff_base * (2 ** min(max(attempt - 1, 0), 8))
                                await self.repository.mark_delivery(
                                    did, "failed", error=str(exc)[:500], retry_at=now_ts() + delay
                                )
                                if attempt >= 1 + self.config.max_delivery_retries:
                                    await self.repository.mark_delivery_permanent_flag(did)
                        elif isinstance(exc, (PermanentSendError, PlatformUnsupportedError)):
                            await self.repository.set_batch_card_status(
                                card["batch_id"], "failed", error=str(exc)[:500]
                            )
                            for did in card["delivery_ids"]:
                                await self.repository.make_delivery_permanent(did, str(exc))
                        else:
                            # Once send() begins, an unclassified exception may mean the platform accepted the message.
                            await self.repository.set_batch_card_status(
                                card["batch_id"], "unknown", error=str(exc)[:500]
                            )
                            for did in card["delivery_ids"]:
                                await self.repository.mark_delivery(did, "unknown", error=str(exc)[:500])
                        send_started = False
                        await self._release_unattempted_claims(claim_ids)
                        return
                    try:
                        await self.repository.set_batch_card_status(
                            card["batch_id"], "success", platform_message_id=result.platform_message_id
                        )
                    except Exception as exc:
                        await self._mark_card_unknown_after_send(card, exc)
                        await self._release_unattempted_claims(claim_ids, protected_ids=links)
                        return
                    active_card_success_persisted = True
                    send_started = False
                    for did in card["delivery_ids"]:
                        await self.repository.refresh_delivery_from_batches(did)
                await self._release_unattempted_claims(claim_ids)
            except Exception as exc:
                if send_started and active_card is not None:
                    # send() was invoked, so a failed write may have lost the real platform outcome.
                    await self._mark_card_unknown_after_send(active_card, exc)
                    await self._release_unattempted_claims(claim_ids, protected_ids=active_card["delivery_ids"])
                    return
                if active_card is not None:
                    if active_card_success_persisted:
                        # The platform receipt and successful card state are durable. A refresh
                        # hiccup must never turn that known success into an automatic resend.
                        for did in active_card["delivery_ids"]:
                            try:
                                await self.repository.refresh_delivery_from_batches(did)
                            except Exception as refresh_error:
                                logger.error("Could not refresh completed delivery (%s)", type(refresh_error).__name__)
                    else:
                        try:
                            await self.repository.set_batch_card_status(
                                active_card["batch_id"], "failed", error=str(exc)[:500]
                            )
                        except Exception as persist_error:
                            logger.error("Could not persist failed unsent card (%s)", type(persist_error).__name__)
                        from .errors import PermanentSendError, RenderingError

                        retry_at = now_ts() + self.config.retry_backoff_base
                        for delivery_id in active_card["delivery_ids"]:
                            if isinstance(exc, (PermanentSendError, RenderingError)):
                                await self.repository.make_delivery_permanent(delivery_id, str(exc))
                            else:
                                await self.repository.fail_claimed_delivery(
                                    delivery_id,
                                    str(exc),
                                    max_attempts=max_attempts,
                                    retry_at=retry_at,
                                )
                    await self._release_unattempted_claims(claim_ids)
                    return
                retry_at = now_ts() + self.config.retry_backoff_base
                from .errors import PermanentSendError, RenderingError

                for delivery_id in claim_ids:
                    if isinstance(exc, (PermanentSendError, RenderingError)):
                        await self.repository.make_delivery_permanent(delivery_id, str(exc))
                    else:
                        await self.repository.fail_claimed_delivery(
                            delivery_id,
                            str(exc),
                            max_attempts=max_attempts,
                            retry_at=retry_at,
                        )

    async def list_feed_articles(self, feed_id: str, limit: int = 100):
        return await self.repository.articles_for_feed(feed_id, min(max(limit, 1), 500))

    async def backup(self, destination):
        dest = Path(destination)
        dest.parent.mkdir(parents=True, exist_ok=True)
        async with self.database.lock:
            target = await __import__("aiosqlite").connect(dest)
            try:
                await self.database.connection.backup(target)
            finally:
                await target.close()
        return str(dest)

    async def cleanup(self, kind: str = "articles"):
        if kind not in {"articles", "history", "all"}:
            raise ValueError("cleanup kind must be articles, history, or all")
        cutoff = now_ts() - self.config.history_retention_days * 86400
        async with self.database.transaction() as conn:
            history = {"deliveries": 0, "batches": 0, "test_operations": 0, "attempts": 0}
            if kind in {"history", "all"}:
                cur = await conn.execute(
                    """DELETE FROM delivery_batches WHERE updated_at<? AND status IN ('success','failed')
                    AND NOT EXISTS(SELECT 1 FROM batch_articles ba JOIN deliveries d USING(delivery_id)
                                   WHERE ba.batch_id=delivery_batches.batch_id
                                     AND (d.status IN ('pending','rendering','sending','unknown')
                                          OR (d.status='failed' AND d.retryable=1)))""",
                    (cutoff,),
                )
                history["batches"] = cur.rowcount
                cur = await conn.execute(
                    "DELETE FROM test_operations WHERE updated_at<? AND status IN ('success','failed')",
                    (cutoff,),
                )
                history["test_operations"] = cur.rowcount
                cur = await conn.execute(
                    """DELETE FROM deliveries WHERE updated_at<? AND (status='success' OR (status='failed' AND retryable=0))
                    AND (status!='success' OR NOT EXISTS(
                      SELECT 1 FROM articles a WHERE a.article_id=deliveries.article_id AND (
                        EXISTS(SELECT 1 FROM direct_subscriptions s WHERE s.target_id=deliveries.target_id AND s.feed_id=a.feed_id
                               AND a.seq>s.baseline_seq AND a.seq>s.last_seq) OR
                        EXISTS(SELECT 1 FROM list_subscriptions s JOIN list_feeds lf USING(list_id)
                               WHERE s.target_id=deliveries.target_id AND lf.feed_id=a.feed_id
                                 AND a.seq>MAX(s.baseline_seq,lf.baseline_seq) AND a.seq>s.last_seq)
                      )
                    ))""",
                    (cutoff,),
                )
                history["deliveries"] = cur.rowcount
                cur = await conn.execute("DELETE FROM delivery_attempts WHERE attempted_at<?", (cutoff,))
                history["attempts"] = cur.rowcount
            articles = 0
            if kind in {"articles", "all"}:
                cur = await conn.execute(
                    """DELETE FROM articles WHERE first_seen_at<?
                AND NOT EXISTS(SELECT 1 FROM deliveries d WHERE d.article_id=articles.article_id)
                AND NOT EXISTS(SELECT 1 FROM direct_subscriptions s
                    WHERE s.feed_id=articles.feed_id
                      AND articles.seq>MAX(s.baseline_seq,s.last_seq))
                AND NOT EXISTS(SELECT 1 FROM list_subscriptions s JOIN list_feeds lf USING(list_id)
                    WHERE lf.feed_id=articles.feed_id
                      AND articles.seq>MAX(s.baseline_seq,s.last_seq,lf.baseline_seq))""",
                    (cutoff,),
                )
                articles = cur.rowcount
            cur = await conn.execute(
                "DELETE FROM telegraph_pages WHERE NOT EXISTS "
                "(SELECT 1 FROM articles WHERE articles.article_id=telegraph_pages.article_id)"
            )
            telegraph_pages = cur.rowcount
            cur = await conn.execute("DELETE FROM fetch_logs WHERE fetched_at<?", (cutoff,))
            fetch_logs = cur.rowcount
        return {
            "kind": kind,
            "articles": articles,
            **history,
            "fetch_logs": fetch_logs,
            "telegraph_pages": telegraph_pages,
        }
