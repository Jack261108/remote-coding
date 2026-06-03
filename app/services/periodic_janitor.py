"""Consolidated periodic task scheduler.

Replaces multiple independent ``while True: await sleep(); await work()``
background loops with a single task that dispatches registered callbacks
at their configured intervals.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress

logger = logging.getLogger(__name__)


class PeriodicJanitor:
    """Single background task that runs multiple periodic callbacks."""

    def __init__(self) -> None:
        self._jobs: dict[str, tuple[float, Callable[[], Awaitable[None]]]] = {}
        self._last_run: dict[str, float] = {}
        self._task: asyncio.Task[None] | None = None

    def register(self, name: str, interval_sec: float, callback: Callable[[], Awaitable[None]]) -> None:
        """Register a periodic job.

        Args:
            name: Unique job name (used for logging and dedup).
            interval_sec: Seconds between consecutive runs.
            callback: Async callable invoked on each tick.
        """
        if interval_sec <= 0:
            raise ValueError(f"interval_sec must be positive, got {interval_sec}")
        self._jobs[name] = (interval_sec, callback)

    async def start(self) -> None:
        """Start the janitor loop."""
        if self._task is not None and not self._task.done():
            return
        now = asyncio.get_event_loop().time()
        for name in self._jobs:
            self._last_run[name] = now
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Cancel the janitor loop and wait for termination."""
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _run(self) -> None:
        try:
            while True:
                now = asyncio.get_event_loop().time()
                min_sleep = float("inf")
                for name, (interval, callback) in self._jobs.items():
                    elapsed = now - self._last_run.get(name, 0)
                    remaining = interval - elapsed
                    if remaining <= 0:
                        try:
                            await callback()
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            logger.exception("janitor job failed", extra={"job": name})
                        self._last_run[name] = asyncio.get_event_loop().time()
                        remaining = interval
                    min_sleep = min(min_sleep, remaining)
                await asyncio.sleep(max(min_sleep, 0.01))
        except asyncio.CancelledError:
            raise
