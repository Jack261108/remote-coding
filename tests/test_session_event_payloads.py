from __future__ import annotations

import json

from app.adapters.storage.file_session_store import FileSessionStore
from app.domain.hook_models import HookEvent
from app.domain.models import utc_now
from app.domain.session_models import (
    ConversationTurn,
    FileSyncedPayload,
    HookReceivedPayload,
    InterruptDetectedPayload,
    PermissionDecisionPayload,
    PermissionResponseFailedPayload,
    SessionEvent,
    SessionEventType,
    SessionPhase,
    ToolCallRecord,
    ToolStatus,
)
from app.services.session_store import SessionStore


def _hook_event(*, event: str, status: str, tool_use_id: str | None = None) -> HookEvent:
    return HookEvent(
        session_id="s1",
        cwd="/tmp/project",
        event=event,
        status=status,
        tool="Bash" if tool_use_id else None,
        tool_input={"command": "pwd"} if tool_use_id else None,
        tool_use_id=tool_use_id,
    )


def test_session_event_serializes_typed_snapshot_payload_recursively() -> None:
    turn = ConversationTurn(
        turn_id="a1",
        role="assistant",
        text="完成",
        source="jsonl",
        is_complete=True,
        started_at=utc_now(),
        ended_at=utc_now(),
    )
    tool = ToolCallRecord(
        tool_use_id="tool-1",
        name="Bash",
        input={"command": "pwd"},
        status=ToolStatus.SUCCESS,
        result="/tmp/project",
        structured_result={"stdout": "/tmp/project"},
        started_at=utc_now(),
        completed_at=utc_now(),
    )
    event = SessionEvent(
        session_id="s1",
        type=SessionEventType.FILE_SYNCED,
        payload=FileSyncedPayload(
            cwd="/tmp/project",
            claude_session_id="s1",
            turns=[turn],
            tool_calls={"tool-1": tool},
            last_reply="完成",
            last_reply_role="assistant",
            last_offset=18,
        ),
    )

    serialized = event.to_dict()

    json.dumps(serialized)
    payload = serialized["payload"]
    assert payload["turns"][0]["source"] == "jsonl"
    assert payload["tool_calls"]["tool-1"]["status"] == "success"
    assert payload["tool_calls"]["tool-1"]["started_at"]


def test_hook_received_payload_keeps_legacy_top_level_shape() -> None:
    hook = _hook_event(event="SessionEnd", status="ended")

    payload = HookReceivedPayload.from_hook_event(hook).to_dict()

    assert payload["session_id"] == "s1"
    assert payload["cwd"] == "/tmp/project"
    assert payload["event"] == "SessionEnd"
    assert payload["status"] == "ended"
    assert "hook" not in payload


def test_session_store_accepts_typed_hook_payload_for_permission_flow(tmp_path) -> None:
    store = SessionStore(FileSessionStore(str(tmp_path)))
    store.get_or_create(session_id="s1", workdir="/tmp/project")

    state = store.process(
        SessionEvent(
            session_id="s1",
            type=SessionEventType.HOOK_RECEIVED,
            payload=HookReceivedPayload.from_hook_event(_hook_event(event="PreToolUse", status="running_tool", tool_use_id="tool-1")),
        )
    )
    assert state.tool_calls["tool-1"].status == ToolStatus.RUNNING
    assert state.phase == SessionPhase.PROCESSING

    state = store.process(
        SessionEvent(
            session_id="s1",
            type=SessionEventType.HOOK_RECEIVED,
            payload=HookReceivedPayload.from_hook_event(
                _hook_event(event="PermissionRequest", status="waiting_for_approval", tool_use_id="tool-1")
            ),
        )
    )

    assert state.phase == SessionPhase.WAITING_FOR_APPROVAL
    assert state.pending_permission is not None
    assert state.pending_permission.tool_use_id == "tool-1"
    assert state.tool_calls["tool-1"].status == ToolStatus.WAITING_FOR_APPROVAL


def test_file_synced_payload_preserves_missing_vs_empty_turns_semantics(tmp_path) -> None:
    store = SessionStore(FileSessionStore(str(tmp_path)))
    state = store.get_or_create(session_id="s1", workdir="/tmp/project")
    state.turns.append(ConversationTurn(turn_id="a1", role="assistant", text="旧回复", is_complete=True))
    state.tool_calls["tool-1"] = ToolCallRecord(tool_use_id="tool-1", name="Bash", status=ToolStatus.SUCCESS)
    store._persist(state)

    state = store.process(
        SessionEvent(
            session_id="s1",
            type=SessionEventType.FILE_SYNCED,
            payload=FileSyncedPayload(last_offset=18),
        )
    )

    assert [turn.turn_id for turn in state.turns] == ["a1"]
    assert set(state.tool_calls) == {"tool-1"}

    state = store.process(
        SessionEvent(
            session_id="s1",
            type=SessionEventType.FILE_SYNCED,
            payload=FileSyncedPayload(turns=[], tool_calls={}, last_offset=19),
        )
    )

    assert state.turns == []
    assert state.tool_calls == {}


def test_permission_decision_payload_preserves_source_and_updates_state(tmp_path) -> None:
    store = SessionStore(FileSessionStore(str(tmp_path)))
    store.process(
        SessionEvent(
            session_id="s1",
            type=SessionEventType.HOOK_RECEIVED,
            payload=HookReceivedPayload.from_hook_event(
                _hook_event(event="PermissionRequest", status="waiting_for_approval", tool_use_id="tool-1")
            ),
        )
    )
    decision = PermissionDecisionPayload(tool_use_id="tool-1", source="terminal")

    state = store.process(SessionEvent(session_id="s1", type=SessionEventType.PERMISSION_APPROVED, payload=decision))

    assert (
        SessionEvent(session_id="s1", type=SessionEventType.PERMISSION_APPROVED, payload=decision).to_dict()["payload"]["source"]
        == "terminal"
    )
    assert state.phase == SessionPhase.PROCESSING
    assert state.pending_permission is None
    assert state.tool_calls["tool-1"].status == ToolStatus.RUNNING


def test_permission_response_failed_payload_without_tool_use_id_interrupts_active_tools(tmp_path) -> None:
    store = SessionStore(FileSessionStore(str(tmp_path)))
    store.process(
        SessionEvent(
            session_id="s1",
            type=SessionEventType.HOOK_RECEIVED,
            payload=HookReceivedPayload.from_hook_event(_hook_event(event="PreToolUse", status="running_tool", tool_use_id="tool-1")),
        )
    )

    state = store.process(
        SessionEvent(
            session_id="s1",
            type=SessionEventType.PERMISSION_RESPONSE_FAILED,
            payload=PermissionResponseFailedPayload(),
        )
    )

    assert state.interrupted is True
    assert state.phase == SessionPhase.WAITING_FOR_INPUT
    assert state.tool_calls["tool-1"].status == ToolStatus.INTERRUPTED


def test_interrupt_detected_payload_interrupts_active_tools(tmp_path) -> None:
    store = SessionStore(FileSessionStore(str(tmp_path)))
    store.process(
        SessionEvent(
            session_id="s1",
            type=SessionEventType.HOOK_RECEIVED,
            payload=HookReceivedPayload.from_hook_event(_hook_event(event="PreToolUse", status="running_tool", tool_use_id="tool-1")),
        )
    )

    state = store.process(
        SessionEvent(
            session_id="s1",
            type=SessionEventType.INTERRUPT_DETECTED,
            payload=InterruptDetectedPayload(cwd="/tmp/project"),
        )
    )

    assert state.interrupted is True
    assert state.phase == SessionPhase.WAITING_FOR_INPUT
    assert state.tool_calls["tool-1"].status == ToolStatus.INTERRUPTED
