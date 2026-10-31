from __future__ import annotations

import asyncio
import logging

from ..core.models import now_ts

logger = logging.getLogger(__name__)


class DeliveryQueue:
    """Persistent target/article queue worker with restart-safe card operations."""

    def __init__(self, runtime):
        self.runtime = runtime

    async def run_once(self) -> int:
        runtime = self.runtime
        items = await runtime.repository.pending_deliveries(now_ts(), 100, 1 + runtime.config.max_delivery_retries)
        grouped: dict[tuple[str, str], list[dict]] = {}
        for row in items:
            key = (
                row["target_id"],
                row["source_path"] if row["source_path"].startswith("list:") else row["delivery_id"],
            )
            grouped.setdefault(key, []).append(row)
        semaphore = asyncio.Semaphore(runtime.config.delivery_concurrency)
        outcomes = await asyncio.gather(
            *(runtime._deliver_group(rows, semaphore) for rows in grouped.values()),
            return_exceptions=True,
        )
        for outcome in outcomes:
            if isinstance(outcome, asyncio.CancelledError):
                raise outcome
            if isinstance(outcome, BaseException):
                logger.error("RSS delivery group failed (%s)", type(outcome).__name__)
        return len(items)

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                if await self.runtime.repository.get_setting("paused", "0") != "1":
                    await self.run_once()
            except Exception:
                logger.exception("RSS delivery iteration failed")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=1)
            except TimeoutError:
                pass
