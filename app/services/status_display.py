"""Telegram ChatAction status display service."""

from __future__ import annotations

import logging
from typing import Any

from aiogram.enums import ChatAction

logger = logging.getLogger(__name__)

# Tool name to ChatAction mapping
_TOOL_ACTION_MAP: dict[str, ChatAction] = {
    "Read": ChatAction.TYPING,
    "Write": ChatAction.UPLOAD_DOCUMENT,
    "Edit": ChatAction.UPLOAD_DOCUMENT,
    "MultiEdit": ChatAction.UPLOAD_DOCUMENT,
    "NotebookEdit": ChatAction.UPLOAD_DOCUMENT,
    "Bash": ChatAction.TYPING,
    "Grep": ChatAction.TYPING,
    "Glob": ChatAction.TYPING,
    "WebFetch": ChatAction.TYPING,
    "WebSearch": ChatAction.TYPING,
    "Agent": ChatAction.TYPING,
}

# Phase to ChatAction mapping
_PHASE_ACTION_MAP: dict[str, ChatAction] = {
    "thinking": ChatAction.TYPING,
    "reading": ChatAction.TYPING,
    "writing": ChatAction.UPLOAD_DOCUMENT,
    "executing": ChatAction.TYPING,
    "approval": ChatAction.TYPING,
    "compacting": ChatAction.TYPING,
    "finalizing": ChatAction.TYPING,
}


class StatusDisplayService:
    """Manages Telegram ChatAction status display for tasks."""

    def __init__(self, *, bot: Any) -> None:
        self._bot = bot
        self._current_action: dict[str, ChatAction] = {}  # task_id -> action

    async def send_typing(self, chat_id: int) -> None:
        """Send typing action."""
        await self._safe_send(chat_id, ChatAction.TYPING)

    async def send_upload_document(self, chat_id: int) -> None:
        """Send upload document action."""
        await self._safe_send(chat_id, ChatAction.UPLOAD_DOCUMENT)

    async def update_for_tool(self, *, chat_id: int, task_id: str, tool_name: str | None) -> None:
        """Update action based on tool name."""
        action = _TOOL_ACTION_MAP.get(tool_name, ChatAction.TYPING) if tool_name else ChatAction.TYPING
        old_action = self._current_action.get(task_id)

        if old_action != action:
            self._current_action[task_id] = action
            await self._safe_send(chat_id, action)

    async def update_for_phase(self, *, chat_id: int, task_id: str, phase: str) -> None:
        """Update action based on phase."""
        action = _PHASE_ACTION_MAP.get(phase, ChatAction.TYPING)
        old_action = self._current_action.get(task_id)

        if old_action != action:
            self._current_action[task_id] = action
            await self._safe_send(chat_id, action)

    async def clear(self, *, chat_id: int, task_id: str) -> None:
        """Clear action state for task."""
        self._current_action.pop(task_id, None)

    async def _safe_send(self, chat_id: int, action: ChatAction) -> None:
        """Safely send ChatAction, ignoring errors."""
        try:
            await self._bot.send_chat_action(chat_id=chat_id, action=action)
        except Exception:
            logger.debug("Failed to send chat action", extra={"chat_id": chat_id, "action": action})
