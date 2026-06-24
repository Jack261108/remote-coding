"""Unit tests for SessionGuardMiddleware."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import CallbackQuery, Message

from app.bot.middleware.session_guard import SessionGuardMiddleware


def _make_message(text: str = "/some_command", user_id: int = 123) -> MagicMock:
    msg = MagicMock(spec=Message)
    msg.text = text
    msg.from_user = MagicMock()
    msg.from_user.id = user_id
    msg.answer = AsyncMock()
    return msg


def _make_callback(data: str = "action:arg", user_id: int = 123) -> MagicMock:
    cb = MagicMock(spec=CallbackQuery)
    cb.data = data
    cb.from_user = MagicMock()
    cb.from_user.id = user_id
    cb.answer = AsyncMock()
    return cb


def _make_session(*, active: bool = True) -> MagicMock:
    session = MagicMock()
    session.claude_chat_active = active
    return session


def _make_session_service(session=None) -> MagicMock:
    svc = MagicMock()
    svc.get = AsyncMock(return_value=session)
    return svc


class TestPassthrough:
    async def test_handler_called_when_session_exists(self) -> None:
        session = _make_session()
        svc = _make_session_service(session)
        mw = SessionGuardMiddleware(session_service=svc, require_active=False)
        handler = AsyncMock(return_value="ok")
        msg = _make_message()
        data: dict = {"user_id": 123}

        result = await mw(handler, msg, data)

        svc.get.assert_awaited_once_with(123)
        handler.assert_awaited_once_with(msg, data)
        assert data["session"] is session
        assert result == "ok"

    async def test_no_user_id_passes_through(self) -> None:
        svc = _make_session_service()
        mw = SessionGuardMiddleware(session_service=svc)
        handler = AsyncMock(return_value="ok")
        msg = _make_message()

        result = await mw(handler, msg, {})

        svc.get.assert_not_awaited()
        handler.assert_awaited_once()
        assert result == "ok"

    async def test_user_id_zero_passes_through(self) -> None:
        svc = _make_session_service()
        mw = SessionGuardMiddleware(session_service=svc)
        handler = AsyncMock(return_value="ok")
        msg = _make_message(user_id=0)

        result = await mw(handler, msg, {"user_id": 0})

        svc.get.assert_not_awaited()
        handler.assert_awaited_once()
        assert result == "ok"


class TestNoSession:
    async def test_no_session_blocks_message(self) -> None:
        svc = _make_session_service(None)
        mw = SessionGuardMiddleware(session_service=svc)
        handler = AsyncMock()
        msg = _make_message()

        result = await mw(handler, msg, {"user_id": 123})

        handler.assert_not_awaited()
        msg.answer.assert_awaited_once_with("请先使用 /session 或 /claude 创建会话")
        assert result is None

    async def test_no_session_blocks_callback(self) -> None:
        svc = _make_session_service(None)
        mw = SessionGuardMiddleware(session_service=svc)
        handler = AsyncMock()
        cb = _make_callback()

        result = await mw(handler, cb, {"user_id": 123})

        handler.assert_not_awaited()
        cb.answer.assert_awaited_once_with("请先使用 /session 或 /claude 创建会话", show_alert=True)
        assert result is None


class TestRequireActive:
    async def test_inactive_session_blocks_when_required(self) -> None:
        session = _make_session(active=False)
        svc = _make_session_service(session)
        mw = SessionGuardMiddleware(session_service=svc, require_active=True)
        handler = AsyncMock()
        msg = _make_message()

        result = await mw(handler, msg, {"user_id": 123})

        handler.assert_not_awaited()
        msg.answer.assert_awaited_once_with("请先发送 /claude 开启会话")
        assert result is None

    async def test_inactive_session_passes_when_not_required(self) -> None:
        session = _make_session(active=False)
        svc = _make_session_service(session)
        mw = SessionGuardMiddleware(session_service=svc, require_active=False)
        handler = AsyncMock(return_value="ok")
        msg = _make_message()

        result = await mw(handler, msg, {"user_id": 123})

        handler.assert_awaited_once()
        assert result == "ok"

    async def test_active_session_passes_when_required(self) -> None:
        session = _make_session(active=True)
        svc = _make_session_service(session)
        mw = SessionGuardMiddleware(session_service=svc, require_active=True)
        handler = AsyncMock(return_value="ok")
        msg = _make_message()

        result = await mw(handler, msg, {"user_id": 123})

        handler.assert_awaited_once()
        assert result == "ok"

    async def test_inactive_callback_blocks(self) -> None:
        session = _make_session(active=False)
        svc = _make_session_service(session)
        mw = SessionGuardMiddleware(session_service=svc, require_active=True)
        handler = AsyncMock()
        cb = _make_callback()

        result = await mw(handler, cb, {"user_id": 123})

        handler.assert_not_awaited()
        cb.answer.assert_awaited_once_with("请先发送 /claude 开启会话", show_alert=True)
        assert result is None


class TestEdgeCases:
    async def test_session_injected_into_data(self) -> None:
        session = _make_session()
        svc = _make_session_service(session)
        mw = SessionGuardMiddleware(session_service=svc)
        handler = AsyncMock()
        msg = _make_message()
        data: dict = {"user_id": 123, "existing": "value"}

        await mw(handler, msg, data)

        assert data["session"] is session
        assert data["existing"] == "value"

    async def test_handler_exception_propagates(self) -> None:
        session = _make_session()
        svc = _make_session_service(session)
        mw = SessionGuardMiddleware(session_service=svc)
        handler = AsyncMock(side_effect=RuntimeError("handler error"))
        msg = _make_message()

        with pytest.raises(RuntimeError, match="handler error"):
            await mw(handler, msg, {"user_id": 123})
