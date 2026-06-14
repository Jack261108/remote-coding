from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from app.domain.session_models import SessionPhase, SessionState
from app.services.claude_jsonl_parser import ClaudeJSONLParser
from app.services.session_store import SessionStore
from app.services.session_watcher_base import BaseSessionWatcher

logger = logging.getLogger(__name__)


class AgentFileWatcher(BaseSessionWatcher):
    def __init__(
        self,
        *,
        session_store: SessionStore,
        claude_jsonl_parser: ClaudeJSONLParser,
        on_update: Callable[[str, str], Awaitable[None]],
        poll_interval_sec: float = 0.2,
    ) -> None:
        super().__init__()
        self._session_store = session_store
        self._claude_jsonl_parser = claude_jsonl_parser
        self._on_update = on_update
        self._poll_interval_sec = poll_interval_sec
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._seen_mtimes: dict[str, float] = {}

    def _clear_seen_mtimes_for_session(self, session_id: str) -> None:
        prefix = f"{session_id}:"
        stale_keys = [key for key in self._seen_mtimes if key.startswith(prefix)]
        for key in stale_keys:
            self._seen_mtimes.pop(key, None)

    def _cleanup_finished_session(self, *, session_id: str, task: asyncio.Task[None] | None) -> None:
        active_task = self._tasks.get(session_id)
        if active_task is not None and active_task is not task:
            return
        if active_task is task:
            self._tasks.pop(session_id, None)
        if active_task is None or active_task is task:
            self._clear_seen_mtimes_for_session(session_id)
            self._session_locks.pop(session_id, None)

    def forget(self, *, session_id: str) -> None:
        self._clear_seen_mtimes_for_session(session_id)
        task = self._tasks.get(session_id)
        if task is None or task.done():
            self._session_locks.pop(session_id, None)
        super().forget(session_id=session_id)

    async def stop_all(self) -> None:
        self._seen_mtimes.clear()
        await super().stop_all()
        self._session_locks.clear()

    async def _watch_session(self, *, session_id: str, workdir: str) -> None:
        lock = self._session_locks.setdefault(session_id, asyncio.Lock())
        task = asyncio.current_task()
        try:
            while self._active:
                needs_update = False
                async with lock:
                    state = self._session_store.get(session_id)
                    if state is None:
                        return
                    if not self._should_watch(state):
                        return
                    if await self._check_changed(state):
                        self._refresh_seen_mtimes(state)
                        needs_update = True
                if needs_update:
                    await self._on_update(session_id, workdir)
                await asyncio.sleep(self._poll_interval_sec)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("agent file watcher failed", extra={"session_id": session_id, "workdir": workdir})
        finally:
            self._cleanup_finished_session(session_id=session_id, task=task)

    def _should_watch(self, state: SessionState) -> bool:
        if state.provider != "claude_code":
            return False
        if state.phase not in {
            SessionPhase.IDLE,
            SessionPhase.PROCESSING,
            SessionPhase.WAITING_FOR_APPROVAL,
            SessionPhase.WAITING_FOR_INPUT,
        }:
            return False
        return any(tool.is_subagent_container for tool in state.tool_calls.values())

    async def _check_changed(self, state: SessionState) -> bool:
        """Check if any subagent files have changed. Returns True if changed."""
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
            try:
                mtime = agent_file.stat().st_mtime
            except OSError:
                continue
            key = f"{session_key}:{tool.tool_use_id}:{agent_id}"
            previous = self._seen_mtimes.get(key)
            if previous is not None and mtime <= previous:
                continue
            changed = True
            break
        if changed:
            self._claude_jsonl_parser.reset_state(state.claude_session_id or state.session_id)
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
            try:
                mtime = agent_file.stat().st_mtime
            except OSError:
                continue
            key = f"{session_key}:{tool.tool_use_id}:{agent_id}"
            self._seen_mtimes[key] = mtime
