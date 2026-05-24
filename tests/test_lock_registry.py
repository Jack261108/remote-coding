from __future__ import annotations

import asyncio

import pytest

from app.services.lock_registry import RefCountedLockRegistry


@pytest.mark.asyncio
async def test_registry_serializes_same_key() -> None:
    registry = RefCountedLockRegistry(ttl_sec=10, cleanup_interval_sec=60, cleanup_batch_size=50)
    entered: list[str] = []
    release_first = asyncio.Event()
    first_entered = asyncio.Event()

    async def worker(name: str) -> None:
        async with registry.lock("tool-1"):
            entered.append(name)
            if name == "first":
                first_entered.set()
                await release_first.wait()

    first = asyncio.create_task(worker("first"))
    await first_entered.wait()
    second = asyncio.create_task(worker("second"))
    await asyncio.sleep(0)

    assert entered == ["first"]

    release_first.set()
    await first
    await second

    assert entered == ["first", "second"]


@pytest.mark.asyncio
async def test_registry_keeps_referenced_key_during_cleanup() -> None:
    now = 100.0

    def clock() -> float:
        return now

    registry = RefCountedLockRegistry(ttl_sec=10, cleanup_interval_sec=1, cleanup_batch_size=50, clock=clock)
    lock_cm = registry.lock("tool-1")
    entered = await lock_cm.__aenter__()
    assert entered is None

    now = 200.0
    await registry.cleanup_expired()

    assert len(registry) == 1

    await lock_cm.__aexit__(None, None, None)
    await registry.cleanup_expired()

    assert len(registry) == 1

    now = 211.0
    await registry.cleanup_expired()

    assert len(registry) == 0


@pytest.mark.asyncio
async def test_registry_requeues_key_after_delete_and_recreate() -> None:
    now = 100.0

    def clock() -> float:
        return now

    registry = RefCountedLockRegistry(ttl_sec=10, cleanup_interval_sec=1, cleanup_batch_size=50, clock=clock)

    async with registry.lock("tool-1"):
        pass

    now = 200.0
    await registry.cleanup_expired()
    assert len(registry) == 0

    async with registry.lock("tool-1"):
        pass

    assert len(registry) == 1


@pytest.mark.asyncio
async def test_cleanup_key_removes_deleted_key_from_cleanup_batch() -> None:
    now = 100.0

    def clock() -> float:
        return now

    registry = RefCountedLockRegistry(ttl_sec=10, cleanup_interval_sec=1, cleanup_batch_size=1, clock=clock)
    async with registry.lock("deleted"):
        pass
    await registry.cleanup_key("deleted", require_expired=False)

    async with registry.lock("stale"):
        pass

    now = 200.0
    await registry.cleanup_expired()

    assert len(registry) == 0


@pytest.mark.asyncio
async def test_registry_cleanup_batch_size_limits_work_per_pass() -> None:
    now = 100.0

    def clock() -> float:
        return now

    registry = RefCountedLockRegistry(ttl_sec=10, cleanup_interval_sec=1, cleanup_batch_size=2, clock=clock)
    for key in ("a", "b", "c"):
        async with registry.lock(key):
            pass

    now = 200.0
    await registry.cleanup_expired()

    assert len(registry) == 1

    now = 202.0
    await registry.cleanup_expired()

    assert len(registry) == 0


@pytest.mark.asyncio
async def test_registry_runs_global_cleanup_on_lock_hot_path() -> None:
    now = 100.0

    def clock() -> float:
        return now

    registry = RefCountedLockRegistry(ttl_sec=10, cleanup_interval_sec=1, cleanup_batch_size=50, clock=clock)
    async with registry.lock("old"):
        pass

    now = 111.0
    async with registry.lock("new"):
        pass

    assert len(registry) == 1


@pytest.mark.asyncio
async def test_registry_instances_keep_cleanup_state_independent() -> None:
    now = 100.0

    def clock() -> float:
        return now

    first = RefCountedLockRegistry(ttl_sec=10, cleanup_interval_sec=1, cleanup_batch_size=1, clock=clock)
    second = RefCountedLockRegistry(ttl_sec=10, cleanup_interval_sec=1, cleanup_batch_size=1, clock=clock)

    async with first.lock("a"):
        pass
    async with second.lock("b"):
        pass

    now = 200.0
    await first.cleanup_expired()

    assert len(first) == 0
    assert len(second) == 1

    await second.cleanup_expired()
    assert len(second) == 0
