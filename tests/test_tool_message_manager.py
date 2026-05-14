from __future__ import annotations

import pytest
from aiogram.enums import ParseMode

from app.bot.presenters.structured_reply_presenter import ToolStatusOutput
from app.bot.presenters.tool_message_manager import ToolMessageManager
from app.domain.session_models import ToolStatus


class DummyTelegramMessage:
    def __init__(self, text: str, parse_mode=None) -> None:
        self.text = text
        self.parse_mode = parse_mode
        self.edits: list[str] = []
        self.edit_parse_modes: list[ParseMode | None] = []
        self.fail_next_edit = False

    async def edit_text(self, text: str, parse_mode=None) -> "DummyTelegramMessage":
        if self.fail_next_edit:
            self.fail_next_edit = False
            raise RuntimeError("edit failed")
        self.text = text
        self.edits.append(text)
        self.edit_parse_modes.append(parse_mode)
        return self


class DummyRootMessage:
    def __init__(self) -> None:
        self.sent: list[DummyTelegramMessage] = []
        self.fail_next_answer = False

    async def answer(self, text: str, parse_mode=None) -> DummyTelegramMessage:
        if self.fail_next_answer:
            self.fail_next_answer = False
            raise RuntimeError("send failed")
        message = DummyTelegramMessage(text, parse_mode=parse_mode)
        self.sent.append(message)
        return message


def _output(status: ToolStatus | str, *, tool_use_id: str = "tool-1") -> ToolStatusOutput:
    status_value = status.value if isinstance(status, ToolStatus) else status
    return ToolStatusOutput(
        tool_use_id=tool_use_id,
        tool_name="Bash",
        tool_input={"command": "pytest -q"},
        status=status_value,
    )


@pytest.mark.asyncio
async def test_tool_message_manager_sends_first_status_message() -> None:
    root = DummyRootMessage()
    manager = ToolMessageManager(root_message=root, task_id="task-1", user_id=1, provider="claude_code")

    await manager.handle(_output(ToolStatus.RUNNING))

    assert len(root.sent) == 1
    assert "执行中" in root.sent[0].text
    assert "工具: Bash" in root.sent[0].text
    assert "命令: pytest -q" in root.sent[0].text
    assert root.sent[0].parse_mode == ParseMode.HTML


@pytest.mark.asyncio
async def test_tool_message_manager_edits_existing_message_to_success() -> None:
    root = DummyRootMessage()
    manager = ToolMessageManager(root_message=root, task_id="task-1", user_id=1, provider="claude_code")

    await manager.handle(_output(ToolStatus.RUNNING))
    await manager.handle(_output(ToolStatus.SUCCESS))

    assert len(root.sent) == 1
    assert "执行完成" in root.sent[0].text
    assert root.sent[0].edits
    assert "执行完成" in root.sent[0].edits[-1]


@pytest.mark.asyncio
async def test_tool_message_manager_keeps_error_status_message() -> None:
    root = DummyRootMessage()
    manager = ToolMessageManager(root_message=root, task_id="task-1", user_id=1, provider="claude_code")

    await manager.handle(_output(ToolStatus.RUNNING))
    await manager.handle(_output(ToolStatus.ERROR))

    assert len(root.sent) == 1
    assert "执行失败" in root.sent[0].text
    assert "执行失败" in root.sent[0].edits[-1]


@pytest.mark.asyncio
async def test_tool_message_manager_sends_success_when_no_existing_message() -> None:
    root = DummyRootMessage()
    manager = ToolMessageManager(root_message=root, task_id="task-1", user_id=1, provider="claude_code")

    await manager.handle(_output(ToolStatus.SUCCESS))

    assert len(root.sent) == 1
    assert "执行完成" in root.sent[0].text


@pytest.mark.asyncio
async def test_tool_message_manager_re_sends_when_edit_fails() -> None:
    root = DummyRootMessage()
    manager = ToolMessageManager(root_message=root, task_id="task-1", user_id=1, provider="claude_code")

    await manager.handle(_output(ToolStatus.RUNNING))
    root.sent[0].fail_next_edit = True
    await manager.handle(_output(ToolStatus.INTERRUPTED))

    assert len(root.sent) == 2
    assert "已中断" in root.sent[1].text


@pytest.mark.asyncio
async def test_tool_message_manager_does_not_raise_when_send_fails() -> None:
    root = DummyRootMessage()
    root.fail_next_answer = True
    manager = ToolMessageManager(root_message=root, task_id="task-1", user_id=1, provider="claude_code")

    await manager.handle(_output(ToolStatus.RUNNING))

    assert root.sent == []
