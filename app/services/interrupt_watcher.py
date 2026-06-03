from __future__ import annotations

import asyncio
import logging

from app.domain.session_models import SessionEvent, SessionEventType, SessionPhase, SessionState
from app.services.claude_jsonl_parser import ClaudeJSONLParser
from app.services.session_store import SessionStore
from app.services.session_watcher_base import BaseSessionWatcher

logger = logging.getLogger(__name__)


class InterruptWatcher(BaseSessionWatcher):
    def __init__(
        self,
        *,
        session_store: SessionStore,
        claude_jsonl_parser: ClaudeJSONLParser,
        poll_interval_sec: float = 0.2,
    ) -> None:
        super().__init__()
        self._session_store = session_store
        self._claude_jsonl_parser = claude_jsonl_parser
        self._poll_interval_sec = poll_interval_sec
        self._session_locks: dict[str, asyncio.Lock] = {}

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
                    self._maybe_detect_interrupt(state)
                await asyncio.sleep(self._poll_interval_sec)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("interrupt watcher failed", extra={"session_id": session_id, "workdir": workdir})
        finally:
            if task is not None and self._tasks.get(session_id) is task:
                self._tasks.pop(session_id, None)

    def _should_watch(self, state: SessionState) -> bool:
        if state.provider != "claude_code":
            return False
        return state.phase in {SessionPhase.PROCESSING, SessionPhase.WAITING_FOR_APPROVAL}

    def _maybe_detect_interrupt(self, state: SessionState) -> None:
        if state.interrupted:
            return
        claude_session_id = state.claude_session_id or state.session_id
        if not claude_session_id:
            return
        snapshot = self._claude_jsonl_parser.parse_incremental(session_id=claude_session_id, cwd=state.workdir)
        if not snapshot.interrupt_detected:
            return
        self._session_store.process(
            SessionEvent(
                session_id=claude_session_id,
                type=SessionEventType.INTERRUPT_DETECTED,
                payload=snapshot.to_payload(),
            )
        )
