from __future__ import annotations

import asyncio
import logging
import math

from ..core.models import now_ts

logger = logging.getLogger(__name__)


class FeedScheduler:
    """Coordinates shared Feed polling and durable path cursor advancement."""

    def __init__(self, runtime):
        self.runtime = runtime

    async def run_once(self, *, force: bool = False) -> dict[str, int]:
        runtime = self.runtime
        generation_snapshot = runtime.feed_refresh_generations_snapshot()
        now = now_ts()
        if force:
            due = await runtime.repository.active_feeds(
                now, runtime.config.global_poll_interval, runtime.config.min_poll_interval
            )
        else:
            due = await runtime.repository.due_feeds(
                now, runtime.config.global_poll_interval, runtime.config.min_poll_interval
            )
        semaphore = asyncio.Semaphore(runtime.config.max_fetch_concurrency)
        results = {"checked": len(due), "fetched": 0, "errors": 0, "new_articles": 0, "queued": 0}

        async def fetch_one(row):
            async with semaphore:
                feed_id = row["feed_id"]
                async with runtime.feed_refresh_lock(feed_id):
                    current_time = now_ts()
                    retry_deadline = await runtime.repository.feed_retry_deadline(feed_id)
                    if retry_deadline is not None and retry_deadline > current_time:
                        return
                    if runtime.feed_refresh_generation(feed_id) != generation_snapshot.get(feed_id, 0):
                        return
                    feed = await runtime.repository.get_feed(feed_id)
                    if feed is None:
                        return
                    state = await runtime.database.fetchone(
                        "SELECT next_fetch_at FROM feeds WHERE feed_id=?", (feed_id,)
                    )
                    if state is None or (not force and state["next_fetch_at"] > current_time):
                        return
                    interval = await runtime.repository.required_fetch_interval(
                        feed_id, runtime.config.global_poll_interval, runtime.config.min_poll_interval
                    )
                    current_row = {**row, "required_interval": interval}
                    stage = "fetch"
                    try:
                        result = await runtime.fetcher.fetch(feed, force=force)
                        if result.error_type:
                            results["errors"] += 1
                            stage = "record_fetch_error"
                            await runtime.repository.log_fetch(
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
                            stage = "update_fetch_state"
                            await runtime.repository.update_fetch_state(
                                feed.feed_id,
                                etag=feed.etag,
                                last_modified=feed.last_modified,
                                initialized=feed.initialized,
                                next_fetch_at=self._error_next_fetch_at(current_row, result, now_ts()),
                                status=result.error_type,
                            )
                            return
                        stage = "persist_fetch"
                        fetched = await runtime._persist_fetch(feed, result, interval)
                        if fetched.get("status") == "error":
                            results["errors"] += 1
                        else:
                            results["fetched"] += 1
                            results["new_articles"] += fetched.get("new_articles", 0)
                    except Exception as exc:
                        results["errors"] += 1
                        logger.warning("RSS feed processing failed during %s (%s)", stage, type(exc).__name__)
                        await runtime.repository.update_fetch_state(
                            feed.feed_id,
                            etag=feed.etag,
                            last_modified=feed.last_modified,
                            initialized=feed.initialized,
                            next_fetch_at=now_ts() + max(runtime.config.min_poll_interval, interval),
                            status=type(exc).__name__,
                        )
                    finally:
                        runtime.bump_feed_refresh_generation(feed_id)

        if due:
            await asyncio.gather(*(fetch_one(row) for row in due))
        results["queued"] = await runtime.repository.enqueue_due_paths(
            now_ts(), runtime.config.global_poll_interval, runtime.config.min_poll_interval, force=force
        )
        return results

    def _error_next_fetch_at(self, row, result, now: float) -> float:
        runtime = self.runtime
        interval = max(
            runtime.config.min_poll_interval,
            row.get("required_interval") or runtime.config.global_poll_interval,
        )
        next_fetch_at = now + interval
        if result.status_code != 429 and result.error_type != "http_429":
            return next_fetch_at
        retry_after_at = getattr(result, "retry_after_at", None)
        if (
            isinstance(retry_after_at, bool)
            or not isinstance(retry_after_at, (int, float))
            or not math.isfinite(retry_after_at)
        ):
            retry_after_at = now + 60
        return max(next_fetch_at, retry_after_at)

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                if await self.runtime.repository.get_setting("paused", "0") != "1":
                    await self.run_once()
            except Exception:
                logger.exception("RSS scheduler iteration failed")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self.runtime.config.scheduler_tick_seconds)
            except TimeoutError:
                pass
