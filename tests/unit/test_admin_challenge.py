"""Unit tests for admin_challenge helper."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.bot.handlers.admin_challenge import maybe_start_admin_challenge


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
