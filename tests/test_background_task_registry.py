from __future__ import annotations

import asyncio
import logging

import pytest

from app.services.background_task_registry import BackgroundTaskRegistry


@pytest.mark.asyncio
async def test_background_task_registry_logs_failed_task_traceback(caplog) -> None:
    marker = "boom trace marker"
    registry = BackgroundTaskRegistry(label="worker")

    async def fail() -> None:
        raise RuntimeError(marker)

    caplog.set_level(logging.WARNING, logger="app.services.background_task_registry")
    task = registry.spawn(fail())
    done, _ = await asyncio.wait({task}, timeout=1)
    assert task in done

    records = [record for record in caplog.records if record.message == "worker task failed"]
    assert records
    assert records[0].exc_info is not None
    assert records[0].exc_info[0] is RuntimeError
    assert marker in caplog.text
    assert "Traceback" in caplog.text
