from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress

from app.domain.session_models import SessionPhase, SessionState
from app.services.claude_jsonl_parser import ClaudeJSONLParser
from app.services.session_store import SessionStore

logger = logging.getLogger(__name__)


class AgentFileWatcher:
    def __init__(
        self,
        *,
        session_store: SessionStore,
        claude_jsonl_parser: ClaudeJSONLParser,
        on_update: Callable[[str, str], Awaitable[None]],
        poll_interval_sec: float = 0.2,
    ) -> None:
        self._session_store = session_store
        self._claude_jsonl_parser = claude_jsonl_parser
        self._on_update = on_update
        self._poll_interval_sec = poll_interval_sec
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._seen_mtimes: dict[str, float] = {}
        self._active = False

    def watch(self, *, session_id: str, workdir: str) -> None:
        self._active = True
        task = self._tasks.get(session_id)
        if task is not None and not task.done():
            return
        self._tasks[session_id] = asyncio.create_task(self._watch_session(session_id=session_id, workdir=workdir))

    def forget(self, session_id: str) -> None:
        task = self._tasks.pop(session_id, None)
        self._seen_mtimes.pop(session_id, None)
        if task is not None:
            task.cancel()

    async def stop_all(self) -> None:
        self._active = False
        tasks = list(self._tasks.values())
        self._tasks.clear()
        self._seen_mtimes.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task

    async def _watch_session(self, *, session_id: str, workdir: str) -> None:
        lock = self._session_locks.setdefault(session_id, asyncio.Lock())
        task = asyncio.current_task()
        try:
            while self._active:
                async with lock:
                    state = self._session_store.get(session_id)
                    if state is None:
                        return
                    if not self._should_watch(state):
                        return
                    if await self._sync_if_needed(state):
                        self._refresh_seen_mtimes(state)
                await asyncio.sleep(self._poll_interval_sec)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("agent file watcher failed", extra={"session_id": session_id, "workdir": workdir})
        finally:
            if task is not None and self._tasks.get(session_id) is task:
                self._tasks.pop(session_id, None)

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

    async def _sync_if_needed(self, state: SessionState) -> bool:
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
            await self._on_update(state.session_id, state.workdir)
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
