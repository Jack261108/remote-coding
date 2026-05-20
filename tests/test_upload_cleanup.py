from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from app.services.upload_cleanup import UploadCleanupService


@pytest.fixture
def mock_upload_store() -> MagicMock:
    store = MagicMock()
    store.cleanup_expired.return_value = 0
    return store


@pytest.fixture
def service(mock_upload_store: MagicMock) -> UploadCleanupService:
    return UploadCleanupService(
        upload_store=mock_upload_store,
        interval_minutes=60,
        max_age_hours=24,
    )


@pytest.mark.asyncio
async def test_start_runs_initial_cleanup(mock_upload_store: MagicMock, service: UploadCleanupService) -> None:
    mock_upload_store.cleanup_expired.return_value = 3

    await service.start()

    mock_upload_store.cleanup_expired.assert_called_once_with(24)

    # Stop the background task to avoid warnings
    await service.stop()


@pytest.mark.asyncio
async def test_start_creates_periodic_task(service: UploadCleanupService) -> None:
    await service.start()

    assert service._task is not None
    assert not service._task.done()

    await service.stop()


@pytest.mark.asyncio
async def test_stop_cancels_periodic_task(service: UploadCleanupService) -> None:
    await service.start()
    task = service._task
    assert task is not None

    await service.stop()

    assert service._task is None
    assert task.cancelled() or task.done()


@pytest.mark.asyncio
async def test_stop_when_not_started(service: UploadCleanupService) -> None:
    # Should not raise
    await service.stop()
    assert service._task is None


@pytest.mark.asyncio
async def test_run_cleanup_delegates_to_store(mock_upload_store: MagicMock, service: UploadCleanupService) -> None:
    mock_upload_store.cleanup_expired.return_value = 5

    result = await service.run_cleanup()

    assert result == 5
    mock_upload_store.cleanup_expired.assert_called_once_with(24)


@pytest.mark.asyncio
async def test_run_cleanup_uses_configured_max_age(mock_upload_store: MagicMock) -> None:
    service = UploadCleanupService(
        upload_store=mock_upload_store,
        interval_minutes=30,
        max_age_hours=12,
    )

    await service.run_cleanup()

    mock_upload_store.cleanup_expired.assert_called_once_with(12)


@pytest.mark.asyncio
async def test_periodic_loop_calls_cleanup_after_interval(mock_upload_store: MagicMock) -> None:
    service = UploadCleanupService(
        upload_store=mock_upload_store,
        interval_minutes=1,  # 1 minute = 60 seconds
        max_age_hours=24,
    )

    call_count = 0

    async def fake_sleep(seconds: float) -> None:
        nonlocal call_count
        call_count += 1
        if call_count >= 2:
            raise asyncio.CancelledError()

    with patch("app.services.upload_cleanup.asyncio.sleep", side_effect=fake_sleep) as mock_sleep:
        await service.start()

        # Wait for task to complete (it will be cancelled after sleep raises)
        try:
            await asyncio.wait_for(service._task, timeout=1.0)  # type: ignore[arg-type]
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass

        # Initial cleanup + at least one periodic cleanup
        assert mock_upload_store.cleanup_expired.call_count >= 2
        # First call from start(), subsequent from periodic loop
        mock_sleep.assert_any_call(60)  # 1 minute * 60

    await service.stop()


@pytest.mark.asyncio
async def test_default_interval_and_max_age(mock_upload_store: MagicMock) -> None:
    service = UploadCleanupService(upload_store=mock_upload_store)
    assert service._interval_minutes == 60
    assert service._max_age_hours == 24
