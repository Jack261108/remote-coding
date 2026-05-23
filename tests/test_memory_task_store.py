from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

from app.adapters.storage.memory import MemoryTaskStore
from app.domain.models import TaskRecord, TaskStatus, utc_now


def _make_task(
    task_id: str = "t1",
    user_id: int = 1,
    status: TaskStatus = TaskStatus.PENDING,
    created_at: datetime | None = None,
    ended_at: datetime | None = None,
) -> TaskRecord:
    return TaskRecord(
        task_id=task_id,
        session_id="s1",
        user_id=user_id,
        provider="test",
        prompt="test",
        workdir="/tmp",
        timeout_sec=60,
        status=status,
        created_at=created_at or utc_now(),
        ended_at=ended_at,
    )


# --- constructor validation ---


def test_negative_max_records_raises():
    with pytest.raises(ValueError, match="max_records"):
        MemoryTaskStore(max_records=0)


def test_negative_ttl_raises():
    with pytest.raises(ValueError, match="ttl_hours"):
        MemoryTaskStore(ttl_hours=-1)


# --- TTL eviction ---


@pytest.mark.asyncio
async def test_expired_final_cleaned_by_ended_at():
    now = utc_now()
    store = MemoryTaskStore(ttl_hours=24, max_records=100)

    old = _make_task(
        task_id="old",
        status=TaskStatus.SUCCEEDED,
        created_at=now - timedelta(days=2),
        ended_at=now - timedelta(days=2),
    )
    await store.add(old)

    result = await store.get("old")
    assert result is None


@pytest.mark.asyncio
async def test_not_yet_expired_final_kept():
    now = utc_now()
    store = MemoryTaskStore(ttl_hours=24, max_records=100)

    recent = _make_task(
        task_id="recent",
        status=TaskStatus.FAILED,
        created_at=now - timedelta(hours=1),
        ended_at=now - timedelta(hours=1),
    )
    await store.add(recent)

    result = await store.get("recent")
    assert result is not None


@pytest.mark.asyncio
async def test_expired_final_ended_at_none_uses_created_at():
    now = utc_now()
    store = MemoryTaskStore(ttl_hours=24, max_records=100)

    old = _make_task(
        task_id="old",
        status=TaskStatus.CANCELED,
        created_at=now - timedelta(days=2),
        ended_at=None,
    )
    await store.add(old)

    result = await store.get("old")
    assert result is None


@pytest.mark.asyncio
async def test_running_not_cleaned_by_ttl():
    now = utc_now()
    store = MemoryTaskStore(ttl_hours=1, max_records=100)

    running = _make_task(
        task_id="run",
        status=TaskStatus.RUNNING,
        created_at=now - timedelta(days=30),
        ended_at=None,
    )
    await store.add(running)

    result = await store.get("run")
    assert result is not None


@pytest.mark.asyncio
async def test_pending_not_cleaned_by_ttl():
    now = utc_now()
    store = MemoryTaskStore(ttl_hours=1, max_records=100)

    pending = _make_task(
        task_id="pend",
        status=TaskStatus.PENDING,
        created_at=now - timedelta(days=30),
        ended_at=None,
    )
    await store.add(pending)

    result = await store.get("pend")
    assert result is not None


# --- capacity eviction ---


@pytest.mark.asyncio
async def test_overflow_deletes_oldest_final():
    now = utc_now()
    store = MemoryTaskStore(max_records=3, ttl_hours=9999)

    for i in range(3):
        await store.add(
            _make_task(
                task_id=f"f{i}",
                status=TaskStatus.SUCCEEDED,
                created_at=now - timedelta(hours=3 - i),
                ended_at=now - timedelta(hours=3 - i),
            )
        )

    items = await store.iter_all()
    assert len(items) == 3

    await store.add(
        _make_task(
            task_id="f3",
            status=TaskStatus.FAILED,
            created_at=now,
            ended_at=now,
        )
    )

    result = await store.get("f0")
    assert result is None

    result = await store.get("f3")
    assert result is not None


@pytest.mark.asyncio
async def test_running_not_deleted_by_capacity():
    now = utc_now()
    store = MemoryTaskStore(max_records=3, ttl_hours=9999)

    for i in range(3):
        await store.add(
            _make_task(
                task_id=f"r{i}",
                status=TaskStatus.RUNNING,
                created_at=now - timedelta(hours=3 - i),
            )
        )

    assert len(await store.iter_all()) == 3

    await store.add(
        _make_task(
            task_id="new_final",
            status=TaskStatus.SUCCEEDED,
            created_at=now,
            ended_at=now,
        )
    )

    all_items = await store.iter_all()
    ids = {r.task_id for r in all_items}
    assert "r0" in ids
    assert "r1" in ids
    assert "r2" in ids
    assert "new_final" not in ids


# --- large scale: 10000 records keep latest 1000 ---


@pytest.mark.asyncio
async def test_10000_records_preserves_latest_1000():
    now = utc_now()
    store = MemoryTaskStore(max_records=1000, ttl_hours=999999)

    for i in range(10000):
        await store.add(
            _make_task(
                task_id=f"t{i}",
                status=TaskStatus.SUCCEEDED,
                created_at=now - timedelta(hours=10000 - i),
                ended_at=now - timedelta(hours=10000 - i),
            )
        )

    all_items = await store.iter_all()
    assert len(all_items) <= 1000

    result = await store.get("t9999")
    assert result is not None

    result = await store.get("t0")
    assert result is None


# --- concurrent access ---


@pytest.mark.asyncio
async def test_concurrent_access_preserves_running():
    now = utc_now()
    store = MemoryTaskStore(max_records=10, ttl_hours=24)

    for i in range(5):
        await store.add(
            _make_task(
                task_id=f"run{i}",
                status=TaskStatus.RUNNING,
                created_at=now - timedelta(hours=i),
            )
        )

    async def add_final(idx: int) -> None:
        await store.add(
            _make_task(
                task_id=f"f{idx}",
                status=TaskStatus.SUCCEEDED,
                created_at=now + timedelta(seconds=idx),
                ended_at=now + timedelta(seconds=idx),
            )
        )

    await asyncio.gather(*(add_final(i) for i in range(20)))

    all_items = await store.iter_all()
    running_ids = {r.task_id for r in all_items if r.status == TaskStatus.RUNNING}
    assert len(running_ids) == 5
    for i in range(5):
        assert f"run{i}" in running_ids


# --- save also triggers eviction ---


@pytest.mark.asyncio
async def test_save_triggers_eviction():
    now = utc_now()
    store = MemoryTaskStore(max_records=5, ttl_hours=9999)

    for i in range(5):
        await store.add(
            _make_task(
                task_id=f"t{i}",
                status=TaskStatus.SUCCEEDED,
                created_at=now - timedelta(hours=5 - i),
                ended_at=now - timedelta(hours=5 - i),
            )
        )

    updated = _make_task(
        task_id="t5",
        status=TaskStatus.SUCCEEDED,
        created_at=now,
        ended_at=now,
    )
    await store.save(updated)

    all_items = await store.iter_all()
    assert len(all_items) <= 5
    result = await store.get("t5")
    assert result is not None
