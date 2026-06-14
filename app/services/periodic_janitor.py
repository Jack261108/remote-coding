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
    """单后台任务多作业调度器。

    将多个独立的周期性清理作业合并到单个后台任务中执行，
    避免为每个作业创建独立的 ``while True`` 循环。

    使用方式::

        janitor = PeriodicJanitor()
        janitor.register("cleanup_a", 60.0, cleanup_a_callback)
        janitor.register("cleanup_b", 300.0, cleanup_b_callback)
        await janitor.start()
        # ...
        await janitor.stop()
    """

    def __init__(self) -> None:
        """初始化周期性清理调度器。"""
        self._jobs: dict[str, tuple[float, Callable[[], Awaitable[Any]]]] = {}
        self._last_run: dict[str, float] = {}
        self._task: asyncio.Task[None] | None = None

    def register(self, name: str, interval_sec: float, callback: Callable[[], Awaitable[Any]]) -> None:
        """注册一个周期性作业。

        Parameters
        ----------
        name:
            作业唯一名称（用于日志和去重）。
        interval_sec:
            作业执行间隔（秒），必须为正数。
        callback:
            每次 tick 调用的异步回调函数。

        Raises
        ------
        ValueError
            当 ``interval_sec`` 不为正数时抛出。
        """
        if interval_sec <= 0:
            raise ValueError(f"interval_sec must be positive, got {interval_sec}")
        self._jobs[name] = (interval_sec, callback)

    async def start(self) -> None:
        """Start the janitor loop.

        Note: This is an alternative API. In the current bootstrap, the
        lifecycle is managed by ``JanitorTask`` (via ``PeriodicBackgroundTask``)
        which calls ``run()`` periodically. This method is provided for
        standalone usage scenarios.
        """
        if self._task is not None and not self._task.done():
            return
        now = asyncio.get_running_loop().time()
        for name in self._jobs:
            self._last_run[name] = now
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Cancel the janitor loop and wait for termination.

        Note: This is an alternative API. See ``start()`` for details.
        """
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def run(self) -> None:
        """执行一次调度检查，运行所有到期的作业。

        遍历所有已注册的作业，检查距离上次执行的时间是否超过配置的间隔。
        超过的作业会被执行，执行失败的作业会被记录异常日志但不会中断其他作业。

        该方法可以被外部周期性任务（如 ``PeriodicBackgroundTask``）调用，
        而不使用内置的 ``start()``/``stop()`` 循环。
        """
        now = asyncio.get_running_loop().time()
        for name, (interval, callback) in self._jobs.items():
            elapsed = now - self._last_run.get(name, 0)
            if elapsed >= interval:
                try:
                    await callback()
                except Exception:
                    logger.exception("janitor job failed", extra={"job": name})
                self._last_run[name] = asyncio.get_running_loop().time()

    async def _run(self) -> None:
        try:
            while True:
                await self.run()
                now = asyncio.get_running_loop().time()
                min_sleep = float("inf")
                for name, (interval, _) in self._jobs.items():
                    elapsed = now - self._last_run.get(name, 0)
                    remaining = interval - elapsed
                    min_sleep = min(min_sleep, remaining)
                await asyncio.sleep(max(min_sleep, 0.01))
        except asyncio.CancelledError:
            raise
