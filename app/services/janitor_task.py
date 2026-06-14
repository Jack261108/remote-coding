"""定期清理任务。

基于 ``PeriodicBackgroundTask`` 的周期性任务，定期调用
``PeriodicJanitor.run()`` 执行所有已注册的清理作业。

``PeriodicJanitor`` 是一个多任务调度器，支持注册多个不同间隔的清理作业
（如上传队列清理、文件清理、会话健康检查等）。本任务以统一的间隔
（默认 300 秒）驱动调度器执行。

使用方式::

    from app.services.janitor_task import JanitorTask

    task = JanitorTask(janitor=periodic_janitor, interval_seconds=300.0)
    task.start()
    # ...
    await task.stop()
"""

from __future__ import annotations

from app.infra.periodic_task import PeriodicBackgroundTask
from app.services.periodic_janitor import PeriodicJanitor


class JanitorTask(PeriodicBackgroundTask):
    """定期清理任务。

    周期性调用 ``PeriodicJanitor.run()`` 执行所有已注册的清理作业。

    Parameters
    ----------
    janitor:
        周期性清理调度器实例。
    interval_seconds:
        调度间隔（秒），默认 300 秒。
    """

    def __init__(
        self,
        janitor: PeriodicJanitor,
        interval_seconds: float = 300.0,
    ) -> None:
        """初始化定期清理任务。

        Parameters
        ----------
        janitor:
            周期性清理调度器实例。
        interval_seconds:
            调度间隔（秒），默认 300 秒。
        """
        super().__init__(interval_seconds, "Janitor")
        self._janitor = janitor

    async def _execute(self) -> None:
        """执行一次清理调度。"""
        await self._janitor.run()
