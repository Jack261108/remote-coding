import asyncio
from pathlib import Path

import pytest

from app.adapters.storage.file_session_store import FileSessionStore
from app.adapters.storage.memory import MemoryTaskStore
from app.domain.session_models import SessionPhase, ToolCallRecord, ToolStatus
from app.services.session_store import SessionStore
from app.services.task_service import TaskService
from tests.fakes.cli import StubAdapter, StubFactory, expected_terminal_id, make_file_backed_session_service, make_settings


class ProtocolOnlyUserQuestionTransport:
    def __init__(self) -> None:
        self.option_actions: list[tuple[str, str, int, bool]] = []
        self.text_actions: list[tuple[str, str, int, str, bool]] = []
        self.multi_select_advances: list[tuple[str, str, bool]] = []

    async def select_option(
        self,
        *,
        terminal_key: str,
        workdir: str,
        option_index: int,
        submit_after: bool = False,
    ) -> tuple[bool, str]:
        self.option_actions.append((terminal_key, workdir, option_index, submit_after))
        return True, ""

    async def answer_with_text(
        self,
        *,
        terminal_key: str,
        workdir: str,
        option_count: int,
        text: str,
        submit_after: bool = False,
    ) -> tuple[bool, str]:
        self.text_actions.append((terminal_key, workdir, option_count, text, submit_after))
        return True, ""

    async def advance_after_multi_select(
        self,
        *,
        terminal_key: str,
        workdir: str,
        final_question: bool,
    ) -> tuple[bool, str]:
        self.multi_select_advances.append((terminal_key, workdir, final_question))
        return True, ""


async def _build_service_with_active_question(
    tmp_path: Path,
    *,
    questions: list[dict[str, object]],
    transport: ProtocolOnlyUserQuestionTransport,
) -> tuple[TaskService, StubFactory, SessionStore]:
    factory = StubFactory(StubAdapter(events=[]))
    session_service = make_file_backed_session_service(tmp_path)
    structured_store = SessionStore(FileSessionStore(str(tmp_path)))
    service = TaskService(
        settings=make_settings(tmp_path, claude_tmux_mode=True),
        task_store=MemoryTaskStore(),
        session_service=session_service,
        cli_factory=factory,
        semaphore=asyncio.Semaphore(2),
        structured_session_store=structured_store,
        user_question_transport=transport,
    )

    await session_service.switch(
        user_id=1,
        provider="claude_code",
        workdir=str(tmp_path),
        terminal_mode=True,
        claude_chat_active=True,
    )
    await session_service.bind_claude_session(user_id=1, claude_session_id="claude-session-1", workdir=str(tmp_path))

    state = structured_store.get_or_create(
        session_id="claude-session-1",
        workdir=str(tmp_path),
        terminal_id=expected_terminal_id(user_id=1, workdir=str(tmp_path)),
        claude_session_id="claude-session-1",
    )
    state.phase = SessionPhase.PROCESSING
    state.tool_calls["tool-ask"] = ToolCallRecord(
        tool_use_id="tool-ask",
        name="AskUserQuestion",
        input={"questions": questions},
        status=ToolStatus.RUNNING,
    )
    structured_store.save(state)
    return service, factory, structured_store


@pytest.mark.asyncio
async def test_user_question_service_uses_explicit_transport_for_single_select(tmp_path: Path) -> None:
    transport = ProtocolOnlyUserQuestionTransport()
    service, factory, _ = await _build_service_with_active_question(
        tmp_path,
        questions=[{"question": "选择", "options": [{"label": "A"}, {"label": "B"}], "multiSelect": False}],
        transport=transport,
    )

    ok, text, next_prompt = await service.answer_pending_user_question_option(
        user_id=1,
        tool_use_id="tool-ask",
        question_index=0,
        option_index=1,
    )

    assert ok is True
    assert text == "已提交你的选择，Claude 继续执行中"
    assert next_prompt is None
    assert transport.option_actions == [(expected_terminal_id(user_id=1, workdir=str(tmp_path)), str(tmp_path), 1, True)]
    assert factory._user_question_option_actions == []
    assert factory._interactive_inputs == []


@pytest.mark.asyncio
async def test_user_question_service_uses_explicit_transport_for_multi_select(tmp_path: Path) -> None:
    transport = ProtocolOnlyUserQuestionTransport()
    service, factory, _ = await _build_service_with_active_question(
        tmp_path,
        questions=[{"question": "多选", "options": [{"label": "A"}, {"label": "B"}], "multiSelect": True}],
        transport=transport,
    )

    ok, text, prompt, selected = await service.toggle_pending_user_question_multi_select_option(
        user_id=1,
        tool_use_id="tool-ask",
        question_index=0,
        option_index=0,
    )
    assert ok is True
    assert text == "已选择: A"
    assert prompt is not None
    assert selected == frozenset({0})

    ok, text, next_prompt = await service.submit_pending_user_question_multi_select(
        user_id=1,
        tool_use_id="tool-ask",
        question_index=0,
    )

    assert ok is True
    assert text == "已提交你的选择，Claude 继续执行中"
    assert next_prompt is None
    assert transport.option_actions == [(expected_terminal_id(user_id=1, workdir=str(tmp_path)), str(tmp_path), 0, False)]
    assert transport.multi_select_advances == [(expected_terminal_id(user_id=1, workdir=str(tmp_path)), str(tmp_path), True)]
    assert factory._user_question_option_actions == []
    assert factory._user_question_multi_select_advances == []
