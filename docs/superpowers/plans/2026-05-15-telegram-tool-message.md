# Telegram Tool Message Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 Telegram 端为每个非交互 Claude 工具调用维护独立状态消息，并在成功后保留“执行完成”最终状态。

**Architecture:** `SessionStore` 继续作为工具状态来源；`StructuredReplyPresenter` 把工具状态变化转换成 `ToolStatusOutput`；新增 `ToolMessageManager` 负责 Telegram 工具消息发送与编辑。普通 Claude 输出仍走 `ChunkSender`，权限请求和 `AskUserQuestion` 仍走现有带按钮消息。

**Tech Stack:** Python 3.11, aiogram 3, pytest, pytest-asyncio, ruff.

---

## Pre-flight

当前仓库已有与本功能无关的未提交文件。执行实现前应在干净隔离环境中工作，推荐用 `superpowers:using-git-worktrees` 创建工作区；如果不使用 worktree，至少先运行：

```bash
git -C /Users/jack/project/remote-coding status --short
```

只允许本计划列出的 `app/...` 和 `tests/...` 文件进入功能提交。不要 stage 现有的附件回传设计/计划文件。

运行 Python 相关命令前检查 pyenv 项目虚拟环境：

```bash
cd /Users/jack/project/remote-coding && pyenv version
```

Expected: 输出包含 `remote-coding`，因为项目根目录 `.python-version` 内容为 `remote-coding`。

## File Structure

- Modify: `app/bot/presenters/structured_reply_presenter.py`
  - 新增 `ToolStatusOutput`。
  - 新增 `build_tool_status_message()`。
  - 将非交互工具状态变化从 `ProgressUpdateOutput` 改为 `ToolStatusOutput`。
  - 保留 `ProgressUpdateOutput` 给 compacting 等非工具进度。

- Create: `app/bot/presenters/tool_message_manager.py`
  - 管理 `tool_use_id -> Telegram Message`。
  - 首次状态发送消息，后续状态编辑同一条消息。
  - `SUCCESS` 编辑为“执行完成”并保留。
  - Telegram 发送/编辑失败时记录日志并降级，不打断任务。

- Modify: `app/bot/handlers/command_run.py`
  - 导入并实例化 `ToolMessageManager`。
  - 在 `emit_presenter_messages()` 中把 `ToolStatusOutput` 分发给 manager。
  - 普通输出、权限消息、用户提问消息保持原路径。

- Modify: `tests/test_structured_reply_presenter.py`
  - 覆盖 `ToolStatusOutput`、状态变化、成功/失败/中断、`AskUserQuestion` 跳过泛化工具状态。

- Create: `tests/test_tool_message_manager.py`
  - 覆盖发送、编辑、成功保留、编辑失败重发、发送失败不抛出。

- Modify: `tests/test_command_run.py`
  - 让 dummy Telegram message 返回可编辑消息对象。
  - 增加端到端编排测试，确认 `ToolStatusOutput` 进入 `ToolMessageManager`。

---

### Task 1: Presenter emits tool status outputs

**Files:**
- Modify: `app/bot/presenters/structured_reply_presenter.py:48-64`
- Modify: `app/bot/presenters/structured_reply_presenter.py:203-213`
- Modify: `app/bot/presenters/structured_reply_presenter.py:333-458`
- Test: `tests/test_structured_reply_presenter.py`

- [ ] **Step 1: Write failing presenter tests**

Modify imports in `tests/test_structured_reply_presenter.py` to include `ToolStatusOutput` and `build_tool_status_message`:

```python
from app.bot.presenters.structured_reply_presenter import (
    PermissionRequestOutput,
    ProgressUpdateOutput,
    StructuredReplyPresenter,
    ToolStatusOutput,
    build_permission_prompt,
    build_tool_progress_message,
    build_tool_status_message,
    build_user_question_prompt,
    normalize_stream_text,
    preview_stream_text,
    strip_bridge_markers,
    UserQuestionOutput,
)
```

Add these tests after `test_build_tool_progress_message_includes_specific_bash_command`:

```python
def test_build_tool_status_message_formats_final_states() -> None:
    assert build_tool_status_message(
        tool_name="Bash",
        tool_input={"command": "pytest -q"},
        status=ToolStatus.SUCCESS.value,
    ) == "执行完成\n工具: Bash\n命令: pytest -q"
    assert build_tool_status_message(
        tool_name="Bash",
        tool_input={"command": "pytest -q"},
        status=ToolStatus.ERROR.value,
    ) == "执行失败\n工具: Bash\n命令: pytest -q"
    assert build_tool_status_message(
        tool_name="Bash",
        tool_input={"command": "pytest -q"},
        status=ToolStatus.INTERRUPTED.value,
    ) == "已中断\n工具: Bash\n命令: pytest -q"
    assert build_tool_status_message(
        tool_name="Bash",
        tool_input={"command": "rm file"},
        status=ToolStatus.WAITING_FOR_APPROVAL.value,
    ) == "等待权限\n工具: Bash\n命令: rm file"
```

Replace the existing `test_presenter_emits_running_tool_progress_once` with:

```python
@pytest.mark.asyncio
async def test_presenter_emits_running_tool_status_once() -> None:
    tool_calls = {
        "tool-1": ToolCallRecord(
            tool_use_id="tool-1",
            name="Bash",
            input={"command": "pytest -q"},
            status=ToolStatus.RUNNING,
        )
    }
    presenter = StructuredReplyPresenter(
        task_service=DummyTaskService(
            [
                _session(phase=SessionPhase.WAITING_FOR_INPUT),
                _session(phase=SessionPhase.PROCESSING, tool_calls=tool_calls),
                _session(phase=SessionPhase.PROCESSING, tool_calls=tool_calls),
            ]
        ),
        user_id=1,
    )

    await presenter.prime()
    first = await presenter.poll(task_id="task-1")
    second = await presenter.poll(task_id="task-1")

    assert first == [
        ToolStatusOutput(
            tool_use_id="tool-1",
            tool_name="Bash",
            tool_input={"command": "pytest -q"},
            status=ToolStatus.RUNNING.value,
        )
    ]
    assert second == []
```

Add success, error, interrupted status tests:

```python
@pytest.mark.asyncio
async def test_presenter_emits_success_tool_status_after_running() -> None:
    running_tool = ToolCallRecord(
        tool_use_id="tool-1",
        name="Bash",
        input={"command": "pytest -q"},
        status=ToolStatus.RUNNING,
    )
    success_tool = ToolCallRecord(
        tool_use_id="tool-1",
        name="Bash",
        input={"command": "pytest -q"},
        status=ToolStatus.SUCCESS,
    )
    presenter = StructuredReplyPresenter(
        task_service=DummyTaskService(
            [
                _session(phase=SessionPhase.WAITING_FOR_INPUT),
                _session(phase=SessionPhase.PROCESSING, tool_calls={"tool-1": running_tool}),
                _session(phase=SessionPhase.WAITING_FOR_INPUT, tool_calls={"tool-1": success_tool}),
            ]
        ),
        user_id=1,
    )

    await presenter.prime()
    first = await presenter.poll(task_id="task-1")
    second = await presenter.poll(task_id="task-1")

    assert first == [
        ToolStatusOutput(
            tool_use_id="tool-1",
            tool_name="Bash",
            tool_input={"command": "pytest -q"},
            status=ToolStatus.RUNNING.value,
        )
    ]
    assert second == [
        ToolStatusOutput(
            tool_use_id="tool-1",
            tool_name="Bash",
            tool_input={"command": "pytest -q"},
            status=ToolStatus.SUCCESS.value,
        )
    ]


@pytest.mark.asyncio
async def test_presenter_emits_error_and_interrupted_tool_statuses() -> None:
    running_tool = ToolCallRecord(
        tool_use_id="tool-1",
        name="Bash",
        input={"command": "pytest -q"},
        status=ToolStatus.RUNNING,
    )
    error_tool = ToolCallRecord(
        tool_use_id="tool-1",
        name="Bash",
        input={"command": "pytest -q"},
        status=ToolStatus.ERROR,
    )
    interrupted_tool = ToolCallRecord(
        tool_use_id="tool-2",
        name="Read",
        input={"file_path": "/tmp/a.txt"},
        status=ToolStatus.INTERRUPTED,
    )
    presenter = StructuredReplyPresenter(
        task_service=DummyTaskService(
            [
                _session(phase=SessionPhase.WAITING_FOR_INPUT),
                _session(phase=SessionPhase.PROCESSING, tool_calls={"tool-1": running_tool}),
                _session(phase=SessionPhase.WAITING_FOR_INPUT, tool_calls={"tool-1": error_tool}),
                _session(phase=SessionPhase.WAITING_FOR_INPUT, tool_calls={"tool-2": interrupted_tool}),
            ]
        ),
        user_id=1,
    )

    await presenter.prime()
    await presenter.poll(task_id="task-1")
    error_output = await presenter.poll(task_id="task-1")
    interrupted_output = await presenter.poll(task_id="task-1")

    assert error_output == [
        ToolStatusOutput(
            tool_use_id="tool-1",
            tool_name="Bash",
            tool_input={"command": "pytest -q"},
            status=ToolStatus.ERROR.value,
        )
    ]
    assert interrupted_output == [
        ToolStatusOutput(
            tool_use_id="tool-2",
            tool_name="Read",
            tool_input={"file_path": "/tmp/a.txt"},
            status=ToolStatus.INTERRUPTED.value,
        )
    ]
```

Update `test_presenter_emits_resume_progress_after_permission` final assertion to expect `ToolStatusOutput`:

```python
    assert messages == [
        ToolStatusOutput(
            tool_use_id="tool-1",
            tool_name="Bash",
            tool_input={"command": "pytest -q"},
            status=ToolStatus.RUNNING.value,
        )
    ]
```

- [ ] **Step 2: Run presenter tests to verify failure**

Run:

```bash
cd /Users/jack/project/remote-coding && python -m pytest tests/test_structured_reply_presenter.py -q
```

Expected: FAIL with import errors for `ToolStatusOutput` / `build_tool_status_message`, or assertion failures because tool progress still emits `ProgressUpdateOutput`.

- [ ] **Step 3: Implement presenter output type and formatter**

In `app/bot/presenters/structured_reply_presenter.py`, add this dataclass after `ProgressUpdateOutput`:

```python
@dataclass(frozen=True)
class ToolStatusOutput:
    tool_use_id: str
    tool_name: str | None
    tool_input: dict | None
    status: str
```

Replace `build_tool_progress_message()` with this pair of functions:

```python
def build_tool_status_message(*, tool_name: str | None, tool_input: dict | None = None, status: str, resumed: bool = False) -> str:
    if status == ToolStatus.SUCCESS.value:
        heading = "执行完成"
    elif status == ToolStatus.ERROR.value:
        heading = "执行失败"
    elif status == ToolStatus.INTERRUPTED.value:
        heading = "已中断"
    elif status == ToolStatus.WAITING_FOR_APPROVAL.value:
        heading = "等待权限"
    elif status == ToolStatus.RUNNING.value and resumed:
        heading = "继续执行"
    else:
        heading = "执行中"

    lines = [heading]
    if tool_name:
        lines.append(f"工具: {tool_name}")

    detail = _format_tool_input_detail(tool_name, tool_input)
    if detail is not None:
        label, value = detail
        lines.append(f"{label}: {value}")

    return "\n".join(lines)


def build_tool_progress_message(*, tool_name: str | None, tool_input: dict | None = None, resumed: bool = False) -> str:
    return build_tool_status_message(
        tool_name=tool_name,
        tool_input=tool_input,
        status=ToolStatus.RUNNING.value,
        resumed=resumed,
    )
```

Update the return type of `poll()`:

```python
    async def poll(
        self,
        *,
        task_id: str,
        final: bool = False,
        log_missing: bool = False,
    ) -> list[str | PermissionRequestOutput | ProgressUpdateOutput | ToolStatusOutput | UserQuestionOutput]:
```

Update the local `messages` declaration in `poll()`:

```python
        messages: list[str | PermissionRequestOutput | ProgressUpdateOutput | ToolStatusOutput | UserQuestionOutput] = []
```

Replace `_collect_progress_updates()` with:

```python
    def _collect_progress_updates(
        self,
        *,
        snapshot: _StructuredSnapshot,
        tool_question_prompts: dict[str, tuple[UserQuestionPrompt, ...]],
    ) -> list[ProgressUpdateOutput | ToolStatusOutput]:
        messages: list[ProgressUpdateOutput | ToolStatusOutput] = []
        if snapshot.phase == SessionPhase.COMPACTING.value and self._last_phase != SessionPhase.COMPACTING.value:
            messages.append(ProgressUpdateOutput(text=build_compacting_progress_message()))
        self._last_phase = snapshot.phase

        current_status_by_id: dict[str, str | None] = {}
        for tool in snapshot.tool_states:
            if tool.status is None:
                continue
            current_status_by_id[tool.tool_use_id] = tool.status
            if tool_question_prompts.get(tool.tool_use_id):
                continue
            previous_status = self._tool_status_by_id.get(tool.tool_use_id)
            if previous_status == tool.status:
                continue
            messages.append(
                ToolStatusOutput(
                    tool_use_id=tool.tool_use_id,
                    tool_name=tool.tool_name,
                    tool_input=tool.tool_input,
                    status=tool.status,
                )
            )
        self._tool_status_by_id = current_status_by_id
        return messages
```

- [ ] **Step 4: Run presenter tests to verify pass**

Run:

```bash
cd /Users/jack/project/remote-coding && python -m pytest tests/test_structured_reply_presenter.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit presenter changes**

Run:

```bash
git -C /Users/jack/project/remote-coding add app/bot/presenters/structured_reply_presenter.py tests/test_structured_reply_presenter.py
git -C /Users/jack/project/remote-coding commit -m "feat: emit structured tool status updates"
```

---

### Task 2: ToolMessageManager

**Files:**
- Create: `app/bot/presenters/tool_message_manager.py`
- Test: `tests/test_tool_message_manager.py`

- [ ] **Step 1: Write failing manager tests**

Create `tests/test_tool_message_manager.py` with:

```python
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
```

- [ ] **Step 2: Run manager tests to verify failure**

Run:

```bash
cd /Users/jack/project/remote-coding && python -m pytest tests/test_tool_message_manager.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'app.bot.presenters.tool_message_manager'`.

- [ ] **Step 3: Implement ToolMessageManager**

Create `app/bot/presenters/tool_message_manager.py` with:

```python
from __future__ import annotations

import logging
from dataclasses import dataclass

from aiogram.enums import ParseMode
from aiogram.types import Message

from app.bot.presenters.structured_reply_presenter import ToolStatusOutput, build_tool_status_message
from app.bot.presenters.telegram_formatting import render_markdownish_to_telegram_html, split_telegram_html

logger = logging.getLogger(__name__)


@dataclass
class _TrackedToolMessage:
    message: Message


class ToolMessageManager:
    def __init__(self, *, root_message: Message, task_id: str, user_id: int, provider: str) -> None:
        self._root_message = root_message
        self._task_id = task_id
        self._user_id = user_id
        self._provider = provider
        self._messages: dict[str, _TrackedToolMessage] = {}

    async def handle(self, output: ToolStatusOutput) -> None:
        text = build_tool_status_message(
            tool_name=output.tool_name,
            tool_input=output.tool_input,
            status=output.status,
        )
        existing = self._messages.get(output.tool_use_id)
        if existing is None:
            await self._send_and_track(output.tool_use_id, text)
            return

        edited = await self._edit(existing.message, text, tool_use_id=output.tool_use_id)
        if edited:
            return
        await self._send_and_track(output.tool_use_id, text)

    async def _send_and_track(self, tool_use_id: str, text: str) -> None:
        sent = await self._send(text, tool_use_id=tool_use_id)
        if sent is not None:
            self._messages[tool_use_id] = _TrackedToolMessage(message=sent)

    async def _send(self, text: str, *, tool_use_id: str) -> Message | None:
        try:
            rendered = self._render(text)
            return await self._root_message.answer(rendered, parse_mode=ParseMode.HTML)
        except Exception:
            logger.exception(
                "telegram tool message send failed",
                extra={
                    "task_id": self._task_id,
                    "user_id": self._user_id,
                    "provider": self._provider,
                    "tool_use_id": tool_use_id,
                },
            )
            return None

    async def _edit(self, message: Message, text: str, *, tool_use_id: str) -> bool:
        try:
            rendered = self._render(text)
            await message.edit_text(rendered, parse_mode=ParseMode.HTML)
            return True
        except Exception:
            logger.exception(
                "telegram tool message edit failed",
                extra={
                    "task_id": self._task_id,
                    "user_id": self._user_id,
                    "provider": self._provider,
                    "tool_use_id": tool_use_id,
                },
            )
            return False

    def _render(self, text: str) -> str:
        rendered = render_markdownish_to_telegram_html(text)
        return split_telegram_html(rendered, 4096)[0]
```

- [ ] **Step 4: Run manager tests to verify pass**

Run:

```bash
cd /Users/jack/project/remote-coding && python -m pytest tests/test_tool_message_manager.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit manager changes**

Run:

```bash
git -C /Users/jack/project/remote-coding add app/bot/presenters/tool_message_manager.py tests/test_tool_message_manager.py
git -C /Users/jack/project/remote-coding commit -m "feat: manage telegram tool messages"
```

---

### Task 3: Wire tool messages into command_run

**Files:**
- Modify: `app/bot/handlers/command_run.py:13-20`
- Modify: `app/bot/handlers/command_run.py:182-213`
- Modify: `tests/test_command_run.py:21-105`
- Test: `tests/test_command_run.py`

- [ ] **Step 1: Write failing command_run integration test**

In `tests/test_command_run.py`, replace `DummyMessage` with this version that preserves existing `answers` behavior and also returns editable sent messages:

```python
class DummyAnswerMessage:
    def __init__(self, text: str, *, reply_markup=None, parse_mode=None) -> None:
        self.text = text
        self.reply_markup = reply_markup
        self.parse_mode = parse_mode
        self.edits: list[str] = []
        self.edit_parse_modes: list[ParseMode | None] = []

    async def edit_text(self, text: str, parse_mode=None) -> "DummyAnswerMessage":
        self.text = text
        self.edits.append(text)
        self.edit_parse_modes.append(parse_mode)
        return self


class DummyMessage:
    def __init__(self, user_id: int = 1, *, fail_on_calls: set[int] | None = None) -> None:
        self.from_user = SimpleNamespace(id=user_id)
        self.answers: list[str] = []
        self.sent_messages: list[DummyAnswerMessage] = []
        self.reply_markups: list[InlineKeyboardMarkup | None] = []
        self.parse_modes: list[ParseMode | None] = []
        self._answer_calls = 0
        self._fail_on_calls = fail_on_calls or set()

    async def answer(self, text: str, reply_markup=None, parse_mode=None) -> DummyAnswerMessage:
        self._answer_calls += 1
        if self._answer_calls in self._fail_on_calls:
            raise TelegramBadRequest(method="sendMessage", message="chat not found")
        sent = DummyAnswerMessage(text, reply_markup=reply_markup, parse_mode=parse_mode)
        self.answers.append(text)
        self.sent_messages.append(sent)
        self.reply_markups.append(reply_markup)
        self.parse_modes.append(parse_mode)
        return sent
```

Extend `DummyTaskService.__init__` signature and body:

```python
class DummyTaskService:
    def __init__(
        self,
        events: list[CLIEvent],
        status: TaskRecord | None = None,
        *,
        interactive: bool = False,
        structured_reply: str = "",
        structured_turns: list[ConversationTurn] | None = None,
        structured_sessions: list[object | None] | None = None,
        event_delays: list[float] | None = None,
    ) -> None:
        self._events = events
        self._status = status
        self._interactive = interactive
        self._structured_reply = structured_reply
        self._structured_turns = structured_turns
        self._structured_sessions = structured_sessions
        self._structured_session_index = 0
        self._event_delays = event_delays or [0.0] * len(events)
        self._revision = 0
        self._structured_reply_turn_id: str | None = None
        self._structured_permission_key: str | None = None
        self._structured_user_question_key: str | None = None
```

At the start of `DummyTaskService.get_structured_session()`, add:

```python
        if self._structured_sessions is not None:
            if self._structured_session_index < len(self._structured_sessions):
                session = self._structured_sessions[self._structured_session_index]
                self._structured_session_index += 1
            else:
                session = self._structured_sessions[-1]
            self._revision += 1
            return session
```

Add this helper near `_status()`:

```python
def _structured_session(*, phase: SessionPhase, tool_calls: dict[str, ToolCallRecord] | None = None):
    return SimpleNamespace(
        session_id="claude-session-1",
        phase=phase,
        turns=[],
        pending_permission=None,
        tool_calls=tool_calls or {},
    )
```

Add this integration test after `test_run_prompt_and_stream_reports_started_output_and_success`:

```python
@pytest.mark.asyncio
async def test_run_prompt_and_stream_updates_tool_message_to_success() -> None:
    running_tool = ToolCallRecord(
        tool_use_id="tool-1",
        name="Bash",
        input={"command": "pytest -q"},
        status=ToolStatus.RUNNING,
    )
    success_tool = ToolCallRecord(
        tool_use_id="tool-1",
        name="Bash",
        input={"command": "pytest -q"},
        status=ToolStatus.SUCCESS,
    )
    message = DummyMessage()
    task_service = DummyTaskService(
        [
            CLIEvent(type=EventType.STARTED, task_id="t1", content="tmux_session=tgcli_user_1"),
            CLIEvent(type=EventType.EXITED, task_id="t1", exit_code=0),
        ],
        _status(task_status=TaskStatus.SUCCEEDED),
        interactive=True,
        structured_sessions=[
            _structured_session(phase=SessionPhase.WAITING_FOR_INPUT),
            _structured_session(phase=SessionPhase.PROCESSING, tool_calls={"tool-1": running_tool}),
            _structured_session(phase=SessionPhase.PROCESSING, tool_calls={"tool-1": running_tool}),
            _structured_session(phase=SessionPhase.WAITING_FOR_INPUT, tool_calls={"tool-1": success_tool}),
            _structured_session(phase=SessionPhase.WAITING_FOR_INPUT, tool_calls={"tool-1": success_tool}),
        ],
        event_delays=[0.0, 0.16],
    )

    await _run_and_wait(message=message, task_service=task_service, wait_sec=0.25)

    tool_messages = [
        sent
        for sent in message.sent_messages
        if "工具: Bash" in sent.text or any("工具: Bash" in edit for edit in sent.edits)
    ]
    assert len(tool_messages) == 1
    assert "执行中" in tool_messages[0].text or any("执行中" in edit for edit in tool_messages[0].edits)
    assert "执行完成" in tool_messages[0].text or any("执行完成" in edit for edit in tool_messages[0].edits)
```

- [ ] **Step 2: Run command_run test to verify failure**

Run:

```bash
cd /Users/jack/project/remote-coding && python -m pytest tests/test_command_run.py::test_run_prompt_and_stream_updates_tool_message_to_success -q
```

Expected: FAIL because `ToolStatusOutput` is not handled in `command_run.py` yet, so no editable tool message is created.

- [ ] **Step 3: Wire ToolMessageManager into command_run**

In `app/bot/handlers/command_run.py`, update the presenter imports:

```python
from app.bot.presenters.structured_reply_presenter import (
    PermissionRequestOutput,
    ProgressUpdateOutput,
    StructuredReplyPresenter,
    ToolStatusOutput,
    UserQuestionOutput,
    _MARKER_LINE_RE as _PRESENTER_MARKER_LINE_RE,
    normalize_stream_text,
)
from app.bot.presenters.tool_message_manager import ToolMessageManager
```

After `presenter = StructuredReplyPresenter(...)`, add:

```python
    tool_message_manager = ToolMessageManager(
        root_message=message,
        task_id=start.task.task_id,
        user_id=user_id,
        provider=start.task.provider,
    )
```

In `emit_presenter_messages()`, add this branch before `ProgressUpdateOutput`:

```python
            if isinstance(output, ToolStatusOutput):
                await sender.flush(send_text)
                await tool_message_manager.handle(output)
                continue
```

The branch order inside `emit_presenter_messages()` should be:

```python
            if isinstance(output, PermissionRequestOutput):
                await sender.flush(send_text)
                await answer_safely(
                    output.text,
                    reply_markup=build_permission_keyboard(tool_use_id=output.tool_use_id),
                )
                continue
            if isinstance(output, UserQuestionOutput):
                await sender.flush(send_text)
                await answer_safely(
                    output.text,
                    reply_markup=build_user_question_keyboard(output),
                )
                continue
            if isinstance(output, ToolStatusOutput):
                await sender.flush(send_text)
                await tool_message_manager.handle(output)
                continue
            if isinstance(output, ProgressUpdateOutput):
                await sender.flush(send_text)
                await answer_safely(output.text)
                continue
            await sender.push(output, send_text)
```

- [ ] **Step 4: Run command_run integration test to verify pass**

Run:

```bash
cd /Users/jack/project/remote-coding && python -m pytest tests/test_command_run.py::test_run_prompt_and_stream_updates_tool_message_to_success -q
```

Expected: PASS.

- [ ] **Step 5: Run full command_run tests**

Run:

```bash
cd /Users/jack/project/remote-coding && python -m pytest tests/test_command_run.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit command_run integration**

Run:

```bash
git -C /Users/jack/project/remote-coding add app/bot/handlers/command_run.py tests/test_command_run.py
git -C /Users/jack/project/remote-coding commit -m "feat: route tool statuses to telegram messages"
```

---

### Task 4: Verification and cleanup

**Files:**
- Verify: `app/bot/presenters/structured_reply_presenter.py`
- Verify: `app/bot/presenters/tool_message_manager.py`
- Verify: `app/bot/handlers/command_run.py`
- Verify: `tests/test_structured_reply_presenter.py`
- Verify: `tests/test_tool_message_manager.py`
- Verify: `tests/test_command_run.py`

- [ ] **Step 1: Run focused test set**

Run:

```bash
cd /Users/jack/project/remote-coding && python -m pytest tests/test_structured_reply_presenter.py tests/test_tool_message_manager.py tests/test_command_run.py -q
```

Expected: PASS for all selected tests.

- [ ] **Step 2: Run lint**

Run:

```bash
cd /Users/jack/project/remote-coding && python -m ruff check app tests
```

Expected: PASS with no new lint errors.

- [ ] **Step 3: Run full test suite**

Run:

```bash
cd /Users/jack/project/remote-coding && python -m pytest -q
```

Expected: PASS.

- [ ] **Step 4: Review staged/uncommitted files**

Run:

```bash
git -C /Users/jack/project/remote-coding status --short
```

Expected: only unrelated pre-existing files remain unstaged, or the working tree is clean if implementation happened in a fresh worktree. The feature commits should include only:

```text
app/bot/presenters/structured_reply_presenter.py
tests/test_structured_reply_presenter.py
app/bot/presenters/tool_message_manager.py
tests/test_tool_message_manager.py
app/bot/handlers/command_run.py
tests/test_command_run.py
```

- [ ] **Step 5: Final behavior check**

Use a Claude prompt that triggers a non-interactive tool call, such as a harmless file read in an allowed workdir. Expected Telegram behavior:

```text
执行中
工具: Read
文件: <path>
```

Then after completion, the same tool message is edited and retained as:

```text
执行完成
工具: Read
文件: <path>
```

For a tool that fails or is interrupted, expected final state is retained as either:

```text
执行失败
工具: <ToolName>
...
```

or:

```text
已中断
工具: <ToolName>
...
```

---

## Self-review

Spec coverage:

- Independent message per non-interactive tool: Task 1 emits `ToolStatusOutput`; Task 2 manages one message per `tool_use_id`; Task 3 wires it into Telegram flow.
- Successful tools keep final state: Task 2 edits `SUCCESS` to “执行完成” and tests retained message behavior.
- Failed/interrupted tools keep final state: Task 1 emits `ERROR` / `INTERRUPTED`; Task 2 edits and retains those messages.
- Permissions and `AskUserQuestion` unchanged: Task 1 skips question tools and Task 3 keeps existing `PermissionRequestOutput` / `UserQuestionOutput` branches.
- Ordinary Claude output unchanged: Task 3 leaves `ChunkSender` string path intact and command_run regression tests run.
- Telegram API failure isolation: Task 2 catches send/edit exceptions and tests no exception escapes.

Placeholder scan: no placeholder tasks or unspecified implementation steps remain.

Type consistency: `ToolStatusOutput.status` is a `str`, matching `ToolStatus.value` used by `SessionStore` snapshots and tests.
