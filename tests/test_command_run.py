from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup

from app.bot.handlers.command_run import _ACTIVE_STREAM_TASKS, run_prompt_and_stream
from app.bot.presenters.chunk_sender import ChunkSender
from app.bot.presenters.structured_reply_presenter import build_permission_prompt, build_tool_progress_message, build_user_question_prompt
from app.domain.models import CLIEvent, EventType, TaskRecord, TaskStatus, utc_now
from app.domain.session_models import ConversationTurn, PendingPermission, SessionPhase, ToolCallRecord, ToolStatus


class DummyMessage:
    def __init__(self, user_id: int = 1, *, fail_on_calls: set[int] | None = None) -> None:
        self.from_user = SimpleNamespace(id=user_id)
        self.answers: list[str] = []
        self.reply_markups: list[InlineKeyboardMarkup | None] = []
        self._answer_calls = 0
        self._fail_on_calls = fail_on_calls or set()

    async def answer(self, text: str, reply_markup=None) -> None:
        self._answer_calls += 1
        if self._answer_calls in self._fail_on_calls:
            raise TelegramBadRequest(method="sendMessage", message="chat not found")
        self.answers.append(text)
        self.reply_markups.append(reply_markup)


class DummyTaskService:
    def __init__(self, events: list[CLIEvent], status: TaskRecord | None = None, *, interactive: bool = False, structured_reply: str = "", structured_turns: list[ConversationTurn] | None = None, event_delays: list[float] | None = None) -> None:
        self._events = events
        self._status = status
        self._interactive = interactive
        self._structured_reply = structured_reply
        self._structured_turns = structured_turns
        self._event_delays = event_delays or [0.0] * len(events)

    async def create_and_run(self, *, user_id: int, provider: str | None, prompt: str, workdir: str | None = None):
        task = SimpleNamespace(task_id="t1", provider="claude_code", session_id="s1")
        return SimpleNamespace(task=task, events=self._stream(), interactive=self._interactive)

    async def get_status(self, task_id: str, user_id: int):
        return self._status

    async def get_structured_session(self, user_id: int, *, log_missing: bool = True):
        if self._structured_turns is not None:
            return SimpleNamespace(
                session_id="claude-session-1",
                phase=SessionPhase.WAITING_FOR_INPUT,
                turns=self._structured_turns,
                pending_permission=None,
            )
        if not self._structured_reply:
            return None
        return SimpleNamespace(
            session_id="claude-session-1",
            phase=SessionPhase.WAITING_FOR_INPUT,
            turns=[ConversationTurn(turn_id="turn-1", role="assistant", text=self._structured_reply, is_complete=True)],
            pending_permission=None,
        )

    async def _stream(self):
        for delay, event in zip(self._event_delays, self._events, strict=False):
            if delay > 0:
                await asyncio.sleep(delay)
            yield event


async def _run_and_wait(*, message: DummyMessage, task_service: DummyTaskService, wait_sec: float = 0.05) -> None:
    task = await run_prompt_and_stream(
        message=message,
        task_service=task_service,
        sender_factory=lambda: ChunkSender(chunk_size=50, flush_interval_sec=0.01),
        user_id=message.from_user.id,
        provider="claude_code",
        prompt="hello",
        workdir="/tmp",
    )
    await asyncio.sleep(wait_sec)
    if task is not None:
        await task


@pytest.mark.asyncio
async def test_run_prompt_and_stream_interactive_pump_silences_missing_structured_logs() -> None:
    message = DummyMessage()
    task_service = DummyTaskService(
        [
            CLIEvent(type=EventType.STARTED, task_id="t1", content="tmux_session=tgcli_user_1"),
            CLIEvent(type=EventType.EXITED, task_id="t1", exit_code=0),
        ],
        _status(task_status=TaskStatus.SUCCEEDED),
        interactive=True,
    )
    task_service.get_structured_session = AsyncMock(return_value=None)

    await _run_and_wait(message=message, task_service=task_service, wait_sec=0.12)

    assert task_service.get_structured_session.await_count >= 2
    initial_call = task_service.get_structured_session.await_args_list[0]
    repeated_calls = task_service.get_structured_session.await_args_list[1:]
    assert initial_call.kwargs == {"log_missing": True}
    assert repeated_calls
    assert any(call.kwargs.get("log_missing") is False for call in repeated_calls)
    assert repeated_calls[-1].kwargs == {"log_missing": True}


def _status(*, task_status: TaskStatus, truncated: bool = False) -> TaskRecord:
    started_at = utc_now() - timedelta(seconds=2)
    ended_at = utc_now()
    return TaskRecord(
        task_id="t1",
        session_id="s1",
        user_id=1,
        provider="claude_code",
        prompt="hello",
        workdir="/tmp",
        timeout_sec=30,
        status=task_status,
        started_at=started_at,
        ended_at=ended_at,
        output_truncated=truncated,
    )


@pytest.mark.asyncio
async def test_run_prompt_and_stream_keeps_background_task_referenced_until_done() -> None:
    message = DummyMessage()
    task_service = DummyTaskService(
        [
            CLIEvent(type=EventType.STARTED, task_id="t1", content="tmux_session=tgcli_user_1"),
            CLIEvent(type=EventType.EXITED, task_id="t1", exit_code=0),
        ],
        _status(task_status=TaskStatus.SUCCEEDED),
        event_delays=[0.0, 0.03],
    )

    task = await run_prompt_and_stream(
        message=message,
        task_service=task_service,
        sender_factory=lambda: ChunkSender(chunk_size=50, flush_interval_sec=0.01),
        user_id=message.from_user.id,
        provider="claude_code",
        prompt="hello",
        workdir="/tmp",
    )

    assert task is not None
    assert task in _ACTIVE_STREAM_TASKS
    await task
    assert task not in _ACTIVE_STREAM_TASKS


@pytest.mark.asyncio
async def test_run_prompt_and_stream_reports_started_output_and_success() -> None:
    message = DummyMessage()
    task_service = DummyTaskService(
        [
            CLIEvent(type=EventType.STARTED, task_id="t1", content="tmux_session=tgcli_user_1"),
            CLIEvent(type=EventType.STDOUT, task_id="t1", content="hello\nworld\n"),
            CLIEvent(type=EventType.EXITED, task_id="t1", exit_code=0),
        ],
        _status(task_status=TaskStatus.SUCCEEDED),
    )

    await _run_and_wait(message=message, task_service=task_service)

    assert message.answers[0] == (
        "任务已接收\n"
        "task_id: t1\n"
        "provider: claude_code\n"
        "session_id: s1\n"
        "status: 等待启动"
    )
    assert message.answers[1] == (
        "任务开始执行\n"
        "task_id: t1\n"
        "status: 正在处理"
    )
    assert message.answers[2] == "hello\nworld"
    assert message.answers[3].startswith("任务执行完成\ntask_id: t1\nstatus: 成功\nexit_code: 0\nduration: ")


@pytest.mark.asyncio
async def test_run_prompt_and_stream_strips_markers_and_marks_stderr() -> None:
    message = DummyMessage()
    task_service = DummyTaskService(
        [
            CLIEvent(type=EventType.STDOUT, task_id="t1", content="TGCLI_BEGIN\n正文\nTGCLI_DONE\n"),
            CLIEvent(type=EventType.STDERR, task_id="t1", content="boom\n"),
            CLIEvent(type=EventType.EXITED, task_id="t1", exit_code=0),
        ],
        _status(task_status=TaskStatus.SUCCEEDED),
    )

    await _run_and_wait(message=message, task_service=task_service)

    assert "TGCLI_BEGIN" not in "\n".join(message.answers)
    assert "TGCLI_DONE" not in "\n".join(message.answers)
    assert "正文" in message.answers[1]
    assert "[stderr] boom" in message.answers[1]


@pytest.mark.asyncio
async def test_run_prompt_and_stream_reports_failed_with_hint_and_truncation() -> None:
    message = DummyMessage()
    task_service = DummyTaskService(
        [
            CLIEvent(type=EventType.FAILED, task_id="t1", error="tmux session lost"),
        ],
        _status(task_status=TaskStatus.FAILED, truncated=True),
    )

    await _run_and_wait(message=message, task_service=task_service)

    assert message.answers[1].startswith("任务执行失败\ntask_id: t1\nstatus: 失败\nerror: tmux session lost\nduration: ")
    assert message.answers[1].endswith("output: truncated")


@pytest.mark.asyncio
async def test_run_prompt_and_stream_reports_timeout() -> None:
    message = DummyMessage()
    task_service = DummyTaskService(
        [
            CLIEvent(type=EventType.TIMEOUT, task_id="t1", error="deadline exceeded"),
        ],
        _status(task_status=TaskStatus.TIMEOUT),
    )

    await _run_and_wait(message=message, task_service=task_service)

    assert message.answers[1].startswith("任务执行超时\ntask_id: t1\nstatus: 超时\nerror: deadline exceeded\nduration: ")


@pytest.mark.asyncio
async def test_run_prompt_and_stream_reports_canceled() -> None:
    message = DummyMessage()
    task_service = DummyTaskService(
        [
            CLIEvent(type=EventType.CANCELED, task_id="t1", error="user canceled"),
        ],
        _status(task_status=TaskStatus.CANCELED),
    )

    await _run_and_wait(message=message, task_service=task_service)

    assert message.answers[1].startswith("任务已取消\ntask_id: t1\nstatus: 已取消\nerror: user canceled\nduration: ")


@pytest.mark.asyncio
async def test_run_prompt_and_stream_prefers_structured_reply_in_interactive_mode() -> None:
    message = DummyMessage()
    turns: list[ConversationTurn] = []
    task_service = DummyTaskService(
        [
            CLIEvent(type=EventType.STARTED, task_id="t1", content="tmux_session=tgcli_user_1"),
            CLIEvent(type=EventType.STDOUT, task_id="t1", content="噪音\nTGCLI_BEGIN\n正文\nTGCLI_DONE\n"),
            CLIEvent(type=EventType.EXITED, task_id="t1", exit_code=0),
        ],
        _status(task_status=TaskStatus.SUCCEEDED),
        interactive=True,
        structured_turns=turns,
        event_delays=[0.0, 0.03, 0.1],
    )

    async def append_new_turn() -> None:
        await asyncio.sleep(0.02)
        turns.append(ConversationTurn(turn_id="turn-1", role="assistant", text="\n干净正文\n", is_complete=True))

    updater = asyncio.create_task(append_new_turn())
    await _run_and_wait(message=message, task_service=task_service, wait_sec=0.2)
    await updater

    assert "噪音" not in "\n".join(message.answers)
    assert message.answers[2] == "干净正文"


@pytest.mark.asyncio
async def test_run_prompt_and_stream_interactive_ignores_old_turn_and_emits_new_completed_turn() -> None:
    message = DummyMessage()
    turns = [ConversationTurn(turn_id="turn-old", role="assistant", text="\n旧回复\n", is_complete=True)]
    task_service = DummyTaskService(
        [
            CLIEvent(type=EventType.STARTED, task_id="t1", content="tmux_session=tgcli_user_1"),
            CLIEvent(type=EventType.STDOUT, task_id="t1", content="tmux 噪音\n"),
            CLIEvent(type=EventType.EXITED, task_id="t1", exit_code=0),
        ],
        _status(task_status=TaskStatus.SUCCEEDED),
        interactive=True,
        structured_turns=turns,
        event_delays=[0.0, 0.03, 0.12],
    )

    async def append_new_turn() -> None:
        await asyncio.sleep(0.02)
        turns.append(ConversationTurn(turn_id="turn-new", role="assistant", text="\n新回复\n", is_complete=True))

    updater = asyncio.create_task(append_new_turn())
    await _run_and_wait(message=message, task_service=task_service, wait_sec=0.25)
    await updater

    assert "旧回复" not in "\n".join(message.answers)
    assert "tmux 噪音" not in "\n".join(message.answers)
    assert message.answers[2] == "新回复"


@pytest.mark.asyncio
async def test_run_prompt_and_stream_interactive_does_not_emit_incomplete_turn() -> None:
    message = DummyMessage()
    turns = [ConversationTurn(turn_id="turn-1", role="assistant", text="\n半截回复\n", is_complete=False)]
    task_service = DummyTaskService(
        [
            CLIEvent(type=EventType.STARTED, task_id="t1", content="tmux_session=tgcli_user_1"),
            CLIEvent(type=EventType.STDOUT, task_id="t1", content="tmux 噪音\n"),
            CLIEvent(type=EventType.EXITED, task_id="t1", exit_code=0),
        ],
        _status(task_status=TaskStatus.SUCCEEDED),
        interactive=True,
        structured_turns=turns,
        event_delays=[0.0, 0.03, 0.08],
    )

    await _run_and_wait(message=message, task_service=task_service, wait_sec=0.12)

    assert all("半截回复" not in item for item in message.answers)
    assert all("tmux 噪音" not in item for item in message.answers)
    assert "结构化回复暂不可用，已回退为原始输出。" in message.answers
    assert message.answers[-2].startswith("任务执行完成")


@pytest.mark.asyncio
async def test_run_prompt_and_stream_interactive_falls_back_to_stdout_without_structured_session() -> None:
    message = DummyMessage()
    task_service = DummyTaskService(
        [
            CLIEvent(type=EventType.STARTED, task_id="t1", content="tmux_session=tgcli_user_1"),
            CLIEvent(type=EventType.STDOUT, task_id="t1", content="原始输出\n"),
            CLIEvent(type=EventType.EXITED, task_id="t1", exit_code=0),
        ],
        _status(task_status=TaskStatus.SUCCEEDED),
        interactive=True,
        event_delays=[0.0, 0.03, 0.08],
    )
    task_service.get_structured_session = AsyncMock(return_value=None)

    await _run_and_wait(message=message, task_service=task_service, wait_sec=0.12)

    assert "原始输出" in "\n".join(message.answers)
    assert "结构化回复暂不可用，已回退为原始输出。" not in message.answers


@pytest.mark.asyncio
async def test_run_prompt_and_stream_continues_after_message_send_failure() -> None:
    message = DummyMessage(fail_on_calls={2})
    task_service = DummyTaskService(
        [
            CLIEvent(type=EventType.STARTED, task_id="t1", content="tmux_session=tgcli_user_1"),
            CLIEvent(type=EventType.STDOUT, task_id="t1", content="hello\nworld\n"),
            CLIEvent(type=EventType.EXITED, task_id="t1", exit_code=0),
        ],
        _status(task_status=TaskStatus.SUCCEEDED),
    )

    await _run_and_wait(message=message, task_service=task_service)

    assert message.answers[0].startswith("任务已接收")
    assert "hello\nworld" in message.answers
    assert any(item.startswith("任务执行完成") for item in message.answers)


@pytest.mark.asyncio
async def test_run_prompt_and_stream_interactive_emits_late_structured_turn_before_exit() -> None:
    message = DummyMessage()
    turns = [ConversationTurn(turn_id="turn-old", role="assistant", text="\n旧回复\n", is_complete=True)]
    task_service = DummyTaskService(
        [
            CLIEvent(type=EventType.STARTED, task_id="t1", content="tmux_session=tgcli_user_1"),
            CLIEvent(type=EventType.STDOUT, task_id="t1", content="tmux 噪音\n"),
            CLIEvent(type=EventType.EXITED, task_id="t1", exit_code=0),
        ],
        _status(task_status=TaskStatus.SUCCEEDED),
        interactive=True,
        structured_turns=turns,
        event_delays=[0.0, 0.02, 0.06],
    )

    async def append_new_turn() -> None:
        await asyncio.sleep(0.05)
        turns.append(ConversationTurn(turn_id="turn-late", role="assistant", text="\n迟到回复\n", is_complete=True))

    updater = asyncio.create_task(append_new_turn())
    await _run_and_wait(message=message, task_service=task_service, wait_sec=0.18)
    await updater

    assert "旧回复" not in "\n".join(message.answers)
    assert "tmux 噪音" not in "\n".join(message.answers)
    assert "迟到回复" in message.answers[2]


@pytest.mark.asyncio
async def test_run_prompt_and_stream_interactive_emits_turn_arriving_after_exit_event() -> None:
    message = DummyMessage()
    turns = [ConversationTurn(turn_id="turn-old", role="assistant", text="\n旧回复\n", is_complete=True)]
    task_service = DummyTaskService(
        [
            CLIEvent(type=EventType.STARTED, task_id="t1", content="tmux_session=tgcli_user_1"),
            CLIEvent(type=EventType.EXITED, task_id="t1", exit_code=0),
        ],
        _status(task_status=TaskStatus.SUCCEEDED),
        interactive=True,
        structured_turns=turns,
        event_delays=[0.0, 0.02],
    )

    async def append_new_turn() -> None:
        await asyncio.sleep(0.06)
        turns.append(ConversationTurn(turn_id="turn-after-exit", role="assistant", text="\n退出后补到的回复\n", is_complete=True))

    updater = asyncio.create_task(append_new_turn())
    await _run_and_wait(message=message, task_service=task_service, wait_sec=0.2)
    await updater

    assert "旧回复" not in "\n".join(message.answers)
    assert "退出后补到的回复" in "\n".join(message.answers)


@pytest.mark.asyncio
async def test_run_prompt_and_stream_interactive_reports_pending_permission_once() -> None:
    message = DummyMessage()
    pending = PendingPermission(tool_use_id="tool-1", tool_name="Bash", tool_input={"command": "pwd"})
    turns = [ConversationTurn(turn_id="turn-1", role="assistant", text="\n已完成回复\n", is_complete=True)]
    task_service = DummyTaskService(
        [
            CLIEvent(type=EventType.STARTED, task_id="t1", content="tmux_session=tgcli_user_1"),
            CLIEvent(type=EventType.EXITED, task_id="t1", exit_code=0),
        ],
        _status(task_status=TaskStatus.SUCCEEDED),
        interactive=True,
        structured_turns=turns,
        event_delays=[0.0, 0.08],
    )

    async def get_structured_session(user_id: int, *, log_missing: bool = True):
        return SimpleNamespace(
            session_id="claude-session-1",
            phase=SessionPhase.WAITING_FOR_APPROVAL,
            turns=turns,
            pending_permission=pending,
        )

    task_service.get_structured_session = AsyncMock(side_effect=get_structured_session)

    await _run_and_wait(message=message, task_service=task_service, wait_sec=0.14)

    expected_prompt = build_permission_prompt(tool_name="Bash", tool_input={"command": "pwd"})
    assert message.answers.count(expected_prompt) == 1
    permission_index = message.answers.index(expected_prompt)
    reply_markup = message.reply_markups[permission_index]
    assert reply_markup is not None
    assert [button.text for button in reply_markup.inline_keyboard[0]] == ["允许", "拒绝"]
    assert [button.callback_data for button in reply_markup.inline_keyboard[0]] == ["perm:allow:tool-1", "perm:deny:tool-1"]


@pytest.mark.asyncio
async def test_run_prompt_and_stream_interactive_reports_user_question_once() -> None:
    message = DummyMessage()
    turns = [ConversationTurn(turn_id="turn-1", role="assistant", text="\n已收到问题\n", is_complete=True)]
    empty_session = SimpleNamespace(
        session_id="claude-session-1",
        phase=SessionPhase.PROCESSING,
        turns=turns,
        pending_permission=None,
        tool_calls={},
    )
    question_session = SimpleNamespace(
        session_id="claude-session-1",
        phase=SessionPhase.PROCESSING,
        turns=turns,
        pending_permission=None,
        tool_calls={},
    )
    question_tool = ToolCallRecord(
        tool_use_id="tool-ask-1",
        name="AskUserQuestion",
        input={
            "questions": [
                {
                    "header": "处理方式",
                    "question": "这两条误写到项目级的记忆，你要我怎么处理？",
                    "options": [
                        {"label": "迁到全局(推荐)", "description": "保留记忆内容并迁移"},
                        {"label": "直接删除", "description": "删除项目级这两条记忆"},
                    ],
                    "multiSelect": False,
                }
            ]
        },
        status=ToolStatus.RUNNING,
    )
    question_session.tool_calls = {"tool-ask-1": question_tool}
    task_service = DummyTaskService(
        [
            CLIEvent(type=EventType.STARTED, task_id="t1", content="tmux_session=tgcli_user_1"),
            CLIEvent(type=EventType.EXITED, task_id="t1", exit_code=0),
        ],
        _status(task_status=TaskStatus.SUCCEEDED),
        interactive=True,
        structured_turns=turns,
        event_delays=[0.0, 0.08],
    )

    responses = iter([empty_session, empty_session, question_session])

    async def get_structured_session(user_id: int, *, log_missing: bool = True):
        try:
            return next(responses)
        except StopIteration:
            return question_session

    task_service.get_structured_session = AsyncMock(side_effect=get_structured_session)

    await _run_and_wait(message=message, task_service=task_service, wait_sec=0.14)

    expected_prompt = build_user_question_prompt(
        SimpleNamespace(
            tool_use_id="tool-ask-1",
            question_index=0,
            total_questions=1,
            header="处理方式",
            question="这两条误写到项目级的记忆，你要我怎么处理？",
            options=(
                SimpleNamespace(label="迁到全局(推荐)", description="保留记忆内容并迁移"),
                SimpleNamespace(label="直接删除", description="删除项目级这两条记忆"),
            ),
            multi_select=False,
        )
    )
    assert message.answers.count(expected_prompt) == 1
    question_index = message.answers.index(expected_prompt)
    reply_markup = message.reply_markups[question_index]
    assert reply_markup is not None
    assert [button.text for row in reply_markup.inline_keyboard for button in row] == ["迁到全局(推荐)", "直接删除"]
    assert [button.callback_data for row in reply_markup.inline_keyboard for button in row] == [
        "ask:tool-ask-1:0:0",
        "ask:tool-ask-1:0:1",
    ]


@pytest.mark.asyncio
async def test_run_prompt_and_stream_interactive_emits_progress_update_immediately() -> None:
    message = DummyMessage()
    turns: list[ConversationTurn] = []
    tool_calls: dict[str, ToolCallRecord] = {}
    current_session = SimpleNamespace(
        session_id="claude-session-1",
        phase=SessionPhase.PROCESSING,
        turns=turns,
        pending_permission=None,
        tool_calls=tool_calls,
    )
    task_service = DummyTaskService(
        [
            CLIEvent(type=EventType.STARTED, task_id="t1", content="tmux_session=tgcli_user_1"),
            CLIEvent(type=EventType.EXITED, task_id="t1", exit_code=0),
        ],
        _status(task_status=TaskStatus.SUCCEEDED),
        interactive=True,
        event_delays=[0.0, 0.12],
    )

    async def get_structured_session(user_id: int, *, log_missing: bool = True):
        return current_session

    task_service.get_structured_session = AsyncMock(side_effect=get_structured_session)

    async def publish_progress() -> None:
        await asyncio.sleep(0.02)
        tool_calls["tool-1"] = ToolCallRecord(
            tool_use_id="tool-1",
            name="Bash",
            input={"command": "pytest -q"},
            status=ToolStatus.RUNNING,
        )
        await asyncio.sleep(0.04)
        turns.append(ConversationTurn(turn_id="turn-1", role="assistant", text="\n测试完成\n", is_complete=True))
        current_session.phase = SessionPhase.WAITING_FOR_INPUT

    updater = asyncio.create_task(publish_progress())
    await _run_and_wait(message=message, task_service=task_service, wait_sec=0.24)
    await updater

    progress_message = build_tool_progress_message(tool_name="Bash", tool_input={"command": "pytest -q"})
    assert message.answers.count(progress_message) == 1
    progress_index = message.answers.index(progress_message)
    assert progress_index == 2
    assert message.reply_markups[progress_index] is None
    assert "测试完成" in message.answers
