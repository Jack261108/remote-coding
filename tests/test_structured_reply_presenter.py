from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.bot.presenters.structured_reply_presenter import (
    FileToolAggregateStatusOutput,
    PermissionRequestOutput,
    ProgressUpdateOutput,
    StructuredReplyPresenter,
    SubagentAggregateStatusOutput,
    SubagentToolStatusOutput,
    TaskListItemStatusOutput,
    TaskListStatusOutput,
    ToolStatusOutput,
    UserQuestionOutput,
    build_file_tool_aggregate_status_message,
    build_permission_prompt,
    build_subagent_aggregate_status_message,
    build_task_list_status_message,
    build_tool_progress_message,
    build_tool_status_message,
    build_tool_task_list_message,
    build_user_question_prompt,
    normalize_stream_text,
    preview_stream_text,
    strip_bridge_markers,
)
from app.adapters.storage.file_session_store import FileSessionStore
from app.domain.session_models import ConversationTurn, ParserCheckpoint, PendingPermission, SessionEvent, SessionEventType, SessionPhase, SubagentToolCall, ToolCallRecord, ToolStatus
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

    assert message == "🔄 执行中\n工具: Bash\n命令: pytest -q"


def test_build_tool_status_message_formats_final_states() -> None:
    assert build_tool_status_message(
        tool_name="Bash",
        tool_input={"command": "pytest -q"},
        status=ToolStatus.SUCCESS.value,
    ) == "✅ 执行完成\n工具: Bash\n命令: pytest -q"
    assert build_tool_status_message(
        tool_name="Bash",
        tool_input={"command": "pytest -q"},
        status=ToolStatus.ERROR.value,
    ) == "❌ 执行失败\n工具: Bash\n命令: pytest -q"
    assert build_tool_status_message(
        tool_name="Bash",
        tool_input={"command": "pytest -q"},
        status=ToolStatus.INTERRUPTED.value,
    ) == "⏹️ 已中断\n工具: Bash\n命令: pytest -q"
    assert build_tool_status_message(
        tool_name="Bash",
        tool_input={"command": "rm file"},
        status=ToolStatus.WAITING_FOR_APPROVAL.value,
    ) == "⏳ 等待权限\n工具: Bash\n命令: rm file"
    assert build_tool_status_message(
        tool_name="Bash",
        tool_input={"command": "pytest -q"},
        status=ToolStatus.RUNNING.value,
        resumed=True,
    ) == "🔄 继续执行\n工具: Bash\n命令: pytest -q"
    assert build_tool_status_message(
        tool_name="Bash",
        tool_input={"command": "pytest -q"},
        status="unknown",
    ) == "⏳ 执行中\n工具: Bash\n命令: pytest -q"


def test_build_tool_task_list_message_marks_current_task() -> None:
    message = build_tool_task_list_message(
        ToolStatusOutput(
            tool_use_id="task-1",
            tool_name="Task",
            tool_input={"description": "修复测试失败"},
            status=ToolStatus.RUNNING.value,
            subagent_tools=(
                SubagentToolStatusOutput(
                    tool_use_id="read-1",
                    tool_name="Read",
                    tool_input={"file_path": "app/foo.py"},
                    status=ToolStatus.SUCCESS.value,
                ),
                SubagentToolStatusOutput(
                    tool_use_id="bash-1",
                    tool_name="Bash",
                    tool_input={"command": "pytest -q"},
                    status=ToolStatus.RUNNING.value,
                ),
            ),
        )
    )

    assert message == (
        "任务列表\n"
        "任务: 修复测试失败\n"
        "状态: 执行中\n"
        "当前: 🔄 2. Bash\n"
        "\n"
        "✅ 1. Read - 完成 - 文件: app/foo.py\n"
        "=> 🔄 2. Bash - 执行中 - 命令: pytest -q"
    )


def test_build_task_list_status_message_marks_current_task() -> None:
    message = build_task_list_status_message(
        TaskListStatusOutput(
            message_key="task-list",
            items=(
                TaskListItemStatusOutput(
                    task_id="1",
                    subject="梳理项目结构",
                    status="completed",
                    active_form="梳理项目结构",
                ),
                TaskListItemStatusOutput(
                    task_id="2",
                    subject="评估当前改动",
                    status="in_progress",
                    active_form="评估当前改动",
                ),
                TaskListItemStatusOutput(
                    task_id="3",
                    subject="形成优化建议",
                    status="pending",
                    active_form="形成优化建议",
                ),
            ),
        )
    )

    assert message == (
        "任务列表\n"
        "当前: 🔄 2. 评估当前改动\n"
        "\n"
        "✅ 1. 梳理项目结构 - 完成\n"
        "=> 🔄 2. 评估当前改动 - 执行中\n"
        "⏳ 3. 形成优化建议 - 待执行"
    )


def test_build_task_list_status_message_marks_first_pending_when_no_task_is_running() -> None:
    message = build_task_list_status_message(
        TaskListStatusOutput(
            message_key="task-list",
            items=(
                TaskListItemStatusOutput(
                    task_id="1",
                    subject="梳理项目结构",
                    status="completed",
                    active_form="梳理项目结构",
                ),
                TaskListItemStatusOutput(
                    task_id="2",
                    subject="识别优化机会",
                    status="pending",
                    active_form="识别优化机会",
                ),
                TaskListItemStatusOutput(
                    task_id="3",
                    subject="汇总优先级建议",
                    status="pending",
                    active_form="汇总优先级建议",
                ),
            ),
        )
    )

    assert message == (
        "任务列表\n"
        "当前: ⏳ 2. 识别优化机会\n"
        "\n"
        "✅ 1. 梳理项目结构 - 完成\n"
        "=> ⏳ 2. 识别优化机会 - 待执行\n"
        "⏳ 3. 汇总优先级建议 - 待执行"
    )


def test_build_file_tool_aggregate_status_message_groups_reads_and_searches() -> None:
    message = build_file_tool_aggregate_status_message(
        FileToolAggregateStatusOutput(
            message_key="file-tool-aggregate",
            tools=(
                ToolStatusOutput(
                    tool_use_id="grep-1",
                    tool_name="Grep",
                    tool_input={"pattern": "SessionStore"},
                    status=ToolStatus.SUCCESS.value,
                ),
                ToolStatusOutput(
                    tool_use_id="read-1",
                    tool_name="Read",
                    tool_input={"file_path": "app/services/session_store.py"},
                    status=ToolStatus.RUNNING.value,
                ),
                ToolStatusOutput(
                    tool_use_id="read-2",
                    tool_name="Read",
                    tool_input={"file_path": "app/bot/handlers/command_run.py"},
                    status=ToolStatus.SUCCESS.value,
                ),
            ),
        )
    )

    assert message == (
        "🔄 文件检索 · 执行中\n"
        "搜索 1 次，读取 2 个文件\n"
        "当前: 🔄 Read · 文件: app/services/session_store.py\n"
        "\n"
        "✅ 1. Grep - 完成 · 内容: SessionStore\n"
        "🔄 2. Read - 执行中 · 文件: app/services/session_store.py\n"
        "✅ 3. Read - 完成 · 文件: app/bot/handlers/command_run.py"
    )


@pytest.mark.asyncio
async def test_presenter_aggregates_top_level_file_tools_without_read_spam() -> None:
    grep_tool = ToolCallRecord(
        tool_use_id="grep-1",
        name="Grep",
        input={"pattern": "SessionStore"},
        status=ToolStatus.SUCCESS,
    )
    read_1_running = ToolCallRecord(
        tool_use_id="read-1",
        name="Read",
        input={"file_path": "app/services/session_store.py"},
        status=ToolStatus.RUNNING,
    )
    read_1_success = ToolCallRecord(
        tool_use_id="read-1",
        name="Read",
        input={"file_path": "app/services/session_store.py"},
        status=ToolStatus.SUCCESS,
    )
    read_2_success = ToolCallRecord(
        tool_use_id="read-2",
        name="Read",
        input={"file_path": "app/bot/handlers/command_run.py"},
        status=ToolStatus.SUCCESS,
    )
    presenter = StructuredReplyPresenter(
        task_service=DummyTaskService(
            [
                _session(phase=SessionPhase.WAITING_FOR_INPUT),
                _session(phase=SessionPhase.PROCESSING, tool_calls={"grep-1": grep_tool, "read-1": read_1_running}),
                _session(phase=SessionPhase.WAITING_FOR_INPUT, tool_calls={"grep-1": grep_tool, "read-1": read_1_success, "read-2": read_2_success}),
            ]
        ),
        user_id=1,
    )

    await presenter.prime()
    first = await presenter.poll(task_id="task-1")
    second = await presenter.poll(task_id="task-1")

    assert first == [
        FileToolAggregateStatusOutput(
            message_key="file-tool-aggregate",
            tools=(
                ToolStatusOutput(
                    tool_use_id="grep-1",
                    tool_name="Grep",
                    tool_input={"pattern": "SessionStore"},
                    status=ToolStatus.SUCCESS.value,
                ),
                ToolStatusOutput(
                    tool_use_id="read-1",
                    tool_name="Read",
                    tool_input={"file_path": "app/services/session_store.py"},
                    status=ToolStatus.RUNNING.value,
                ),
            ),
        )
    ]
    assert second == [
        FileToolAggregateStatusOutput(
            message_key="file-tool-aggregate",
            tools=(
                ToolStatusOutput(
                    tool_use_id="grep-1",
                    tool_name="Grep",
                    tool_input={"pattern": "SessionStore"},
                    status=ToolStatus.SUCCESS.value,
                ),
                ToolStatusOutput(
                    tool_use_id="read-1",
                    tool_name="Read",
                    tool_input={"file_path": "app/services/session_store.py"},
                    status=ToolStatus.SUCCESS.value,
                ),
                ToolStatusOutput(
                    tool_use_id="read-2",
                    tool_name="Read",
                    tool_input={"file_path": "app/bot/handlers/command_run.py"},
                    status=ToolStatus.SUCCESS.value,
                ),
            ),
        )
    ]


def test_build_subagent_aggregate_status_message_shows_subagent_type() -> None:
    message = build_subagent_aggregate_status_message(
        SubagentAggregateStatusOutput(
            message_key="subagent-aggregate",
            containers=(
                ToolStatusOutput(
                    tool_use_id="agent-1",
                    tool_name="Agent",
                    tool_input={"subagent_type": "Explore", "description": "项目优化点审计"},
                    status=ToolStatus.RUNNING.value,
                    subagent_tools=(
                        SubagentToolStatusOutput(
                            tool_use_id="read-1",
                            tool_name="Read",
                            tool_input={"file_path": "app/foo.py"},
                            status=ToolStatus.SUCCESS.value,
                        ),
                        SubagentToolStatusOutput(
                            tool_use_id="read-2",
                            tool_name="Read",
                            tool_input={"file_path": "app/bar.py"},
                            status=ToolStatus.RUNNING.value,
                        ),
                        SubagentToolStatusOutput(
                            tool_use_id="glob-1",
                            tool_name="Glob",
                            tool_input={"path": "tests"},
                            status=ToolStatus.SUCCESS.value,
                        ),
                    ),
                ),
            ),
        )
    )

    assert message == (
        "🔄 1 agents running\n"
        "\n"
        "- 🔄 Explore(项目优化点审计) · 3 tool uses · Running\n"
        "  名称: Read ×2、Glob"
    )


def test_build_subagent_aggregate_status_message_uses_waiting_icon_for_unknown_status() -> None:
    message = build_subagent_aggregate_status_message(
        SubagentAggregateStatusOutput(
            message_key="subagent-aggregate",
            containers=(
                ToolStatusOutput(
                    tool_use_id="agent-1",
                    tool_name="Agent",
                    tool_input={"description": "未知状态"},
                    status="unknown",
                ),
            ),
        )
    )

    assert message == "⏳ 1 agents finished\n\n- ⏳ 未知状态 · 0 tool uses · Done"


def test_build_subagent_aggregate_status_message_formats_agent_summary() -> None:
    message = build_subagent_aggregate_status_message(
        SubagentAggregateStatusOutput(
            message_key="subagent-aggregate",
            containers=(
                ToolStatusOutput(
                    tool_use_id="agent-1",
                    tool_name="Agent",
                    tool_input={"description": "项目架构扫描"},
                    status=ToolStatus.SUCCESS.value,
                    subagent_tools=tuple(
                        SubagentToolStatusOutput(
                            tool_use_id=f"agent-1-tool-{index}",
                            tool_name="Read",
                            tool_input={"file_path": f"app/{index}.py"},
                            status=ToolStatus.SUCCESS.value,
                        )
                        for index in range(51)
                    ),
                ),
                ToolStatusOutput(
                    tool_use_id="agent-2",
                    tool_name="Agent",
                    tool_input={"description": "测试质量扫描"},
                    status=ToolStatus.SUCCESS.value,
                    subagent_tools=tuple(
                        SubagentToolStatusOutput(
                            tool_use_id=f"agent-2-tool-{index}",
                            tool_name="Glob",
                            tool_input={"path": "tests"},
                            status=ToolStatus.SUCCESS.value,
                        )
                        for index in range(29)
                    ),
                ),
                ToolStatusOutput(
                    tool_use_id="agent-3",
                    tool_name="Agent",
                    tool_input={"description": "安全性能扫描"},
                    status=ToolStatus.SUCCESS.value,
                    subagent_tools=tuple(
                        SubagentToolStatusOutput(
                            tool_use_id=f"agent-3-tool-{index}",
                            tool_name="Grep",
                            tool_input={"pattern": "password"},
                            status=ToolStatus.SUCCESS.value,
                        )
                        for index in range(40)
                    ),
                ),
            ),
        )
    )

    assert message == (
        "✅ 3 agents finished\n"
        "\n"
        "- ✅ 项目架构扫描 · 51 tool uses · Done\n"
        "  名称: Read ×51\n"
        "- ✅ 测试质量扫描 · 29 tool uses · Done\n"
        "  名称: Glob ×29\n"
        "- ✅ 安全性能扫描 · 40 tool uses · Done\n"
        "  名称: Grep ×40"
    )


@pytest.mark.asyncio
async def test_presenter_emits_one_aggregate_for_multiple_agents() -> None:
    agent_1 = ToolCallRecord(
        tool_use_id="agent-1",
        name="Agent",
        input={"description": "项目架构扫描"},
        status=ToolStatus.RUNNING,
        subagent_tools=[
            SubagentToolCall(
                tool_use_id="read-1",
                name="Read",
                input={"file_path": "app/foo.py"},
                status=ToolStatus.SUCCESS,
            )
        ],
    )
    agent_2 = ToolCallRecord(
        tool_use_id="agent-2",
        name="Agent",
        input={"description": "测试质量扫描"},
        status=ToolStatus.RUNNING,
        subagent_tools=[
            SubagentToolCall(
                tool_use_id="grep-1",
                name="Grep",
                input={"pattern": "pytest"},
                status=ToolStatus.RUNNING,
            )
        ],
    )
    presenter = StructuredReplyPresenter(
        task_service=DummyTaskService(
            [
                _session(phase=SessionPhase.WAITING_FOR_INPUT),
                _session(phase=SessionPhase.PROCESSING, tool_calls={"agent-1": agent_1, "agent-2": agent_2}),
            ]
        ),
        user_id=1,
    )

    await presenter.prime()
    outputs = await presenter.poll(task_id="task-1")

    assert outputs == [
        SubagentAggregateStatusOutput(
            message_key="subagent-aggregate",
            containers=(
                ToolStatusOutput(
                    tool_use_id="agent-1",
                    tool_name="Agent",
                    tool_input={"description": "项目架构扫描"},
                    status=ToolStatus.RUNNING.value,
                    subagent_tools=(
                        SubagentToolStatusOutput(
                            tool_use_id="read-1",
                            tool_name="Read",
                            tool_input={"file_path": "app/foo.py"},
                            status=ToolStatus.SUCCESS.value,
                        ),
                    ),
                ),
                ToolStatusOutput(
                    tool_use_id="agent-2",
                    tool_name="Agent",
                    tool_input={"description": "测试质量扫描"},
                    status=ToolStatus.RUNNING.value,
                    subagent_tools=(
                        SubagentToolStatusOutput(
                            tool_use_id="grep-1",
                            tool_name="Grep",
                            tool_input={"pattern": "pytest"},
                            status=ToolStatus.RUNNING.value,
                        ),
                    ),
                ),
            ),
        )
    ]


@pytest.mark.asyncio
async def test_presenter_emits_subagent_aggregate_with_subagent_tools() -> None:
    task_tool = ToolCallRecord(
        tool_use_id="task-1",
        name="Task",
        input={"description": "修复测试失败"},
        status=ToolStatus.RUNNING,
        subagent_tools=[
            SubagentToolCall(
                tool_use_id="read-1",
                name="Read",
                input={"file_path": "app/foo.py"},
                status=ToolStatus.RUNNING,
            )
        ],
    )
    presenter = StructuredReplyPresenter(
        task_service=DummyTaskService(
            [
                _session(phase=SessionPhase.WAITING_FOR_INPUT),
                _session(phase=SessionPhase.PROCESSING, tool_calls={"task-1": task_tool}),
                _session(phase=SessionPhase.PROCESSING, tool_calls={"task-1": task_tool}),
            ]
        ),
        user_id=1,
    )

    await presenter.prime()
    first = await presenter.poll(task_id="task-1")
    second = await presenter.poll(task_id="task-1")

    assert first == [
        SubagentAggregateStatusOutput(
            message_key="subagent-aggregate",
            containers=(
                ToolStatusOutput(
                    tool_use_id="task-1",
                    tool_name="Task",
                    tool_input={"description": "修复测试失败"},
                    status=ToolStatus.RUNNING.value,
                    subagent_tools=(
                        SubagentToolStatusOutput(
                            tool_use_id="read-1",
                            tool_name="Read",
                            tool_input={"file_path": "app/foo.py"},
                            status=ToolStatus.RUNNING.value,
                        ),
                    ),
                ),
            ),
        )
    ]
    assert second == []


@pytest.mark.asyncio
async def test_presenter_emits_subagent_aggregate_when_subagent_status_changes() -> None:
    read_running_task = ToolCallRecord(
        tool_use_id="task-1",
        name="Task",
        input={"description": "修复测试失败"},
        status=ToolStatus.RUNNING,
        subagent_tools=[
            SubagentToolCall(
                tool_use_id="read-1",
                name="Read",
                input={"file_path": "app/foo.py"},
                status=ToolStatus.RUNNING,
            )
        ],
    )
    bash_running_task = ToolCallRecord(
        tool_use_id="task-1",
        name="Task",
        input={"description": "修复测试失败"},
        status=ToolStatus.RUNNING,
        subagent_tools=[
            SubagentToolCall(
                tool_use_id="read-1",
                name="Read",
                input={"file_path": "app/foo.py"},
                status=ToolStatus.SUCCESS,
            ),
            SubagentToolCall(
                tool_use_id="bash-1",
                name="Bash",
                input={"command": "pytest -q"},
                status=ToolStatus.RUNNING,
            ),
        ],
    )
    presenter = StructuredReplyPresenter(
        task_service=DummyTaskService(
            [
                _session(phase=SessionPhase.WAITING_FOR_INPUT),
                _session(phase=SessionPhase.PROCESSING, tool_calls={"task-1": read_running_task}),
                _session(phase=SessionPhase.PROCESSING, tool_calls={"task-1": bash_running_task}),
            ]
        ),
        user_id=1,
    )

    await presenter.prime()
    first = await presenter.poll(task_id="task-1")
    second = await presenter.poll(task_id="task-1")

    assert first == [
        SubagentAggregateStatusOutput(
            message_key="subagent-aggregate",
            containers=(
                ToolStatusOutput(
                    tool_use_id="task-1",
                    tool_name="Task",
                    tool_input={"description": "修复测试失败"},
                    status=ToolStatus.RUNNING.value,
                    subagent_tools=(
                        SubagentToolStatusOutput(
                            tool_use_id="read-1",
                            tool_name="Read",
                            tool_input={"file_path": "app/foo.py"},
                            status=ToolStatus.RUNNING.value,
                        ),
                    ),
                ),
            ),
        )
    ]
    assert second == [
        SubagentAggregateStatusOutput(
            message_key="subagent-aggregate",
            containers=(
                ToolStatusOutput(
                    tool_use_id="task-1",
                    tool_name="Task",
                    tool_input={"description": "修复测试失败"},
                    status=ToolStatus.RUNNING.value,
                    subagent_tools=(
                        SubagentToolStatusOutput(
                            tool_use_id="read-1",
                            tool_name="Read",
                            tool_input={"file_path": "app/foo.py"},
                            status=ToolStatus.SUCCESS.value,
                        ),
                        SubagentToolStatusOutput(
                            tool_use_id="bash-1",
                            tool_name="Bash",
                            tool_input={"command": "pytest -q"},
                            status=ToolStatus.RUNNING.value,
                        ),
                    ),
                ),
            ),
        )
    ]


@pytest.mark.asyncio
async def test_presenter_preserves_subagent_tool_count_when_final_snapshot_is_empty() -> None:
    running_agent = ToolCallRecord(
        tool_use_id="agent-1",
        name="Agent",
        input={"description": "项目优化点调研"},
        status=ToolStatus.RUNNING,
        subagent_tools=[
            SubagentToolCall(
                tool_use_id="read-1",
                name="Read",
                input={"file_path": "app/foo.py"},
                status=ToolStatus.SUCCESS,
            ),
            SubagentToolCall(
                tool_use_id="grep-1",
                name="Grep",
                input={"pattern": "pytest"},
                status=ToolStatus.RUNNING,
            ),
        ],
    )
    finished_agent = ToolCallRecord(
        tool_use_id="agent-1",
        name="Agent",
        input={"description": "项目优化点调研"},
        status=ToolStatus.SUCCESS,
        subagent_tools=[],
    )
    presenter = StructuredReplyPresenter(
        task_service=DummyTaskService(
            [
                _session(phase=SessionPhase.WAITING_FOR_INPUT),
                _session(phase=SessionPhase.PROCESSING, tool_calls={"agent-1": running_agent}),
                _session(phase=SessionPhase.WAITING_FOR_INPUT, tool_calls={"agent-1": finished_agent}),
            ]
        ),
        user_id=1,
    )

    await presenter.prime()
    first = await presenter.poll(task_id="task-1")
    second = await presenter.poll(task_id="task-1")

    assert first == [
        SubagentAggregateStatusOutput(
            message_key="subagent-aggregate",
            containers=(
                ToolStatusOutput(
                    tool_use_id="agent-1",
                    tool_name="Agent",
                    tool_input={"description": "项目优化点调研"},
                    status=ToolStatus.RUNNING.value,
                    subagent_tools=(
                        SubagentToolStatusOutput(
                            tool_use_id="read-1",
                            tool_name="Read",
                            tool_input={"file_path": "app/foo.py"},
                            status=ToolStatus.SUCCESS.value,
                        ),
                        SubagentToolStatusOutput(
                            tool_use_id="grep-1",
                            tool_name="Grep",
                            tool_input={"pattern": "pytest"},
                            status=ToolStatus.RUNNING.value,
                        ),
                    ),
                ),
            ),
        )
    ]
    assert second == [
        SubagentAggregateStatusOutput(
            message_key="subagent-aggregate",
            containers=(
                ToolStatusOutput(
                    tool_use_id="agent-1",
                    tool_name="Agent",
                    tool_input={"description": "项目优化点调研"},
                    status=ToolStatus.SUCCESS.value,
                    subagent_tools=(
                        SubagentToolStatusOutput(
                            tool_use_id="read-1",
                            tool_name="Read",
                            tool_input={"file_path": "app/foo.py"},
                            status=ToolStatus.SUCCESS.value,
                        ),
                        SubagentToolStatusOutput(
                            tool_use_id="grep-1",
                            tool_name="Grep",
                            tool_input={"pattern": "pytest"},
                            status=ToolStatus.SUCCESS.value,
                        ),
                    ),
                ),
            ),
        )
    ]
    assert "项目优化点调研 · 2 tool uses · Done" in build_subagent_aggregate_status_message(second[0])


@pytest.mark.asyncio
async def test_presenter_skips_flat_status_for_nested_tool_duplicate() -> None:
    task_tool = ToolCallRecord(
        tool_use_id="task-1",
        name="Task",
        input={"description": "修复测试失败"},
        status=ToolStatus.RUNNING,
        subagent_tools=[
            SubagentToolCall(
                tool_use_id="bash-1",
                name="Bash",
                input={"command": "pytest -q"},
                status=ToolStatus.RUNNING,
            )
        ],
    )
    duplicate_bash_tool = ToolCallRecord(
        tool_use_id="bash-1",
        name="Bash",
        input={"command": "pytest -q"},
        status=ToolStatus.RUNNING,
    )
    presenter = StructuredReplyPresenter(
        task_service=DummyTaskService(
            [
                _session(phase=SessionPhase.WAITING_FOR_INPUT),
                _session(
                    phase=SessionPhase.PROCESSING,
                    tool_calls={"task-1": task_tool, "bash-1": duplicate_bash_tool},
                ),
            ]
        ),
        user_id=1,
    )

    await presenter.prime()
    outputs = await presenter.poll(task_id="task-1")

    assert outputs == [
        SubagentAggregateStatusOutput(
            message_key="subagent-aggregate",
            containers=(
                ToolStatusOutput(
                    tool_use_id="task-1",
                    tool_name="Task",
                    tool_input={"description": "修复测试失败"},
                    status=ToolStatus.RUNNING.value,
                    subagent_tools=(
                        SubagentToolStatusOutput(
                            tool_use_id="bash-1",
                            tool_name="Bash",
                            tool_input={"command": "pytest -q"},
                            status=ToolStatus.RUNNING.value,
                        ),
                    ),
                ),
            ),
        )
    ]


@pytest.mark.asyncio
async def test_presenter_emits_task_list_for_task_create_and_update_without_tool_spam() -> None:
    create_1 = ToolCallRecord(
        tool_use_id="create-1",
        name="TaskCreate",
        input={
            "subject": "梳理项目结构",
            "description": "查看当前仓库的主要目录、技术栈和入口文件。",
            "activeForm": "梳理项目结构",
        },
        status=ToolStatus.SUCCESS,
        structured_result={"task": {"id": "1", "subject": "梳理项目结构"}},
    )
    create_2 = ToolCallRecord(
        tool_use_id="create-2",
        name="TaskCreate",
        input={
            "subject": "评估当前改动",
            "description": "查看当前未提交改动。",
            "activeForm": "评估当前改动",
        },
        status=ToolStatus.SUCCESS,
        structured_result={"task": {"id": "2", "subject": "评估当前改动"}},
    )
    update_1_running = ToolCallRecord(
        tool_use_id="update-1-running",
        name="TaskUpdate",
        input={"taskId": "1", "status": "in_progress"},
        status=ToolStatus.SUCCESS,
    )
    update_1_completed = ToolCallRecord(
        tool_use_id="update-1-completed",
        name="TaskUpdate",
        input={"taskId": "1", "status": "completed"},
        status=ToolStatus.SUCCESS,
    )
    update_2_running = ToolCallRecord(
        tool_use_id="update-2-running",
        name="TaskUpdate",
        input={"taskId": "2", "status": "in_progress"},
        status=ToolStatus.SUCCESS,
    )
    glob_tool = ToolCallRecord(
        tool_use_id="glob-1",
        name="Glob",
        input={"pattern": "**/*.py"},
        status=ToolStatus.RUNNING,
    )
    presenter = StructuredReplyPresenter(
        task_service=DummyTaskService(
            [
                _session(phase=SessionPhase.WAITING_FOR_INPUT),
                _session(
                    phase=SessionPhase.PROCESSING,
                    tool_calls={
                        "create-1": create_1,
                        "create-2": create_2,
                        "update-1-running": update_1_running,
                        "glob-1": glob_tool,
                    },
                ),
                _session(
                    phase=SessionPhase.PROCESSING,
                    tool_calls={
                        "create-1": create_1,
                        "create-2": create_2,
                        "update-1-running": update_1_running,
                        "update-1-completed": update_1_completed,
                        "update-2-running": update_2_running,
                        "glob-1": glob_tool,
                    },
                ),
            ]
        ),
        user_id=1,
    )

    await presenter.prime()
    first = await presenter.poll(task_id="task-1")
    second = await presenter.poll(task_id="task-1")

    assert first == [
        TaskListStatusOutput(
            message_key="task-list",
            items=(
                TaskListItemStatusOutput(
                    task_id="1",
                    subject="梳理项目结构",
                    status="in_progress",
                    active_form="梳理项目结构",
                ),
                TaskListItemStatusOutput(
                    task_id="2",
                    subject="评估当前改动",
                    status="pending",
                    active_form="评估当前改动",
                ),
            ),
        )
    ]
    assert second == [
        TaskListStatusOutput(
            message_key="task-list",
            items=(
                TaskListItemStatusOutput(
                    task_id="1",
                    subject="梳理项目结构",
                    status="completed",
                    active_form="梳理项目结构",
                ),
                TaskListItemStatusOutput(
                    task_id="2",
                    subject="评估当前改动",
                    status="in_progress",
                    active_form="评估当前改动",
                ),
            ),
        )
    ]


@pytest.mark.asyncio
async def test_presenter_keeps_task_update_status_when_update_is_before_create() -> None:
    update_1_completed = ToolCallRecord(
        tool_use_id="update-1-completed",
        name="TaskUpdate",
        input={"taskId": "1", "status": "completed"},
        status=ToolStatus.SUCCESS,
    )
    create_1 = ToolCallRecord(
        tool_use_id="create-1",
        name="TaskCreate",
        input={"subject": "梳理项目结构", "activeForm": "梳理项目结构"},
        status=ToolStatus.SUCCESS,
        structured_result={"task": {"id": "1", "subject": "梳理项目结构"}},
    )
    presenter = StructuredReplyPresenter(
        task_service=DummyTaskService(
            [
                _session(phase=SessionPhase.WAITING_FOR_INPUT),
                _session(
                    phase=SessionPhase.PROCESSING,
                    tool_calls={"update-1-completed": update_1_completed, "create-1": create_1},
                ),
            ]
        ),
        user_id=1,
    )

    await presenter.prime()
    outputs = await presenter.poll(task_id="task-1")

    assert outputs == [
        TaskListStatusOutput(
            message_key="task-list",
            items=(
                TaskListItemStatusOutput(
                    task_id="1",
                    subject="梳理项目结构",
                    status="completed",
                    active_form="梳理项目结构",
                ),
            ),
        )
    ]


@pytest.mark.asyncio
async def test_presenter_updates_preexisting_flat_tool_after_task_list_appears() -> None:
    bash_running = ToolCallRecord(
        tool_use_id="bash-1",
        name="Bash",
        input={"command": "pytest -q"},
        status=ToolStatus.RUNNING,
    )
    bash_success = ToolCallRecord(
        tool_use_id="bash-1",
        name="Bash",
        input={"command": "pytest -q"},
        status=ToolStatus.SUCCESS,
    )
    create_1 = ToolCallRecord(
        tool_use_id="create-1",
        name="TaskCreate",
        input={"subject": "运行测试", "activeForm": "运行测试"},
        status=ToolStatus.SUCCESS,
        structured_result={"task": {"id": "1", "subject": "运行测试"}},
    )
    presenter = StructuredReplyPresenter(
        task_service=DummyTaskService(
            [
                _session(phase=SessionPhase.WAITING_FOR_INPUT),
                _session(phase=SessionPhase.PROCESSING, tool_calls={"bash-1": bash_running}),
                _session(phase=SessionPhase.PROCESSING, tool_calls={"create-1": create_1, "bash-1": bash_success}),
            ]
        ),
        user_id=1,
    )

    await presenter.prime()
    first = await presenter.poll(task_id="task-1")
    second = await presenter.poll(task_id="task-1")

    assert first == [
        ToolStatusOutput(
            tool_use_id="bash-1",
            tool_name="Bash",
            tool_input={"command": "pytest -q"},
            status=ToolStatus.RUNNING.value,
        )
    ]
    assert second == [
        TaskListStatusOutput(
            message_key="task-list",
            items=(
                TaskListItemStatusOutput(
                    task_id="1",
                    subject="运行测试",
                    status="pending",
                    active_form="运行测试",
                ),
            ),
        ),
        ToolStatusOutput(
            tool_use_id="bash-1",
            tool_name="Bash",
            tool_input={"command": "pytest -q"},
            status=ToolStatus.SUCCESS.value,
        ),
    ]


@pytest.mark.asyncio
async def test_presenter_marks_failed_task_update_as_failed_instead_of_completed() -> None:
    create_1 = ToolCallRecord(
        tool_use_id="create-1",
        name="TaskCreate",
        input={"subject": "运行测试", "activeForm": "运行测试"},
        status=ToolStatus.SUCCESS,
        structured_result={"task": {"id": "1", "subject": "运行测试"}},
    )
    failed_update = ToolCallRecord(
        tool_use_id="update-1",
        name="TaskUpdate",
        input={"taskId": "1", "status": "completed"},
        status=ToolStatus.ERROR,
    )
    presenter = StructuredReplyPresenter(
        task_service=DummyTaskService(
            [
                _session(phase=SessionPhase.WAITING_FOR_INPUT),
                _session(phase=SessionPhase.PROCESSING, tool_calls={"create-1": create_1, "update-1": failed_update}),
            ]
        ),
        user_id=1,
    )

    await presenter.prime()
    outputs = await presenter.poll(task_id="task-1")

    assert outputs == [
        TaskListStatusOutput(
            message_key="task-list",
            items=(
                TaskListItemStatusOutput(
                    task_id="1",
                    subject="运行测试",
                    status="failed",
                    active_form="运行测试",
                ),
            ),
        )
    ]


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
        FileToolAggregateStatusOutput(
            message_key="file-tool-aggregate",
            tools=(
                ToolStatusOutput(
                    tool_use_id="tool-2",
                    tool_name="Read",
                    tool_input={"file_path": "/tmp/a.txt"},
                    status=ToolStatus.INTERRUPTED.value,
                ),
            ),
        )
    ]


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

    assert messages == [
        ToolStatusOutput(
            tool_use_id="tool-1",
            tool_name="Bash",
            tool_input={"command": "pytest -q"},
            status=ToolStatus.RUNNING.value,
        )
    ]


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
