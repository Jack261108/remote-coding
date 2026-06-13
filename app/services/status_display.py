"""Telegram ChatAction status display service with state machine."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from aiogram.enums import ChatAction

logger = logging.getLogger(__name__)


class TaskPhase(StrEnum):
    """Task execution phases."""

    IDLE = "idle"
    STARTING = "starting"
    THINKING = "thinking"
    READING = "reading"
    WRITING = "writing"
    EXECUTING = "executing"
    APPROVAL = "approval"
    COMPLETED = "completed"
    FAILED = "failed"


# Valid transitions: from_phase -> (to_phase, chat_action)
TRANSITIONS: dict[TaskPhase, dict[TaskPhase, ChatAction | None]] = {
    TaskPhase.IDLE: {
        TaskPhase.STARTING: ChatAction.TYPING,
        TaskPhase.COMPLETED: None,
        TaskPhase.FAILED: None,
    },
    TaskPhase.STARTING: {
        TaskPhase.THINKING: ChatAction.TYPING,
        TaskPhase.READING: ChatAction.TYPING,
        TaskPhase.WRITING: ChatAction.UPLOAD_DOCUMENT,
        TaskPhase.EXECUTING: ChatAction.TYPING,
        TaskPhase.APPROVAL: ChatAction.TYPING,
        TaskPhase.COMPLETED: None,
        TaskPhase.FAILED: None,
    },
    TaskPhase.THINKING: {
        TaskPhase.READING: ChatAction.TYPING,
        TaskPhase.WRITING: ChatAction.UPLOAD_DOCUMENT,
        TaskPhase.EXECUTING: ChatAction.TYPING,
        TaskPhase.APPROVAL: ChatAction.TYPING,
        TaskPhase.COMPLETED: None,
        TaskPhase.FAILED: None,
    },
    TaskPhase.READING: {
        TaskPhase.THINKING: ChatAction.TYPING,
        TaskPhase.WRITING: ChatAction.UPLOAD_DOCUMENT,
        TaskPhase.EXECUTING: ChatAction.TYPING,
        TaskPhase.APPROVAL: ChatAction.TYPING,
        TaskPhase.COMPLETED: None,
        TaskPhase.FAILED: None,
    },
    TaskPhase.WRITING: {
        TaskPhase.THINKING: ChatAction.TYPING,
        TaskPhase.READING: ChatAction.TYPING,
        TaskPhase.EXECUTING: ChatAction.TYPING,
        TaskPhase.APPROVAL: ChatAction.TYPING,
        TaskPhase.COMPLETED: None,
        TaskPhase.FAILED: None,
    },
    TaskPhase.EXECUTING: {
        TaskPhase.THINKING: ChatAction.TYPING,
        TaskPhase.READING: ChatAction.TYPING,
        TaskPhase.WRITING: ChatAction.UPLOAD_DOCUMENT,
        TaskPhase.APPROVAL: ChatAction.TYPING,
        TaskPhase.COMPLETED: None,
        TaskPhase.FAILED: None,
    },
    TaskPhase.APPROVAL: {
        TaskPhase.THINKING: ChatAction.TYPING,
        TaskPhase.READING: ChatAction.TYPING,
        TaskPhase.WRITING: ChatAction.UPLOAD_DOCUMENT,
        TaskPhase.EXECUTING: ChatAction.TYPING,
        TaskPhase.COMPLETED: None,
        TaskPhase.FAILED: None,
    },
    TaskPhase.COMPLETED: {
        TaskPhase.STARTING: ChatAction.TYPING,
    },
    TaskPhase.FAILED: {
        TaskPhase.STARTING: ChatAction.TYPING,
    },
}

# Tool name to phase mapping
TOOL_PHASE_MAP: dict[str, TaskPhase] = {
    "Read": TaskPhase.READING,
    "Grep": TaskPhase.READING,
    "Glob": TaskPhase.READING,
    "Write": TaskPhase.WRITING,
    "Edit": TaskPhase.WRITING,
    "MultiEdit": TaskPhase.WRITING,
    "NotebookEdit": TaskPhase.WRITING,
    "Bash": TaskPhase.EXECUTING,
    "WebFetch": TaskPhase.EXECUTING,
    "WebSearch": TaskPhase.EXECUTING,
    "Agent": TaskPhase.EXECUTING,
    "TaskCreate": TaskPhase.EXECUTING,
    "TaskUpdate": TaskPhase.EXECUTING,
}


@dataclass
class TaskState:
    """State machine for a single task."""

    task_id: str
    chat_id: int
    current_phase: TaskPhase = TaskPhase.IDLE


class StatusDisplayService:
    """Manages Telegram ChatAction status display using state machine."""

    def __init__(self, *, bot: Any) -> None:
        self._bot = bot
        self._tasks: dict[str, TaskState] = {}

    def get_phase(self, task_id: str) -> TaskPhase:
        """Get current phase for a task."""
        state = self._tasks.get(task_id)
        return state.current_phase if state else TaskPhase.IDLE

    async def transition(self, *, task_id: str, chat_id: int, to_phase: TaskPhase) -> bool:
        """Transition task to new phase.

        Returns True if transition was valid and executed.
        """
        state = self._tasks.get(task_id)

        if state is None:
            # Task not found - only allow STARTING transition
            if to_phase != TaskPhase.STARTING:
                logger.debug(
                    "Invalid transition for unknown task",
                    extra={"task_id": task_id, "to": to_phase.value},
                )
                return False
            state = TaskState(task_id=task_id, chat_id=chat_id, current_phase=TaskPhase.IDLE)
            self._tasks[task_id] = state

        from_phase = state.current_phase

        # Check if transition is valid
        valid_targets = TRANSITIONS.get(from_phase, {})
        if to_phase not in valid_targets:
            logger.debug(
                "Invalid transition ignored",
                extra={"task_id": task_id, "from": from_phase.value, "to": to_phase.value},
            )
            return False

        # Execute transition
        action = valid_targets[to_phase]
        state.current_phase = to_phase

        if action is not None:
            await self._send_chat_action(chat_id, action)

        logger.debug(
            "Phase transition",
            extra={"task_id": task_id, "from": from_phase.value, "to": to_phase.value, "action": action},
        )
        return True

    async def start(self, *, task_id: str, chat_id: int) -> bool:
        """Start a task."""
        return await self.transition(task_id=task_id, chat_id=chat_id, to_phase=TaskPhase.STARTING)

    async def complete(self, *, task_id: str, chat_id: int) -> bool:
        """Mark task as completed."""
        return await self.transition(task_id=task_id, chat_id=chat_id, to_phase=TaskPhase.COMPLETED)

    async def fail(self, *, task_id: str, chat_id: int) -> bool:
        """Mark task as failed."""
        return await self.transition(task_id=task_id, chat_id=chat_id, to_phase=TaskPhase.FAILED)

    async def update_for_tool(self, *, task_id: str, chat_id: int, tool_name: str | None) -> bool:
        """Update phase based on tool name."""
        phase = TOOL_PHASE_MAP.get(tool_name, TaskPhase.THINKING) if tool_name else TaskPhase.THINKING
        return await self.transition(task_id=task_id, chat_id=chat_id, to_phase=phase)

    async def send_typing(self, chat_id: int) -> None:
        """Send typing ChatAction."""
        await self._send_chat_action(chat_id, ChatAction.TYPING)

    async def clear(self, *, chat_id: int, task_id: str) -> None:
        """Remove task state and stop displaying status."""
        self._tasks.pop(task_id, None)

    def remove(self, task_id: str) -> None:
        """Remove task state."""
        self._tasks.pop(task_id, None)

    async def _send_chat_action(self, chat_id: int, action: ChatAction) -> None:
        """Safely send ChatAction, ignoring errors."""
        try:
            await self._bot.send_chat_action(chat_id=chat_id, action=action)
        except Exception:
            logger.debug("Failed to send chat action", extra={"chat_id": chat_id, "action": action})
