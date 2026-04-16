from __future__ import annotations

import asyncio
import json

import pytest

from app.bootstrap import AppContainer
from app.config.settings import Settings
from app.domain.hook_models import HookEvent
from app.domain.session_models import SessionPhase


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

    await container.session_service.switch(
        user_id=1,
        provider="claude_code",
        workdir=str(tmp_path),
        terminal_mode=True,
        claude_chat_active=True,
    )

    async def fake_sync(session_id: str, cwd: str) -> None:
        container.structured_session_store.process(
            container.structured_session_store.process.__globals__["SessionEvent"](
                session_id=session_id,
                type=container.structured_session_store.process.__globals__["SessionEventType"].FILE_SYNCED,
                payload={
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
    await asyncio.sleep(0.03)

    session = await container.session_service.get(1)
    assert session is not None
    assert session.claude_session_id == "claude-session-1"

    state = container.structured_session_store.get("claude-session-1")
    assert state is not None
    assert state.user_id == 1
    assert state.terminal_id == "user_1"
    assert state.phase == SessionPhase.WAITING_FOR_INPUT
    assert state.last_reply == "干净回复"


@pytest.mark.asyncio
async def test_handle_hook_event_matches_resolved_workdir_path(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    container = AppContainer(make_settings(tmp_path, install_hooks=False))
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    resolved = str(nested.resolve())
    raw = str(nested.parent / "b" / ".")

    await container.session_service.switch(
        user_id=1,
        provider="claude_code",
        workdir=resolved,
        terminal_mode=True,
        claude_chat_active=True,
    )

    async def fake_sync(session_id: str, cwd: str) -> None:
        return None

    monkeypatch.setattr(container, "sync_claude_session", fake_sync)

    await container._handle_hook_event(
        HookEvent(
            session_id="claude-session-1",
            cwd=raw,
            event="SessionStart",
            status="starting",
        )
    )
    await asyncio.sleep(0.03)

    session = await container.session_service.get(1)
    assert session is not None
    assert session.claude_session_id == "claude-session-1"


@pytest.mark.asyncio
async def test_handle_hook_event_debounces_jsonl_sync(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    container = AppContainer(make_settings(tmp_path, install_hooks=False))
    seen: list[tuple[str, str]] = []

    async def fake_sync(session_id: str, cwd: str) -> None:
        seen.append((session_id, cwd))

    monkeypatch.setattr(container, "sync_claude_session", fake_sync)

    await container._handle_hook_event(
        HookEvent(session_id="claude-session-1", cwd=str(tmp_path), event="Notification", status="running")
    )
    await container._handle_hook_event(
        HookEvent(session_id="claude-session-1", cwd=str(tmp_path), event="Notification", status="running")
    )
    await asyncio.sleep(0.03)

    assert seen == [("claude-session-1", str(tmp_path))]


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
    assert "claude-session-1" in container._jsonl_sync_tasks

    await container.stop()

    assert container._jsonl_sync_tasks == {}
    assert container._periodic_recheck_task is None


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

    monkeypatch.setattr(second.hook_socket_server, "start", fake_start)
    monkeypatch.setattr(second.hook_socket_server, "stop", fake_stop)
    monkeypatch.setattr(second.bot.session, "close", fake_close)

    await second.start()

    restored_session = await second.session_service.get(1)
    restored_state = second.structured_session_store.get("claude-session-1")

    assert restored_session is not None
    assert restored_session.session_id == session.session_id
    assert restored_session.claude_session_id == "claude-session-1"
    assert restored_state is not None
    assert restored_state.user_id == 1
    assert restored_state.terminal_id == "user_1"
    assert restored_state.last_reply == "恢复后的回复"

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

    await second.start()

    restored_session = await second.session_service.get(1)

    assert restored_session is not None
    assert restored_session.claude_session_id is None
    assert restored_session.claude_chat_active is True

    await second.stop()
