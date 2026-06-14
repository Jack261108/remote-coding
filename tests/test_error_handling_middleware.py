"""Tests for ErrorHandlingMiddleware.

Covers: ValueError handling, generic Exception handling, Message vs CallbackQuery
dispatch, and _extract_event_context.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.types import CallbackQuery, Message, User

from app.bot.middleware.error_handling import ErrorHandlingMiddleware, _extract_event_context

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_message(user_id: int = 42, username: str = "testuser") -> Message:
    msg = MagicMock()
    msg.__class__ = Message
    msg.from_user = MagicMock()
    msg.from_user.id = user_id
    msg.from_user.username = username
    msg.answer = AsyncMock()
    return msg


def _make_callback_query(user_id: int = 42, username: str = "testuser") -> CallbackQuery:
    cq = MagicMock(spec=CallbackQuery)
    cq.from_user = MagicMock(spec=User)
    cq.from_user.id = user_id
    cq.from_user.username = username
    cq.answer = AsyncMock()
    return cq


def _make_event_no_user() -> Message:
    msg = MagicMock(spec=Message)
    msg.from_user = None
    msg.answer = AsyncMock()
    return msg


# ---------------------------------------------------------------------------
# _extract_event_context
# ---------------------------------------------------------------------------


class TestExtractEventContext:
    def test_extracts_user_from_message(self):
        msg = _make_message(99, "alice")
        ctx = _extract_event_context(msg)
        assert ctx["user_id"] == 99
        assert ctx["username"] == "alice"

    def test_extracts_user_from_callback_query(self):
        cq = _make_callback_query(7, "bob")
        ctx = _extract_event_context(cq)
        assert ctx["event_type"] == "MagicMock"  # MagicMock(spec=CallbackQuery) type name
        assert ctx["user_id"] == 7
        assert ctx["username"] == "bob"

    def test_handles_none_user(self):
        msg = _make_event_no_user()
        ctx = _extract_event_context(msg)
        assert ctx["event_type"] == "MagicMock"
        assert "user_id" not in ctx
        assert "username" not in ctx

    def test_handles_generic_event_type(self):
        event = object()
        ctx = _extract_event_context(event)
        assert ctx == {"event_type": "object"}


# ---------------------------------------------------------------------------
# ErrorHandlingMiddleware
# ---------------------------------------------------------------------------


class TestErrorHandlingMiddleware:
    @pytest.mark.asyncio
    async def test_passes_through_when_no_error(self):
        middleware = ErrorHandlingMiddleware()
        event = _make_message()
        handler = AsyncMock(return_value="result")

        result = await middleware(handler, event, {})

        assert result == "result"
        handler.assert_awaited_once_with(event, {})

    @pytest.mark.asyncio
    async def test_handles_value_error_on_message(self):
        middleware = ErrorHandlingMiddleware()
        event = _make_message()
        handler = AsyncMock(side_effect=ValueError("bad input"))

        result = await middleware(handler, event, {})

        assert result is None
        event.answer.assert_awaited_once_with("操作失败: bad input")

    @pytest.mark.asyncio
    async def test_handles_value_error_on_callback_query(self):
        middleware = ErrorHandlingMiddleware()
        event = _make_callback_query()
        handler = AsyncMock(side_effect=ValueError("invalid choice"))

        result = await middleware(handler, event, {})

        assert result is None
        event.answer.assert_awaited_once_with("操作失败: invalid choice", show_alert=True)

    @pytest.mark.asyncio
    async def test_handles_generic_exception_on_message(self):
        middleware = ErrorHandlingMiddleware()
        event = _make_message()
        handler = AsyncMock(side_effect=RuntimeError("something broke"))

        with patch("app.bot.middleware.error_handling.logger") as mock_logger:
            result = await middleware(handler, event, {})

        assert result is None
        event.answer.assert_awaited_once_with("发生内部错误，请稍后重试")
        mock_logger.exception.assert_called_once()

    @pytest.mark.asyncio
    async def test_handles_generic_exception_on_callback_query(self):
        middleware = ErrorHandlingMiddleware()
        event = _make_callback_query()
        handler = AsyncMock(side_effect=RuntimeError("boom"))

        with patch("app.bot.middleware.error_handling.logger"):
            result = await middleware(handler, event, {})

        assert result is None
        event.answer.assert_awaited_once_with("发生内部错误，请稍后重试", show_alert=True)

    @pytest.mark.asyncio
    async def test_handles_error_on_event_without_answer(self):
        """When event is neither Message nor CallbackQuery, no answer is sent."""
        middleware = ErrorHandlingMiddleware()
        event = MagicMock(spec=[])  # no answer method
        event.from_user = None
        handler = AsyncMock(side_effect=ValueError("oops"))

        result = await middleware(handler, event, {})

        assert result is None

    @pytest.mark.asyncio
    async def test_passes_data_dict_to_handler(self):
        middleware = ErrorHandlingMiddleware()
        event = _make_message()
        data = {"key": "value"}
        handler = AsyncMock(return_value=None)

        await middleware(handler, event, data)

        handler.assert_awaited_once_with(event, data)


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
