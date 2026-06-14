from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from app.adapters.cli.factory import CLIAdapterFactory
from app.config.settings import Settings, is_workdir_allowed
from app.domain.models import SessionContext
from app.services.auto_approve_service import AutoApproveService
from app.services.session_service import SessionService

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TaskTerminalContext:
    session: SessionContext
    terminal_key: str | None
    interactive: bool


class TerminalSessionService:
    def __init__(
        self,
        *,
        settings: Settings,
        session_service: SessionService,
        cli_factory: CLIAdapterFactory,
        clear_user_questions: Callable[[int], None],
        auto_approve_service: AutoApproveService | None = None,
    ) -> None:
        self._settings = settings
        self._session_service = session_service
        self._cli_factory = cli_factory
        self._clear_user_questions = clear_user_questions
        self._auto_approve_service = auto_approve_service

    async def cleanup_orphaned_terminal(self, orphaned_terminal_id: str, *, claude_session_id: str | None, user_id: int) -> None:
        logger.info(
            "cleaning up orphaned terminal",
            extra={
                "terminal_id": orphaned_terminal_id,
                "claude_session_id": claude_session_id,
                "user_id": user_id,
            },
        )
        contexts = [ctx for ctx in await self._session_service.list_all() if ctx.terminal_id == orphaned_terminal_id]
        claude_session_ids = sorted({ctx.claude_session_id for ctx in contexts if ctx.claude_session_id})
        if claude_session_id:
            claude_session_ids.append(claude_session_id)
        if self._auto_approve_service is not None:
            for session_id in sorted(set(claude_session_ids)):
                await self._auto_approve_service.clear_session(session_id)
        affected_user_ids = await self._session_service.clear_terminal_group(orphaned_terminal_id)
        for affected_user_id in affected_user_ids or [user_id]:
            self._clear_user_questions(affected_user_id)

    async def resolve_for_task(self, *, user_id: int, provider: str, workdir: str) -> TaskTerminalContext:
        terminal_mode = provider == "claude_code" and self._settings.claude_tmux_mode
        session, orphaned = await self._session_service.get_or_create(
            user_id=user_id,
            provider=provider,
            workdir=workdir,
            terminal_mode=terminal_mode,
        )
        # Clean up orphaned terminal resources if detected
        if orphaned is not None:
            await self.cleanup_orphaned_terminal(
                orphaned.terminal_id,
                claude_session_id=orphaned.claude_session_id,
                user_id=orphaned.user_id,
            )
        terminal_key = session.terminal_id if session.terminal_mode else None
        interactive = bool(terminal_key and provider == "claude_code" and session.claude_chat_active and self._settings.claude_tmux_mode)
        return TaskTerminalContext(session=session, terminal_key=terminal_key, interactive=interactive)

    async def close_terminal(self, user_id: int) -> tuple[bool, str]:
        async def _exit_claude_chat_only(session: SessionContext) -> tuple[bool, str]:
            if self._auto_approve_service is not None and session.claude_session_id:
                await self._auto_approve_service.clear_session(session.claude_session_id)
            await self._session_service.clear_claude_session(user_id=user_id)
            await self._session_service.switch(user_id=user_id, claude_chat_active=False)
            self._clear_user_questions(user_id)
            return True, "Claude 会话已退出"

        while True:
            session = await self._session_service.get(user_id)
            if session is None:
                return False, "当前无 session"
            if not session.terminal_mode or not session.terminal_id:
                if session.claude_chat_active:
                    return await _exit_claude_chat_only(session)
                return False, "当前没有可关闭的持久终端"

            terminal_id = session.terminal_id
            async with self._session_service.terminal_group_lock(terminal_id):
                current = await self._session_service.get(user_id)
                if current is None:
                    return False, "当前无 session"
                if not current.terminal_mode or not current.terminal_id:
                    if current.claude_chat_active:
                        return await _exit_claude_chat_only(current)
                    return False, "当前没有可关闭的持久终端"
                if current.terminal_id != terminal_id:
                    continue

                close_result = await self._cli_factory.close_terminal(terminal_id)
                if isinstance(close_result, tuple):
                    closed, close_text = close_result
                else:
                    closed, close_text = close_result, "终端不存在"
                if not closed:
                    return False, close_text

                contexts = [ctx for ctx in await self._session_service.list_all() if ctx.terminal_id == terminal_id]
                claude_session_ids = sorted({ctx.claude_session_id for ctx in contexts if ctx.claude_session_id})
                if self._auto_approve_service is not None:
                    for session_id in claude_session_ids:
                        await self._auto_approve_service.clear_session(session_id)
                affected_user_ids = await self._session_service.clear_terminal_group(terminal_id)
            for affected_user_id in affected_user_ids:
                self._clear_user_questions(affected_user_id)
            return True, "终端已关闭"

    async def _prepare_claude_session(self, user_id: int, workdir: str | None) -> tuple[SessionContext, str, bool] | str:
        """公共准备逻辑：清理旧会话、解析 workdir、创建新会话。

        Returns:
            成功: (session, selected_workdir, had_old_terminal)
            失败: 错误信息字符串
        """
        session = await self._session_service.get(user_id)
        had_old_terminal = bool(session and session.terminal_mode and session.terminal_id)
        self._clear_user_questions(user_id)
        if had_old_terminal:
            closed, text = await self.close_terminal(user_id)
            if not closed:
                if text != "终端不存在":
                    return f"旧终端关闭失败: {text}"
                if session is not None and session.terminal_id:
                    await self.cleanup_orphaned_terminal(
                        session.terminal_id,
                        claude_session_id=session.claude_session_id,
                        user_id=user_id,
                    )
                else:
                    await self._session_service.clear_claude_session(user_id=user_id)
        elif session is not None:
            await self._session_service.clear_claude_session(user_id=user_id)

        workdir_source = workdir or (session.workdir if session else self._settings.default_workdir)
        selected_workdir = str(Path(workdir_source).resolve())
        if workdir is None and not Path(selected_workdir).is_dir():
            selected_workdir = str(Path(self._settings.default_workdir).resolve())
        if not self._is_workdir_allowed(selected_workdir):
            raise ValueError("workdir 不在 ALLOWED_WORKDIRS 白名单内")
        if not Path(selected_workdir).is_dir():
            return f"workdir 不存在或不是目录: {selected_workdir}"

        updated_session, orphaned = await self._session_service.switch(
            user_id=user_id,
            provider="claude_code",
            workdir=selected_workdir,
            terminal_mode=True,
            claude_chat_active=True,
        )
        # Clean up orphaned terminal resources if detected
        if orphaned is not None:
            await self.cleanup_orphaned_terminal(
                orphaned.terminal_id,
                claude_session_id=orphaned.claude_session_id,
                user_id=orphaned.user_id,
            )

        if not updated_session.terminal_id:
            return "会话创建失败: terminal_id 为空"

        return updated_session, selected_workdir, had_old_terminal

    async def open_claude_chat_session(self, user_id: int, *, workdir: str | None = None) -> tuple[bool, str]:
        result = await self._prepare_claude_session(user_id, workdir)
        if isinstance(result, str):
            return False, result
        session, selected_workdir, had_old_terminal = result
        assert session.terminal_id

        ensure_result = await self.ensure_and_reveal_terminal(
            terminal_id=session.terminal_id,
            workdir=session.workdir,
            reveal=True,
            interactive=True,
        )
        if not ensure_result[0]:
            await self._session_service.switch(user_id=user_id, terminal_mode=False, claude_chat_active=False)
            return False, ensure_result[1]

        detail = ensure_result[1]
        if detail.startswith("未能自动打开桌面终端:"):
            return True, detail

        action = "Claude 会话已重建" if had_old_terminal else "Claude 会话已开启"
        message = action
        if detail:
            message = f"{message}\n{detail}"
        return True, message

    async def open_claude_resume_session(self, user_id: int, session_id: str, *, workdir: str | None = None) -> tuple[bool, str]:
        result = await self._prepare_claude_session(user_id, workdir)
        if isinstance(result, str):
            return False, result
        session, selected_workdir, had_old_terminal = result
        assert session.terminal_id

        # Use the resume-specific ensure method
        ensured, err = await self._cli_factory.ensure_claude_resume_session(
            terminal_key=session.terminal_id,
            workdir=session.workdir,
            session_id=session_id,
        )
        if not ensured:
            await self._session_service.switch(user_id=user_id, terminal_mode=False, claude_chat_active=False)
            return False, err

        # Reveal the terminal
        revealed, reveal_text = await self._cli_factory.reveal_terminal(session.terminal_id)

        # Bind the claude_session_id to the resumed session
        await self._session_service.bind_claude_session(
            user_id=user_id,
            claude_session_id=session_id,
            workdir=selected_workdir,
        )

        if not revealed:
            return True, f"Claude 会话已恢复\n未能自动打开桌面终端: {reveal_text}"
        message = "Claude 会话已恢复"
        if reveal_text:
            message = f"{message}\n{reveal_text}"
        return True, message

    async def ensure_and_reveal_terminal(
        self,
        *,
        terminal_id: str,
        workdir: str,
        reveal: bool,
        interactive: bool = False,
    ) -> tuple[bool, str]:
        if interactive:
            ensured, err = await self._cli_factory.ensure_claude_interactive_session(
                terminal_key=terminal_id,
                workdir=workdir,
            )
        else:
            ensured, err = await self._cli_factory.ensure_terminal(terminal_key=terminal_id, workdir=workdir)

        if not ensured:
            return False, err

        if not reveal:
            return True, ""

        revealed, reveal_text = await self._cli_factory.reveal_terminal(terminal_id)
        if revealed:
            return True, reveal_text
        return True, f"未能自动打开桌面终端: {reveal_text}"

    def _is_workdir_allowed(self, workdir: str) -> bool:
        return is_workdir_allowed(workdir, self._settings.allowed_workdirs)
