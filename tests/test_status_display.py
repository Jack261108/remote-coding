"""Tests for status display service with state machine."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.status_display import TOOL_PHASE_MAP, StatusDisplayService, TaskPhase


@pytest.fixture
def mock_bot() -> MagicMock:
    bot = MagicMock()
    bot.send_chat_action = AsyncMock()
    return bot


@pytest.fixture
def status_display(mock_bot: MagicMock) -> StatusDisplayService:
    return StatusDisplayService(bot=mock_bot)


class TestStatusDisplayStateMachine:
    def test_initial_phase(self, status_display: StatusDisplayService) -> None:
        assert status_display.get_phase("task-1") == TaskPhase.IDLE

    @pytest.mark.asyncio
    async def test_start_transition(self, status_display: StatusDisplayService, mock_bot: MagicMock) -> None:
        result = await status_display.start(task_id="task-1", chat_id=123)
        assert result is True
        assert status_display.get_phase("task-1") == TaskPhase.STARTING
        mock_bot.send_chat_action.assert_called_once()

    @pytest.mark.asyncio
    async def test_complete_transition(self, status_display: StatusDisplayService, mock_bot: MagicMock) -> None:
        await status_display.start(task_id="task-1", chat_id=123)
        mock_bot.send_chat_action.reset_mock()

        result = await status_display.complete(task_id="task-1", chat_id=123)
        assert result is True
        assert status_display.get_phase("task-1") == TaskPhase.COMPLETED
        # Completed phase sends no action
        mock_bot.send_chat_action.assert_not_called()

    @pytest.mark.asyncio
    async def test_fail_transition(self, status_display: StatusDisplayService, mock_bot: MagicMock) -> None:
        await status_display.start(task_id="task-1", chat_id=123)
        mock_bot.send_chat_action.reset_mock()

        result = await status_display.fail(task_id="task-1", chat_id=123)
        assert result is True
        assert status_display.get_phase("task-1") == TaskPhase.FAILED

    @pytest.mark.asyncio
    async def test_invalid_transition_ignored(self, status_display: StatusDisplayService, mock_bot: MagicMock) -> None:
        # Can't go directly from IDLE to COMPLETED
        result = await status_display.transition(task_id="task-1", chat_id=123, to_phase=TaskPhase.COMPLETED)
        assert result is False
        assert status_display.get_phase("task-1") == TaskPhase.IDLE

    @pytest.mark.asyncio
    async def test_tool_update_read(self, status_display: StatusDisplayService, mock_bot: MagicMock) -> None:
        await status_display.start(task_id="task-1", chat_id=123)
        mock_bot.send_chat_action.reset_mock()

        result = await status_display.update_for_tool(task_id="task-1", chat_id=123, tool_name="Read")
        assert result is True
        assert status_display.get_phase("task-1") == TaskPhase.READING

    @pytest.mark.asyncio
    async def test_tool_update_write(self, status_display: StatusDisplayService, mock_bot: MagicMock) -> None:
        await status_display.start(task_id="task-1", chat_id=123)

        result = await status_display.update_for_tool(task_id="task-1", chat_id=123, tool_name="Write")
        assert result is True
        assert status_display.get_phase("task-1") == TaskPhase.WRITING

    @pytest.mark.asyncio
    async def test_tool_update_bash(self, status_display: StatusDisplayService, mock_bot: MagicMock) -> None:
        await status_display.start(task_id="task-1", chat_id=123)

        result = await status_display.update_for_tool(task_id="task-1", chat_id=123, tool_name="Bash")
        assert result is True
        assert status_display.get_phase("task-1") == TaskPhase.EXECUTING

    @pytest.mark.asyncio
    async def test_tool_update_unknown_thinking(self, status_display: StatusDisplayService, mock_bot: MagicMock) -> None:
        await status_display.start(task_id="task-1", chat_id=123)

        result = await status_display.update_for_tool(task_id="task-1", chat_id=123, tool_name="UnknownTool")
        assert result is True
        assert status_display.get_phase("task-1") == TaskPhase.THINKING

    @pytest.mark.asyncio
    async def test_tool_update_none_thinking(self, status_display: StatusDisplayService, mock_bot: MagicMock) -> None:
        await status_display.start(task_id="task-1", chat_id=123)

        result = await status_display.update_for_tool(task_id="task-1", chat_id=123, tool_name=None)
        assert result is True
        assert status_display.get_phase("task-1") == TaskPhase.THINKING

    @pytest.mark.asyncio
    async def test_full_lifecycle(self, status_display: StatusDisplayService, mock_bot: MagicMock) -> None:
        # Start
        await status_display.start(task_id="task-1", chat_id=123)
        assert status_display.get_phase("task-1") == TaskPhase.STARTING

        # Thinking
        await status_display.update_for_tool(task_id="task-1", chat_id=123, tool_name=None)
        assert status_display.get_phase("task-1") == TaskPhase.THINKING

        # Reading
        await status_display.update_for_tool(task_id="task-1", chat_id=123, tool_name="Read")
        assert status_display.get_phase("task-1") == TaskPhase.READING

        # Writing
        await status_display.update_for_tool(task_id="task-1", chat_id=123, tool_name="Write")
        assert status_display.get_phase("task-1") == TaskPhase.WRITING

        # Executing
        await status_display.update_for_tool(task_id="task-1", chat_id=123, tool_name="Bash")
        assert status_display.get_phase("task-1") == TaskPhase.EXECUTING

        # Complete
        await status_display.complete(task_id="task-1", chat_id=123)
        assert status_display.get_phase("task-1") == TaskPhase.COMPLETED

    @pytest.mark.asyncio
    async def test_restart_after_complete(self, status_display: StatusDisplayService, mock_bot: MagicMock) -> None:
        await status_display.start(task_id="task-1", chat_id=123)
        await status_display.complete(task_id="task-1", chat_id=123)

        # Can restart after completion
        result = await status_display.start(task_id="task-1", chat_id=123)
        assert result is True
        assert status_display.get_phase("task-1") == TaskPhase.STARTING

    def test_remove(self, status_display: StatusDisplayService) -> None:
        status_display._tasks["task-1"] = MagicMock()
        status_display.remove("task-1")
        assert status_display.get_phase("task-1") == TaskPhase.IDLE

    @pytest.mark.asyncio
    async def test_safe_send_handles_exception(self, mock_bot: MagicMock) -> None:
        mock_bot.send_chat_action = AsyncMock(side_effect=Exception("API error"))
        status_display = StatusDisplayService(bot=mock_bot)

        # Should not raise
        result = await status_display.start(task_id="task-1", chat_id=123)
        assert result is True

    def test_tool_phase_map_completeness(self) -> None:
        # Verify all expected tools are mapped
        expected_tools = {"Read", "Write", "Edit", "Bash", "Grep", "Glob"}
        assert expected_tools.issubset(TOOL_PHASE_MAP.keys())
