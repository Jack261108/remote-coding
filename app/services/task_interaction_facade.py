from __future__ import annotations

from app.domain.session_models import SessionState
from app.domain.user_question_models import UserQuestionPrompt
from app.services.permission_service import PermissionService
from app.services.structured_session_resolver import StructuredSessionResolver
from app.services.user_question_service import UserQuestionService


class TaskInteractionFacade:
    def __init__(
        self,
        *,
        structured_session_resolver: StructuredSessionResolver,
        user_question_service: UserQuestionService,
        permission_service: PermissionService,
    ) -> None:
        self._structured_session_resolver = structured_session_resolver
        self._user_question_service = user_question_service
        self._permission_service = permission_service

    async def get_structured_session(self, user_id: int, *, log_missing: bool = True) -> SessionState | None:
        return await self._structured_session_resolver.get_structured_session(user_id, log_missing=log_missing)

    async def get_structured_session_for_task(self, *, task_id: str, user_id: int, log_missing: bool = True) -> SessionState | None:
        return await self._structured_session_resolver.get_structured_session_for_task(
            task_id=task_id,
            user_id=user_id,
            log_missing=log_missing,
        )

    async def get_structured_session_for_scope(self, *, user_id: int, task_id: str | None, log_missing: bool) -> SessionState | None:
        return await self._structured_session_resolver.get_structured_session_for_scope(
            user_id=user_id,
            task_id=task_id,
            log_missing=log_missing,
        )

    def lookup_structured_session(
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
        return self._structured_session_resolver._lookup_structured_session(
            user_id=user_id,
            provider=provider,
            workdir=workdir,
            claude_session_id=claude_session_id,
            terminal_id=terminal_id,
            claude_chat_active=claude_chat_active,
            log_missing=log_missing,
        )

    def is_claude_session_id(self, session_id: str | None) -> bool:
        return self._structured_session_resolver._is_claude_session_id(session_id)

    async def is_state_owned_by_user(self, *, state: SessionState | None, user_id: int) -> bool:
        return await self._structured_session_resolver.is_state_owned_by_user(state=state, user_id=user_id)

    async def get_structured_session_cursor(self, user_id: int, *, task_id: str | None = None) -> int:
        return await self._structured_session_resolver.get_structured_session_cursor(user_id, task_id=task_id)

    async def get_structured_session_revision(self, user_id: int) -> int:
        return await self._structured_session_resolver.get_structured_session_revision(user_id)

    async def get_structured_reply_cursor(self, user_id: int, *, task_id: str | None = None) -> tuple[str | None, str | None]:
        return await self._structured_session_resolver.get_structured_reply_cursor(user_id, task_id=task_id)

    async def acknowledge_structured_reply(
        self,
        user_id: int,
        *,
        turn_id: str | None = None,
        permission_key: str | None = None,
        task_id: str | None = None,
    ) -> None:
        await self._structured_session_resolver.acknowledge_structured_reply(
            user_id,
            turn_id=turn_id,
            permission_key=permission_key,
            task_id=task_id,
        )

    async def get_structured_user_question_cursor(self, user_id: int, *, task_id: str | None = None) -> str | None:
        if task_id is not None:
            return await self._structured_session_resolver.get_structured_user_question_cursor_for_task(user_id, task_id=task_id)
        return await self._user_question_service.get_structured_user_question_cursor(user_id)

    async def acknowledge_structured_user_question(
        self, user_id: int, *, question_key: str | None = None, task_id: str | None = None
    ) -> None:
        if task_id is not None:
            await self._structured_session_resolver.acknowledge_structured_user_question_for_task(
                user_id,
                question_key=question_key,
                task_id=task_id,
            )
            return
        await self._user_question_service.acknowledge_structured_user_question(user_id, question_key=question_key)

    async def wait_for_structured_session_update(
        self, *, user_id: int, since_cursor: int, timeout_sec: float, task_id: str | None = None
    ) -> bool:
        return await self._structured_session_resolver.wait_for_structured_session_update(
            user_id=user_id,
            since_cursor=since_cursor,
            timeout_sec=timeout_sec,
            task_id=task_id,
        )

    async def wait_for_structured_session_change(self, *, user_id: int, since_revision: int, timeout_sec: float) -> bool:
        return await self._structured_session_resolver.wait_for_structured_session_change(
            user_id=user_id,
            since_revision=since_revision,
            timeout_sec=timeout_sec,
        )

    async def get_pending_user_questions(self, user_id: int) -> tuple[UserQuestionPrompt, ...]:
        return await self._user_question_service.get_pending_user_questions(user_id)

    async def answer_pending_user_question_option(
        self,
        *,
        user_id: int,
        tool_use_id: str,
        question_index: int,
        option_index: int,
    ) -> tuple[bool, str, UserQuestionPrompt | None]:
        return await self._user_question_service.answer_pending_user_question_option(
            user_id=user_id,
            tool_use_id=tool_use_id,
            question_index=question_index,
            option_index=option_index,
        )

    async def toggle_pending_user_question_multi_select_option(
        self,
        *,
        user_id: int,
        tool_use_id: str,
        question_index: int,
        option_index: int,
    ) -> tuple[bool, str, UserQuestionPrompt | None, frozenset[int] | None]:
        return await self._user_question_service.toggle_pending_user_question_multi_select_option(
            user_id=user_id,
            tool_use_id=tool_use_id,
            question_index=question_index,
            option_index=option_index,
        )

    async def submit_pending_user_question_multi_select(
        self,
        *,
        user_id: int,
        tool_use_id: str,
        question_index: int,
    ) -> tuple[bool, str, UserQuestionPrompt | None]:
        return await self._user_question_service.submit_pending_user_question_multi_select(
            user_id=user_id,
            tool_use_id=tool_use_id,
            question_index=question_index,
        )

    async def answer_pending_user_question_text(
        self,
        *,
        user_id: int,
        text: str,
    ) -> tuple[bool, str, UserQuestionPrompt | None]:
        return await self._user_question_service.answer_pending_user_question_text(user_id=user_id, text=text)

    def extract_user_question_prompts_for_tool_use_id(
        self,
        state: SessionState | None,
        *,
        tool_use_id: str,
    ) -> tuple[UserQuestionPrompt, ...]:
        return self._user_question_service._extract_user_question_prompts_for_tool_use_id(
            state,
            tool_use_id=tool_use_id,
        )

    def ensure_user_question_draft(self, *, user_id: int, prompts: tuple[UserQuestionPrompt, ...]) -> object:
        return self._user_question_service._ensure_user_question_draft(user_id=user_id, prompts=prompts)

    async def respond_to_pending_permission(
        self,
        *,
        user_id: int,
        decision: str,
        reason: str | None = None,
        expected_tool_use_id: str | None = None,
    ) -> tuple[bool, str]:
        return await self._permission_service.respond_to_pending_permission(
            user_id=user_id,
            decision=decision,
            reason=reason,
            expected_tool_use_id=expected_tool_use_id,
        )
