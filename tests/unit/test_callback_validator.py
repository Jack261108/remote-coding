"""Unit tests for CallbackValidatorMiddleware."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from aiogram.types import CallbackQuery

from app.bot.middleware.callback_validator import CallbackValidatorMiddleware


def _make_callback(data: str | None = None) -> MagicMock:
    """Create a mock CallbackQuery with given data."""
    cb = MagicMock(spec=CallbackQuery)
    cb.data = data
    cb.answer = AsyncMock()
    return cb


def _make_handler() -> AsyncMock:
    return AsyncMock(return_value="ok")


# -- 核心功能 --


class TestPassthrough:
    async def test_non_callback_event_passes_through(self) -> None:
        mw = CallbackValidatorMiddleware(expected_parts=2)
        handler = _make_handler()
        event = MagicMock()  # not a CallbackQuery
        result = await mw(handler, event, {"key": "val"})
        handler.assert_awaited_once_with(event, {"key": "val"})
        assert result == "ok"

    async def test_valid_callback_passes_through(self) -> None:
        mw = CallbackValidatorMiddleware(expected_parts=2)
        handler = _make_handler()
        event = _make_callback("action:item1")
        result = await mw(handler, event, {})
        handler.assert_awaited_once()
        assert result == "ok"

    async def test_callback_parts_injected_into_data(self) -> None:
        mw = CallbackValidatorMiddleware(expected_parts=3)
        handler = _make_handler()
        event = _make_callback("a:b:c")
        data: dict = {}
        await mw(handler, event, data)
        assert data["callback_parts"] == ("a", "b", "c")


# -- 验证段数 --


class TestPartsValidation:
    async def test_single_expected_parts(self) -> None:
        mw = CallbackValidatorMiddleware(expected_parts=2)
        handler = _make_handler()
        event = _make_callback("a:b")
        await mw(handler, event, {})
        handler.assert_awaited_once()

    async def test_wrong_parts_count_rejected(self) -> None:
        mw = CallbackValidatorMiddleware(expected_parts=2)
        handler = _make_handler()
        event = _make_callback("a:b:c")
        result = await mw(handler, event, {})
        handler.assert_not_awaited()
        event.answer.assert_awaited_once_with("无效的回调数据", show_alert=True)
        assert result is None

    async def test_tuple_of_acceptable_parts(self) -> None:
        mw = CallbackValidatorMiddleware(expected_parts=(2, 3))
        handler = _make_handler()
        event = _make_callback("a:b:c")
        await mw(handler, event, {})
        handler.assert_awaited_once()

    async def test_single_part_callback(self) -> None:
        mw = CallbackValidatorMiddleware(expected_parts=1)
        handler = _make_handler()
        event = _make_callback("action")
        await mw(handler, event, {})
        handler.assert_awaited_once()


# -- 前缀验证 --


class TestPrefixValidation:
    async def test_single_prefix_match(self) -> None:
        mw = CallbackValidatorMiddleware(expected_parts=2, prefix="run")
        handler = _make_handler()
        event = _make_callback("run:item1")
        await mw(handler, event, {})
        handler.assert_awaited_once()

    async def test_single_prefix_mismatch_rejected(self) -> None:
        mw = CallbackValidatorMiddleware(expected_parts=2, prefix="run")
        handler = _make_handler()
        event = _make_callback("stop:item1")
        result = await mw(handler, event, {})
        handler.assert_not_awaited()
        event.answer.assert_awaited_once_with("无效的回调数据", show_alert=True)
        assert result is None

    async def test_tuple_prefix_match(self) -> None:
        mw = CallbackValidatorMiddleware(expected_parts=2, prefix=("run", "stop"))
        handler = _make_handler()
        event = _make_callback("stop:item1")
        await mw(handler, event, {})
        handler.assert_awaited_once()

    async def test_prefix_with_no_prefix_config_passes(self) -> None:
        mw = CallbackValidatorMiddleware(expected_parts=2)
        handler = _make_handler()
        event = _make_callback("anything:item1")
        await mw(handler, event, {})
        handler.assert_awaited_once()


# -- 边界条件 --


class TestEdgeCases:
    async def test_empty_callback_data_rejected(self) -> None:
        mw = CallbackValidatorMiddleware(expected_parts=1)
        handler = _make_handler()
        event = _make_callback(None)
        event.data = None
        result = await mw(handler, event, {})
        handler.assert_not_awaited()
        event.answer.assert_awaited_once()
        assert result is None

    async def test_empty_string_data_rejected(self) -> None:
        """Empty string is falsy, so not event.data triggers rejection."""
        mw = CallbackValidatorMiddleware(expected_parts=1)
        handler = _make_handler()
        event = _make_callback("")
        result = await mw(handler, event, {})
        handler.assert_not_awaited()
        event.answer.assert_awaited_once()
        assert result is None

    async def test_prefix_startswith_not_exact_match(self) -> None:
        mw = CallbackValidatorMiddleware(expected_parts=2, prefix="run")
        handler = _make_handler()
        event = _make_callback("running:item1")
        await mw(handler, event, {})
        handler.assert_awaited_once()

    async def test_expected_parts_as_int(self) -> None:
        mw = CallbackValidatorMiddleware(expected_parts=2)
        handler = _make_handler()
        event = _make_callback("a:b")
        await mw(handler, event, {})
        handler.assert_awaited_once()
