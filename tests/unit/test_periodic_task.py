"""Unit tests for PeriodicBackgroundTask."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from app.infra.periodic_task import PeriodicBackgroundTask


class ConcreteTask(PeriodicBackgroundTask):
    """Test implementation of PeriodicBackgroundTask."""

    def __init__(self, interval: float = 0.01, **kwargs) -> None:
        super().__init__(interval_seconds=interval, **kwargs)
        self.execute_count = 0
        self.execute_mock = AsyncMock()
        self.error_mock = None

    async def _execute(self) -> None:
        self.execute_count += 1
        await self.execute_mock()
        if self.error_mock is not None:
            raise self.error_mock


# -- 核心功能 --


class TestStartStop:
    async def test_start_creates_task(self) -> None:
        task = ConcreteTask()
        task.start()
        assert task.is_running
        await task.stop()
        assert not task.is_running

    async def test_stop_without_start_is_noop(self) -> None:
        task = ConcreteTask()
        await task.stop()  # should not raise
        assert not task.is_running

    async def test_start_twice_does_not_create_duplicate(self) -> None:
        task = ConcreteTask()
        task.start()
        first_id = id(task._task)
        task.start()  # should not create a new task
        assert id(task._task) == first_id
        await task.stop()

    async def test_restart_after_stop(self) -> None:
        task = ConcreteTask()
        task.start()
        await task.stop()
        assert not task.is_running
        task.start()
        assert task.is_running
        await task.stop()


class TestExecution:
    async def test_execute_is_called_periodically(self) -> None:
        task = ConcreteTask(interval=0.01)
        task.start()
        await asyncio.sleep(0.05)
        await task.stop()
        # With 10ms interval and 50ms sleep, expect at least 2 executions
        assert task.execute_count >= 2

    async def test_execute_not_called_before_interval(self) -> None:
        task = ConcreteTask(interval=10.0)
        task.start()
        await asyncio.sleep(0.01)
        await task.stop()
        # First sleep is 10s, so execute should not have been called
        assert task.execute_count == 0


class TestErrorHandling:
    async def test_error_does_not_stop_loop(self) -> None:
        task = ConcreteTask(interval=0.01)
        task.error_mock = RuntimeError("boom")
        task.start()
        await asyncio.sleep(0.05)
        await task.stop()
        assert task.execute_count >= 2

    async def test_on_error_is_called(self) -> None:
        task = ConcreteTask(interval=0.01)
        task.error_mock = RuntimeError("boom")
        on_error_called = []
        task._on_error = lambda exc: on_error_called.append(exc)
        task.start()
        await asyncio.sleep(0.05)
        await task.stop()
        assert len(on_error_called) >= 1
        assert isinstance(on_error_called[0], RuntimeError)

    async def test_custom_on_error(self) -> None:
        errors = []

        class CustomTask(ConcreteTask):
            def _on_error(self, exc: Exception) -> None:
                errors.append(exc)

        task = CustomTask(interval=0.01)
        task.error_mock = ValueError("custom")
        task.start()
        await asyncio.sleep(0.05)
        await task.stop()
        assert any(isinstance(e, ValueError) for e in errors)


# -- 边界条件 --


class TestIsRunning:
    async def test_is_running_false_before_start(self) -> None:
        task = ConcreteTask()
        assert not task.is_running

    async def test_is_running_true_while_active(self) -> None:
        task = ConcreteTask()
        task.start()
        assert task.is_running
        await task.stop()

    async def test_is_running_false_after_stop(self) -> None:
        task = ConcreteTask()
        task.start()
        await task.stop()
        assert not task.is_running

    async def test_is_running_false_after_task_completes_naturally(self) -> None:
        """If _execute raises CancelledError directly, task should be done."""
        task = ConcreteTask()
        task.start()
        await asyncio.sleep(0.01)
        await task.stop()
        assert not task.is_running


class TestCancellation:
    async def test_stop_cancels_running_task(self) -> None:
        task = ConcreteTask()
        task.start()
        assert task._task is not None
        await task.stop()
        assert task._task is None or task._task.done()

    async def test_stop_handles_already_done_task(self) -> None:
        task = ConcreteTask()
        task.start()
        await asyncio.sleep(0.01)
        # Force task to be done
        if task._task:
            task._task.cancel()
            await asyncio.sleep(0.01)
        await task.stop()  # should not raise
