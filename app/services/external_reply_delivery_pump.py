from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from enum import StrEnum

from app.services.background_task_registry import BackgroundTaskRegistry
from app.services.external_binding_store import ExternalBindingStore
from app.services.session_store import SessionStore

logger = logging.getLogger(__name__)


class ExternalReplyDrainResult(StrEnum):
    DELIVERED = "delivered"
    NO_NEW_REPLY = "no_new_reply"
    DELIVERY_FAILED = "delivery_failed"


@dataclass
class _PumpSlot:
    session_id: str
    cwd: str
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    settle_generation: int = 0
    processed_settle_generation: int = 0
    finalize_requested: bool = False
    task: asyncio.Task[None] | None = None


class ExternalReplyDeliveryPump:
    """Continuously drains synchronized replies for bound external sessions."""

    def __init__(
        self,
        *,
        session_store: SessionStore,
        binding_store: ExternalBindingStore,
        background_tasks: BackgroundTaskRegistry,
        sync_callback: Callable[[str, str], Awaitable[None]],
        drain_callback: Callable[[str], Awaitable[ExternalReplyDrainResult]],
        finalize_callback: Callable[[str], Awaitable[bool]],
        settle_delays: tuple[float, ...] = (0.1, 0.25, 0.5, 1.0),
        retry_delays: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0, 30.0),
        idle_check_sec: float = 5.0,
    ) -> None:
        self._session_store = session_store
        self._binding_store = binding_store
        self._background_tasks = background_tasks
        self._sync_callback = sync_callback
        self._drain_callback = drain_callback
        self._finalize_callback = finalize_callback
        self._settle_delays = settle_delays
        self._retry_delays = retry_delays
        self._idle_check_sec = idle_check_sec
        self._slots: dict[str, _PumpSlot] = {}
        self._closing = False

    @property
    def active_count(self) -> int:
        return len(self._slots)

    def reopen(self) -> None:
        self._closing = False

    def ensure(self, *, session_id: str, cwd: str) -> None:
        if self._closing:
            return
        slot = self._slots.get(session_id)
        if slot is not None and slot.task is not None and not slot.task.done():
            slot.cwd = cwd
            return

        slot = _PumpSlot(session_id=session_id, cwd=cwd)
        task = self._background_tasks.spawn(self._run(slot))
        slot.task = task
        self._slots[session_id] = slot
        task.add_done_callback(lambda done: self._remove_slot(session_id, slot, done))

    def request_settle(self, *, session_id: str, cwd: str) -> None:
        self.ensure(session_id=session_id, cwd=cwd)
        slot = self._slots.get(session_id)
        if slot is None:
            return
        slot.cwd = cwd
        slot.settle_generation += 1
        slot.wake.set()

    def request_finalize(self, *, session_id: str, cwd: str) -> None:
        self.ensure(session_id=session_id, cwd=cwd)
        slot = self._slots.get(session_id)
        if slot is None:
            return
        slot.cwd = cwd
        slot.finalize_requested = True
        slot.settle_generation += 1
        slot.wake.set()

    async def stop(self, session_id: str) -> None:
        slot = self._slots.pop(session_id, None)
        if slot is None or slot.task is None:
            return
        slot.task.cancel()
        with suppress(asyncio.CancelledError):
            await slot.task

    async def stop_all(self) -> None:
        self._closing = True
        slots = list(self._slots.values())
        self._slots.clear()
        for slot in slots:
            if slot.task is not None:
                slot.task.cancel()
        for slot in slots:
            if slot.task is not None:
                with suppress(asyncio.CancelledError):
                    await slot.task

    def _remove_slot(self, session_id: str, slot: _PumpSlot, task: asyncio.Task[None]) -> None:
        if task.done() and self._slots.get(session_id) is slot:
            self._slots.pop(session_id, None)

    async def _run(self, slot: _PumpSlot) -> None:
        retry_index = 0
        while True:
            binding = self._binding_store.get_binding(slot.session_id)
            if binding is None:
                return
            slot.cwd = binding.cwd
            if getattr(binding, "ended_at", None) is not None and not slot.finalize_requested:
                slot.finalize_requested = True
                slot.settle_generation += 1

            cursor_before = self._session_store.get_publish_cursor(slot.session_id)
            result = await self._drain(slot.session_id)
            cursor_after = self._session_store.get_publish_cursor(slot.session_id)
            if result == ExternalReplyDrainResult.DELIVERY_FAILED:
                await asyncio.sleep(self._retry_delay(retry_index))
                retry_index += 1
                continue
            retry_index = 0
            if cursor_after != cursor_before:
                continue

            if slot.processed_settle_generation < slot.settle_generation:
                generation = slot.settle_generation
                settle_result = await self._settle(slot, exhaustive=slot.finalize_requested)
                if settle_result == ExternalReplyDrainResult.DELIVERY_FAILED:
                    await asyncio.sleep(self._retry_delay(retry_index))
                    retry_index += 1
                    continue
                slot.processed_settle_generation = generation
                continue

            if slot.finalize_requested:
                if await self._finalize(slot.session_id):
                    return
                await asyncio.sleep(self._retry_delay(retry_index))
                retry_index += 1
                continue

            await self._wait_for_activity(slot, since_cursor=cursor_after, timeout_sec=self._idle_check_sec)

    async def _settle(self, slot: _PumpSlot, *, exhaustive: bool = False) -> ExternalReplyDrainResult:
        outcome = ExternalReplyDrainResult.NO_NEW_REPLY
        sync_failed = False
        for delay in self._settle_delays:
            await asyncio.sleep(delay)
            binding = self._binding_store.get_binding(slot.session_id)
            if binding is None:
                return ExternalReplyDrainResult.NO_NEW_REPLY
            slot.cwd = binding.cwd
            try:
                await self._sync_callback(slot.session_id, slot.cwd)
            except asyncio.CancelledError:
                raise
            except Exception:
                sync_failed = True
                logger.exception("external reply settle sync failed", extra={"session_id": slot.session_id, "cwd": slot.cwd})
                continue

            result = await self._drain(slot.session_id)
            if result == ExternalReplyDrainResult.DELIVERY_FAILED:
                return result
            if result == ExternalReplyDrainResult.DELIVERED:
                outcome = result
                if not exhaustive:
                    return result
        if sync_failed:
            return ExternalReplyDrainResult.DELIVERY_FAILED
        return outcome

    def _retry_delay(self, retry_index: int) -> float:
        return self._retry_delays[min(retry_index, len(self._retry_delays) - 1)]

    async def _finalize(self, session_id: str) -> bool:
        try:
            return await self._finalize_callback(session_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("external reply finalization failed", extra={"session_id": session_id})
            return False

    async def _drain(self, session_id: str) -> ExternalReplyDrainResult:
        try:
            return await self._drain_callback(session_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("external reply drain failed", extra={"session_id": session_id})
            return ExternalReplyDrainResult.DELIVERY_FAILED

    async def _wait_for_activity(self, slot: _PumpSlot, *, since_cursor: int, timeout_sec: float) -> None:
        if slot.wake.is_set():
            slot.wake.clear()
            return

        wake_task = asyncio.create_task(slot.wake.wait())
        publish_task = asyncio.create_task(
            self._session_store.wait_for_publish(
                slot.session_id,
                since_cursor=since_cursor,
                timeout_sec=timeout_sec,
            )
        )
        try:
            await asyncio.wait({wake_task, publish_task}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (wake_task, publish_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(wake_task, publish_task, return_exceptions=True)
            if slot.wake.is_set():
                slot.wake.clear()
