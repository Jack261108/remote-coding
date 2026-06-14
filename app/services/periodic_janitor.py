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
from typing import Any

logger = logging.getLogger(__name__)


class PeriodicJanitor:
    """Single background task that runs multiple periodic callbacks."""

    def __init__(self) -> None:
        self._jobs: dict[str, tuple[float, Callable[[], Awaitable[Any]]]] = {}
        self._last_run: dict[str, float] = {}
        self._task: asyncio.Task[None] | None = None

    def register(self, name: str, interval_sec: float, callback: Callable[[], Awaitable[Any]]) -> None:
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
        now = asyncio.get_running_loop().time()
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
        if not self._jobs:
            return
        try:
            while True:
                now = asyncio.get_running_loop().time()
                min_sleep = float("inf")
                due_jobs: list[tuple[str, Callable[[], Awaitable[Any]]]] = []
                for name, (interval, callback) in self._jobs.items():
                    elapsed = now - self._last_run.get(name, 0)
                    remaining = interval - elapsed
                    if remaining <= 0:
                        due_jobs.append((name, callback))
                        remaining = interval
                    min_sleep = min(min_sleep, remaining)

                # Execute due jobs in parallel
                if due_jobs:

                    async def _run_job(name: str, callback: Callable[[], Awaitable[Any]]) -> None:
                        try:
                            await callback()
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            logger.exception("janitor job failed", extra={"job": name})

                    tasks = [asyncio.create_task(_run_job(name, cb)) for name, cb in due_jobs]
                    await asyncio.gather(*tasks, return_exceptions=True)
                    now = asyncio.get_event_loop().time()
                    for name, _ in due_jobs:
                        self._last_run[name] = now
                await asyncio.sleep(max(min_sleep, 0.01))
        except asyncio.CancelledError:
            raise
