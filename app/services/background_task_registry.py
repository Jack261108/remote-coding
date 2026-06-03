"""Standardized fire-and-forget task tracking with error logging."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from contextlib import suppress
from typing import Any

logger = logging.getLogger(__name__)


class BackgroundTaskRegistry:
    """Track fire-and-forget asyncio tasks with automatic error logging.

    Replaces ad-hoc ``_background_tasks: set[Task]`` patterns scattered
    across the codebase.  Every spawned task is recorded and its done-callback
    logs exceptions that would otherwise be silently lost.
    """

    def __init__(self, *, label: str = "background") -> None:
        self._tasks: set[asyncio.Task[None]] = set()
        self._label = label

    @property
    def active_count(self) -> int:
        return len(self._tasks)

    def spawn(self, coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        """Create a tracked task from *coro*.

        The task is automatically removed from the registry when it completes,
        and any unhandled exception is logged at WARNING level.
        """
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._on_done)
        return task

    async def cancel_all(self) -> None:
        """Cancel all tracked tasks and wait for them to finish."""
        tasks = list(self._tasks)
        self._tasks.clear()
        for t in tasks:
            t.cancel()
        for t in tasks:
            with suppress(asyncio.CancelledError):
                await t

    def _on_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.warning("%s task failed", self._label, exc_info=exc)
