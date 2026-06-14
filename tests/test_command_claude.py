from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.bot.handlers.command_claude import register_claude_handler, resolve_claude_workdir_arg
from tests.fakes.telegram import DummyMessage


class DummyRouter:
    def __init__(self) -> None:
        self.handlers = []
        self.message = self

    def __call__(self, *_filters):
        def decorator(fn):
            self.handlers.append(fn)
            return fn

        return decorator


def test_resolve_claude_workdir_arg_returns_none_when_missing() -> None:
    assert resolve_claude_workdir_arg(None) is None
    assert resolve_claude_workdir_arg("   ") is None


def test_resolve_claude_workdir_arg_resolves_path_with_spaces(tmp_path: Path) -> None:
    workdir = tmp_path / "my project"
    workdir.mkdir()

    resolved = resolve_claude_workdir_arg(f"  {workdir}  ")

    assert resolved == str(workdir.resolve())


@pytest.mark.asyncio
async def test_command_claude_reports_open_errors() -> None:
    task_service = SimpleNamespace(
        is_workdir_allowed=lambda workdir: True,
        open_claude_chat_session=AsyncMock(side_effect=RuntimeError("boom")),
    )
    router = DummyRouter()
    register_claude_handler(router, task_service=task_service)
    message = DummyMessage("/claude")

    await router.handlers[0](message, SimpleNamespace(args=None))

    assert message.answers == ["开启失败: boom"]


@pytest.mark.asyncio
async def test_command_claude_reports_value_errors() -> None:
    task_service = SimpleNamespace(
        is_workdir_allowed=lambda workdir: True,
        open_claude_chat_session=AsyncMock(side_effect=ValueError("bad workdir")),
    )
    router = DummyRouter()
    register_claude_handler(router, task_service=task_service)
    message = DummyMessage("/claude")

    await router.handlers[0](message, SimpleNamespace(args=None))

    assert message.answers == ["参数错误: bad workdir"]
