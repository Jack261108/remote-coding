from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.bot.presenters.structured_reply_presenter import (
    PermissionRequestOutput,
    ProgressUpdateOutput,
    StructuredReplyPresenter,
    build_permission_prompt,
    build_tool_progress_message,
    build_user_question_prompt,
    normalize_stream_text,
    preview_stream_text,
    strip_bridge_markers,
    UserQuestionOutput,
)
from app.adapters.storage.file_session_store import FileSessionStore
from app.domain.session_models import ConversationTurn, ParserCheckpoint, PendingPermission, SessionEvent, SessionEventType, SessionPhase, ToolCallRecord, ToolStatus
from app.domain.user_question_models import UserQuestionOption, UserQuestionPrompt
from app.services.session_store import SessionStore


class DummyTaskService:
    def __init__(self, sessions: list[object | None]) -> None:
        self._sessions = sessions
        self._index = 0
        self._question_key: str | None = None

    async def get_structured_session(self, user_id: int, *, log_missing: bool = True):
        if self._index >= len(self._sessions):
            return self._sessions[-1]
        session = self._sessions[self._index]
        self._index += 1
        return session

    async def get_structured_session_cursor(self, user_id: int, *, task_id: str | None = None) -> int:
        return self._index

    async def get_structured_reply_cursor(self, user_id: int, *, task_id: str | None = None):
        return None, None

    async def acknowledge_structured_reply(self, user_id: int, *, turn_id: str | None = None, permission_key: str | None = None, task_id: str | None = None) -> None:
        return None

    async def get_structured_user_question_cursor(self, user_id: int, *, task_id: str | None = None):
        return self._question_key

    async def acknowledge_structured_user_question(self, user_id: int, *, question_key: str | None = None, task_id: str | None = None) -> None:
        self._question_key = question_key

    async def wait_for_structured_session_update(self, *, user_id: int, since_cursor: int, timeout_sec: float, task_id: str | None = None) -> bool:
        return True


class PersistentTaskService:
    def __init__(self, store: SessionStore) -> None:
        self._store = store

    async def get_structured_session(self, user_id: int, *, log_missing: bool = True):
        return self._store.get("claude-session-1")

    async def get_structured_session_cursor(self, user_id: int, *, task_id: str | None = None) -> int:
        return self._store.get_cursor("claude-session-1")

    async def get_structured_reply_cursor(self, user_id: int, *, task_id: str | None = None):
        return self._store.get_structured_reply_cursor("claude-session-1")

    async def acknowledge_structured_reply(self, user_id: int, *, turn_id: str | None = None, permission_key: str | None = None, task_id: str | None = None) -> None:
        if turn_id is not None:
            self._store.mark_structured_reply_emitted("claude-session-1", turn_id=turn_id)
        if permission_key is not None:
            self._store.mark_structured_permission_emitted("claude-session-1", permission_key=permission_key)

    async def get_structured_user_question_cursor(self, user_id: int, *, task_id: str | None = None):
        return self._store.get_structured_user_question_cursor("claude-session-1")

    async def acknowledge_structured_user_question(self, user_id: int, *, question_key: str | None = None, task_id: str | None = None) -> None:
        if question_key is not None:
            self._store.mark_structured_user_question_emitted("claude-session-1", question_key=question_key)

    async def wait_for_structured_session_update(self, *, user_id: int, since_cursor: int, timeout_sec: float, task_id: str | None = None) -> bool:
        return await self._store.wait_for_publish("claude-session-1", since_cursor=since_cursor, timeout_sec=timeout_sec)


def _session(
    *,
    phase: SessionPhase,
    turns: list[ConversationTurn] | None = None,
    pending: PendingPermission | None = None,
    tool_calls: dict[str, ToolCallRecord] | None = None,
    session_id: str = "claude-session-1",
):
    return SimpleNamespace(
        session_id=session_id,
        phase=phase,
        turns=turns or [],
        pending_permission=pending,
        tool_calls=tool_calls or {},
    )


@pytest.mark.asyncio
async def test_presenter_emits_new_completed_turn_once() -> None:
    presenter = StructuredReplyPresenter(
        task_service=DummyTaskService(
            [
                _session(phase=SessionPhase.WAITING_FOR_INPUT),
                _session(
                    phase=SessionPhase.WAITING_FOR_INPUT,
                    turns=[ConversationTurn(turn_id="turn-1", role="assistant", text="\n你好\n", is_complete=True)],
                ),
                _session(
                    phase=SessionPhase.WAITING_FOR_INPUT,
                    turns=[ConversationTurn(turn_id="turn-1", role="assistant", text="\n你好\n", is_complete=True)],
                ),
            ]
        ),
        user_id=1,
    )

    await presenter.prime()
    first = await presenter.poll(task_id="task-1")
    second = await presenter.poll(task_id="task-1")

    assert first == ["你好"]
    assert second == []


@pytest.mark.asyncio
async def test_presenter_reports_pending_permission_once() -> None:
    pending = PendingPermission(tool_use_id="tool-1", tool_name="Bash", tool_input={"command": "pwd"})
    presenter = StructuredReplyPresenter(
        task_service=DummyTaskService(
            [
                _session(phase=SessionPhase.WAITING_FOR_INPUT),
                _session(phase=SessionPhase.WAITING_FOR_APPROVAL, pending=pending),
                _session(phase=SessionPhase.WAITING_FOR_APPROVAL, pending=pending),
            ]
        ),
        user_id=1,
    )

    await presenter.prime()
    first = await presenter.poll(task_id="task-1")
    second = await presenter.poll(task_id="task-1")

    assert first == [
        PermissionRequestOutput(
            text=build_permission_prompt(tool_name="Bash", tool_input={"command": "pwd"}),
            tool_use_id="tool-1",
            tool_name="Bash",
        )
    ]
    assert second == []


@pytest.mark.asyncio
async def test_presenter_reports_user_question_once_without_generic_progress() -> None:
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
    prompt = UserQuestionPrompt(
        tool_use_id="tool-ask-1",
        question_index=0,
        total_questions=1,
        header="处理方式",
        question="这两条误写到项目级的记忆，你要我怎么处理？",
        options=(
            UserQuestionOption(label="迁到全局(推荐)", description="保留记忆内容并迁移"),
            UserQuestionOption(label="直接删除", description="删除项目级这两条记忆"),
        ),
        multi_select=False,
    )
    presenter = StructuredReplyPresenter(
        task_service=DummyTaskService(
            [
                _session(phase=SessionPhase.WAITING_FOR_INPUT),
                _session(phase=SessionPhase.PROCESSING, tool_calls={"tool-ask-1": question_tool}),
                _session(phase=SessionPhase.PROCESSING, tool_calls={"tool-ask-1": question_tool}),
            ]
        ),
        user_id=1,
    )

    await presenter.prime()
    first = await presenter.poll(task_id="task-1")
    second = await presenter.poll(task_id="task-1")

    assert first == [UserQuestionOutput(text=build_user_question_prompt(prompt), question=prompt)]
    assert second == []


@pytest.mark.asyncio
async def test_presenter_reports_only_first_question_when_tool_contains_multiple_questions() -> None:
    question_tool = ToolCallRecord(
        tool_use_id="tool-ask-1",
        name="AskUserQuestion",
        input={
            "questions": [
                {
                    "header": "处理范围",
                    "question": "你说的范围我理解为这三块之一，具体按哪种处理？",
                    "options": [
                        {"label": "当前相关改动(推荐)", "description": "只处理相关已改动文件"},
                        {"label": "三个目录全部", "description": "范围非常大"},
                    ],
                    "multiSelect": False,
                },
                {
                    "header": "提交前置",
                    "question": "按你的 CLAUDE.md，要修改代码前先提交现有改动。现在是否允许我先做这一步？",
                    "options": [
                        {"label": "允许先提交(推荐)", "description": "先提交后继续"},
                        {"label": "暂不允许", "description": "先不改代码"},
                    ],
                    "multiSelect": False,
                },
            ]
        },
        status=ToolStatus.RUNNING,
    )
    first_prompt = UserQuestionPrompt(
        tool_use_id="tool-ask-1",
        question_index=0,
        total_questions=2,
        header="处理范围",
        question="你说的范围我理解为这三块之一，具体按哪种处理？",
        options=(
            UserQuestionOption(label="当前相关改动(推荐)", description="只处理相关已改动文件"),
            UserQuestionOption(label="三个目录全部", description="范围非常大"),
        ),
        multi_select=False,
    )
    presenter = StructuredReplyPresenter(
        task_service=DummyTaskService(
            [
                _session(phase=SessionPhase.WAITING_FOR_INPUT),
                _session(phase=SessionPhase.PROCESSING, tool_calls={"tool-ask-1": question_tool}),
                _session(phase=SessionPhase.PROCESSING, tool_calls={"tool-ask-1": question_tool}),
            ]
        ),
        user_id=1,
    )

    await presenter.prime()
    first = await presenter.poll(task_id="task-1")
    second = await presenter.poll(task_id="task-1")

    assert first == [UserQuestionOutput(text=build_user_question_prompt(first_prompt), question=first_prompt)]
    assert second == []


@pytest.mark.asyncio
async def test_presenter_skips_question_already_acknowledged_by_handler() -> None:
    question_tool = ToolCallRecord(
        tool_use_id="tool-ask-1",
        name="AskUserQuestion",
        input={
            "questions": [
                {
                    "header": "处理范围",
                    "question": "你说的范围我理解为这三块之一，具体按哪种处理？",
                    "options": [
                        {"label": "当前相关改动(推荐)", "description": "只处理相关已改动文件"},
                        {"label": "三个目录全部", "description": "范围非常大"},
                    ],
                    "multiSelect": False,
                },
                {
                    "header": "提交前置",
                    "question": "按你的 CLAUDE.md，要修改代码前先提交现有改动。现在是否允许我先做这一步？",
                    "options": [
                        {"label": "允许先提交(推荐)", "description": "先提交后继续"},
                        {"label": "暂不允许", "description": "先不改代码"},
                    ],
                    "multiSelect": False,
                },
            ]
        },
        status=ToolStatus.RUNNING,
    )
    service = DummyTaskService(
        [
            _session(phase=SessionPhase.WAITING_FOR_INPUT),
            _session(phase=SessionPhase.PROCESSING, tool_calls={"tool-ask-1": question_tool}),
            _session(phase=SessionPhase.PROCESSING, tool_calls={"tool-ask-1": question_tool}),
        ]
    )
    presenter = StructuredReplyPresenter(task_service=service, user_id=1)

    await presenter.prime()
    first = await presenter.poll(task_id="task-1")
    service._question_key = "tool-ask-1:0"
    second = await presenter.poll(task_id="task-1")

    assert len(first) == 1
    assert isinstance(first[0], UserQuestionOutput)
    assert second == []


@pytest.mark.asyncio
async def test_presenter_does_not_regress_to_first_question_when_cursor_already_advanced() -> None:
    question_tool = ToolCallRecord(
        tool_use_id="tool-ask-1",
        name="AskUserQuestion",
        input={
            "questions": [
                {
                    "header": "处理范围",
                    "question": "第一题",
                    "options": [
                        {"label": "A", "description": "A"},
                        {"label": "B", "description": "B"},
                    ],
                    "multiSelect": False,
                },
                {
                    "header": "提交前置",
                    "question": "第二题",
                    "options": [
                        {"label": "C", "description": "C"},
                        {"label": "D", "description": "D"},
                    ],
                    "multiSelect": False,
                },
            ]
        },
        status=ToolStatus.RUNNING,
    )
    service = DummyTaskService(
        [
            _session(phase=SessionPhase.WAITING_FOR_INPUT),
            _session(phase=SessionPhase.PROCESSING, tool_calls={"tool-ask-1": question_tool}),
        ]
    )
    service._question_key = "tool-ask-1:1"
    presenter = StructuredReplyPresenter(task_service=service, user_id=1)

    await presenter.prime()
    outputs = await presenter.poll(task_id="task-1")

    assert outputs == []


@pytest.mark.asyncio
async def test_presenter_reports_pending_ask_user_question_instead_of_permission_prompt() -> None:
    pending = PendingPermission(
        tool_use_id="tool-ask-pending",
        tool_name="AskUserQuestion",
        tool_input={
            "questions": [
                {
                    "header": "出发日期",
                    "question": "你想查哪一天出发？",
                    "options": [
                        {"label": "今天", "description": "查询今天从郑州到西安的车票"},
                        {"label": "明天", "description": "查询明天从郑州到西安的车票"},
                    ],
                    "multiSelect": False,
                },
                {
                    "header": "出发站",
                    "question": "你希望从哪个站出发？",
                    "options": [
                        {"label": "郑州站", "description": "只查询郑州站"},
                        {"label": "都查", "description": "同时查询郑州相关车站"},
                    ],
                    "multiSelect": False,
                },
            ]
        },
    )
    first_prompt = UserQuestionPrompt(
        tool_use_id="tool-ask-pending",
        question_index=0,
        total_questions=2,
        header="出发日期",
        question="你想查哪一天出发？",
        options=(
            UserQuestionOption(label="今天", description="查询今天从郑州到西安的车票"),
            UserQuestionOption(label="明天", description="查询明天从郑州到西安的车票"),
        ),
        multi_select=False,
    )
    presenter = StructuredReplyPresenter(
        task_service=DummyTaskService(
            [
                _session(phase=SessionPhase.WAITING_FOR_INPUT),
                _session(phase=SessionPhase.WAITING_FOR_APPROVAL, pending=pending),
                _session(phase=SessionPhase.WAITING_FOR_APPROVAL, pending=pending),
            ]
        ),
        user_id=1,
    )

    await presenter.prime()
    first = await presenter.poll(task_id="task-1")
    second = await presenter.poll(task_id="task-1")

    assert first == [UserQuestionOutput(text=build_user_question_prompt(first_prompt), question=first_prompt)]
    assert second == []


@pytest.mark.asyncio
async def test_presenter_reports_waiting_for_approval_ask_user_question_without_pending_permission_snapshot() -> None:
    waiting_question_tool = ToolCallRecord(
        tool_use_id="tool-ask-waiting",
        name="AskUserQuestion",
        input={
            "questions": [
                {
                    "header": "出发日期",
                    "question": "你想查哪一天出发？",
                    "options": [
                        {"label": "今天", "description": "查询今天从郑州到西安的车票"},
                        {"label": "明天", "description": "查询明天从郑州到西安的车票"},
                    ],
                    "multiSelect": False,
                },
                {
                    "header": "出发站",
                    "question": "你希望从哪个站出发？",
                    "options": [
                        {"label": "郑州站", "description": "只查询郑州站"},
                        {"label": "都查", "description": "同时查询郑州相关车站"},
                    ],
                    "multiSelect": False,
                },
            ]
        },
        status=ToolStatus.WAITING_FOR_APPROVAL,
    )
    first_prompt = UserQuestionPrompt(
        tool_use_id="tool-ask-waiting",
        question_index=0,
        total_questions=2,
        header="出发日期",
        question="你想查哪一天出发？",
        options=(
            UserQuestionOption(label="今天", description="查询今天从郑州到西安的车票"),
            UserQuestionOption(label="明天", description="查询明天从郑州到西安的车票"),
        ),
        multi_select=False,
    )
    presenter = StructuredReplyPresenter(
        task_service=DummyTaskService(
            [
                _session(phase=SessionPhase.WAITING_FOR_INPUT),
                _session(phase=SessionPhase.WAITING_FOR_APPROVAL, tool_calls={"tool-ask-waiting": waiting_question_tool}),
                _session(phase=SessionPhase.WAITING_FOR_APPROVAL, tool_calls={"tool-ask-waiting": waiting_question_tool}),
            ]
        ),
        user_id=1,
    )

    await presenter.prime()
    first = await presenter.poll(task_id="task-1")
    second = await presenter.poll(task_id="task-1")

    assert first == [UserQuestionOutput(text=build_user_question_prompt(first_prompt), question=first_prompt)]
    assert second == []


def test_build_permission_prompt_includes_specific_bash_command() -> None:
    prompt = build_permission_prompt(tool_name="Bash", tool_input={"command": "pwd"})

    assert prompt == "权限请求\n工具: Bash\n命令: pwd\n\n请点击下方按钮选择允许或拒绝。"


def test_build_permission_prompt_falls_back_to_compact_json_preview() -> None:
    prompt = build_permission_prompt(tool_name="Edit", tool_input={"old_string": "a" * 400, "new_string": "b"})

    assert "权限请求" in prompt
    assert "工具: Edit" in prompt
    assert "参数:" in prompt
    assert "..." in prompt


def test_build_tool_progress_message_includes_specific_bash_command() -> None:
    message = build_tool_progress_message(tool_name="Bash", tool_input={"command": "pytest -q"})

    assert message == "执行中\n工具: Bash\n命令: pytest -q"


@pytest.mark.asyncio
async def test_presenter_emits_running_tool_progress_once() -> None:
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

    assert first == [ProgressUpdateOutput(text="执行中\n工具: Bash\n命令: pytest -q")]
    assert second == []


@pytest.mark.asyncio
async def test_presenter_emits_compacting_progress_once() -> None:
    presenter = StructuredReplyPresenter(
        task_service=DummyTaskService(
            [
                _session(phase=SessionPhase.PROCESSING),
                _session(phase=SessionPhase.COMPACTING),
                _session(phase=SessionPhase.COMPACTING),
            ]
        ),
        user_id=1,
    )

    await presenter.prime()
    first = await presenter.poll(task_id="task-1")
    second = await presenter.poll(task_id="task-1")

    assert first == [ProgressUpdateOutput(text="执行进度\n正在整理上下文，稍后继续。")]
    assert second == []


@pytest.mark.asyncio
async def test_presenter_emits_resume_progress_after_permission() -> None:
    waiting_tool = ToolCallRecord(
        tool_use_id="tool-1",
        name="Bash",
        input={"command": "pytest -q"},
        status=ToolStatus.WAITING_FOR_APPROVAL,
    )
    resumed_tool = ToolCallRecord(
        tool_use_id="tool-1",
        name="Bash",
        input={"command": "pytest -q"},
        status=ToolStatus.RUNNING,
    )
    presenter = StructuredReplyPresenter(
        task_service=DummyTaskService(
            [
                _session(phase=SessionPhase.WAITING_FOR_APPROVAL, tool_calls={"tool-1": waiting_tool}),
                _session(phase=SessionPhase.PROCESSING, tool_calls={"tool-1": resumed_tool}),
            ]
        ),
        user_id=1,
    )

    await presenter.prime(baseline_current_snapshot=True)
    messages = await presenter.poll(task_id="task-1")

    assert messages == [ProgressUpdateOutput(text="继续执行\n工具: Bash\n命令: pytest -q")]


@pytest.mark.asyncio
async def test_presenter_final_poll_emits_fallback_once() -> None:
    presenter = StructuredReplyPresenter(
        task_service=DummyTaskService(
            [
                _session(phase=SessionPhase.PROCESSING, turns=[]),
                _session(phase=SessionPhase.WAITING_FOR_INPUT, turns=[]),
                _session(phase=SessionPhase.WAITING_FOR_INPUT, turns=[]),
            ]
        ),
        user_id=1,
    )

    await presenter.prime()
    first = await presenter.poll(task_id="task-1", final=True)
    second = await presenter.poll(task_id="task-1", final=True)

    assert first == ["结构化回复暂不可用，已回退为原始输出。"]
    assert second == []


@pytest.mark.asyncio
async def test_presenter_final_poll_does_not_fallback_after_structured_reply_emitted() -> None:
    presenter = StructuredReplyPresenter(
        task_service=DummyTaskService(
            [
                _session(phase=SessionPhase.PROCESSING, turns=[]),
                _session(
                    phase=SessionPhase.WAITING_FOR_INPUT,
                    turns=[ConversationTurn(turn_id="turn-1", role="assistant", text="\n你好\n", is_complete=True)],
                ),
                _session(
                    phase=SessionPhase.WAITING_FOR_INPUT,
                    turns=[ConversationTurn(turn_id="turn-1", role="assistant", text="\n你好\n", is_complete=True)],
                ),
            ]
        ),
        user_id=1,
    )

    await presenter.prime()
    first = await presenter.poll(task_id="task-1")
    final = await presenter.poll(task_id="task-1", final=True)

    assert first == ["你好"]
    assert final == []


@pytest.mark.asyncio
async def test_presenter_without_structured_session_emits_nothing() -> None:
    presenter = StructuredReplyPresenter(task_service=DummyTaskService([None, None]), user_id=1)

    await presenter.prime()
    messages = await presenter.poll(task_id="task-1", final=True)

    assert messages == []


def test_stream_text_helpers_strip_and_preview() -> None:
    raw = "TGCLI_BEGIN\n正文\n\n\nTGCLI_DONE\n"

    assert strip_bridge_markers(raw) == "正文\n\n\n"
    assert normalize_stream_text(raw) == "正文"
    assert preview_stream_text(raw) == "正文"


class SwitchingTaskService:
    def __init__(self) -> None:
        self.current = _session(phase=SessionPhase.PROCESSING, session_id="old-session")
        self._cursors = {"old-session": 35, "new-session": 12}

    async def get_structured_session(self, user_id: int, *, log_missing: bool = True):
        return self.current

    async def get_structured_session_cursor(self, user_id: int, *, task_id: str | None = None) -> int:
        return self._cursors[self.current.session_id]

    async def get_structured_reply_cursor(self, user_id: int, *, task_id: str | None = None):
        return None, None

    async def acknowledge_structured_reply(self, user_id: int, *, turn_id: str | None = None, permission_key: str | None = None, task_id: str | None = None) -> None:
        return None

    async def get_structured_user_question_cursor(self, user_id: int, *, task_id: str | None = None):
        return None

    async def acknowledge_structured_user_question(self, user_id: int, *, question_key: str | None = None, task_id: str | None = None) -> None:
        return None

    async def wait_for_structured_session_update(self, *, user_id: int, since_cursor: int, timeout_sec: float, task_id: str | None = None) -> bool:
        return self._cursors[self.current.session_id] > since_cursor


@pytest.mark.asyncio
async def test_presenter_wait_for_update_detects_session_switch_with_lower_revision() -> None:
    task_service = SwitchingTaskService()
    presenter = StructuredReplyPresenter(task_service=task_service, user_id=1)
    await presenter.prime()

    task_service.current = _session(
        phase=SessionPhase.WAITING_FOR_INPUT,
        session_id="new-session",
        turns=[ConversationTurn(turn_id="turn-new", role="assistant", text="\n你好\n", is_complete=True)],
    )

    changed = await presenter.wait_for_update(timeout_sec=0.01)

    assert changed is True
    assert await presenter.poll(task_id="task-1") == ["你好"]


@pytest.mark.asyncio
async def test_presenter_persists_reply_cursor_across_restarts(tmp_path) -> None:
    store = SessionStore(FileSessionStore(str(tmp_path)))
    state = store.get_or_create(session_id="claude-session-1", user_id=1, workdir="/tmp", terminal_id="term-1")
    store.process(SessionEvent(session_id=state.session_id, type=SessionEventType.TURN_STARTED, payload={"turn_id": "turn-1", "role": "assistant"}))
    store.process(SessionEvent(session_id=state.session_id, type=SessionEventType.PARSER_UPDATED, payload={"turn_id": "turn-1", "text": "\n你好\n", "is_complete": True}))

    presenter = StructuredReplyPresenter(task_service=PersistentTaskService(store), user_id=1)
    await presenter.prime()
    first = await presenter.poll(task_id="task-1")
    assert first == ["你好"]

    reloaded = SessionStore(FileSessionStore(str(tmp_path)))
    presenter = StructuredReplyPresenter(task_service=PersistentTaskService(reloaded), user_id=1)
    await presenter.prime()
    second = await presenter.poll(task_id="task-1")

    assert second == []


@pytest.mark.asyncio
async def test_presenter_restart_with_ack_only_persist_does_not_emit(tmp_path) -> None:
    store = SessionStore(FileSessionStore(str(tmp_path)))
    state = store.get_or_create(session_id="claude-session-1", user_id=1, workdir="/tmp", terminal_id="term-1")
    store.process(SessionEvent(session_id=state.session_id, type=SessionEventType.TURN_STARTED, payload={"turn_id": "turn-1", "role": "assistant"}))
    store.process(SessionEvent(session_id=state.session_id, type=SessionEventType.PARSER_UPDATED, payload={"turn_id": "turn-1", "text": "\n你好\n", "is_complete": True}))
    store.mark_structured_reply_emitted("claude-session-1", turn_id="turn-1")

    reloaded = SessionStore(FileSessionStore(str(tmp_path)))
    presenter = StructuredReplyPresenter(task_service=PersistentTaskService(reloaded), user_id=1)
    await presenter.prime()

    assert await presenter.poll(task_id="task-1") == []


@pytest.mark.asyncio
async def test_presenter_prime_uses_current_snapshot_as_baseline_when_cursor_missing(tmp_path) -> None:
    store = SessionStore(FileSessionStore(str(tmp_path)))
    state = store.get_or_create(session_id="claude-session-1", user_id=1, workdir="/tmp", terminal_id="term-1")
    state.phase = SessionPhase.WAITING_FOR_INPUT
    state.turns.append(ConversationTurn(turn_id="turn-old", role="assistant", text="\n旧回复\n", is_complete=True))
    store._persist(state)

    presenter = StructuredReplyPresenter(task_service=PersistentTaskService(store), user_id=1)
    await presenter.prime(baseline_current_snapshot=True)

    assert await presenter.poll(task_id="task-1") == []


@pytest.mark.asyncio
async def test_presenter_final_poll_still_falls_back_when_only_old_reply_cursor_exists(tmp_path) -> None:
    store = SessionStore(FileSessionStore(str(tmp_path)))
    state = store.get_or_create(session_id="claude-session-1", user_id=1, workdir="/tmp", terminal_id="term-1")
    state.phase = SessionPhase.PROCESSING
    state.turns.append(ConversationTurn(turn_id="turn-1", role="assistant", text="\n旧回复\n", is_complete=True))
    store._persist(state)
    store.mark_structured_reply_emitted("claude-session-1", turn_id="turn-1")

    presenter = StructuredReplyPresenter(task_service=PersistentTaskService(store), user_id=1)
    await presenter.prime()

    assert await presenter.poll(task_id="task-1") == []

    state.phase = SessionPhase.WAITING_FOR_INPUT
    store._persist(state)

    assert await presenter.poll(task_id="task-1", final=True) == ["结构化回复暂不可用，已回退为原始输出。"]


@pytest.mark.asyncio
async def test_presenter_wait_for_update_ignores_checkpoint_only_persist(tmp_path) -> None:
    store = SessionStore(FileSessionStore(str(tmp_path)))
    store.get_or_create(session_id="claude-session-1", user_id=1, workdir="/tmp", terminal_id="term-1")
    presenter = StructuredReplyPresenter(task_service=PersistentTaskService(store), user_id=1)
    await presenter.prime()

    assert await presenter.poll(task_id="task-1") == []

    store.save_checkpoint("claude-session-1", ParserCheckpoint(last_offset=5))

    changed = await presenter.wait_for_update(timeout_sec=0.01)
    assert changed is False
    assert await presenter.poll(task_id="task-1") == []
