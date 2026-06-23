"""Tests for PeriodicBackgroundTask.

Covers: start, stop, is_running, _periodic_loop, and _on_error.
"""

from __future__ import annotations

import asyncio

import pytest

from app.infra.periodic_task import PeriodicBackgroundTask

# ---------------------------------------------------------------------------
# PeriodicBackgroundTask
# ---------------------------------------------------------------------------


class ConcretePeriodicTask(PeriodicBackgroundTask):
    def __init__(self, interval: float = 0.05) -> None:
        super().__init__(interval, "TestTask")
        self.execute_count = 0
        self.errors: list[Exception] = []

    async def _execute(self) -> None:
        self.execute_count += 1

    def _on_error(self, exc: Exception) -> None:
        self.errors.append(exc)


class FailingPeriodicTask(ConcretePeriodicTask):
    async def _execute(self) -> None:
        self.execute_count += 1
        raise RuntimeError(f"fail-{self.execute_count}")


class TestPeriodicBackgroundTask:
    @pytest.mark.asyncio
    async def test_start_creates_task(self):
        task = ConcretePeriodicTask()
        task.start()
        assert task.is_running
        await task.stop()

    @pytest.mark.asyncio
    async def test_start_is_idempotent(self):
        task = ConcretePeriodicTask()
        task.start()
        t1 = task._task
        task.start()
        t2 = task._task
        assert t1 is t2
        await task.stop()

    @pytest.mark.asyncio
    async def test_stop_handles_no_task(self):
        task = ConcretePeriodicTask()
        await task.stop()  # should not raise
        assert not task.is_running

    @pytest.mark.asyncio
    async def test_stop_cancels_running_task(self):
        task = ConcretePeriodicTask()
        task.start()
        assert task.is_running
        await task.stop()
        assert not task.is_running
        assert task._task is None

    @pytest.mark.asyncio
    async def test_executes_periodically(self):
        task = ConcretePeriodicTask(interval=0.03)
        task.start()
        await asyncio.sleep(0.15)
        await task.stop()
        assert task.execute_count >= 2

    @pytest.mark.asyncio
    async def test_error_does_not_stop_loop(self):
        task = FailingPeriodicTask(interval=0.03)
        task.start()
        await asyncio.sleep(0.15)
        await task.stop()
        assert task.execute_count >= 2
        assert len(task.errors) >= 2

    @pytest.mark.asyncio
    async def test_is_running_property(self):
        task = ConcretePeriodicTask()
        assert not task.is_running
        task.start()
        assert task.is_running
        await task.stop()
        assert not task.is_running


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
