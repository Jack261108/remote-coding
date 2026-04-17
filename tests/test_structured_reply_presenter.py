from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.bot.presenters.structured_reply_presenter import (
    StructuredReplyPresenter,
    normalize_stream_text,
    preview_stream_text,
    strip_bridge_markers,
)
from app.adapters.storage.file_session_store import FileSessionStore
from app.domain.session_models import ConversationTurn, ParserCheckpoint, PendingPermission, SessionEvent, SessionEventType, SessionPhase
from app.services.session_store import SessionStore


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

    async def get_structured_session_cursor(self, user_id: int) -> int:
        return self._index

    async def get_structured_reply_cursor(self, user_id: int):
        return None, None

    async def acknowledge_structured_reply(self, user_id: int, *, turn_id: str | None = None, permission_key: str | None = None) -> None:
        return None

    async def wait_for_structured_session_update(self, *, user_id: int, since_cursor: int, timeout_sec: float) -> bool:
        return True


class PersistentTaskService:
    def __init__(self, store: SessionStore) -> None:
        self._store = store

    async def get_structured_session(self, user_id: int, *, log_missing: bool = True):
        return self._store.get("claude-session-1")

    async def get_structured_session_cursor(self, user_id: int) -> int:
        return self._store.get_cursor("claude-session-1")

    async def get_structured_reply_cursor(self, user_id: int):
        return self._store.get_structured_reply_cursor("claude-session-1")

    async def acknowledge_structured_reply(self, user_id: int, *, turn_id: str | None = None, permission_key: str | None = None) -> None:
        if turn_id is not None:
            self._store.mark_structured_reply_emitted("claude-session-1", turn_id=turn_id)
        if permission_key is not None:
            self._store.mark_structured_permission_emitted("claude-session-1", permission_key=permission_key)

    async def wait_for_structured_session_update(self, *, user_id: int, since_cursor: int, timeout_sec: float) -> bool:
        return await self._store.wait_for_publish("claude-session-1", since_cursor=since_cursor, timeout_sec=timeout_sec)


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
