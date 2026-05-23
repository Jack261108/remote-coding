from __future__ import annotations

import asyncio
import json

import pytest

from app.bootstrap import AppContainer
from app.config.settings import Settings
from app.domain.hook_models import HookEvent
from app.domain.models import TaskRecord, TaskStatus
from app.domain.session_models import ConversationTurn, SessionEvent, SessionEventType, SessionPhase, ToolCallRecord, ToolStatus
from app.services.agent_file_watcher import AgentFileWatcher
from app.services.interrupt_watcher import InterruptWatcher


async def wait_for_jsonl_sync_idle(container: AppContainer, session_id: str) -> None:
    while True:
        task = container._jsonl_sync_tasks.get(session_id)
        if task is None:
            return
        await task


def make_settings(tmp_path, *, install_hooks: bool = True) -> Settings:
    return Settings.model_validate(
        {
            "TG_BOT_TOKEN": "123456:TESTTOKEN",
            "TG_ALLOWED_USER_IDS": "1",
            "DEFAULT_PROVIDER": "claude_code",
            "DEFAULT_TIMEOUT_SEC": 10,
            "MAX_CONCURRENT_TASKS": 1,
            "CLAUDE_TMUX_MODE": False,
            "TMUX_DATA_DIR": str(tmp_path),
            "CLAUDE_CLI_BIN": "claude",
            "CLAUDE_INSTALL_HOOKS": install_hooks,
            "CLAUDE_CONFIG_DIR": str(tmp_path / ".claude"),
            "CLAUDE_HOOK_SOCKET_PATH": str(tmp_path / "hook.sock"),
            "CLAUDE_JSONL_SYNC_DEBOUNCE_MS": 10,
            "CLAUDE_PERIODIC_RECHECK_MS": 10,
            "CODEX_CLI_BIN": "codex",
            "GEMINI_CLI_BIN": "gemini",
            "ALLOWED_WORKDIRS": str(tmp_path),
        }
    )


def use_legacy_hook_binding_path(container: AppContainer) -> None:
    delattr(container, "ownership_resolver")


@pytest.mark.asyncio
async def test_app_container_start_installs_hooks_and_starts_server(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    container = AppContainer(make_settings(tmp_path, install_hooks=True))

    seen = {"install": 0, "start": 0, "stop": 0}

    def fake_install():
        seen["install"] += 1

    async def fake_start(handler, permission_failure_handler=None):
        seen["start"] += 1
        assert handler is not None

    async def fake_stop():
        seen["stop"] += 1

    async def fake_close():
        return None

    monkeypatch.setattr(container.hook_installer, "install", fake_install)
    monkeypatch.setattr(container.hook_socket_server, "start", fake_start)
    monkeypatch.setattr(container.hook_socket_server, "stop", fake_stop)
    monkeypatch.setattr(container.bot.session, "close", fake_close)

    await container.start()
    await container.stop()

    assert seen == {"install": 1, "start": 1, "stop": 1}


@pytest.mark.asyncio
async def test_app_container_start_skips_install_when_disabled(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    container = AppContainer(make_settings(tmp_path, install_hooks=False))

    seen = {"install": 0, "start": 0, "stop": 0}

    def fake_install():
        seen["install"] += 1

    async def fake_start(handler, permission_failure_handler=None):
        seen["start"] += 1

    async def fake_stop():
        seen["stop"] += 1

    async def fake_close():
        return None

    monkeypatch.setattr(container.hook_installer, "install", fake_install)
    monkeypatch.setattr(container.hook_socket_server, "start", fake_start)
    monkeypatch.setattr(container.hook_socket_server, "stop", fake_stop)
    monkeypatch.setattr(container.bot.session, "close", fake_close)

    await container.start()
    await container.stop()

    assert seen == {"install": 0, "start": 1, "stop": 1}


@pytest.mark.asyncio
async def test_handle_hook_event_binds_session_and_syncs_jsonl(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    container = AppContainer(make_settings(tmp_path, install_hooks=False))
    use_legacy_hook_binding_path(container)

    session = await container.session_service.switch(
        user_id=1,
        provider="claude_code",
        workdir=str(tmp_path),
        terminal_mode=True,
        claude_chat_active=True,
    )
    await container.task_store.add(
        TaskRecord(
            task_id="task-1",
            session_id=session.session_id,
            user_id=1,
            provider="claude_code",
            prompt="hello",
            workdir=str(tmp_path),
            timeout_sec=10,
            status=TaskStatus.RUNNING,
        )
    )

    async def fake_sync(session_id: str, cwd: str) -> None:
        await container._dispatch_session_event(
            SessionEvent(
                session_id=session_id,
                type=SessionEventType.FILE_SYNCED,
                payload={
                    "cwd": cwd,
                    "claude_session_id": session_id,
                    "turns": [
                        {
                            "turn_id": "a1",
                            "role": "assistant",
                            "text": "\n干净回复\n",
                            "source": "jsonl",
                            "is_complete": True,
                            "started_at": "2026-04-16T10:00:01+00:00",
                            "ended_at": "2026-04-16T10:00:01+00:00",
                        }
                    ],
                    "tool_calls": {},
                    "last_reply": "干净回复",
                    "last_reply_role": "assistant",
                    "last_offset": 12,
                },
            )
        )

    monkeypatch.setattr(container, "sync_claude_session", fake_sync)

    await container._handle_hook_event(
        HookEvent(
            session_id="claude-session-1",
            cwd=str(tmp_path),
            event="SessionStart",
            status="starting",
        )
    )
    await wait_for_jsonl_sync_idle(container, "claude-session-1")

    session = await container.session_service.get(1)
    assert session is not None
    assert session.claude_session_id == "claude-session-1"

    state = container.structured_session_store.get("claude-session-1")
    assert state is not None
    assert state.user_id == 1
    assert state.terminal_id == session.terminal_id
    assert state.phase == SessionPhase.WAITING_FOR_INPUT
    assert state.last_reply == "干净回复"


@pytest.mark.asyncio
async def test_handle_hook_event_binds_session_by_unique_active_claude_chat_workdir(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    container = AppContainer(make_settings(tmp_path, install_hooks=False))
    use_legacy_hook_binding_path(container)

    session = await container.session_service.switch(
        user_id=1,
        provider="claude_code",
        workdir=str(tmp_path),
        terminal_mode=True,
        claude_chat_active=True,
    )

    async def fake_sync(session_id: str, cwd: str) -> None:
        return None

    monkeypatch.setattr(container, "sync_claude_session", fake_sync)

    await container._handle_hook_event(
        HookEvent(
            session_id="claude-session-1",
            cwd=str(tmp_path),
            event="SessionStart",
            status="starting",
        )
    )
    await wait_for_jsonl_sync_idle(container, "claude-session-1")

    updated = await container.session_service.get(1)
    assert updated is not None
    assert updated.claude_session_id == "claude-session-1"
    assert updated.terminal_id == session.terminal_id


@pytest.mark.asyncio
async def test_handle_hook_event_does_not_bind_session_by_unique_workdir_when_claude_chat_inactive(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    container = AppContainer(make_settings(tmp_path, install_hooks=False))

    session = await container.session_service.switch(
        user_id=1,
        provider="claude_code",
        workdir=str(tmp_path),
        terminal_mode=True,
        claude_chat_active=False,
    )

    async def fake_sync(session_id: str, cwd: str) -> None:
        return None

    monkeypatch.setattr(container, "sync_claude_session", fake_sync)

    await container._handle_hook_event(
        HookEvent(
            session_id="claude-session-1",
            cwd=str(tmp_path),
            event="SessionStart",
            status="starting",
        )
    )
    await wait_for_jsonl_sync_idle(container, "claude-session-1")

    updated = await container.session_service.get(1)
    assert updated is not None
    assert updated.claude_session_id is None
    assert updated.terminal_id == session.terminal_id


@pytest.mark.asyncio
async def test_handle_hook_event_does_not_bind_session_for_stale_processing_terminal_state(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    container = AppContainer(make_settings(tmp_path, install_hooks=False))

    session = await container.session_service.switch(
        user_id=1,
        provider="claude_code",
        workdir=str(tmp_path),
        terminal_mode=True,
        claude_chat_active=True,
    )
    terminal_state = container.structured_session_store.get_or_create(
        session_id=f"tgcli_{session.terminal_id}",
        provider="claude_code",
        workdir=str(tmp_path),
        terminal_id=session.terminal_id,
        user_id=1,
    )
    terminal_state.phase = SessionPhase.PROCESSING
    container.structured_session_store._persist(terminal_state)

    async def fake_sync(session_id: str, cwd: str) -> None:
        return None

    monkeypatch.setattr(container, "sync_claude_session", fake_sync)

    await container._handle_hook_event(
        HookEvent(
            session_id="claude-session-1",
            cwd=str(tmp_path),
            event="SessionStart",
            status="starting",
        )
    )
    await wait_for_jsonl_sync_idle(container, "claude-session-1")

    updated = await container.session_service.get(1)
    assert updated is not None
    assert updated.claude_session_id is None
    assert updated.terminal_id == session.terminal_id


@pytest.mark.asyncio
async def test_handle_hook_event_binds_session_when_terminal_state_has_content_without_task(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    container = AppContainer(make_settings(tmp_path, install_hooks=False))
    use_legacy_hook_binding_path(container)

    session = await container.session_service.switch(
        user_id=1,
        provider="claude_code",
        workdir=str(tmp_path),
        terminal_mode=True,
        claude_chat_active=True,
    )
    await container.session_service.bind_claude_session(
        user_id=1,
        claude_session_id="old-session",
        workdir=str(tmp_path),
    )
    previous = container.structured_session_store.get_or_create(
        session_id="old-session",
        provider="claude_code",
        workdir=str(tmp_path),
        terminal_id=session.terminal_id,
        user_id=1,
        claude_session_id="old-session",
    )
    previous.phase = SessionPhase.PROCESSING
    previous.tool_calls["tool-1"] = ToolCallRecord(
        tool_use_id="tool-1",
        name="Bash",
        input={"command": "pwd"},
        status=ToolStatus.RUNNING,
    )
    container.structured_session_store._persist(previous)

    async def fake_sync(session_id: str, cwd: str) -> None:
        return None

    monkeypatch.setattr(container, "sync_claude_session", fake_sync)

    await container._handle_hook_event(
        HookEvent(
            session_id="claude-session-2",
            cwd=str(tmp_path),
            event="SessionStart",
            status="starting",
        )
    )
    await wait_for_jsonl_sync_idle(container, "claude-session-2")

    updated = await container.session_service.get(1)
    assert updated is not None
    assert updated.claude_session_id == "claude-session-2"
    assert updated.terminal_id == session.terminal_id


@pytest.mark.asyncio
async def test_handle_hook_event_binds_session_when_pending_interactive_task_matches_workdir(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    container = AppContainer(make_settings(tmp_path, install_hooks=False))
    use_legacy_hook_binding_path(container)

    session = await container.session_service.switch(
        user_id=1,
        provider="claude_code",
        workdir=str(tmp_path),
        terminal_mode=True,
        claude_chat_active=True,
    )
    await container.task_store.add(
        TaskRecord(
            task_id="task-1",
            session_id=session.session_id,
            user_id=1,
            provider="claude_code",
            prompt="hello",
            workdir=str(tmp_path),
            timeout_sec=10,
            status=TaskStatus.PENDING,
        )
    )

    async def fake_sync(session_id: str, cwd: str) -> None:
        return None

    monkeypatch.setattr(container, "sync_claude_session", fake_sync)

    await container._handle_hook_event(
        HookEvent(
            session_id="claude-session-1",
            cwd=str(tmp_path),
            event="SessionStart",
            status="starting",
        )
    )
    await wait_for_jsonl_sync_idle(container, "claude-session-1")

    updated = await container.session_service.get(1)
    assert updated is not None
    assert updated.claude_session_id == "claude-session-1"
    assert updated.terminal_id == session.terminal_id


@pytest.mark.asyncio
async def test_handle_hook_event_does_not_bind_session_when_only_final_task_matches_workdir(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    container = AppContainer(make_settings(tmp_path, install_hooks=False))

    session = await container.session_service.switch(
        user_id=1,
        provider="claude_code",
        workdir=str(tmp_path),
        terminal_mode=True,
        claude_chat_active=False,
    )
    await container.task_store.add(
        TaskRecord(
            task_id="task-1",
            session_id=session.session_id,
            user_id=1,
            provider="claude_code",
            prompt="hello",
            workdir=str(tmp_path),
            timeout_sec=10,
            status=TaskStatus.SUCCEEDED,
        )
    )

    async def fake_sync(session_id: str, cwd: str) -> None:
        return None

    monkeypatch.setattr(container, "sync_claude_session", fake_sync)

    await container._handle_hook_event(
        HookEvent(
            session_id="claude-session-1",
            cwd=str(tmp_path),
            event="SessionStart",
            status="starting",
        )
    )
    await wait_for_jsonl_sync_idle(container, "claude-session-1")

    updated = await container.session_service.get(1)
    assert updated is not None
    assert updated.claude_session_id is None
    assert updated.terminal_id == session.terminal_id


@pytest.mark.asyncio
async def test_handle_hook_event_runs_bind_before_dispatch_and_sync(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    container = AppContainer(make_settings(tmp_path, install_hooks=False))
    use_legacy_hook_binding_path(container)
    seen: list[str] = []

    async def fake_bind(event: HookEvent) -> None:
        seen.append(f"bind:{event.session_id}")

    async def fake_dispatch(event) -> None:
        seen.append(f"dispatch:{event.session_id}")

    def fake_schedule(session_id: str, cwd: str) -> None:
        seen.append(f"sync:{session_id}:{cwd}")

    monkeypatch.setattr(container, "_bind_hook_session", fake_bind)
    monkeypatch.setattr(container, "_dispatch_session_event", fake_dispatch)
    monkeypatch.setattr(container, "_schedule_jsonl_sync", fake_schedule)

    await container._handle_hook_event(HookEvent(session_id="claude-session-1", cwd=str(tmp_path), event="Notification", status="running"))

    assert seen == [
        "bind:claude-session-1",
        "dispatch:claude-session-1",
        f"sync:claude-session-1:{str(tmp_path)}",
    ]


@pytest.mark.asyncio
async def test_handle_hook_event_rejects_workdir_outside_allowlist(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    container = AppContainer(make_settings(tmp_path, install_hooks=False))
    seen: list[str] = []

    async def fake_bind(event: HookEvent) -> None:
        seen.append(f"bind:{event.session_id}")

    async def fake_dispatch(event) -> None:
        seen.append(f"dispatch:{event.session_id}")

    def fake_schedule(session_id: str, cwd: str) -> None:
        seen.append(f"sync:{session_id}:{cwd}")

    monkeypatch.setattr(container, "_bind_hook_session", fake_bind)
    monkeypatch.setattr(container, "_dispatch_session_event", fake_dispatch)
    monkeypatch.setattr(container, "_schedule_jsonl_sync", fake_schedule)

    await container._handle_hook_event(
        HookEvent(
            session_id="claude-session-outside",
            cwd=str(tmp_path.parent / f"{tmp_path.name}-outside"),
            event="Notification",
            status="running",
        )
    )

    assert seen == []
    assert container.structured_session_store.get("claude-session-outside") is None


@pytest.mark.asyncio
async def test_handle_hook_event_debounces_jsonl_sync(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    container = AppContainer(make_settings(tmp_path, install_hooks=False))
    use_legacy_hook_binding_path(container)
    seen: list[tuple[str, str]] = []

    async def fake_sync(session_id: str, cwd: str) -> None:
        seen.append((session_id, cwd))

    monkeypatch.setattr(container, "sync_claude_session", fake_sync)

    await container._handle_hook_event(HookEvent(session_id="claude-session-1", cwd=str(tmp_path), event="Notification", status="running"))
    await container._handle_hook_event(HookEvent(session_id="claude-session-1", cwd=str(tmp_path), event="Notification", status="running"))
    await wait_for_jsonl_sync_idle(container, "claude-session-1")

    assert seen == [("claude-session-1", str(tmp_path))]


@pytest.mark.asyncio
async def test_sync_claude_session_uses_per_session_lock(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    container = AppContainer(make_settings(tmp_path, install_hooks=False))

    class _Snapshot:
        turns = []
        tool_calls = {}
        last_reply = None
        last_reply_role = None
        last_offset = 0
        clear_detected = False

        def to_payload(self):
            return {"turns": [], "tool_calls": {}, "last_offset": 0}

    seen: list[tuple[str, str]] = []

    def fake_parse_incremental(*, session_id: str, cwd: str):
        seen.append((session_id, cwd))
        return _Snapshot()

    monkeypatch.setattr(container.claude_jsonl_parser, "parse_incremental", fake_parse_incremental)

    held_lock = container._jsonl_sync_locks.lock("claude-session-1")
    await held_lock.__aenter__()

    first = asyncio.create_task(container.sync_claude_session("claude-session-1", str(tmp_path)))
    second = asyncio.create_task(container.sync_claude_session("claude-session-1", str(tmp_path)))
    await asyncio.sleep(0)
    assert seen == []
    assert first.done() is False
    assert second.done() is False

    await held_lock.__aexit__(None, None, None)
    await first
    await second

    assert seen == [
        ("claude-session-1", str(tmp_path)),
        ("claude-session-1", str(tmp_path)),
    ]


@pytest.mark.asyncio
async def test_stop_cancels_pending_jsonl_sync_tasks(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    container = AppContainer(make_settings(tmp_path, install_hooks=False))

    async def fake_start(handler, permission_failure_handler=None):
        return None

    async def fake_stop():
        return None

    async def fake_close():
        return None

    async def fake_sync(session_id: str, cwd: str) -> None:
        await asyncio.sleep(1)

    monkeypatch.setattr(container.hook_socket_server, "start", fake_start)
    monkeypatch.setattr(container.hook_socket_server, "stop", fake_stop)
    monkeypatch.setattr(container.bot.session, "close", fake_close)
    monkeypatch.setattr(container, "sync_claude_session", fake_sync)

    await container.start()
    container._schedule_jsonl_sync("claude-session-1", str(tmp_path))
    task = container._jsonl_sync_tasks.get("claude-session-1")
    assert task is not None

    await container.stop()

    assert container._jsonl_sync_tasks == {}
    assert container._jsonl_sync_requests == {}
    assert len(container._jsonl_sync_locks) == 0
    assert container._periodic_recheck_task is None


@pytest.mark.asyncio
async def test_debounced_sync_keeps_request_added_during_sync(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    container = AppContainer(make_settings(tmp_path, install_hooks=False))
    seen: list[tuple[str, str]] = []
    first_started = asyncio.Event()
    second_started = asyncio.Event()
    release = asyncio.Event()

    async def fake_sync(session_id: str, cwd: str) -> None:
        seen.append((session_id, cwd))
        if len(seen) == 1:
            container._schedule_jsonl_sync(session_id, f"{cwd}-next")
            first_started.set()
            await release.wait()
            return
        second_started.set()

    monkeypatch.setattr(container, "sync_claude_session", fake_sync)

    container._schedule_jsonl_sync("claude-session-1", str(tmp_path))
    await first_started.wait()
    release.set()
    await second_started.wait()
    await wait_for_jsonl_sync_idle(container, "claude-session-1")

    assert seen == [
        ("claude-session-1", str(tmp_path)),
        ("claude-session-1", f"{str(tmp_path)}-next"),
    ]


@pytest.mark.asyncio
async def test_debounced_sync_requeues_request_after_failure(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    container = AppContainer(make_settings(tmp_path, install_hooks=False))
    seen: list[tuple[str, str]] = []
    attempts = 0

    async def fake_sync(session_id: str, cwd: str) -> None:
        nonlocal attempts
        attempts += 1
        seen.append((session_id, cwd))
        if attempts == 1:
            raise RuntimeError("boom")

    monkeypatch.setattr(container, "sync_claude_session", fake_sync)

    container._schedule_jsonl_sync("claude-session-1", str(tmp_path))
    await wait_for_jsonl_sync_idle(container, "claude-session-1")

    assert attempts == 2
    assert seen == [
        ("claude-session-1", str(tmp_path)),
        ("claude-session-1", str(tmp_path)),
    ]
    assert container._jsonl_sync_requests == {}


@pytest.mark.asyncio
async def test_session_end_keeps_pending_sync_until_flushed(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    container = AppContainer(make_settings(tmp_path, install_hooks=False))
    use_legacy_hook_binding_path(container)
    seen: list[tuple[str, str]] = []

    await container.session_service.switch(
        user_id=1,
        provider="claude_code",
        workdir=str(tmp_path),
        terminal_mode=True,
        claude_chat_active=True,
    )

    async def fake_sync(session_id: str, cwd: str) -> None:
        seen.append((session_id, cwd))

    monkeypatch.setattr(container, "sync_claude_session", fake_sync)

    await container._handle_hook_event(HookEvent(session_id="claude-session-1", cwd=str(tmp_path), event="SessionEnd", status="ended"))
    await wait_for_jsonl_sync_idle(container, "claude-session-1")

    assert seen == [("claude-session-1", str(tmp_path))]


@pytest.mark.asyncio
async def test_session_end_cleans_event_lock_registry(tmp_path) -> None:
    container = AppContainer(make_settings(tmp_path, install_hooks=False))

    await container._dispatch_session_event(
        SessionEvent(
            session_id="claude-session-ended",
            type=SessionEventType.SESSION_ENDED,
            payload={"cwd": str(tmp_path)},
        )
    )

    assert len(container._session_event_locks) == 0


@pytest.mark.asyncio
async def test_container_uses_independent_session_lock_registries(tmp_path) -> None:
    settings = make_settings(tmp_path, install_hooks=False)
    container = AppContainer(settings)

    assert container._jsonl_sync_locks._ttl_sec == settings.session_lock_ttl_sec
    assert container._session_event_locks._ttl_sec == settings.session_lock_ttl_sec
    assert container._jsonl_sync_locks._cleanup_interval_sec == settings.lock_cleanup_interval_sec
    assert container._session_event_locks._cleanup_interval_sec == settings.lock_cleanup_interval_sec
    assert container._jsonl_sync_locks is not container._session_event_locks


@pytest.mark.asyncio
async def test_match_session_context_does_not_fallback_on_workdir_collision(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    container = AppContainer(make_settings(tmp_path, install_hooks=False))

    session_one = await container.session_service.switch(
        user_id=1,
        provider="claude_code",
        workdir=str(tmp_path),
        terminal_mode=True,
        claude_chat_active=True,
    )
    session_two = type(session_one).from_dict(
        {
            **session_one.to_dict(),
            "user_id": 2,
            "session_id": "session-2",
            "terminal_id": "user_2",
            "claude_session_id": None,
        }
    )

    async def fake_list_all():
        return [session_one, session_two]

    monkeypatch.setattr(container.session_service, "list_all", fake_list_all)

    matched = await container._match_session_context(
        HookEvent(session_id="claude-session-1", cwd=str(tmp_path), event="Notification", status="running")
    )

    assert matched is None


@pytest.mark.asyncio
async def test_match_session_context_prefers_terminal_binding(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    container = AppContainer(make_settings(tmp_path, install_hooks=False))
    session = await container.session_service.switch(
        user_id=1,
        provider="claude_code",
        workdir=str(tmp_path),
        terminal_mode=True,
        claude_chat_active=True,
    )
    state = container.structured_session_store.get_or_create(
        session_id="claude-session-1",
        provider="claude_code",
        workdir=str(tmp_path),
        terminal_id=session.terminal_id,
        user_id=1,
        claude_session_id="claude-session-1",
    )
    container.structured_session_store._persist(state)

    matched = await container._match_session_context(
        HookEvent(session_id="claude-session-1", cwd="/other", event="Notification", status="running")
    )

    assert matched is not None
    assert matched.user_id == 1


@pytest.mark.asyncio
async def test_restore_session_bindings_clears_empty_terminal_binding_when_snapshot_missing(tmp_path) -> None:
    container = AppContainer(make_settings(tmp_path, install_hooks=False))
    session = await container.session_service.switch(
        user_id=1,
        provider="claude_code",
        workdir=str(tmp_path),
        terminal_mode=True,
        claude_chat_active=True,
    )
    await container.session_service.bind_claude_session(
        user_id=1,
        claude_session_id="missing-session",
        workdir=str(tmp_path),
    )
    terminal_state = container.structured_session_store.get_or_create(
        session_id=f"tgcli_{session.terminal_id}",
        provider="claude_code",
        workdir=str(tmp_path),
        terminal_id=session.terminal_id,
        user_id=1,
    )
    terminal_state.phase = SessionPhase.PROCESSING
    container.structured_session_store._persist(terminal_state)

    await container._restore_session_bindings()

    restored_session = await container.session_service.get(1)
    assert restored_session is not None
    assert restored_session.claude_session_id is None


@pytest.mark.asyncio
async def test_interrupt_watcher_dispatches_interrupt_event(tmp_path) -> None:
    container = AppContainer(make_settings(tmp_path, install_hooks=False))
    state = container.structured_session_store.get_or_create(
        session_id="claude-session-1",
        provider="claude_code",
        workdir=str(tmp_path),
        terminal_id="user_1",
        user_id=1,
        claude_session_id="claude-session-1",
    )
    state.phase = SessionPhase.PROCESSING
    state.tool_calls["tool-1"] = ToolCallRecord(
        tool_use_id="tool-1",
        name="Bash",
        input={"command": "sleep 10"},
    )
    container.structured_session_store._persist(state)

    project_dir = str(tmp_path).replace("/", "-").replace(".", "-")
    session_file = container.claude_paths.projects_dir / project_dir / "claude-session-1.jsonl"
    session_file.parent.mkdir(parents=True, exist_ok=True)
    session_file.write_text(
        json.dumps(
            {
                "type": "assistant",
                "timestamp": "2026-04-16T10:00:01Z",
                "toolUseResult": {"stderr": "Interrupted by user"},
                "message": {
                    "id": "a1",
                    "content": [{"type": "tool_result", "tool_use_id": "tool-1", "content": "Interrupted by user", "is_error": True}],
                },
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    watcher = InterruptWatcher(session_store=container.structured_session_store, claude_jsonl_parser=container.claude_jsonl_parser)
    watcher.watch(session_id="claude-session-1", workdir=str(tmp_path))
    await asyncio.sleep(0.05)
    await watcher.stop_all()
    await asyncio.sleep(0)

    state = container.structured_session_store.get("claude-session-1")
    assert state is not None
    assert state.interrupted is True
    assert state.phase == SessionPhase.WAITING_FOR_INPUT
    assert state.tool_calls["tool-1"].status.value == "interrupted"


@pytest.mark.asyncio
async def test_periodic_recheck_syncs_processing_claude_session(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    container = AppContainer(make_settings(tmp_path, install_hooks=False))
    await container.session_service.switch(
        user_id=1,
        provider="claude_code",
        workdir=str(tmp_path),
        terminal_mode=True,
        claude_chat_active=True,
    )
    await container.session_service.bind_claude_session(
        user_id=1,
        claude_session_id="claude-session-1",
        workdir=str(tmp_path),
    )
    state = container.structured_session_store.get_or_create(
        session_id="claude-session-1",
        provider="claude_code",
        workdir=str(tmp_path),
        terminal_id="user_1",
        user_id=1,
    )
    state.phase = SessionPhase.PROCESSING
    container.structured_session_store._persist(state)

    seen: list[tuple[str, str]] = []

    async def fake_sync(session_id: str, cwd: str) -> None:
        seen.append((session_id, cwd))

    monkeypatch.setattr(container, "sync_claude_session", fake_sync)

    await container._recheck_active_claude_sessions()

    assert seen == [("claude-session-1", str(tmp_path))]


@pytest.mark.asyncio
async def test_start_restores_persisted_claude_session_snapshot(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = make_settings(tmp_path, install_hooks=False)
    first = AppContainer(settings)
    session = await first.session_service.switch(
        user_id=1,
        provider="claude_code",
        workdir=str(tmp_path),
        terminal_mode=True,
        claude_chat_active=True,
    )
    await first.session_service.bind_claude_session(
        user_id=1,
        claude_session_id="claude-session-1",
        workdir=str(tmp_path),
    )
    project_dir = str(tmp_path).replace("/", "-").replace(".", "-")
    session_file = first.claude_paths.projects_dir / project_dir / "claude-session-1.jsonl"
    session_file.parent.mkdir(parents=True, exist_ok=True)
    session_file.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "assistant",
                        "timestamp": "2026-04-16T10:00:01+00:00",
                        "message": {
                            "id": "a1",
                            "content": [{"type": "text", "text": "恢复后的回复"}],
                        },
                    },
                    ensure_ascii=False,
                )
            ]
        ),
        encoding="utf-8",
    )

    second = AppContainer(settings)

    async def fake_start(handler, permission_failure_handler=None):
        return None

    async def fake_stop():
        return None

    async def fake_close():
        return None

    async def fake_sync(session_id: str, cwd: str) -> None:
        await second._dispatch_session_event(
            SessionEvent(
                session_id=session_id,
                type=SessionEventType.FILE_SYNCED,
                payload={
                    "cwd": cwd,
                    "claude_session_id": session_id,
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

    monkeypatch.setattr(second.hook_socket_server, "start", fake_start)
    monkeypatch.setattr(second.hook_socket_server, "stop", fake_stop)
    monkeypatch.setattr(second.bot.session, "close", fake_close)
    monkeypatch.setattr(second, "sync_claude_session", fake_sync)

    try:
        await second.start()

        restored_session = await second.session_service.get(1)
        restored_state = second.structured_session_store.get("claude-session-1")

        assert restored_session is not None
        assert restored_session.session_id == session.session_id
        assert restored_session.claude_session_id == "claude-session-1"
        assert restored_state is not None
        assert restored_state.user_id == 1
        assert restored_state.terminal_id == restored_session.terminal_id
        assert restored_state.last_reply == "恢复后的回复"
    finally:
        await second.stop()


@pytest.mark.asyncio
async def test_start_clears_stale_claude_session_binding_when_snapshot_missing(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = make_settings(tmp_path, install_hooks=False)
    first = AppContainer(settings)
    await first.session_service.switch(
        user_id=1,
        provider="claude_code",
        workdir=str(tmp_path),
        terminal_mode=True,
        claude_chat_active=True,
    )
    await first.session_service.bind_claude_session(
        user_id=1,
        claude_session_id="missing-session",
        workdir=str(tmp_path),
    )

    second = AppContainer(settings)

    async def fake_start(handler, permission_failure_handler=None):
        return None

    async def fake_stop():
        return None

    async def fake_close():
        return None

    monkeypatch.setattr(second.hook_socket_server, "start", fake_start)
    monkeypatch.setattr(second.hook_socket_server, "stop", fake_stop)
    monkeypatch.setattr(second.bot.session, "close", fake_close)

    try:
        await second.start()

        restored_session = await second.session_service.get(1)

        assert restored_session is not None
        assert restored_session.claude_session_id is None
        assert restored_session.claude_chat_active is True
    finally:
        await second.stop()


@pytest.mark.asyncio
async def test_start_clears_stale_claude_session_binding_when_only_empty_terminal_state_exists(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(tmp_path, install_hooks=False)
    first = AppContainer(settings)
    session = await first.session_service.switch(
        user_id=1,
        provider="claude_code",
        workdir=str(tmp_path),
        terminal_mode=True,
        claude_chat_active=True,
    )
    await first.session_service.bind_claude_session(
        user_id=1,
        claude_session_id="missing-session",
        workdir=str(tmp_path),
    )
    empty_terminal_state = first.structured_session_store.get_or_create(
        session_id=f"tgcli_{session.terminal_id}",
        provider="claude_code",
        workdir=str(tmp_path),
        terminal_id=session.terminal_id,
        user_id=1,
    )
    empty_terminal_state.phase = SessionPhase.PROCESSING
    first.structured_session_store._persist(empty_terminal_state)

    second = AppContainer(settings)

    async def fake_start(handler, permission_failure_handler=None):
        return None

    async def fake_stop():
        return None

    async def fake_close():
        return None

    monkeypatch.setattr(second.hook_socket_server, "start", fake_start)
    monkeypatch.setattr(second.hook_socket_server, "stop", fake_stop)
    monkeypatch.setattr(second.bot.session, "close", fake_close)

    try:
        await second.start()

        restored_session = await second.session_service.get(1)

        assert restored_session is not None
        assert restored_session.claude_session_id is None
        assert restored_session.claude_chat_active is True
    finally:
        await second.stop()


@pytest.mark.asyncio
async def test_start_keeps_claude_session_binding_when_terminal_state_has_content(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = make_settings(tmp_path, install_hooks=False)
    first = AppContainer(settings)
    session = await first.session_service.switch(
        user_id=1,
        provider="claude_code",
        workdir=str(tmp_path),
        terminal_mode=True,
        claude_chat_active=True,
    )
    await first.session_service.bind_claude_session(
        user_id=1,
        claude_session_id="missing-session",
        workdir=str(tmp_path),
    )
    terminal_state = first.structured_session_store.get_or_create(
        session_id=f"tgcli_{session.terminal_id}",
        provider="claude_code",
        workdir=str(tmp_path),
        terminal_id=session.terminal_id,
        user_id=1,
    )
    terminal_state.phase = SessionPhase.PROCESSING
    terminal_state.turns = [ConversationTurn(turn_id="a1", role="assistant", text="\n恢复中\n", is_complete=True)]
    first.structured_session_store._persist(terminal_state)

    second = AppContainer(settings)

    async def fake_start(handler, permission_failure_handler=None):
        return None

    async def fake_stop():
        return None

    async def fake_close():
        return None

    monkeypatch.setattr(second.hook_socket_server, "start", fake_start)
    monkeypatch.setattr(second.hook_socket_server, "stop", fake_stop)
    monkeypatch.setattr(second.bot.session, "close", fake_close)

    try:
        await second.start()

        restored_session = await second.session_service.get(1)

        assert restored_session is not None
        assert restored_session.claude_session_id == "missing-session"
        assert restored_session.claude_chat_active is True
    finally:
        await second.stop()


@pytest.mark.asyncio
async def test_agent_file_watcher_syncs_when_subagent_file_changes(tmp_path) -> None:
    container = AppContainer(make_settings(tmp_path, install_hooks=False))
    session_file = container.claude_jsonl_parser.session_file_path(session_id="claude-session-1", cwd=str(tmp_path))
    session_file.parent.mkdir(parents=True, exist_ok=True)
    session_file.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "assistant",
                        "timestamp": "2026-04-16T10:00:00Z",
                        "message": {
                            "id": "a1",
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": "tool-task",
                                    "name": "Task",
                                    "input": {"description": "watch subagent"},
                                }
                            ],
                        },
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "timestamp": "2026-04-16T10:00:01Z",
                        "toolUseResult": {"agentId": "agent-1", "status": "running"},
                        "message": {
                            "id": "a2",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": "tool-task",
                                    "content": "running",
                                    "is_error": False,
                                }
                            ],
                        },
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    await container.sync_claude_session("claude-session-1", str(tmp_path))

    agent_file = container.claude_jsonl_parser.subagent_file_path(
        session_id="claude-session-1",
        agent_id="agent-1",
        cwd=str(tmp_path),
    )
    agent_file.parent.mkdir(parents=True, exist_ok=True)
    agent_file.write_text(
        json.dumps(
            {
                "type": "assistant",
                "timestamp": "2026-04-16T10:00:02Z",
                "message": {
                    "id": "a3",
                    "content": [{"type": "tool_use", "id": "nested-1", "name": "Bash", "input": {"command": "pwd"}}],
                },
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    seen_sync: list[tuple[str, str]] = []

    async def fake_sync(session_id: str, cwd: str) -> None:
        seen_sync.append((session_id, cwd))
        await container.sync_claude_session(session_id, cwd)

    watcher = AgentFileWatcher(
        session_store=container.structured_session_store,
        claude_jsonl_parser=container.claude_jsonl_parser,
        on_update=fake_sync,
        poll_interval_sec=0.01,
    )
    watcher.watch(session_id="claude-session-1", workdir=str(tmp_path))
    await asyncio.sleep(0.05)
    await watcher.stop_all()
    await asyncio.sleep(0)

    assert seen_sync == [("claude-session-1", str(tmp_path))]
    synced = container.structured_session_store.get("claude-session-1")
    assert synced is not None
    assert synced.tool_calls["tool-task"].subagent_tools
    assert synced.tool_calls["tool-task"].subagent_tools[0].tool_use_id == "nested-1"
    assert synced.tool_calls["tool-task"].subagent_tools[0].name == "Bash"


@pytest.mark.asyncio
async def test_start_restores_agent_file_watcher_for_existing_subagent_container(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = make_settings(tmp_path, install_hooks=False)
    first = AppContainer(settings)
    await first.session_service.switch(
        user_id=1,
        provider="claude_code",
        workdir=str(tmp_path),
        terminal_mode=True,
        claude_chat_active=True,
    )
    await first.session_service.bind_claude_session(
        user_id=1,
        claude_session_id="claude-session-1",
        workdir=str(tmp_path),
    )
    state = first.structured_session_store.get_or_create(
        session_id="claude-session-1",
        provider="claude_code",
        workdir=str(tmp_path),
        terminal_id="user_1",
        user_id=1,
        claude_session_id="claude-session-1",
    )
    state.phase = SessionPhase.WAITING_FOR_INPUT
    state.tool_calls["tool-task"] = ToolCallRecord(
        tool_use_id="tool-task",
        name="Task",
        input={"description": "watch subagent"},
        structured_result={"agentId": "agent-1"},
    )
    first.structured_session_store._persist(state)

    second = AppContainer(settings)
    seen_agent_watch: list[tuple[str, str]] = []

    async def fake_start(handler, permission_failure_handler=None):
        return None

    async def fake_stop():
        return None

    async def fake_close():
        return None

    def fake_agent_watch(*, session_id: str, workdir: str) -> None:
        seen_agent_watch.append((session_id, workdir))

    monkeypatch.setattr(second.hook_socket_server, "start", fake_start)
    monkeypatch.setattr(second.hook_socket_server, "stop", fake_stop)
    monkeypatch.setattr(second.bot.session, "close", fake_close)
    monkeypatch.setattr(second.agent_file_watcher, "watch", fake_agent_watch)

    try:
        await second.start()
        assert ("claude-session-1", str(tmp_path)) in seen_agent_watch
    finally:
        await second.stop()
