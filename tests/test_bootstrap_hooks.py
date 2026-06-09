from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from app.bootstrap import AppContainer
from app.config.settings import Settings
from app.domain.hook_models import HookEvent
from app.domain.models import TaskRecord, TaskStatus
from app.domain.session_models import ConversationTurn, SessionEvent, SessionEventType, SessionPhase, ToolCallRecord, ToolStatus
from app.domain.user_question_models import UserQuestionPrompt
from app.services.agent_file_watcher import AgentFileWatcher
from app.services.auto_approve_service import ActivationSlot
from app.services.external_user_question_state import PendingExternalUserQuestion
from app.services.interrupt_watcher import InterruptWatcher
from app.services.unbound_permission_handler import UnboundPermissionHandler


async def wait_for_jsonl_sync_idle(container: AppContainer, session_id: str) -> None:
    """Wait for the debounced JSONL sync to complete via the session supervisor."""
    debounce = container.settings.claude_jsonl_sync_debounce_ms / 1000
    # Supervisor poll interval (0.2s) + debounce + margin
    await asyncio.sleep(0.2 + debounce + 0.1)


def make_settings(tmp_path, *, install_hooks: bool = True, tmux_mode: bool = False) -> Settings:
    return Settings.model_validate(
        {
            "TG_BOT_TOKEN": "123456:TESTTOKEN",
            "TG_ALLOWED_USER_IDS": "1",
            "DEFAULT_PROVIDER": "claude_code",
            "DEFAULT_TIMEOUT_SEC": 10,
            "MAX_CONCURRENT_TASKS": 1,
            "CLAUDE_TMUX_MODE": tmux_mode,
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
async def test_wire_passes_dead_unbound_cleanup_to_list_router(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    container = AppContainer(make_settings(tmp_path, install_hooks=False))
    captured: dict[str, object] = {}

    def fake_create_router(**kwargs):
        captured.update(kwargs)
        return Router()

    from aiogram import Router

    monkeypatch.setattr("app.bootstrap.create_router", fake_create_router)

    try:
        container.wire()
    finally:
        await container.bot.session.close()

    assert captured["dead_unbound_cleanup"] == container._cleanup_dead_unbound_external_session


@pytest.mark.asyncio
async def test_app_container_start_installs_hooks_and_starts_server(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    container = AppContainer(make_settings(tmp_path, install_hooks=True))

    seen = {"install": 0, "start": 0, "stop": 0}

    def fake_install():
        seen["install"] += 1

    async def fake_start(handler, permission_failure_handler=None, permission_resolved_handler=None):
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

    async def fake_start(handler, permission_failure_handler=None, permission_resolved_handler=None):
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
async def test_prune_unbound_external_sessions_reuses_discovery_pruners(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    container = AppContainer(make_settings(tmp_path, install_hooks=False))
    calls: list[str] = []

    def fake_prune_dead() -> list[str]:
        calls.append("unbound_dead")
        return []

    def fake_prune_stale() -> list[str]:
        calls.append("unbound_stale")
        return ["stale-unbound"]

    monkeypatch.setattr(container.external_discovery, "prune_dead", fake_prune_dead)
    monkeypatch.setattr(container.external_discovery, "prune_stale", fake_prune_stale)

    try:
        await container._prune_unbound_external_sessions()
    finally:
        await container.bot.session.close()

    assert calls == ["unbound_dead", "unbound_stale"]


@pytest.mark.asyncio
async def test_prune_unbound_external_sessions_invalidates_dead_pruned_permission_state(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    container = AppContainer(make_settings(tmp_path, install_hooks=False))
    calls: list[str] = []

    def fake_prune_dead() -> list[str]:
        calls.append("unbound_dead")
        return ["dead-unbound"]

    def fake_prune_stale() -> list[str]:
        calls.append("unbound_stale")
        return []

    async def fake_registry_invalidate(session_id: str) -> int:
        calls.append(f"registry:{session_id}")
        return 1

    async def fake_unbound_invalidate(session_id: str) -> int:
        calls.append(f"unbound:{session_id}")
        return 1

    async def fake_cancel_pending_permissions(*, session_id: str) -> None:
        calls.append(f"hook:{session_id}")

    monkeypatch.setattr(container.external_discovery, "prune_dead", fake_prune_dead)
    monkeypatch.setattr(container.external_discovery, "prune_stale", fake_prune_stale)
    monkeypatch.setattr(container.permission_callback_registry, "invalidate_session", fake_registry_invalidate)
    monkeypatch.setattr(container.unbound_permission_handler, "invalidate_session", fake_unbound_invalidate)
    monkeypatch.setattr(container.hook_socket_server, "cancel_pending_permissions", fake_cancel_pending_permissions)

    try:
        await container._prune_unbound_external_sessions()
    finally:
        await container.bot.session.close()

    assert calls == [
        "unbound_dead",
        "registry:dead-unbound",
        "unbound:dead-unbound",
        "hook:dead-unbound",
        "unbound_stale",
    ]


@pytest.mark.asyncio
async def test_dead_unbound_cleanup_clears_auto_approve_state(tmp_path) -> None:
    container = AppContainer(make_settings(tmp_path, install_hooks=False))
    active_session_id = "dead-active"
    slotted_session_id = "dead-slotted"

    await container.auto_approve_service.activate_if_session_alive(user_id=42, session_id=active_session_id)
    container.auto_approve_service._slots[slotted_session_id] = ActivationSlot(
        session_id=slotted_session_id,
        holder_user_id=43,
        attempt_id="attempt-1",
    )

    try:
        await container._cleanup_dead_unbound_external_session(active_session_id)
        await container._cleanup_dead_unbound_external_session(slotted_session_id)
    finally:
        await container.bot.session.close()

    assert container.auto_approve_service.get_active_user_for_session(active_session_id) is None
    assert active_session_id not in container.auto_approve_service._slots
    assert container.auto_approve_service.is_session_ended(active_session_id) is True
    assert slotted_session_id not in container.auto_approve_service._slots
    assert container.auto_approve_service.is_session_ended(slotted_session_id) is True


@pytest.mark.asyncio
async def test_dead_unbound_cleanup_clears_external_user_question_state(tmp_path) -> None:
    container = AppContainer(make_settings(tmp_path, install_hooks=False))
    session_id = "dead-uq"
    tool_use_id = "tool-uq"
    container.external_uq_state.store(
        PendingExternalUserQuestion(
            tool_use_id=tool_use_id,
            session_id=session_id,
            user_id=42,
            pid=12345,
            prompts=(
                UserQuestionPrompt(
                    tool_use_id=tool_use_id,
                    question_index=0,
                    total_questions=1,
                    question="Continue?",
                ),
            ),
            pane_id="%1",
        )
    )

    try:
        await container._cleanup_dead_unbound_external_session(session_id)
    finally:
        await container.bot.session.close()

    assert container.external_uq_state.get(tool_use_id) is None


@pytest.mark.asyncio
async def test_dead_unbound_cleanup_failure_is_retained_for_retry_when_called_directly(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    container = AppContainer(make_settings(tmp_path, install_hooks=False))
    session_id = "dead-direct"

    async def failing_registry_invalidate(session_id_arg: str) -> int:
        assert session_id_arg == session_id
        raise RuntimeError("registry failure")

    monkeypatch.setattr(container.permission_callback_registry, "invalidate_session", failing_registry_invalidate)

    try:
        success = await container._cleanup_dead_unbound_external_session(session_id)
    finally:
        await container.bot.session.close()

    assert success is False
    assert session_id in container._pending_dead_unbound_cleanup_ids


@pytest.mark.asyncio
async def test_prune_unbound_external_sessions_continues_cleanup_when_one_step_fails(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    container = AppContainer(make_settings(tmp_path, install_hooks=False))
    calls: list[str] = []

    def fake_prune_dead() -> list[str]:
        calls.append("unbound_dead")
        return ["dead-a", "dead-b"]

    def fake_prune_stale() -> list[str]:
        calls.append("unbound_stale")
        return []

    async def fake_registry_invalidate(session_id: str) -> int:
        calls.append(f"registry:{session_id}")
        if session_id == "dead-a":
            raise RuntimeError("registry failure")
        return 1

    async def fake_unbound_invalidate(session_id: str) -> int:
        calls.append(f"unbound:{session_id}")
        return 1

    async def fake_cancel_pending_permissions(*, session_id: str) -> None:
        calls.append(f"hook:{session_id}")

    monkeypatch.setattr(container.external_discovery, "prune_dead", fake_prune_dead)
    monkeypatch.setattr(container.external_discovery, "prune_stale", fake_prune_stale)
    monkeypatch.setattr(container.permission_callback_registry, "invalidate_session", fake_registry_invalidate)
    monkeypatch.setattr(container.unbound_permission_handler, "invalidate_session", fake_unbound_invalidate)
    monkeypatch.setattr(container.hook_socket_server, "cancel_pending_permissions", fake_cancel_pending_permissions)

    try:
        await container._prune_unbound_external_sessions()
    finally:
        await container.bot.session.close()

    assert calls == [
        "unbound_dead",
        "registry:dead-a",
        "unbound:dead-a",
        "hook:dead-a",
        "registry:dead-b",
        "unbound:dead-b",
        "hook:dead-b",
        "unbound_stale",
    ]


@pytest.mark.asyncio
async def test_prune_unbound_external_sessions_retries_failed_cleanup_next_pass(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    container = AppContainer(make_settings(tmp_path, install_hooks=False))
    calls: list[str] = []
    prune_results = [["dead-a"], []]

    def fake_prune_dead() -> list[str]:
        calls.append("unbound_dead")
        return prune_results.pop(0)

    def fake_prune_stale() -> list[str]:
        calls.append("unbound_stale")
        return []

    registry_failures_remaining = 1

    async def fake_registry_invalidate(session_id: str) -> int:
        nonlocal registry_failures_remaining
        calls.append(f"registry:{session_id}")
        if registry_failures_remaining:
            registry_failures_remaining -= 1
            raise RuntimeError("registry failure")
        return 1

    async def fake_unbound_invalidate(session_id: str) -> int:
        calls.append(f"unbound:{session_id}")
        return 1

    async def fake_cancel_pending_permissions(*, session_id: str) -> None:
        calls.append(f"hook:{session_id}")

    monkeypatch.setattr(container.external_discovery, "prune_dead", fake_prune_dead)
    monkeypatch.setattr(container.external_discovery, "prune_stale", fake_prune_stale)
    monkeypatch.setattr(container.permission_callback_registry, "invalidate_session", fake_registry_invalidate)
    monkeypatch.setattr(container.unbound_permission_handler, "invalidate_session", fake_unbound_invalidate)
    monkeypatch.setattr(container.hook_socket_server, "cancel_pending_permissions", fake_cancel_pending_permissions)

    try:
        await container._prune_unbound_external_sessions()
        await container._prune_unbound_external_sessions()
    finally:
        await container.bot.session.close()

    assert calls == [
        "unbound_dead",
        "registry:dead-a",
        "unbound:dead-a",
        "hook:dead-a",
        "unbound_stale",
        "unbound_dead",
        "registry:dead-a",
        "unbound:dead-a",
        "hook:dead-a",
        "unbound_stale",
    ]


@pytest.mark.asyncio
async def test_prune_unbound_external_sessions_prunes_stale_when_dead_prune_fails(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    container = AppContainer(make_settings(tmp_path, install_hooks=False))
    calls: list[str] = []

    def fake_prune_dead() -> list[str]:
        calls.append("unbound_dead")
        raise OverflowError("bad pid")

    def fake_prune_stale() -> list[str]:
        calls.append("unbound_stale")
        return ["stale-unbound"]

    monkeypatch.setattr(container.external_discovery, "prune_dead", fake_prune_dead)
    monkeypatch.setattr(container.external_discovery, "prune_stale", fake_prune_stale)

    try:
        await container._prune_unbound_external_sessions()
    finally:
        await container.bot.session.close()

    assert calls == ["unbound_dead", "unbound_stale"]


@pytest.mark.asyncio
async def test_start_runs_tmux_health_check_before_restore(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    container = AppContainer(make_settings(tmp_path, install_hooks=False, tmux_mode=True))
    calls: list[str] = []

    async def fake_health_check() -> None:
        calls.append("health")

    async def fake_restore() -> None:
        calls.append("restore")

    monkeypatch.setattr(container.bot, "set_my_commands", AsyncMock(return_value=None))
    monkeypatch.setattr(container.hook_socket_server, "start", AsyncMock(return_value=None))
    monkeypatch.setattr(container.hook_socket_server, "stop", AsyncMock(return_value=None))
    monkeypatch.setattr(container.bot.session, "close", AsyncMock(return_value=None))
    monkeypatch.setattr(container.session_registry, "_run_health_check", fake_health_check)
    monkeypatch.setattr(container, "_restore_session_bindings", fake_restore)
    monkeypatch.setattr(container.external_binding_cleanup_service, "_cleanup", AsyncMock(return_value=None))
    monkeypatch.setattr(container.upload_cleanup, "run_cleanup", AsyncMock(return_value=None))
    monkeypatch.setattr(container._janitor, "start", AsyncMock(return_value=None))
    monkeypatch.setattr(container._janitor, "stop", AsyncMock(return_value=None))

    await container.start()
    try:
        assert calls == ["health", "restore"]
    finally:
        await container.stop()


@pytest.mark.asyncio
async def test_start_clears_dead_tmux_binding_before_restoring_watchers(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = make_settings(tmp_path, install_hooks=False, tmux_mode=True)
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
        claude_session_id="claude-session-dead",
        workdir=str(tmp_path),
    )
    state = first.structured_session_store.get_or_create(
        session_id="claude-session-dead",
        provider="claude_code",
        workdir=str(tmp_path),
        terminal_id=session.terminal_id,
        user_id=1,
        claude_session_id="claude-session-dead",
    )
    state.phase = SessionPhase.PROCESSING
    state.turns = [ConversationTurn(turn_id="a1", role="assistant", text="\n恢复中\n", is_complete=True)]
    first.structured_session_store._persist(state)
    await first.bot.session.close()

    second = AppContainer(settings)
    watched: list[tuple[str, str]] = []

    def fake_watch(*, session_id: str, workdir: str) -> None:
        watched.append((session_id, workdir))

    monkeypatch.setattr(second.bot, "set_my_commands", AsyncMock(return_value=None))
    monkeypatch.setattr(second.hook_socket_server, "start", AsyncMock(return_value=None))
    monkeypatch.setattr(second.hook_socket_server, "stop", AsyncMock(return_value=None))
    monkeypatch.setattr(second.bot.session, "close", AsyncMock(return_value=None))
    monkeypatch.setattr(second.tmux_runner, "session_exists", AsyncMock(return_value=False))
    monkeypatch.setattr(second.session_supervisor, "watch", fake_watch)
    monkeypatch.setattr(second.external_binding_cleanup_service, "_cleanup", AsyncMock(return_value=None))
    monkeypatch.setattr(second.upload_cleanup, "run_cleanup", AsyncMock(return_value=None))
    monkeypatch.setattr(second._janitor, "start", AsyncMock(return_value=None))
    monkeypatch.setattr(second._janitor, "stop", AsyncMock(return_value=None))

    await second.start()
    try:
        restored_session = await second.session_service.get(1)
        assert restored_session is not None
        assert restored_session.terminal_mode is False
        assert restored_session.terminal_id is None
        assert restored_session.claude_chat_active is False
        assert restored_session.claude_session_id is None
        assert watched == []
    finally:
        await second.stop()


@pytest.mark.asyncio
async def test_start_registers_unbound_cleanup_without_replacing_existing_lifecycle_jobs(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    container = AppContainer(make_settings(tmp_path, install_hooks=False))
    calls: list[str] = []

    async def fake_unbound_cleanup() -> None:
        calls.append("unbound")

    monkeypatch.setattr(container.bot, "set_my_commands", AsyncMock(return_value=None))
    monkeypatch.setattr(container.hook_socket_server, "start", AsyncMock(return_value=None))
    monkeypatch.setattr(container.hook_socket_server, "stop", AsyncMock(return_value=None))
    monkeypatch.setattr(container.bot.session, "close", AsyncMock(return_value=None))
    monkeypatch.setattr(container, "_restore_session_bindings", AsyncMock(return_value=None))
    monkeypatch.setattr(container.upload_cleanup, "run_cleanup", AsyncMock(return_value=None))
    monkeypatch.setattr(container.external_binding_cleanup_service, "_cleanup", AsyncMock(return_value=None))
    monkeypatch.setattr(container.session_registry, "_run_health_check", AsyncMock(return_value=None))
    monkeypatch.setattr(container, "_prune_unbound_external_sessions", fake_unbound_cleanup)
    monkeypatch.setattr(container._janitor, "start", AsyncMock(return_value=None))

    await container.start()
    try:
        jobs = container._janitor._jobs
        assert "upload_queue_cleanup" in jobs
        assert "upload_file_cleanup" in jobs
        assert "periodic_recheck" in jobs
        assert "external_binding_cleanup" in jobs
        assert "session_health_check" in jobs
        assert "external_discovery_cleanup" in jobs
        assert "session_lifecycle_reconcile" not in jobs

        interval, callback = jobs["external_discovery_cleanup"]
        assert interval == container.settings.session_health_check_interval_sec
        await callback()
        assert calls == ["unbound"]
    finally:
        await container.stop()


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
    monkeypatch.setattr(container.session_supervisor, "_on_jsonl_sync", fake_sync)

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
    monkeypatch.setattr(container.session_supervisor, "_on_jsonl_sync", fake_sync)

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
    monkeypatch.setattr(container.session_supervisor, "_on_jsonl_sync", fake_sync)

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
    monkeypatch.setattr(container.session_supervisor, "_on_jsonl_sync", fake_sync)

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
    monkeypatch.setattr(container.session_supervisor, "_on_jsonl_sync", fake_sync)

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
    monkeypatch.setattr(container.session_supervisor, "_on_jsonl_sync", fake_sync)

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
    monkeypatch.setattr(container.session_supervisor, "_on_jsonl_sync", fake_sync)

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
    monkeypatch.setattr(container.session_supervisor, "_on_jsonl_sync", fake_sync)

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

    async def fake_start(handler, permission_failure_handler=None, permission_resolved_handler=None):
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
    monkeypatch.setattr(container.session_supervisor, "_on_jsonl_sync", fake_sync)

    await container.start()
    container._schedule_jsonl_sync("claude-session-1", str(tmp_path))

    await container.stop()


@pytest.mark.asyncio
async def test_debounced_sync_keeps_request_added_during_sync(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that a sync request scheduled during an active sync is picked up on the next poll."""
    container = AppContainer(make_settings(tmp_path, install_hooks=False))
    seen: list[tuple[str, str]] = []

    # Supervisor needs session state in the store to watch
    container.structured_session_store.get_or_create(
        session_id="claude-session-1",
        provider="claude_code",
        workdir=str(tmp_path),
    )

    async def fake_sync(session_id: str, cwd: str) -> None:
        seen.append((session_id, cwd))
        # Schedule a second sync during the first one
        if len(seen) == 1:
            container._schedule_jsonl_sync(session_id, f"{cwd}-next")

    monkeypatch.setattr(container, "sync_claude_session", fake_sync)
    monkeypatch.setattr(container.session_supervisor, "_on_jsonl_sync", fake_sync)

    container._schedule_jsonl_sync("claude-session-1", str(tmp_path))
    # Wait for debounce + 2 poll cycles (first processes initial, second picks up re-scheduled)
    await asyncio.sleep(0.2 * 2 + 0.01 + 0.2)

    assert seen == [
        ("claude-session-1", str(tmp_path)),
        ("claude-session-1", f"{str(tmp_path)}-next"),
    ]


@pytest.mark.asyncio
async def test_debounced_sync_requeues_request_after_failure(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Supervisor logs sync errors but does not re-queue (differs from old behavior)."""
    container = AppContainer(make_settings(tmp_path, install_hooks=False))
    seen: list[tuple[str, str]] = []

    # Supervisor needs session state in the store to watch
    container.structured_session_store.get_or_create(
        session_id="claude-session-1",
        provider="claude_code",
        workdir=str(tmp_path),
    )

    async def fake_sync(session_id: str, cwd: str) -> None:
        seen.append((session_id, cwd))
        if len(seen) == 1:
            raise RuntimeError("boom")

    monkeypatch.setattr(container, "sync_claude_session", fake_sync)
    monkeypatch.setattr(container.session_supervisor, "_on_jsonl_sync", fake_sync)

    container._schedule_jsonl_sync("claude-session-1", str(tmp_path))
    await wait_for_jsonl_sync_idle(container, "claude-session-1")

    # Supervisor catches the error and logs it; the request is consumed.
    assert seen == [
        ("claude-session-1", str(tmp_path)),
    ]


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
    monkeypatch.setattr(container.session_supervisor, "_on_jsonl_sync", fake_sync)

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
    assert container.permission_callback_registry._ttl_sec == settings.claude_hook_pending_permission_ttl_sec


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
    monkeypatch.setattr(container.session_supervisor, "_on_jsonl_sync", fake_sync)

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

    async def fake_start(handler, permission_failure_handler=None, permission_resolved_handler=None):
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

    async def fake_start(handler, permission_failure_handler=None, permission_resolved_handler=None):
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

    async def fake_start(handler, permission_failure_handler=None, permission_resolved_handler=None):
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

    async def fake_start(handler, permission_failure_handler=None, permission_resolved_handler=None):
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
    seen_supervisor_watch: list[tuple[str, str]] = []

    async def fake_start(handler, permission_failure_handler=None, permission_resolved_handler=None):
        return None

    async def fake_stop():
        return None

    async def fake_close():
        return None

    def fake_watch(*, session_id: str, workdir: str) -> None:
        seen_supervisor_watch.append((session_id, workdir))

    monkeypatch.setattr(second.hook_socket_server, "start", fake_start)
    monkeypatch.setattr(second.hook_socket_server, "stop", fake_stop)
    monkeypatch.setattr(second.bot.session, "close", fake_close)
    monkeypatch.setattr(second.session_supervisor, "watch", fake_watch)

    try:
        await second.start()
        assert ("claude-session-1", str(tmp_path)) in seen_supervisor_watch
    finally:
        await second.stop()


@pytest.mark.asyncio
async def test_unbound_permission_handler_requires_gateway_before_notify(tmp_path) -> None:
    handler = UnboundPermissionHandler(
        message_sender=type("MessageSender", (), {"send_message": AsyncMock(), "send_photo": AsyncMock(), "send_document": AsyncMock()})(),
        hook_socket_server=type("HookSocket", (), {"respond_to_permission": AsyncMock(return_value=True)})(),
        allowed_user_ids={1},
    )

    event = HookEvent(
        session_id="session-before-gateway",
        cwd=str(tmp_path),
        event="PermissionRequest",
        status="waiting_for_approval",
        tool="Bash",
        tool_input={"command": "pwd"},
        tool_use_id="tool-before-gateway",
    )

    with pytest.raises(RuntimeError, match="gateway"):
        await handler.handle_unbound_permission(event)


@pytest.mark.asyncio
async def test_session_end_runs_unified_permission_cleanup_in_order(tmp_path) -> None:
    seen: list[str] = []

    class _AAS:
        async def deactivate_all_for_session(self, session_id: str) -> int:
            seen.append(f"aas_deactivate:{session_id}")
            return 1

        async def release_all_slots_for_session(self, session_id: str) -> int:
            seen.append(f"aas_release:{session_id}")
            return 1

    class _Registry:
        async def invalidate_session(self, session_id: str) -> int:
            seen.append(f"registry:{session_id}")
            return 1

    class _Unbound:
        async def invalidate_session(self, session_id: str) -> int:
            seen.append(f"unbound:{session_id}")
            return 1

    class _BindingStore:
        def remove_binding(self, session_id: str) -> None:
            seen.append(f"binding:{session_id}")

    class _Container(AppContainer):
        def __init__(self) -> None:
            self.settings = type("Settings", (), {"allowed_workdirs": [str(tmp_path)]})()
            self.auto_approve_service = _AAS()
            self.permission_callback_registry = _Registry()
            self.unbound_permission_handler = _Unbound()
            self.external_binding_store = _BindingStore()

        async def _bind_hook_session(self, event: HookEvent) -> None:
            seen.append(f"bind:{event.session_id}")

        async def _dispatch_session_event(self, event: SessionEvent) -> None:
            seen.append(f"dispatch:{event.session_id}")

        def _schedule_jsonl_sync(self, session_id: str, cwd: str) -> None:
            seen.append(f"sync:{session_id}")

    container = _Container()

    await container._resolve_ownership_stage(HookEvent(session_id="ended-session", cwd=str(tmp_path), event="SessionEnd", status="ended"))

    assert seen[:5] == [
        "aas_deactivate:ended-session",
        "aas_release:ended-session",
        "registry:ended-session",
        "unbound:ended-session",
        "binding:ended-session",
    ]
