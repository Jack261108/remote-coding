import json

import pytest

from app.adapters.storage.file_session_store import FileSessionStore
from app.domain.session_models import ParserCheckpoint, SessionEvent, SessionEventType, SessionPhase
from app.services.session_store import SessionStore


def test_session_store_persists_checkpoint_and_turns(tmp_path) -> None:
    store = SessionStore(FileSessionStore(str(tmp_path)))
    state = store.get_or_create(session_id="s1", user_id=1, workdir="/tmp", terminal_id="user_1_8c393341f536")

    checkpoint = ParserCheckpoint(in_reply_block=True, pending_buffer="abc", current_turn_id="turn-1")
    store.save_checkpoint("s1", checkpoint)
    store.process(SessionEvent(session_id="s1", type=SessionEventType.TURN_STARTED, payload={"turn_id": "turn-1", "role": "assistant"}))
    store.process(SessionEvent(session_id="s1", type=SessionEventType.PARSER_UPDATED, payload={"turn_id": "turn-1", "text": "\n你好\n", "is_complete": False}))

    reloaded = SessionStore(FileSessionStore(str(tmp_path))).get("s1")

    assert state.session_id == "s1"
    assert reloaded is not None
    assert reloaded.checkpoint.pending_buffer == "abc"
    assert reloaded.current_turn_id == "turn-1"
    assert reloaded.turns[-1].text == "\n你好\n"
    assert reloaded.phase == SessionPhase.PROCESSING


def test_session_store_turn_completed_moves_to_waiting_for_input(tmp_path) -> None:
    store = SessionStore(FileSessionStore(str(tmp_path)))
    store.get_or_create(session_id="s1")
    store.process(SessionEvent(session_id="s1", type=SessionEventType.TURN_STARTED, payload={"turn_id": "turn-1", "role": "assistant"}))

    state = store.process(SessionEvent(session_id="s1", type=SessionEventType.TURN_COMPLETED, payload={"turn_id": "turn-1"}))

    assert state.phase == SessionPhase.WAITING_FOR_INPUT
    assert state.current_turn_id is None
    assert state.turns[-1].is_complete is True


def test_session_store_interactive_completion_prefers_bound_claude_session(tmp_path) -> None:
    store = SessionStore(FileSessionStore(str(tmp_path)))
    store.get_or_create(session_id="tgcli_user_1_8c393341f536", workdir="/tmp", terminal_id="user_1_8c393341f536")
    bound = store.get_or_create(session_id="claude-session-1", workdir="/tmp", terminal_id="user_1_8c393341f536")
    bound.phase = SessionPhase.WAITING_FOR_INPUT
    store._persist(bound)

    phase = store.interactive_completion_phase(
        terminal_id="user_1_8c393341f536",
        workdir="/tmp",
        fallback_session_id="tgcli_user_1_8c393341f536",
    )

    assert phase == SessionPhase.WAITING_FOR_INPUT


def test_session_store_interactive_completion_prefers_explicit_claude_session_id(tmp_path) -> None:
    store = SessionStore(FileSessionStore(str(tmp_path)))
    stale = store.get_or_create(session_id="claude-session-stale", workdir="/tmp", terminal_id="user_1_8c393341f536")
    stale.phase = SessionPhase.PROCESSING
    target = store.get_or_create(session_id="claude-session-1", workdir="/tmp", terminal_id=None)
    target.phase = SessionPhase.WAITING_FOR_INPUT
    store._persist(stale)
    store._persist(target)

    phase = store.interactive_completion_phase(
        terminal_id="user_1_8c393341f536",
        workdir="/tmp",
        claude_session_id="claude-session-1",
        fallback_session_id="tgcli_user_1_8c393341f536",
    )

    assert phase == SessionPhase.WAITING_FOR_INPUT


def test_session_store_interactive_completion_waits_for_bound_claude_session(tmp_path) -> None:
    store = SessionStore(FileSessionStore(str(tmp_path)))
    fallback = store.get_or_create(session_id="tgcli_user_1_8c393341f536", workdir="/tmp", terminal_id="user_1_8c393341f536")
    fallback.phase = SessionPhase.WAITING_FOR_INPUT
    store._persist(fallback)

    phase = store.interactive_completion_phase(
        terminal_id="user_1_8c393341f536",
        workdir="/tmp",
        fallback_session_id="tgcli_user_1_8c393341f536",
    )

    assert phase is None


def test_session_store_returns_latest_completed_assistant_turn_id(tmp_path) -> None:
    store = SessionStore(FileSessionStore(str(tmp_path)))
    state = store.get_or_create(session_id="claude-session-1", workdir="/tmp", terminal_id="user_1_8c393341f536")
    store.get_or_create(session_id="tgcli_user_1_8c393341f536", workdir="/tmp", terminal_id="user_1_8c393341f536")
    store.process(
        SessionEvent(
            session_id=state.session_id,
            type=SessionEventType.FILE_SYNCED,
            payload={
                "turns": [
                    {
                        "turn_id": "u1",
                        "role": "user",
                        "text": "\n你好\n",
                        "source": "jsonl",
                        "is_complete": True,
                        "started_at": "2026-04-16T10:00:00+00:00",
                        "ended_at": "2026-04-16T10:00:00+00:00",
                    },
                    {
                        "turn_id": "a1",
                        "role": "assistant",
                        "text": "\n第一条\n",
                        "source": "jsonl",
                        "is_complete": True,
                        "started_at": "2026-04-16T10:00:01+00:00",
                        "ended_at": "2026-04-16T10:00:01+00:00",
                    },
                ],
                "tool_calls": {},
                "last_reply": "第一条",
                "last_reply_role": "assistant",
                "last_offset": 12,
            },
        )
    )

    turn_id = store.latest_completed_assistant_turn_id(
        terminal_id="user_1_8c393341f536",
        workdir="/tmp",
        claude_session_id="claude-session-1",
        fallback_session_id="tgcli_user_1_8c393341f536",
    )

    assert turn_id == "a1"


def test_session_store_file_synced_records_claude_identity_and_history(tmp_path) -> None:
    store = SessionStore(FileSessionStore(str(tmp_path)))

    state = store.process(
        SessionEvent(
            session_id="claude-session-1",
            type=SessionEventType.HISTORY_LOADED,
            payload={
                "cwd": "/tmp/project",
                "claude_session_id": "claude-session-1",
                "turns": [
                    {
                        "turn_id": "a1",
                        "role": "assistant",
                        "text": "\n恢复后的回复\n",
                        "source": "jsonl",
                        "is_complete": True,
                        "started_at": "2026-04-16T10:00:01+00:00",
                        "ended_at": "2026-04-16T10:00:01+00:00",
                    }
                ],
                "tool_calls": {},
                "last_reply": "恢复后的回复",
                "last_reply_role": "assistant",
                "last_offset": 12,
            },
        )
    )

    assert state.claude_session_id == "claude-session-1"
    assert state.workdir == "/tmp/project"
    assert state.history_loaded is True
    assert state.phase == SessionPhase.WAITING_FOR_INPUT


def test_session_store_clear_detected_resets_runtime_state(tmp_path) -> None:
    store = SessionStore(FileSessionStore(str(tmp_path)))
    store.process(SessionEvent(session_id="s1", type=SessionEventType.TURN_STARTED, payload={"turn_id": "turn-1", "role": "assistant"}))
    store.process(SessionEvent(session_id="s1", type=SessionEventType.PARSER_UPDATED, payload={"turn_id": "turn-1", "text": "\n你好\n", "is_complete": True}))

    state = store.process(SessionEvent(session_id="s1", type=SessionEventType.CLEAR_DETECTED))

    assert state.turns == []
    assert state.tool_calls == {}
    assert state.pending_permission is None
    assert state.clear_detected is True
    assert state.phase == SessionPhase.WAITING_FOR_INPUT


def test_session_store_interrupt_detected_marks_running_tool(tmp_path) -> None:
    store = SessionStore(FileSessionStore(str(tmp_path)))
    store.process(
        SessionEvent(
            session_id="claude-session-1",
            type=SessionEventType.HOOK_RECEIVED,
            payload={
                "session_id": "claude-session-1",
                "cwd": "/tmp",
                "event": "PreToolUse",
                "status": "processing",
                "tool": "Bash",
                "tool_input": {"command": "pwd"},
                "tool_use_id": "tool-1",
            },
        )
    )

    state = store.process(SessionEvent(session_id="claude-session-1", type=SessionEventType.INTERRUPT_DETECTED))

    assert state.interrupted is True
    assert state.pending_permission is None
    assert state.tool_calls["tool-1"].status.value == "interrupted"
    assert state.phase == SessionPhase.WAITING_FOR_INPUT


def test_file_session_store_writes_checkpoint_atomically(tmp_path) -> None:
    storage = FileSessionStore(str(tmp_path))
    checkpoint = ParserCheckpoint(last_offset=7, pending_buffer="abc")

    storage.save_checkpoint("s1", checkpoint)

    path = storage.cursor_path("s1")
    assert json.loads(path.read_text(encoding="utf-8"))["last_offset"] == 7
    assert not list(path.parent.glob("tmp*"))


def test_file_session_store_writes_state_and_conversation_atomically(tmp_path) -> None:
    store = SessionStore(FileSessionStore(str(tmp_path)))
    state = store.get_or_create(session_id="s1", user_id=1, workdir="/tmp", terminal_id="user_1_8c393341f536")
    store.process(SessionEvent(session_id="s1", type=SessionEventType.TURN_STARTED, payload={"turn_id": "turn-1", "role": "assistant"}))
    store.process(SessionEvent(session_id="s1", type=SessionEventType.PARSER_UPDATED, payload={"turn_id": "turn-1", "text": "\n你好\n", "is_complete": True}))

    storage = FileSessionStore(str(tmp_path))
    state_path = storage.state_path("s1")
    conversation_path = storage.conversation_path("s1")

    assert json.loads(state_path.read_text(encoding="utf-8"))["session_id"] == "s1"
    assert json.loads(conversation_path.read_text(encoding="utf-8"))[0]["text"] == "\n你好\n"
    assert not list(state_path.parent.glob("tmp*"))


@pytest.mark.asyncio
async def test_session_service_switch_rebuilds_terminal_id_when_workdir_changes(tmp_path) -> None:
    from app.services.session_service import SessionService

    old_workdir = str(tmp_path / "one")
    new_workdir = str(tmp_path / "two")

    class _Store:
        def __init__(self):
            self.session = None

        async def get(self, user_id: int):
            return self.session

        async def save(self, session):
            self.session = session

    store = _Store()
    service = SessionService(store)

    first = await service.switch(
        user_id=1,
        provider="claude_code",
        workdir=old_workdir,
        terminal_mode=True,
        claude_chat_active=True,
    )
    first_terminal_id = first.terminal_id
    second = await service.switch(user_id=1, workdir=new_workdir)

    assert second.workdir == new_workdir
    assert first_terminal_id != second.terminal_id
