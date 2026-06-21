from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import Router
from aiogram.types import CallbackQuery, Message

from app.bot.router import _register_middleware


def _make_message(text: str, user_id: int = 123) -> MagicMock:
    message = MagicMock(spec=Message)
    message.text = text
    message.from_user = MagicMock()
    message.from_user.id = user_id
    message.answer = AsyncMock()
    return message


def _make_callback(data: str, user_id: int = 123) -> MagicMock:
    callback = MagicMock(spec=CallbackQuery)
    callback.data = data
    callback.from_user = MagicMock()
    callback.from_user.id = user_id
    callback.answer = AsyncMock()
    return callback


def _make_session_service() -> MagicMock:
    service = MagicMock()
    service.get = AsyncMock(return_value=None)
    return service


@pytest.mark.parametrize("text", ["/approve", "/deny", "/deny 不允许"])
async def test_permission_commands_skip_basic_session_guard(text: str) -> None:
    service = _make_session_service()
    guard_basic, _ = _register_middleware(Router(), service)
    handler = AsyncMock(return_value="ok")
    message = _make_message(text)

    result = await guard_basic(handler, message, {})

    assert result == "ok"
    handler.assert_awaited_once_with(message, {})
    service.get.assert_not_awaited()
    message.answer.assert_not_awaited()


@pytest.mark.parametrize(
    "data",
    [
        "perm:tok12345:allow",
        "perm:tok12345:deny",
        "perm:tok12345:auto_approve",
    ],
)
async def test_permission_callbacks_skip_basic_session_guard(data: str) -> None:
    service = _make_session_service()
    guard_basic, _ = _register_middleware(Router(), service)
    handler = AsyncMock(return_value="ok")
    callback = _make_callback(data)

    result = await guard_basic(handler, callback, {})

    assert result == "ok"
    handler.assert_awaited_once_with(callback, {})
    service.get.assert_not_awaited()
    callback.answer.assert_not_awaited()
