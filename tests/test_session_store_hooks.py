from __future__ import annotations

from app.adapters.storage.file_session_store import FileSessionStore
from app.domain.session_models import ConversationTurn, SessionEvent, SessionEventType, SessionPhase, ToolStatus
from app.services.session_store import SessionStore


def test_session_store_tracks_permission_from_hook_events(tmp_path) -> None:
    store = SessionStore(FileSessionStore(str(tmp_path)))
    store.get_or_create(session_id="s1", workdir="/tmp/project")

    state = store.process(
        SessionEvent(
            session_id="s1",
            type=SessionEventType.HOOK_RECEIVED,
            payload={
                "session_id": "s1",
                "cwd": "/tmp/project",
                "event": "PreToolUse",
                "status": "running_tool",
                "tool": "Bash",
                "tool_input": {"command": "pwd"},
                "tool_use_id": "tool-1",
            },
        )
    )
    assert state.tool_calls["tool-1"].status == ToolStatus.RUNNING
    assert state.phase == SessionPhase.PROCESSING

    state = store.process(
        SessionEvent(
            session_id="s1",
            type=SessionEventType.HOOK_RECEIVED,
            payload={
                "session_id": "s1",
                "cwd": "/tmp/project",
                "event": "PermissionRequest",
                "status": "waiting_for_approval",
                "tool": "Bash",
                "tool_input": {"command": "pwd"},
                "tool_use_id": "tool-1",
            },
        )
    )
    assert state.phase == SessionPhase.WAITING_FOR_APPROVAL
    assert state.pending_permission is not None
    assert state.pending_permission.tool_use_id == "tool-1"
    assert state.tool_calls["tool-1"].status == ToolStatus.WAITING_FOR_APPROVAL

    state = store.process(
        SessionEvent(
            session_id="s1",
            type=SessionEventType.PERMISSION_APPROVED,
            payload={"tool_use_id": "tool-1"},
        )
    )
    assert state.phase == SessionPhase.PROCESSING
    assert state.pending_permission is None
    assert state.tool_calls["tool-1"].status == ToolStatus.RUNNING


def test_session_store_applies_jsonl_snapshot(tmp_path) -> None:
    store = SessionStore(FileSessionStore(str(tmp_path)))
    store.get_or_create(session_id="s1", workdir="/tmp/project")

    state = store.process(
        SessionEvent(
            session_id="s1",
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
                        "text": "\n这里是干净回复\n",
                        "source": "jsonl",
                        "is_complete": True,
                        "started_at": "2026-04-16T10:00:01+00:00",
                        "ended_at": "2026-04-16T10:00:01+00:00",
                    },
                ],
                "tool_calls": {
                    "tool-1": {
                        "tool_use_id": "tool-1",
                        "name": "Bash",
                        "input": {"command": "pwd"},
                        "status": "success",
                        "result": "/tmp/project",
                        "structured_result": {"stdout": "/tmp/project"},
                        "started_at": "2026-04-16T10:00:01+00:00",
                        "completed_at": "2026-04-16T10:00:02+00:00",
                    }
                },
                "summary": "查看当前目录",
                "last_reply": "这里是干净回复",
                "last_reply_role": "assistant",
                "last_offset": 123,
            },
        )
    )

    assert state.phase == SessionPhase.WAITING_FOR_INPUT
    assert state.summary == "查看当前目录"
    assert state.last_reply == "这里是干净回复"
    assert state.turns[-1].text == "\n这里是干净回复\n"
    assert state.tool_calls["tool-1"].status == ToolStatus.SUCCESS
    assert state.tool_calls["tool-1"].structured_result == {"stdout": "/tmp/project"}
    assert state.checkpoint.last_offset == 123
    assert state.checkpoint.completed_tool_ids == ["tool-1"]


def test_session_store_file_synced_replaces_old_state_and_interrupts_on_end(tmp_path) -> None:
    store = SessionStore(FileSessionStore(str(tmp_path)))
    store.get_or_create(session_id="s1", workdir="/tmp/project")
    store.process(
        SessionEvent(
            session_id="s1",
            type=SessionEventType.HOOK_RECEIVED,
            payload={
                "session_id": "s1",
                "cwd": "/tmp/project",
                "event": "PreToolUse",
                "status": "running_tool",
                "tool": "Bash",
                "tool_input": {"command": "pwd"},
                "tool_use_id": "tool-1",
            },
        )
    )

    state = store.process(
        SessionEvent(
            session_id="s1",
            type=SessionEventType.FILE_SYNCED,
            payload={
                "turns": [
                    {
                        "turn_id": "a2",
                        "role": "assistant",
                        "text": "\n新快照\n",
                        "source": "jsonl",
                        "is_complete": True,
                        "started_at": "2026-04-16T10:00:03+00:00",
                        "ended_at": "2026-04-16T10:00:03+00:00",
                    }
                ],
                "tool_calls": {},
                "last_reply": "新快照",
                "last_reply_role": "assistant",
                "last_offset": 222,
            },
        )
    )

    assert [turn.turn_id for turn in state.turns] == ["a2"]
    assert state.tool_calls == {}
    assert state.phase == SessionPhase.WAITING_FOR_INPUT

    state = store.process(
        SessionEvent(
            session_id="s1",
            type=SessionEventType.HOOK_RECEIVED,
            payload={
                "session_id": "s1",
                "cwd": "/tmp/project",
                "event": "PreToolUse",
                "status": "running_tool",
                "tool": "Bash",
                "tool_input": {"command": "pwd"},
                "tool_use_id": "tool-2",
            },
        )
    )
    assert state.tool_calls["tool-2"].status == ToolStatus.RUNNING

    state = store.process(
        SessionEvent(
            session_id="s1",
            type=SessionEventType.HOOK_RECEIVED,
            payload={
                "session_id": "s1",
                "cwd": "/tmp/project",
                "event": "SessionEnd",
                "status": "ended",
            },
        )
    )

    assert state.phase == SessionPhase.ENDED
    assert state.tool_calls["tool-2"].status == ToolStatus.INTERRUPTED


def test_session_store_file_synced_promotes_complete_turn_without_waiting_hook(tmp_path) -> None:
    store = SessionStore(FileSessionStore(str(tmp_path)))
    state = store.get_or_create(session_id="claude-session-1", workdir="/tmp/project", terminal_id="user_1_36d00faeb25f")
    state.phase = SessionPhase.PROCESSING
    store._persist(state)

    state = store.process(
        SessionEvent(
            session_id="claude-session-1",
            type=SessionEventType.FILE_SYNCED,
            payload={
                "turns": [
                    {
                        "turn_id": "a1",
                        "role": "assistant",
                        "text": "\n补同步后的回复\n",
                        "source": "jsonl",
                        "is_complete": True,
                        "started_at": "2026-04-16T10:00:03+00:00",
                        "ended_at": "2026-04-16T10:00:03+00:00",
                    }
                ],
                "tool_calls": {},
                "last_reply": "补同步后的回复",
                "last_reply_role": "assistant",
                "last_offset": 18,
            },
        )
    )

    assert state.phase == SessionPhase.WAITING_FOR_INPUT
    assert state.last_reply == "补同步后的回复"


def test_session_store_file_synced_ignores_older_snapshot(tmp_path) -> None:
    store = SessionStore(FileSessionStore(str(tmp_path)))
    state = store.get_or_create(session_id="claude-session-1", workdir="/tmp/project")
    state.phase = SessionPhase.WAITING_FOR_INPUT
    state.checkpoint.last_offset = 30
    state.last_reply = "最新回复"
    state.turns.append(ConversationTurn(turn_id="a-current", role="assistant", text="\n最新回复\n", is_complete=True))
    store._persist(state)

    state = store.process(
        SessionEvent(
            session_id="claude-session-1",
            type=SessionEventType.FILE_SYNCED,
            payload={
                "turns": [],
                "tool_calls": {},
                "last_reply": "旧回复",
                "last_reply_role": "assistant",
                "last_offset": 18,
            },
        )
    )

    assert state.phase == SessionPhase.WAITING_FOR_INPUT
    assert state.last_reply == "最新回复"
    assert state.checkpoint.last_offset == 30
