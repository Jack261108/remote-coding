from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from aiogram import Router
from aiogram.types import CallbackQuery, Message

from app.bot.middleware.error_handling import ErrorHandlingMiddleware
from app.bot.middleware.session_guard import SessionGuardMiddleware
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


def _make_session_service(session=None) -> MagicMock:
    service = MagicMock()
    service.get = AsyncMock(return_value=session)
    return service


def test_register_middleware_registers_only_error_handling_globally() -> None:
    router = Router()
    service = _make_session_service()

    guard_basic, guard_active = _register_middleware(router, service)

    assert isinstance(guard_basic, SessionGuardMiddleware)
    assert isinstance(guard_active, SessionGuardMiddleware)
    assert len(router.message.middleware._middlewares) == 1
    assert len(router.callback_query.middleware._middlewares) == 1
    assert isinstance(router.message.middleware._middlewares[0], ErrorHandlingMiddleware)
    assert isinstance(router.callback_query.middleware._middlewares[0], ErrorHandlingMiddleware)


async def test_basic_session_guard_blocks_missing_message_session() -> None:
    service = _make_session_service(None)
    guard_basic, _ = _register_middleware(Router(), service)
    handler = AsyncMock(return_value="ok")
    message = _make_message("/cmds")

    result = await guard_basic(handler, message, {"user_id": 123})

    assert result is None
    handler.assert_not_awaited()
    service.get.assert_awaited_once_with(123)
    message.answer.assert_awaited_once_with("请先使用 /session 或 /claude 创建会话")


async def test_active_session_guard_blocks_inactive_callback_session() -> None:
    session = MagicMock()
    session.claude_chat_active = False
    service = _make_session_service(session)
    _, guard_active = _register_middleware(Router(), service)
    handler = AsyncMock(return_value="ok")
    callback = _make_callback("clcmd:/help")

    result = await guard_active(handler, callback, {"user_id": 123})

    assert result is None
    handler.assert_not_awaited()
    callback.answer.assert_awaited_once_with("请先发送 /claude 开启会话", show_alert=True)
