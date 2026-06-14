"""周期性后台任务基类。

提供 ``asyncio.create_task`` + ``while True: sleep; work`` 的标准封装，
子类只需实现 ``_execute()`` 方法即可获得完整的生命周期管理。

核心特性：
- ``start()`` / ``stop()`` 控制任务启停，支持重复调用。
- ``is_running`` 属性检查任务状态。
- ``_on_error()`` 钩子允许子类自定义错误处理策略。
- ``stop()`` 使用 ``cancel()`` + ``suppress(CancelledError)`` 安全终止。

使用方式::

    from app.infra.periodic_task import PeriodicBackgroundTask

    class MyTask(PeriodicBackgroundTask):
        def __init__(self) -> None:
            super().__init__(interval_seconds=60.0, task_name="MyTask")

        async def _execute(self) -> None:
            # 具体任务逻辑
            pass

    task = MyTask()
    task.start()   # 启动后台循环
    await task.stop()  # 安全停止
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from contextlib import suppress

logger = logging.getLogger(__name__)


class PeriodicBackgroundTask(ABC):
    """周期性后台任务基类。

    封装 asyncio.create_task + while True: sleep; work + cancel+suppress 的标准模式。

    Parameters
    ----------
    interval_seconds:
        任务执行间隔（秒）。
    task_name:
        任务名称，用于日志记录。
    """

    def __init__(
        self,
        interval_seconds: float,
        task_name: str = "PeriodicTask",
    ) -> None:
        """初始化周期性后台任务。

        Parameters
        ----------
        interval_seconds:
            任务执行间隔（秒）。
        task_name:
            任务名称，用于日志记录。
        """
        self._interval = interval_seconds
        self._task_name = task_name
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """启动后台任务。"""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._periodic_loop())
            logger.info("%s started (interval=%.1fs)", self._task_name, self._interval)

    async def stop(self) -> None:
        """停止后台任务并等待其终止。"""
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            logger.info("%s stopped", self._task_name)

    @property
    def is_running(self) -> bool:
        """任务是否正在运行。"""
        return self._task is not None and not self._task.done()

    @abstractmethod
    async def _execute(self) -> None:
        """执行具体任务逻辑（子类必须实现）。

        该方法在每个间隔周期被调用一次。
        抛出的异常会被 ``_on_error()`` 捕获处理，不会终止循环。

        Raises
        ------
        Exception
            子类实现可以抛出任何异常，由 ``_on_error()`` 统一处理。
        """
        ...

    def _on_error(self, exc: Exception) -> None:
        """错误处理钩子（子类可覆盖）。

        默认行为是记录异常日志。子类可以覆盖此方法实现自定义错误处理策略，
        例如发送告警通知或记录到监控系统。

        Parameters
        ----------
        exc:
            ``_execute()`` 抛出的异常。
        """
        logger.exception("%s error", self._task_name, exc_info=(type(exc), exc, exc.__traceback__))

    async def _periodic_loop(self) -> None:
        """周期性执行循环。"""
        try:
            while True:
                await asyncio.sleep(self._interval)
                try:
                    await self._execute()
                except Exception as exc:
                    self._on_error(exc)
        except asyncio.CancelledError:
            logger.info("%s cancelled", self._task_name)
            raise
