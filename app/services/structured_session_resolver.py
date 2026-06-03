from __future__ import annotations

import logging
from datetime import timedelta

from app.adapters.cli.factory import CLIAdapterFactory
from app.adapters.storage.memory import MemoryTaskStore
from app.domain.session_models import SessionState, is_claude_session_id
from app.services.session_lookup_service import SessionLookupService, _same_workdir
from app.services.session_notifier import SessionNotifier
from app.services.session_service import SessionService
from app.services.session_store import SessionStore
from app.services.structured_reply_tracker import StructuredReplyTracker

logger = logging.getLogger(__name__)


class StructuredSessionResolver:
    def __init__(
        self,
        *,
        session_service: SessionService,
        task_store: MemoryTaskStore,
        cli_factory: CLIAdapterFactory,
        lookup: SessionLookupService | None = None,
        tracker: StructuredReplyTracker | None = None,
        notifier: SessionNotifier | None = None,
        # Backward compat: accept old kwarg and extract components
        structured_session_store: SessionStore | None = None,
    ) -> None:
        self._session_service = session_service
        self._task_store = task_store
        self._cli_factory = cli_factory

        # If new-style dependencies are provided, use them directly.
        # Otherwise, extract from the old SessionStore facade for backward compat.
        if lookup is not None or tracker is not None or notifier is not None:
            self._lookup = lookup
            self._tracker = tracker
            self._notifier = notifier
        elif structured_session_store is not None:
            self._lookup = structured_session_store._lookup
            self._tracker = structured_session_store._tracker
            self._notifier = structured_session_store._notifier
        else:
            self._lookup = None
            self._tracker = None
            self._notifier = None

    async def get_structured_session(self, user_id: int, *, log_missing: bool = True) -> SessionState | None:
        session = await self._session_service.get(user_id)
        if session is None:
            if log_missing:
                logger.info("structured session lookup failed", extra={"user_id": user_id, "reason": "no_session"})
            return None
        return self._lookup_structured_session(
            user_id=user_id,
            provider=session.provider,
            workdir=session.workdir,
            claude_session_id=session.claude_session_id,
            terminal_id=session.terminal_id,
            claude_chat_active=session.claude_chat_active,
            log_missing=log_missing,
        )

    async def get_structured_session_for_task(self, *, task_id: str, user_id: int, log_missing: bool = True) -> SessionState | None:
        task = await self._task_store.get(task_id)
        if task is None or task.user_id != user_id:
            if log_missing:
                logger.info("structured session lookup failed", extra={"user_id": user_id, "task_id": task_id, "reason": "task_not_found"})
            return None
        session = await self._session_service.get(user_id)
        terminal_id = None
        claude_session_id = task.claude_session_id
        claude_chat_active = False
        if session is not None and session.provider == "claude_code" and _same_workdir(session.workdir, task.workdir):
            terminal_id = session.terminal_id
            claude_chat_active = session.claude_chat_active

        prompt_matched_state = None
        if self._lookup is not None and terminal_id and task.prompt and task.started_at is not None:
            prompt_matched_state = self._lookup.find_by_user_turn_text(
                user_id=user_id,
                workdir=task.workdir,
                text=task.prompt,
                since=task.started_at - timedelta(seconds=2),
                until=task.ended_at,
                terminal_id=terminal_id,
            )
        if prompt_matched_state is not None and is_claude_session_id(
            prompt_matched_state.claude_session_id or prompt_matched_state.session_id
        ):
            if task.claude_session_id != prompt_matched_state.session_id:
                task.claude_session_id = prompt_matched_state.session_id
                await self._task_store.save(task)
            logger.info(
                "structured session lookup hit prompt turn",
                extra={
                    "user_id": user_id,
                    "task_id": task_id,
                    "session_id": prompt_matched_state.session_id,
                    "claude_session_id": prompt_matched_state.claude_session_id,
                    "turn_count": len(prompt_matched_state.turns),
                },
            )
            return prompt_matched_state

        return self._lookup_structured_session(
            user_id=user_id,
            provider=task.provider,
            workdir=task.workdir,
            claude_session_id=claude_session_id,
            terminal_id=terminal_id,
            claude_chat_active=claude_chat_active,
            log_missing=log_missing,
        )

    async def get_structured_session_for_scope(self, *, user_id: int, task_id: str | None, log_missing: bool) -> SessionState | None:
        if task_id is not None:
            return await self.get_structured_session_for_task(task_id=task_id, user_id=user_id, log_missing=log_missing)
        return await self.get_structured_session(user_id, log_missing=log_missing)

    def _lookup_structured_session(
        self,
        *,
        user_id: int,
        provider: str,
        workdir: str,
        claude_session_id: str | None,
        terminal_id: str | None,
        claude_chat_active: bool,
        log_missing: bool,
    ) -> SessionState | None:
        if provider != "claude_code":
            if log_missing:
                logger.info(
                    "structured session lookup failed",
                    extra={"user_id": user_id, "provider": provider, "workdir": workdir, "reason": "not_claude_provider"},
                )
            return None

        explicit_claude_session_id = claude_session_id if is_claude_session_id(claude_session_id) else None

        if self._lookup is not None:
            state = None
            matched_by = None
            if explicit_claude_session_id is not None:
                state = self._lookup._get(explicit_claude_session_id)
                matched_by = "claude_session_id"
                if state is not None and not _same_workdir(state.workdir, workdir):
                    if log_missing:
                        logger.info(
                            "structured session lookup skipped",
                            extra={
                                "user_id": user_id,
                                "claude_session_id": explicit_claude_session_id,
                                "workdir": workdir,
                                "state_workdir": state.workdir,
                                "reason": "claude_session_workdir_mismatch",
                            },
                        )
                    state = None
                    matched_by = None
            if state is None and terminal_id:
                candidate = self._lookup.find_by_terminal_id(terminal_id)
                if candidate is not None and is_claude_session_id(candidate.claude_session_id or candidate.session_id):
                    if _same_workdir(candidate.workdir, workdir):
                        state = candidate
                        matched_by = "terminal_id"
                    elif log_missing:
                        logger.info(
                            "structured session lookup skipped",
                            extra={
                                "user_id": user_id,
                                "terminal_id": terminal_id,
                                "workdir": workdir,
                                "state_workdir": candidate.workdir,
                                "reason": "terminal_workdir_mismatch",
                            },
                        )
            if state is not None:
                logger.info(
                    "structured session lookup hit store",
                    extra={
                        "user_id": user_id,
                        "matched_by": matched_by,
                        "claude_session_id": state.claude_session_id,
                        "session_id": state.session_id,
                        "phase": state.phase.value,
                        "turn_count": len(state.turns),
                    },
                )
                return state

        if explicit_claude_session_id is None:
            if log_missing:
                logger.info(
                    "structured session lookup failed",
                    extra={
                        "user_id": user_id,
                        "reason": "no_lookup_identity",
                        "provider": provider,
                        "workdir": workdir,
                        "terminal_id": terminal_id,
                        "claude_chat_active": claude_chat_active,
                    },
                )
            return None

        getter = getattr(self._cli_factory, "get_claude_session_state", None) or getattr(self._cli_factory, "get_session_state", None)
        if getter is None:
            if log_missing:
                logger.info(
                    "structured session lookup failed",
                    extra={"user_id": user_id, "claude_session_id": explicit_claude_session_id, "reason": "no_getter"},
                )
            return None
        state = getter(explicit_claude_session_id)
        if state is not None and not _same_workdir(state.workdir, workdir):
            if log_missing:
                logger.info(
                    "structured session lookup skipped",
                    extra={
                        "user_id": user_id,
                        "claude_session_id": explicit_claude_session_id,
                        "workdir": workdir,
                        "state_workdir": state.workdir,
                        "reason": "fallback_workdir_mismatch",
                    },
                )
            return None
        logger.info(
            "structured session lookup fallback",
            extra={
                "user_id": user_id,
                "claude_session_id": explicit_claude_session_id,
                "state_found": state is not None,
                "phase": state.phase.value if state is not None else None,
                "turn_count": len(state.turns) if state is not None else 0,
            },
        )
        return state

    async def is_state_owned_by_user(self, *, state: SessionState | None, user_id: int) -> bool:
        if state is None:
            return False
        if state.user_id is not None:
            return state.user_id == user_id

        session = await self._session_service.get(user_id)
        if session is None or session.provider != "claude_code":
            return False

        if session.claude_session_id:
            if state.session_id == session.claude_session_id or state.claude_session_id == session.claude_session_id:
                return True

        if session.terminal_id and state.terminal_id and session.terminal_id == state.terminal_id:
            return _same_workdir(session.workdir, state.workdir)

        return False

    async def get_structured_session_cursor(self, user_id: int, *, task_id: str | None = None) -> int:
        if self._lookup is None:
            return 0
        state = await self.get_structured_session_for_scope(user_id=user_id, task_id=task_id, log_missing=False)
        if state is None:
            return 0
        return self._lookup.get_cursor(state.session_id)

    async def get_structured_session_revision(self, user_id: int) -> int:
        return await self.get_structured_session_cursor(user_id)

    async def get_structured_reply_cursor(self, user_id: int, *, task_id: str | None = None) -> tuple[str | None, str | None]:
        if self._tracker is None:
            return None, None
        state = await self.get_structured_session_for_scope(user_id=user_id, task_id=task_id, log_missing=False)
        if state is None:
            return None, None
        return self._tracker.get_structured_reply_cursor(state.session_id)

    async def acknowledge_structured_reply(
        self, user_id: int, *, turn_id: str | None = None, permission_key: str | None = None, task_id: str | None = None
    ) -> None:
        if self._tracker is None:
            return
        state = await self.get_structured_session_for_scope(user_id=user_id, task_id=task_id, log_missing=False)
        if state is None:
            return
        if turn_id is not None:
            self._tracker.mark_structured_reply_emitted(state.session_id, turn_id=turn_id)
        if permission_key is not None:
            self._tracker.mark_structured_permission_emitted(state.session_id, permission_key=permission_key)

    async def get_structured_user_question_cursor_for_task(self, user_id: int, *, task_id: str) -> str | None:
        if self._tracker is None:
            return None
        state = await self.get_structured_session_for_scope(user_id=user_id, task_id=task_id, log_missing=False)
        if state is None:
            return None
        return self._tracker.get_structured_user_question_cursor(state.session_id)

    async def acknowledge_structured_user_question_for_task(self, user_id: int, *, question_key: str | None, task_id: str) -> None:
        if self._tracker is None:
            return
        state = await self.get_structured_session_for_scope(user_id=user_id, task_id=task_id, log_missing=False)
        if state is None:
            return
        if question_key is not None:
            self._tracker.mark_structured_user_question_emitted(state.session_id, question_key=question_key)

    async def wait_for_structured_session_update(
        self, *, user_id: int, since_cursor: int, timeout_sec: float, task_id: str | None = None
    ) -> bool:
        if self._notifier is None:
            return False
        state = await self.get_structured_session_for_scope(user_id=user_id, task_id=task_id, log_missing=False)
        if state is None:
            return False
        return await self._notifier.wait_for_publish(state.session_id, since_cursor=since_cursor, timeout_sec=timeout_sec)

    async def wait_for_structured_session_change(self, *, user_id: int, since_revision: int, timeout_sec: float) -> bool:
        return await self.wait_for_structured_session_update(user_id=user_id, since_cursor=since_revision, timeout_sec=timeout_sec)
