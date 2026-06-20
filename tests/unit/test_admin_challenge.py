"""Unit tests for admin_challenge helper."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.bot.handlers.admin_challenge import maybe_handle_admin_password_text, maybe_start_admin_challenge
from app.services.admin_password_service import AdminPasswordService


@pytest.fixture
def message() -> AsyncMock:
    msg = AsyncMock()
    msg.answer = AsyncMock()
    return msg


@pytest.fixture
def admin_service() -> MagicMock:
    svc = MagicMock()
    svc.is_enabled = True
    svc.start_challenge = MagicMock(return_value=True)
    return svc


class FakeSessionService:
    def __init__(self) -> None:
        self.switch_calls: list[dict[str, object]] = []

    async def switch(self, *, user_id: int, provider: str | None = None, workdir: str | None = None):
        self.switch_calls.append({"user_id": user_id, "provider": provider, "workdir": workdir})
        return SimpleNamespace(session_id="session-1", provider=provider, workdir=workdir, claude_chat_active=False), None


class FakeTaskService:
    def __init__(self) -> None:
        self.cleaned: list[tuple[str, str | None, int]] = []

    async def cleanup_orphaned_terminal(self, terminal_id: str, *, claude_session_id: str | None = None, user_id: int) -> None:
        self.cleaned.append((terminal_id, claude_session_id, user_id))


class TestMaybeStartAdminChallenge:
    @pytest.mark.asyncio
    async def test_returns_false_when_service_is_none(self, message: AsyncMock) -> None:
        result = await maybe_start_admin_challenge(message, 42, "/some/dir", "session", None)
        assert result is False
        message.answer.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_returns_false_when_service_not_enabled(self, message: AsyncMock, admin_service: MagicMock) -> None:
        admin_service.is_enabled = False
        result = await maybe_start_admin_challenge(message, 42, "/some/dir", "session", admin_service)
        assert result is False
        message.answer.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_returns_true_and_sends_error_when_workdir_not_exists(self, message: AsyncMock, admin_service: MagicMock) -> None:
        with patch("app.bot.handlers.admin_challenge.Path") as mock_path_cls:
            mock_path = MagicMock()
            mock_path.is_dir.return_value = False
            mock_path_cls.return_value = mock_path
            result = await maybe_start_admin_challenge(message, 42, "/nonexistent", "session", admin_service)

        assert result is True
        message.answer.assert_awaited_once()
        assert "不存在" in message.answer.call_args[0][0]

    @pytest.mark.asyncio
    async def test_returns_true_when_challenge_already_pending(self, message: AsyncMock, admin_service: MagicMock) -> None:
        admin_service.start_challenge.return_value = False
        with patch("app.bot.handlers.admin_challenge.Path") as mock_path_cls:
            mock_path = MagicMock()
            mock_path.is_dir.return_value = True
            mock_path_cls.return_value = mock_path
            result = await maybe_start_admin_challenge(message, 42, "/some/dir", "session", admin_service)

        assert result is True
        message.answer.assert_awaited_once()
        assert "已有待处理" in message.answer.call_args[0][0]

    @pytest.mark.asyncio
    async def test_returns_true_on_normal_challenge_start(self, message: AsyncMock, admin_service: MagicMock) -> None:
        with patch("app.bot.handlers.admin_challenge.Path") as mock_path_cls:
            mock_path = MagicMock()
            mock_path.is_dir.return_value = True
            mock_path_cls.return_value = mock_path
            result = await maybe_start_admin_challenge(message, 42, "/some/dir", "session", admin_service)

        assert result is True
        admin_service.start_challenge.assert_called_once_with(42, "/some/dir", "session")
        message.answer.assert_awaited_once()
        assert "密码" in message.answer.call_args[0][0]


class TestMaybeHandleAdminPasswordText:
    @pytest.mark.asyncio
    async def test_returns_false_without_pending_challenge(self, message: AsyncMock) -> None:
        service = AdminPasswordService("secret")
        result = await maybe_handle_admin_password_text(
            message,
            task_service=FakeTaskService(),
            session_service=FakeSessionService(),
            admin_password_service=service,
        )

        assert result is False
        message.answer.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_wrong_password_keeps_challenge(self, message: AsyncMock, tmp_path) -> None:
        message.text = "wrong"
        message.from_user = SimpleNamespace(id=42)
        service = AdminPasswordService("secret")
        service.start_challenge(42, str(tmp_path), "session", provider="claude_code")

        result = await maybe_handle_admin_password_text(
            message,
            task_service=FakeTaskService(),
            session_service=FakeSessionService(),
            admin_password_service=service,
        )

        assert result is True
        assert service.has_pending(42) is True
        assert "密码错误" in message.answer.await_args.args[0]

    @pytest.mark.asyncio
    async def test_correct_password_switches_session(self, message: AsyncMock, tmp_path) -> None:
        message.text = "secret"
        message.from_user = SimpleNamespace(id=42)
        service = AdminPasswordService("secret")
        service.start_challenge(42, str(tmp_path), "session", provider="claude_code")
        session_service = FakeSessionService()

        result = await maybe_handle_admin_password_text(
            message,
            task_service=FakeTaskService(),
            session_service=session_service,
            admin_password_service=service,
        )

        assert result is True
        assert service.has_pending(42) is False
        assert session_service.switch_calls == [{"user_id": 42, "provider": "claude_code", "workdir": str(tmp_path)}]
        assert "session 已更新" in message.answer.await_args.args[0]
