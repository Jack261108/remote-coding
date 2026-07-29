from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

import pytest

from app.adapters.storage.file_session_store import FileSessionStore
from app.domain.external_session_models import ExternalBinding
from app.domain.models import utc_now
from app.services.background_task_registry import BackgroundTaskRegistry
from app.services.external_binding_store import ExternalBindingStore
from app.services.external_reply_delivery_pump import ExternalReplyDeliveryPump, ExternalReplyDrainResult
from app.services.session_store import SessionStore


async def _finalize_success(_session_id: str) -> bool:
    return True


async def _wait_until(predicate: Callable[[], bool], *, timeout_sec: float = 1.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_sec
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError("condition not reached")
        await asyncio.sleep(0.005)


def _binding_store(tmp_path: Path, *, session_id: str = "session-1") -> ExternalBindingStore:
    store = ExternalBindingStore(tmp_path)
    store.save_binding(
        ExternalBinding(
            session_id=session_id,
            user_id=1,
            cwd=str(tmp_path),
            bound_at=utc_now(),
            jsonl_path=None,
            reply_cursor_initialized=True,
        )
    )
    return store


def _pump(
    tmp_path: Path,
    *,
    sync_callback,
    drain_callback,
    finalize_callback=_finalize_success,
    settle_delays: tuple[float, ...] = (0.01, 0.02),
    retry_delays: tuple[float, ...] = (0.01, 0.02),
    idle_check_sec: float = 0.02,
) -> tuple[ExternalReplyDeliveryPump, SessionStore, ExternalBindingStore, BackgroundTaskRegistry]:
    session_store = SessionStore(FileSessionStore(str(tmp_path)))
    binding_store = _binding_store(tmp_path)
    background_tasks = BackgroundTaskRegistry(label="test-external-reply-pump")
    pump = ExternalReplyDeliveryPump(
        session_store=session_store,
        binding_store=binding_store,
        background_tasks=background_tasks,
        sync_callback=sync_callback,
        drain_callback=drain_callback,
        finalize_callback=finalize_callback,
        settle_delays=settle_delays,
        retry_delays=retry_delays,
        idle_check_sec=idle_check_sec,
    )
    return pump, session_store, binding_store, background_tasks


@pytest.mark.asyncio
async def test_ensure_starts_one_pump_per_session(tmp_path: Path) -> None:
    drain_count = 0

    async def sync_callback(session_id: str, cwd: str) -> None:
        return None

    async def drain_callback(session_id: str) -> ExternalReplyDrainResult:
        nonlocal drain_count
        drain_count += 1
        return ExternalReplyDrainResult.NO_NEW_REPLY

    pump, _, _, background_tasks = _pump(tmp_path, sync_callback=sync_callback, drain_callback=drain_callback)
    pump.ensure(session_id="session-1", cwd=str(tmp_path))
    pump.ensure(session_id="session-1", cwd=str(tmp_path))

    await _wait_until(lambda: drain_count >= 1)
    assert pump.active_count == 1
    assert background_tasks.active_count == 1

    await pump.stop_all()
    assert pump.active_count == 0
    assert background_tasks.active_count == 0


@pytest.mark.asyncio
async def test_revision_publish_wakes_pump_and_drains(tmp_path: Path) -> None:
    drain_count = 0

    async def sync_callback(session_id: str, cwd: str) -> None:
        return None

    async def drain_callback(session_id: str) -> ExternalReplyDrainResult:
        nonlocal drain_count
        drain_count += 1
        return ExternalReplyDrainResult.NO_NEW_REPLY

    pump, session_store, _, _ = _pump(tmp_path, sync_callback=sync_callback, drain_callback=drain_callback)
    session_store.get_or_create(session_id="session-1", workdir=str(tmp_path))
    pump.ensure(session_id="session-1", cwd=str(tmp_path))
    await _wait_until(lambda: drain_count >= 1)

    state = session_store.get("session-1")
    assert state is not None
    session_store.save(state)

    await _wait_until(lambda: drain_count >= 2)
    await pump.stop_all()


@pytest.mark.asyncio
async def test_revision_published_during_drain_is_not_lost(tmp_path: Path) -> None:
    session_store = SessionStore(FileSessionStore(str(tmp_path)))
    state = session_store.get_or_create(session_id="session-1", workdir=str(tmp_path))
    binding_store = _binding_store(tmp_path)
    background_tasks = BackgroundTaskRegistry(label="test-external-reply-pump")
    drain_count = 0

    async def sync_callback(session_id: str, cwd: str) -> None:
        return None

    async def drain_callback(session_id: str) -> ExternalReplyDrainResult:
        nonlocal drain_count
        drain_count += 1
        if drain_count == 1:
            session_store.save(state)
        return ExternalReplyDrainResult.NO_NEW_REPLY

    pump = ExternalReplyDeliveryPump(
        session_store=session_store,
        binding_store=binding_store,
        background_tasks=background_tasks,
        sync_callback=sync_callback,
        drain_callback=drain_callback,
        finalize_callback=_finalize_success,
        idle_check_sec=1,
    )
    pump.ensure(session_id="session-1", cwd=str(tmp_path))

    await _wait_until(lambda: drain_count >= 2)
    await pump.stop_all()


@pytest.mark.asyncio
async def test_settle_retries_sync_until_reply_is_available(tmp_path: Path) -> None:
    sync_count = 0
    delivered = False

    async def sync_callback(session_id: str, cwd: str) -> None:
        nonlocal sync_count, delivered
        sync_count += 1
        delivered = True

    async def drain_callback(session_id: str) -> ExternalReplyDrainResult:
        if delivered:
            return ExternalReplyDrainResult.DELIVERED
        return ExternalReplyDrainResult.NO_NEW_REPLY

    pump, _, _, _ = _pump(tmp_path, sync_callback=sync_callback, drain_callback=drain_callback)
    pump.request_settle(session_id="session-1", cwd=str(tmp_path))

    await _wait_until(lambda: sync_count >= 1)
    await pump.stop_all()


@pytest.mark.asyncio
async def test_delivery_failure_retries_without_new_revision(tmp_path: Path) -> None:
    drain_count = 0

    async def sync_callback(session_id: str, cwd: str) -> None:
        return None

    async def drain_callback(session_id: str) -> ExternalReplyDrainResult:
        nonlocal drain_count
        drain_count += 1
        if drain_count == 1:
            return ExternalReplyDrainResult.DELIVERY_FAILED
        return ExternalReplyDrainResult.DELIVERED

    pump, _, _, _ = _pump(tmp_path, sync_callback=sync_callback, drain_callback=drain_callback)
    pump.ensure(session_id="session-1", cwd=str(tmp_path))

    await _wait_until(lambda: drain_count >= 2)
    await pump.stop_all()


@pytest.mark.asyncio
async def test_removed_binding_stops_idle_pump(tmp_path: Path) -> None:
    async def sync_callback(session_id: str, cwd: str) -> None:
        return None

    async def drain_callback(session_id: str) -> ExternalReplyDrainResult:
        return ExternalReplyDrainResult.NO_NEW_REPLY

    pump, _, binding_store, _ = _pump(tmp_path, sync_callback=sync_callback, drain_callback=drain_callback)
    pump.ensure(session_id="session-1", cwd=str(tmp_path))
    await _wait_until(lambda: pump.active_count == 1)

    binding_store.remove_binding("session-1")

    await _wait_until(lambda: pump.active_count == 0)


@pytest.mark.asyncio
async def test_stop_all_blocks_new_pumps_until_reopened(tmp_path: Path) -> None:
    async def sync_callback(session_id: str, cwd: str) -> None:
        return None

    async def drain_callback(session_id: str) -> ExternalReplyDrainResult:
        return ExternalReplyDrainResult.NO_NEW_REPLY

    pump, _, _, background_tasks = _pump(
        tmp_path,
        sync_callback=sync_callback,
        drain_callback=drain_callback,
    )

    await pump.stop_all()
    pump.request_settle(session_id="session-1", cwd=str(tmp_path))
    pump.request_finalize(session_id="session-1", cwd=str(tmp_path))

    assert pump.active_count == 0
    assert background_tasks.active_count == 0

    pump.reopen()
    pump.ensure(session_id="session-1", cwd=str(tmp_path))
    await _wait_until(lambda: pump.active_count == 1)
    await pump.stop_all()


@pytest.mark.asyncio
async def test_ended_binding_exhausts_settle_before_finalizing(tmp_path: Path) -> None:
    sync_count = 0
    finalize_sync_counts: list[int] = []

    async def sync_callback(session_id: str, cwd: str) -> None:
        nonlocal sync_count
        sync_count += 1

    async def drain_callback(session_id: str) -> ExternalReplyDrainResult:
        if sync_count > 0:
            return ExternalReplyDrainResult.DELIVERED
        return ExternalReplyDrainResult.NO_NEW_REPLY

    async def finalize_callback(session_id: str) -> bool:
        finalize_sync_counts.append(sync_count)
        return True

    pump, _, binding_store, _ = _pump(
        tmp_path,
        sync_callback=sync_callback,
        drain_callback=drain_callback,
        finalize_callback=finalize_callback,
        settle_delays=(0.01, 0.01),
    )
    binding_store.mark_ended("session-1", utc_now())
    pump.ensure(session_id="session-1", cwd=str(tmp_path))

    await _wait_until(lambda: finalize_sync_counts == [2])
    await _wait_until(lambda: pump.active_count == 0)


@pytest.mark.asyncio
async def test_ended_binding_retries_failed_settle_before_finalizing(tmp_path: Path) -> None:
    sync_count = 0
    finalized = False

    async def sync_callback(session_id: str, cwd: str) -> None:
        nonlocal sync_count
        sync_count += 1
        if sync_count == 1:
            raise RuntimeError("sync failed")

    async def drain_callback(session_id: str) -> ExternalReplyDrainResult:
        return ExternalReplyDrainResult.NO_NEW_REPLY

    async def finalize_callback(session_id: str) -> bool:
        nonlocal finalized
        finalized = True
        return True

    pump, _, binding_store, _ = _pump(
        tmp_path,
        sync_callback=sync_callback,
        drain_callback=drain_callback,
        finalize_callback=finalize_callback,
        settle_delays=(0.01,),
        retry_delays=(0.03,),
    )
    binding_store.mark_ended("session-1", utc_now())
    pump.ensure(session_id="session-1", cwd=str(tmp_path))

    await _wait_until(lambda: sync_count >= 1)
    assert finalized is False
    await _wait_until(lambda: finalized)
    assert sync_count >= 2
    await _wait_until(lambda: pump.active_count == 0)


@pytest.mark.asyncio
async def test_finalization_failure_retries_without_dropping_binding(tmp_path: Path) -> None:
    finalize_count = 0

    async def sync_callback(session_id: str, cwd: str) -> None:
        return None

    async def drain_callback(session_id: str) -> ExternalReplyDrainResult:
        return ExternalReplyDrainResult.NO_NEW_REPLY

    async def finalize_callback(session_id: str) -> bool:
        nonlocal finalize_count
        finalize_count += 1
        return finalize_count >= 2

    pump, _, binding_store, _ = _pump(
        tmp_path,
        sync_callback=sync_callback,
        drain_callback=drain_callback,
        finalize_callback=finalize_callback,
        settle_delays=(),
        retry_delays=(0.02,),
    )
    binding_store.mark_ended("session-1", utc_now())
    pump.ensure(session_id="session-1", cwd=str(tmp_path))

    await _wait_until(lambda: finalize_count >= 1)
    assert binding_store.get_binding("session-1") is not None
    await _wait_until(lambda: finalize_count >= 2)
    await _wait_until(lambda: pump.active_count == 0)


@pytest.mark.asyncio
async def test_revision_does_not_bypass_delivery_retry_delay(tmp_path: Path) -> None:
    drain_times: list[float] = []

    async def sync_callback(session_id: str, cwd: str) -> None:
        return None

    async def drain_callback(session_id: str) -> ExternalReplyDrainResult:
        drain_times.append(asyncio.get_running_loop().time())
        if len(drain_times) == 1:
            return ExternalReplyDrainResult.DELIVERY_FAILED
        return ExternalReplyDrainResult.DELIVERED

    pump, session_store, _, _ = _pump(
        tmp_path,
        sync_callback=sync_callback,
        drain_callback=drain_callback,
        retry_delays=(0.08,),
        idle_check_sec=1,
    )
    state = session_store.get_or_create(session_id="session-1", workdir=str(tmp_path))
    pump.ensure(session_id="session-1", cwd=str(tmp_path))
    await _wait_until(lambda: len(drain_times) == 1)

    session_store.save(state)
    await asyncio.sleep(0.03)
    assert len(drain_times) == 1

    await _wait_until(lambda: len(drain_times) >= 2)
    assert drain_times[1] - drain_times[0] >= 0.07
    await pump.stop_all()
