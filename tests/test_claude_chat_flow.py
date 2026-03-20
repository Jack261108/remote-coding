from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from aiogram import F

from app.adapters.storage.memory import MemorySessionStore
from app.bot.handlers.command_run import _MARKER_LINE_RE, parse_run_args
from app.services.session_service import SessionService


class DummyTaskService:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def create_and_run(self, *, user_id: int, provider: str | None, prompt: str, workdir: str | None = None):
        self.calls.append(
            {
                "user_id": user_id,
                "provider": provider,
                "prompt": prompt,
                "workdir": workdir,
            }
        )
        return SimpleNamespace(task=SimpleNamespace(task_id="t1", provider="claude_code", session_id="s1"), events=_empty_events())

    async def get_status(self, task_id: str, user_id: int):
        return None

    def is_claude_tmux_enabled(self) -> bool:
        return True


async def _empty_events():
    if False:
        yield None


class DummyMessage:
    def __init__(self, text: str, user_id: int = 1) -> None:
        self.text = text
        self.from_user = SimpleNamespace(id=user_id)
        self.answers: list[str] = []

    async def answer(self, text: str) -> None:
        self.answers.append(text)


def test_non_command_text_filter_match() -> None:
    assert (F.text & ~F.text.startswith("/")).resolve(SimpleNamespace(text="hello")) is True
    assert (F.text & ~F.text.startswith("/")).resolve(SimpleNamespace(text="/run hi")) is False


@pytest.mark.asyncio
async def test_non_command_text_requires_claude_session() -> None:
    session_service = SessionService(MemorySessionStore())
    task_service = DummyTaskService()
    message = DummyMessage("hello")

    session = await session_service.get(message.from_user.id)
    if session is None or not session.claude_chat_active:
        await message.answer("请先发送 /claude")
    else:
        await task_service.create_and_run(
            user_id=message.from_user.id,
            provider="claude_code",
            prompt=message.text,
            workdir=session.workdir,
        )

    assert message.answers == ["请先发送 /claude"]
    assert task_service.calls == []


@pytest.mark.asyncio
async def test_non_command_text_routes_to_claude_provider() -> None:
    session_service = SessionService(MemorySessionStore())
    task_service = DummyTaskService()
    message = DummyMessage("help me")

    await session_service.switch(
        user_id=message.from_user.id,
        provider="claude_code",
        workdir="/tmp",
        terminal_mode=True,
        claude_chat_active=True,
    )

    session = await session_service.get(message.from_user.id)
    if session is None or not session.claude_chat_active:
        await message.answer("请先发送 /claude")
    else:
        await task_service.create_and_run(
            user_id=message.from_user.id,
            provider="claude_code",
            prompt=message.text,
            workdir=session.workdir,
        )

    assert task_service.calls == [
        {
            "user_id": 1,
            "provider": "claude_code",
            "prompt": "help me",
            "workdir": "/tmp",
        }
    ]


def test_parse_run_args_still_works() -> None:
    provider, prompt = parse_run_args("claude hello")
    assert provider == "claude"
    assert prompt == "hello"


def test_marker_line_regex_matches_bridge_markers() -> None:
    assert _MARKER_LINE_RE.match("TGCLI_BEGIN")
    assert _MARKER_LINE_RE.match("TGCLI_DONE")
    assert _MARKER_LINE_RE.match("__TGCLI_BEGIN__ task-1")
    assert _MARKER_LINE_RE.match("TGCLI_DONE: a36a571a-beec-467c-a569-fae6a4ea5742")
    assert _MARKER_LINE_RE.match("  TGCLI_BEGIN  ")
    assert not _MARKER_LINE_RE.match("你好，Jack")
