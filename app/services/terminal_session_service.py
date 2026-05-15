from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from app.adapters.cli.factory import CLIAdapterFactory
from app.config.settings import Settings, is_workdir_allowed
from app.domain.models import SessionContext
from app.services.session_service import SessionService


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
    ) -> None:
        self._settings = settings
        self._session_service = session_service
        self._cli_factory = cli_factory
        self._clear_user_questions = clear_user_questions

    async def resolve_for_task(self, *, user_id: int, provider: str, workdir: str) -> TaskTerminalContext:
        terminal_mode = provider == "claude_code" and self._settings.claude_tmux_mode
        session = await self._session_service.get_or_create(
            user_id=user_id,
            provider=provider,
            workdir=workdir,
            terminal_mode=terminal_mode,
        )
        terminal_key = session.terminal_id if session.terminal_mode else None
        interactive = bool(
            terminal_key
            and provider == "claude_code"
            and session.claude_chat_active
            and self._settings.claude_tmux_mode
        )
        return TaskTerminalContext(session=session, terminal_key=terminal_key, interactive=interactive)

    async def close_terminal(self, user_id: int) -> tuple[bool, str]:
        session = await self._session_service.get(user_id)
        if session is None:
            return False, "当前无 session"
        if not session.terminal_mode or not session.terminal_id:
            if session.claude_chat_active:
                session.claude_session_id = None
                await self._session_service.clear_claude_session(user_id=user_id)
                await self._session_service.switch(user_id=user_id, claude_chat_active=False)
                self._clear_user_questions(user_id)
                return True, "Claude 会话已退出"
            return False, "当前没有可关闭的持久终端"

        closed = await self._cli_factory.close_terminal(session.terminal_id)
        if not closed:
            return False, "终端不存在或关闭失败"

        session.claude_session_id = None
        await self._session_service.clear_claude_session(user_id=user_id)
        await self._session_service.switch(user_id=user_id, terminal_mode=False, claude_chat_active=False)
        self._clear_user_questions(user_id)
        return True, "终端已关闭"

    async def open_claude_chat_session(self, user_id: int, *, workdir: str | None = None) -> tuple[bool, str]:
        session = await self._session_service.get(user_id)
        had_old_terminal = bool(session and session.terminal_mode and session.terminal_id)
        self._clear_user_questions(user_id)
        if session is not None:
            await self._session_service.clear_claude_session(user_id=user_id)
        if had_old_terminal:
            closed, text = await self.close_terminal(user_id)
            if not closed and text != "终端不存在或关闭失败":
                return False, f"旧终端关闭失败: {text}"

        workdir_source = workdir or (session.workdir if session else self._settings.default_workdir)
        selected_workdir = str(Path(workdir_source).resolve())
        if workdir is None and not Path(selected_workdir).is_dir():
            selected_workdir = str(Path(self._settings.default_workdir).resolve())
        if not self._is_workdir_allowed(selected_workdir):
            raise ValueError("workdir 不在 ALLOWED_WORKDIRS 白名单内")
        if not Path(selected_workdir).is_dir():
            return False, f"workdir 不存在或不是目录: {selected_workdir}"

        updated_session = await self._session_service.switch(
            user_id=user_id,
            provider="claude_code",
            workdir=selected_workdir,
            terminal_mode=True,
            claude_chat_active=True,
        )

        if not updated_session.terminal_id:
            return False, "会话创建失败: terminal_id 为空"

        ensure_result = await self.ensure_and_reveal_terminal(
            terminal_id=updated_session.terminal_id,
            workdir=updated_session.workdir,
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

    async def bind_claude_session(self, *, user_id: int, claude_session_id: str, workdir: str | None = None) -> None:
        await self._session_service.bind_claude_session(
            user_id=user_id,
            claude_session_id=claude_session_id,
            workdir=workdir,
        )

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
