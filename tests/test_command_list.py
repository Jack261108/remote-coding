from __future__ import annotations

import pytest
from aiogram import Router

from app.adapters.storage.file_session_store import FileSessionStore
from app.adapters.storage.file_session_context_store import FileSessionContextStore
from app.services.session_registry import SessionRegistryService
from app.services.session_service import SessionService
from app.services.session_store import SessionStore
from app.bot.handlers.command_list import register_list_handler


class FakeTmuxRunner:
    def __init__(self) -> None:
        self._alive_sessions: set[str] = set()

    def _build_session_name(self, terminal_key: str) -> str:
        return f"tgcli_{terminal_key}"[:64]

    async def _session_exists(self, session_name: str) -> bool:
        return session_name in self._alive_sessions

    async def _list_managed_sessions(self) -> list[str]:
        return sorted(s for s in self._alive_sessions if s.startswith("tgcli_"))


def _setup(tmp_path, *, alive_sessions: set[str] | None = None):
    file_store = FileSessionStore(str(tmp_path))
    ctx_store = FileSessionContextStore(file_store)
    session_service = SessionService(store=ctx_store)
    session_store = SessionStore(file_store)
    tmux = FakeTmuxRunner()
    if alive_sessions:
        tmux._alive_sessions = alive_sessions
    registry = SessionRegistryService(
        session_service=session_service,
        session_store=session_store,
        tmux_runner=tmux,
        file_session_store=file_store,
    )
    router = Router()
    register_list_handler(router, registry_service=registry)
    return registry, session_service, session_store


@pytest.mark.asyncio
async def test_list_shows_no_sessions(tmp_path) -> None:
    registry, _, _ = _setup(tmp_path)
    sessions = await registry.list_active_sessions()
    assert sessions == []


@pytest.mark.asyncio
async def test_list_shows_active_session(tmp_path) -> None:
    registry, session_service, session_store = _setup(tmp_path, alive_sessions={"tgcli_user_1_abc123"})
    await session_service.switch(
        user_id=1,
        provider="claude_code",
        workdir="/proj",
        terminal_mode=True,
        claude_chat_active=True,
    )
    ctx = await session_service.get(1)
    ctx.terminal_id = "user_1_abc123"
    await session_service._store.save(ctx)
    session_store.get_or_create(
        session_id="s1",
        provider="claude_code",
        workdir="/proj",
        terminal_id="user_1_abc123",
        user_id=1,
    )

    sessions = await registry.list_active_sessions()
    assert len(sessions) == 1
    assert sessions[0].terminal_id == "user_1_abc123"
    assert sessions[0].workdir == "/proj"
    assert sessions[0].owner_user_id == 1
