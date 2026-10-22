from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class _CallState:
    started: bool = False


class BoundedThreadWorkers:
    """Track bounded thread-backed work independently of its caller task."""

    def __init__(self, concurrency: int = 5) -> None:
        self._semaphore = asyncio.Semaphore(max(1, int(concurrency)))
        self._lock = asyncio.Lock()
        self._closed = False
        self._calls: set[asyncio.Task[Any]] = set()
        self._workers: set[asyncio.Task[Any]] = set()

    async def run(self, function: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        async with self._lock:
            if self._closed:
                raise RuntimeError("thread worker pool is closed")
            state = _CallState()
            call = asyncio.create_task(self._run_one(function, args, kwargs, state))
            self._calls.add(call)
            call.add_done_callback(self._discard_completed)
        try:
            return await asyncio.shield(call)
        except asyncio.CancelledError:
            if not state.started:
                call.cancel()
            raise

    async def _run_one(
        self, function: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any], state: _CallState
    ) -> Any:
        async with self._semaphore:
            async with self._lock:
                if self._closed:
                    raise RuntimeError("thread worker pool is closed")
                state.started = True
                worker = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
                self._workers.add(worker)
                worker.add_done_callback(self._discard_completed)
            return await asyncio.shield(worker)

    async def close(self) -> None:
        async with self._lock:
            self._closed = True
            outstanding = tuple(self._calls | self._workers)
        if outstanding:
            drain = asyncio.create_task(self._drain(outstanding))
            try:
                await asyncio.shield(drain)
            except asyncio.CancelledError:
                await asyncio.shield(drain)
                raise

    @staticmethod
    async def _drain(tasks: tuple[asyncio.Task[Any], ...]) -> None:
        await asyncio.gather(*(asyncio.shield(task) for task in tasks), return_exceptions=True)

    def _discard_completed(self, task: asyncio.Task[Any]) -> None:
        self._calls.discard(task)
        self._workers.discard(task)
        if not task.cancelled():
            # Mark exceptions observed even when the original caller was cancelled.
            task.exception()
