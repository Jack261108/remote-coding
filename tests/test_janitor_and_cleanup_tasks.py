"""Tests for JanitorTask and ExternalBindingCleanupTask.

Covers: _execute delegation to underlying service.
"""

from __future__ import annotations

import pytest

from app.services.external_binding_cleanup_task import ExternalBindingCleanupTask
from app.services.janitor_task import JanitorTask


class TestJanitorTask:
    @pytest.mark.asyncio
    async def test_execute_calls_janitor_run(self):
        from unittest.mock import AsyncMock

        mock_janitor = type("Janitor", (), {"run": AsyncMock()})()
        task = JanitorTask(janitor=mock_janitor, interval_seconds=0.01)
        await task._execute()
        mock_janitor.run.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_start_and_stop(self):
        from unittest.mock import AsyncMock

        mock_janitor = type("Janitor", (), {"run": AsyncMock()})()
        task = JanitorTask(janitor=mock_janitor, interval_seconds=60)
        task.start()
        assert task.is_running
        await task.stop()
        assert not task.is_running


class TestExternalBindingCleanupTask:
    @pytest.mark.asyncio
    async def test_execute_calls_cleanup_service(self):
        from unittest.mock import AsyncMock

        mock_service = type("Service", (), {"run_cleanup": AsyncMock()})()
        task = ExternalBindingCleanupTask(cleanup_service=mock_service, interval_seconds=0.01)
        await task._execute()
        mock_service.run_cleanup.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_start_and_stop(self):
        from unittest.mock import AsyncMock

        mock_service = type("Service", (), {"run_cleanup": AsyncMock()})()
        task = ExternalBindingCleanupTask(cleanup_service=mock_service, interval_seconds=60)
        task.start()
        assert task.is_running
        await task.stop()
        assert not task.is_running


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
