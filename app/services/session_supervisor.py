"""Unified per-session watcher combining interrupt detection, file sync, and JSONL sync.

Replaces three independent per-session polling tasks (InterruptWatcher,
AgentFileWatcher, debounced JSONL sync) with a single loop per session.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress

from app.domain.session_models import SessionEvent, SessionEventType, SessionPhase, SessionState
from app.infra.file_mtime_utils import clear_seen_mtimes, refresh_seen_mtimes
from app.services.claude_jsonl_parser import ClaudeJSONLParser
from app.services.session_store import SessionStore

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
        self._session_mtime_keys: dict[str, set[str]] = {}  # session_id -> tracked file paths
        self._active = False

    def watch(self, *, session_id: str, workdir: str) -> None:
        """Start watching a session if not already watched."""
        self._active = True
        task = self._tasks.get(session_id)
        if task is not None and not task.done():
            return
        self._start_watch_task(session_id=session_id, workdir=workdir)

    def _start_watch_task(self, *, session_id: str, workdir: str) -> None:
        task = asyncio.create_task(self._watch_session(session_id=session_id, workdir=workdir))
        self._tasks[session_id] = task
        task.add_done_callback(lambda done: self._on_watch_done(done, session_id=session_id, workdir=workdir))

    def _on_watch_done(self, task: asyncio.Task[None], *, session_id: str, workdir: str) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is None:
            return
        logger.exception(
            "session supervisor watcher crashed",
            extra={"session_id": session_id, "workdir": workdir},
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        current = self._tasks.get(session_id)
        if self._active and (current is None or current is task):
            self._start_watch_task(session_id=session_id, workdir=workdir)

    def schedule_jsonl_sync(self, session_id: str, cwd: str) -> None:
        """Request a debounced JSONL sync -- picked up on next poll tick."""
        self._jsonl_sync_requests[session_id] = (cwd, asyncio.get_running_loop().time())

    async def forget(self, session_id: str) -> None:
        """Stop watching a session and clean up state."""
        task = self._tasks.pop(session_id, None)
        await self._clear_session_mtimes(session_id)
        self._jsonl_sync_requests.pop(session_id, None)
        self._locks.pop(session_id, None)
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def stop_all(self) -> None:
        """Cancel all watched sessions and wait for termination."""
        self._active = False
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()
        self._locks.clear()
        self._seen_mtimes.clear()
        self._session_mtime_keys.clear()
        self._jsonl_sync_requests.clear()

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _watch_session(self, *, session_id: str, workdir: str) -> None:
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        task = asyncio.current_task()
        try:
            while self._active:
                try:
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
                                await self._refresh_seen_mtimes(state)

                    # Debounced JSONL sync (outside lock to avoid deadlock with _on_jsonl_sync)
                    await self._maybe_process_jsonl_sync(session_id)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("session supervisor tick failed", extra={"session_id": session_id, "workdir": workdir})

                await asyncio.sleep(self._poll_interval_sec)
        except asyncio.CancelledError:
            raise
        finally:
            if task is not None and self._tasks.get(session_id) is task:
                self._tasks.pop(session_id, None)
                await self._clear_session_mtimes(session_id)
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

    async def _clear_session_mtimes(self, session_id: str) -> None:
        """Remove all tracked mtime entries for a session using the key index."""
        keys = self._session_mtime_keys.pop(session_id, set())
        await clear_seen_mtimes(keys, self._seen_mtimes)

    async def _sync_files_if_needed(self, state: SessionState) -> bool:
        changed = False
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
            file_path = str(agent_file)
            mtime = agent_file.stat().st_mtime
            previous = self._seen_mtimes.get(file_path)
            if previous is not None and mtime <= previous:
                continue
            changed = True
            break
        if changed:
            self._claude_jsonl_parser.reset_state(state.claude_session_id or state.session_id)
            await self._on_jsonl_sync(state.session_id, state.workdir)
        return changed

    async def _refresh_seen_mtimes(self, state: SessionState) -> None:
        session_key = state.session_id
        # Build path set from current tool calls
        paths: set[str] = set()
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
            paths.add(str(agent_file))
        # Clear stale keys via tracked index and refresh current ones
        await self._clear_session_mtimes(session_key)
        await refresh_seen_mtimes(paths, self._seen_mtimes)
        self._session_mtime_keys[session_key] = set(paths)

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
