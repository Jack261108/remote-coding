"""外部绑定清理任务。

基于 ``PeriodicBackgroundTask`` 的周期性任务，定期调用
``ExternalBindingCleanupService.run_cleanup()`` 清理过期或无效的外部会话绑定。

该任务在应用启动时自动启动，在应用关闭时安全停止。
默认清理间隔为 60 秒。

使用方式::

    from app.services.external_binding_cleanup_task import ExternalBindingCleanupTask

    task = ExternalBindingCleanupTask(cleanup_service, interval_seconds=60.0)
    task.start()
    # ...
    await task.stop()
"""

from __future__ import annotations

from app.infra.periodic_task import PeriodicBackgroundTask
from app.services.external_binding_cleanup_service import ExternalBindingCleanupService


class ExternalBindingCleanupTask(PeriodicBackgroundTask):
    """外部绑定清理任务。

    周期性调用 ``ExternalBindingCleanupService.run_cleanup()`` 清理过期或无效的外部会话绑定。

    Parameters
    ----------
    cleanup_service:
        外部绑定清理服务实例。
    interval_seconds:
        清理间隔（秒），默认 60 秒。
    """

    def __init__(
        self,
        cleanup_service: ExternalBindingCleanupService,
        interval_seconds: float = 60.0,
    ) -> None:
        """初始化外部绑定清理任务。

        Parameters
        ----------
        cleanup_service:
            外部绑定清理服务实例。
        interval_seconds:
            清理间隔（秒），默认 60 秒。
        """
        super().__init__(interval_seconds, "ExternalBindingCleanup")
        self._cleanup_service = cleanup_service

    async def _execute(self) -> None:
        """执行一次外部绑定清理。"""
        await self._cleanup_service.run_cleanup()
