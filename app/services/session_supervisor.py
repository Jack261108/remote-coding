"""Unified per-session watcher combining interrupt detection, file sync, and JSONL sync.

Replaces three independent per-session polling tasks (InterruptWatcher,
AgentFileWatcher, debounced JSONL sync) with a single loop per session.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import TYPE_CHECKING

from app.domain.session_models import SessionEvent, SessionEventType, SessionPhase, SessionState
from app.services.claude_jsonl_parser import ClaudeJSONLParser
from app.services.session_store import SessionStore

if TYPE_CHECKING:
    from app.services.jsonl_file_watcher import JSONLFileWatcher

logger = logging.getLogger(__name__)


class SessionSupervisor:
    """Unified per-session watcher combining interrupt detection, file sync, and JSONL sync."""

    def __init__(
        self,
        *,
        session_store: SessionStore,
        claude_jsonl_parser: ClaudeJSONLParser,
        on_jsonl_sync: Callable[[str, str], Awaitable[None]],
        on_dispatch_event: Callable[[SessionEvent], Awaitable[None]],
        poll_interval_sec: float = 0.2,
        debounce_sec: float = 0.1,
        jsonl_file_watcher: JSONLFileWatcher | None = None,
    ) -> None:
        self._session_store = session_store
        self._claude_jsonl_parser = claude_jsonl_parser
        self._on_jsonl_sync = on_jsonl_sync
        self._on_dispatch_event = on_dispatch_event
        self._poll_interval_sec = poll_interval_sec
        self._debounce_sec = debounce_sec
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._jsonl_sync_requests: dict[str, tuple[str, float]] = {}  # session_id -> (cwd, requested_at)
        self._seen_mtimes: dict[str, float] = {}
        self._active = False
        self._wake: asyncio.Event = asyncio.Event()
        self._jsonl_file_watcher = jsonl_file_watcher

    def watch(self, *, session_id: str, workdir: str) -> None:
        """Start watching a session if not already watched."""
        self._active = True
        task = self._tasks.get(session_id)
        if task is not None and not task.done():
            return
        if self._jsonl_file_watcher is not None:
            self._jsonl_file_watcher.add(session_id, workdir)
        self._tasks[session_id] = asyncio.create_task(self._watch_session(session_id=session_id, workdir=workdir))

    def schedule_jsonl_sync(self, session_id: str, cwd: str) -> None:
        """Request a debounced JSONL sync -- picked up on next poll tick.

        Must be called from the asyncio event loop thread (directly from async
        code or via ``loop.call_soon_threadsafe``).
        """
        self._jsonl_sync_requests[session_id] = (cwd, asyncio.get_running_loop().time())
        self._wake.set()

    def forget(self, session_id: str) -> None:
        """Stop watching a session and clean up state."""
        task = self._tasks.pop(session_id, None)
        self._clear_seen_mtimes(session_id)
        self._jsonl_sync_requests.pop(session_id, None)
        if self._jsonl_file_watcher is not None:
            self._jsonl_file_watcher.remove(session_id)
        self._locks.pop(session_id, None)
        if task is not None:
            task.cancel()

    async def stop_all(self) -> None:
        """Cancel all watched sessions and wait for termination."""
        self._active = False
        self._wake.set()
        tasks = list(self._tasks.values())
        self._tasks.clear()
        self._locks.clear()
        self._seen_mtimes.clear()
        self._jsonl_sync_requests.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _watch_session(self, *, session_id: str, workdir: str) -> None:
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        task = asyncio.current_task()
        try:
            while self._active:
                async with lock:
                    state = self._session_store.get(session_id)
                    if state is None:
                        return

                    # Interrupt detection (PROCESSING, WAITING_FOR_APPROVAL)
                    if state.phase in {SessionPhase.PROCESSING, SessionPhase.WAITING_FOR_APPROVAL}:
                        await self._maybe_detect_interrupt(state)

                    # Agent file sync (any phase with subagent containers)
                    if self._has_subagent_files(state):
                        if await self._sync_files_if_needed(state):
                            self._refresh_seen_mtimes(state)

                # Debounced JSONL sync (outside lock to avoid deadlock with _on_jsonl_sync)
                await self._maybe_process_jsonl_sync(session_id)

                # Wait for wake signal or timeout (event-driven instead of polling)
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=self._poll_interval_sec)
                except TimeoutError:
                    pass
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("session supervisor failed", extra={"session_id": session_id, "workdir": workdir})
        finally:
            if task is not None and self._tasks.get(session_id) is task:
                self._tasks.pop(session_id, None)
                self._clear_seen_mtimes(session_id)
            # Always clean lock created by this watch_session invocation
            if self._locks.get(session_id) is lock:
                self._locks.pop(session_id, None)

    # ── Interrupt detection ───────────────────────────────────────────────────

    async def _maybe_detect_interrupt(self, state: SessionState) -> None:
        if state.interrupted:
            return
        claude_session_id = state.claude_session_id or state.session_id
        if not claude_session_id:
            return
        snapshot = self._claude_jsonl_parser.parse_incremental(session_id=claude_session_id, cwd=state.workdir)
        if not snapshot.interrupt_detected:
            return
        await self._on_dispatch_event(
            SessionEvent(
                session_id=claude_session_id,
                type=SessionEventType.INTERRUPT_DETECTED,
                payload=snapshot.to_payload(),
            )
        )

    # ── Agent file sync ───────────────────────────────────────────────────────

    def _has_subagent_files(self, state: SessionState) -> bool:
        return any(tool.is_subagent_container for tool in state.tool_calls.values())

    async def _sync_files_if_needed(self, state: SessionState) -> bool:
        changed = False
        session_key = state.session_id
        for tool in state.tool_calls.values():
            if not tool.is_subagent_container:
                continue
            result = tool.structured_result or {}
            agent_id = str(result.get("agentId") or "")
            if not agent_id:
                continue
            agent_file = self._claude_jsonl_parser.subagent_file_path(
                session_id=state.claude_session_id or state.session_id,
                agent_id=agent_id,
                cwd=state.workdir,
            )
            if not agent_file.exists():
                continue
            mtime = agent_file.stat().st_mtime
            key = f"{session_key}:{tool.tool_use_id}:{agent_id}"
            previous = self._seen_mtimes.get(key)
            if previous is not None and mtime <= previous:
                continue
            changed = True
            break
        if changed:
            self._claude_jsonl_parser.reset_state(state.claude_session_id or state.session_id)
            await self._on_jsonl_sync(state.session_id, state.workdir)
        return changed

    def _refresh_seen_mtimes(self, state: SessionState) -> None:
        session_key = state.session_id
        stale_keys = [key for key in self._seen_mtimes if key.startswith(f"{session_key}:")]
        for key in stale_keys:
            self._seen_mtimes.pop(key, None)
        for tool in state.tool_calls.values():
            if not tool.is_subagent_container:
                continue
            result = tool.structured_result or {}
            agent_id = str(result.get("agentId") or "")
            if not agent_id:
                continue
            agent_file = self._claude_jsonl_parser.subagent_file_path(
                session_id=state.claude_session_id or state.session_id,
                agent_id=agent_id,
                cwd=state.workdir,
            )
            if not agent_file.exists():
                continue
            key = f"{session_key}:{tool.tool_use_id}:{agent_id}"
            self._seen_mtimes[key] = agent_file.stat().st_mtime

    def _clear_seen_mtimes(self, session_id: str) -> None:
        prefix = f"{session_id}:"
        stale_keys = [key for key in self._seen_mtimes if key == session_id or key.startswith(prefix)]
        for key in stale_keys:
            self._seen_mtimes.pop(key, None)

    # ── Debounced JSONL sync ──────────────────────────────────────────────────

    async def _maybe_process_jsonl_sync(self, session_id: str) -> None:
        entry = self._jsonl_sync_requests.get(session_id)
        if entry is None:
            return
        cwd, requested_at = entry
        now = asyncio.get_running_loop().time()
        if now - requested_at < self._debounce_sec:
            return
        self._jsonl_sync_requests.pop(session_id, None)
        try:
            await self._on_jsonl_sync(session_id, cwd)
        except Exception:
            logger.exception("jsonl sync failed", extra={"session_id": session_id})
