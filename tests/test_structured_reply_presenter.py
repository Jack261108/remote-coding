from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.bot.presenters.structured_reply_presenter import (
    StructuredReplyPresenter,
    normalize_stream_text,
    preview_stream_text,
    strip_bridge_markers,
)
from app.domain.session_models import ConversationTurn, PendingPermission, SessionPhase


class DummyTaskService:
    def __init__(self, sessions: list[object | None]) -> None:
        self._sessions = sessions
        self._index = 0

    async def get_structured_session(self, user_id: int, *, log_missing: bool = True):
        if self._index >= len(self._sessions):
            return self._sessions[-1]
        session = self._sessions[self._index]
        self._index += 1
        return session


def _session(*, phase: SessionPhase, turns: list[ConversationTurn] | None = None, pending: PendingPermission | None = None):
    return SimpleNamespace(
        phase=phase,
        turns=turns or [],
        pending_permission=pending,
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

    assert first == ["检测到权限请求，请发送 /approve 或 /deny [reason]。"]
    assert second == []


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
