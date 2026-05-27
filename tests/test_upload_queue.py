import asyncio

import pytest

from app.services.upload_queue import UploadQueueManager


@pytest.mark.asyncio
async def test_upload_queue_accepts_and_drains_fifo() -> None:
    manager = UploadQueueManager(max_files_per_user=3, max_bytes_per_user=100)

    first = await manager.enqueue(user_id=1, filename="first.txt", data=b"first")
    second = await manager.enqueue(user_id=1, filename="second.txt", data=b"second")

    assert first.accepted is True
    assert second.accepted is True
    assert await manager.queued_count(user_id=1) == 2

    drained = await manager.drain(user_id=1)

    assert [(item.filename, item.data, item.size_bytes) for item in drained] == [
        ("first.txt", b"first", 5),
        ("second.txt", b"second", 6),
    ]
    assert await manager.queued_count(user_id=1) == 0


@pytest.mark.asyncio
async def test_upload_queue_rejects_when_file_count_limit_reached() -> None:
    manager = UploadQueueManager(max_files_per_user=1, max_bytes_per_user=100)

    await manager.enqueue(user_id=1, filename="first.txt", data=b"first")
    result = await manager.enqueue(user_id=1, filename="second.txt", data=b"second")

    assert result.accepted is False
    assert "队列已满" in result.reason
    assert await manager.queued_count(user_id=1) == 1


@pytest.mark.asyncio
async def test_upload_queue_rejects_when_byte_limit_exceeded() -> None:
    manager = UploadQueueManager(max_files_per_user=2, max_bytes_per_user=3)

    await manager.enqueue(user_id=1, filename="first.txt", data=b"ab")
    result = await manager.enqueue(user_id=1, filename="second.txt", data=b"cd")

    assert result.accepted is False
    assert "队列容量" in result.reason
    assert await manager.queued_count(user_id=1) == 1


@pytest.mark.asyncio
async def test_upload_queue_disabled_with_zero_file_limit() -> None:
    manager = UploadQueueManager(max_files_per_user=0, max_bytes_per_user=100)

    result = await manager.enqueue(user_id=1, filename="first.txt", data=b"first")

    assert result.accepted is False
    assert "上传队列已关闭" in result.reason
    assert await manager.queued_count(user_id=1) == 0


@pytest.mark.asyncio
async def test_upload_queue_rejected_first_upload_by_byte_limit_leaves_no_user_state() -> None:
    manager = UploadQueueManager(max_files_per_user=2, max_bytes_per_user=3)
    user_id = 42

    result = await manager.enqueue(user_id=user_id, filename="too-large.txt", data=b"abcd")

    assert result.accepted is False
    assert "队列容量" in result.reason
    assert await manager.queued_count(user_id=user_id) == 0
    assert user_id not in manager._queues
    assert user_id not in manager._byte_totals
    assert await manager.drain(user_id=user_id) == []


@pytest.mark.asyncio
async def test_upload_queue_concurrent_enqueues_respect_file_limit() -> None:
    manager = UploadQueueManager(max_files_per_user=1, max_bytes_per_user=100)

    first_result, second_result = await asyncio.gather(
        manager.enqueue(user_id=1, filename="first.txt", data=b"first"),
        manager.enqueue(user_id=1, filename="second.txt", data=b"second"),
    )

    assert [first_result.accepted, second_result.accepted].count(True) == 1
    assert [first_result.accepted, second_result.accepted].count(False) == 1

    drained = await manager.drain(user_id=1)

    assert len(drained) == 1
    assert drained[0].filename in {"first.txt", "second.txt"}


@pytest.mark.asyncio
async def test_upload_queue_expires_old_items_before_drain() -> None:
    now = 1000.0

    def clock() -> float:
        return now

    manager = UploadQueueManager(max_files_per_user=2, max_bytes_per_user=10, ttl_sec=5.0, clock=clock)
    await manager.enqueue(user_id=1, filename="old.txt", data=b"12345")

    now = 1006.0

    assert await manager.queued_count(user_id=1) == 0
    assert await manager.drain(user_id=1) == []

    result = await manager.enqueue(user_id=1, filename="new.txt", data=b"1234567890")
    assert result.accepted is True


@pytest.mark.asyncio
async def test_upload_queue_drain_prunes_expired_items_without_count_first() -> None:
    now = 1000.0

    def clock() -> float:
        return now

    manager = UploadQueueManager(max_files_per_user=1, max_bytes_per_user=5, ttl_sec=5.0, clock=clock)
    await manager.enqueue(user_id=1, filename="old.txt", data=b"12345")

    now = 1006.0

    assert await manager.drain(user_id=1) == []
    assert 1 not in manager._queues
    assert 1 not in manager._byte_totals


@pytest.mark.asyncio
async def test_upload_queue_background_cleanup_expires_without_user_operation() -> None:
    now = 1000.0

    def clock() -> float:
        return now

    manager = UploadQueueManager(max_files_per_user=1, max_bytes_per_user=5, ttl_sec=5.0, cleanup_interval_sec=0.01, clock=clock)
    await manager.enqueue(user_id=1, filename="old.txt", data=b"12345")

    now = 1006.0
    await manager.start_cleanup()
    try:
        await asyncio.sleep(0.05)
    finally:
        await manager.stop_cleanup()

    assert 1 not in manager._queues
    assert 1 not in manager._byte_totals


@pytest.mark.asyncio
async def test_upload_queue_enqueue_prunes_expired_items_before_limits() -> None:
    now = 1000.0

    def clock() -> float:
        return now

    manager = UploadQueueManager(max_files_per_user=1, max_bytes_per_user=5, ttl_sec=5.0, clock=clock)
    await manager.enqueue(user_id=1, filename="old.txt", data=b"12345")

    now = 1006.0
    result = await manager.enqueue(user_id=1, filename="new.txt", data=b"12345")

    assert result.accepted is True
    drained = await manager.drain(user_id=1)
    assert [(item.filename, item.data, item.size_bytes) for item in drained] == [("new.txt", b"12345", 5)]
