from app.adapters.storage.file_session_store import FileSessionStore
from app.domain.session_models import ParserCheckpoint, SessionEvent, SessionEventType, SessionPhase
from app.services.session_store import SessionStore


def test_session_store_persists_checkpoint_and_turns(tmp_path) -> None:
    store = SessionStore(FileSessionStore(str(tmp_path)))
    state = store.get_or_create(session_id="s1", user_id=1, workdir="/tmp", terminal_id="user_1")

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
    store.get_or_create(session_id="tgcli_user_1", workdir="/tmp", terminal_id="user_1")
    bound = store.get_or_create(session_id="claude-session-1", workdir="/tmp", terminal_id="user_1")
    bound.phase = SessionPhase.WAITING_FOR_INPUT
    store._persist(bound)

    phase = store.interactive_completion_phase(
        terminal_id="user_1",
        workdir="/tmp",
        fallback_session_id="tgcli_user_1",
    )

    assert phase == SessionPhase.WAITING_FOR_INPUT


def test_session_store_interactive_completion_waits_for_bound_claude_session(tmp_path) -> None:
    store = SessionStore(FileSessionStore(str(tmp_path)))
    fallback = store.get_or_create(session_id="tgcli_user_1", workdir="/tmp", terminal_id="user_1")
    fallback.phase = SessionPhase.WAITING_FOR_INPUT
    store._persist(fallback)

    phase = store.interactive_completion_phase(
        terminal_id="user_1",
        workdir="/tmp",
        fallback_session_id="tgcli_user_1",
    )

    assert phase is None


def test_session_store_returns_latest_completed_assistant_turn_id(tmp_path) -> None:
    store = SessionStore(FileSessionStore(str(tmp_path)))
    state = store.get_or_create(session_id="claude-session-1", workdir="/tmp", terminal_id="user_1")
    store.get_or_create(session_id="tgcli_user_1", workdir="/tmp", terminal_id="user_1")
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
        terminal_id="user_1",
        workdir="/tmp",
        claude_session_id="claude-session-1",
        fallback_session_id="tgcli_user_1",
    )

    assert turn_id == "a1"
