"""Tests for status display service."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.status_display import StatusDisplayService


@pytest.fixture
def mock_bot() -> MagicMock:
    bot = MagicMock()
    bot.send_chat_action = AsyncMock()
    return bot


@pytest.fixture
def status_display(mock_bot: MagicMock) -> StatusDisplayService:
    return StatusDisplayService(bot=mock_bot)


class TestStatusDisplay:
    @pytest.mark.asyncio
    async def test_send_typing(self, status_display: StatusDisplayService, mock_bot: MagicMock) -> None:
        await status_display.send_typing(chat_id=123)
        mock_bot.send_chat_action.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_upload_document(self, status_display: StatusDisplayService, mock_bot: MagicMock) -> None:
        await status_display.send_upload_document(chat_id=123)
        mock_bot.send_chat_action.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_for_tool_read(self, status_display: StatusDisplayService, mock_bot: MagicMock) -> None:
        await status_display.update_for_tool(chat_id=123, task_id="test-task", tool_name="Read")
        mock_bot.send_chat_action.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_for_tool_write(self, status_display: StatusDisplayService, mock_bot: MagicMock) -> None:
        await status_display.update_for_tool(chat_id=123, task_id="test-task", tool_name="Write")
        mock_bot.send_chat_action.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_for_tool_bash(self, status_display: StatusDisplayService, mock_bot: MagicMock) -> None:
        await status_display.update_for_tool(chat_id=123, task_id="test-task", tool_name="Bash")
        mock_bot.send_chat_action.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_for_tool_none(self, status_display: StatusDisplayService, mock_bot: MagicMock) -> None:
        await status_display.update_for_tool(chat_id=123, task_id="test-task", tool_name=None)
        mock_bot.send_chat_action.assert_called_once()

    @pytest.mark.asyncio
    async def test_clear(self, status_display: StatusDisplayService, mock_bot: MagicMock) -> None:
        await status_display.clear(chat_id=123, task_id="test-task")
        # Clear doesn't send any action, just removes internal state
        assert "test-task" not in status_display._current_action

    @pytest.mark.asyncio
    async def test_safe_send_handles_exception(self, mock_bot: MagicMock) -> None:
        mock_bot.send_chat_action = AsyncMock(side_effect=Exception("API error"))
        status_display = StatusDisplayService(bot=mock_bot)
        # Should not raise
        await status_display._safe_send(chat_id=123, action="typing")
