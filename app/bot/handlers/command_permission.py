from __future__ import annotations

import logging

from aiogram import F
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.services.task_service import TaskService

logger = logging.getLogger(__name__)
_PERMISSION_CALLBACK_PREFIX = "perm"


def build_permission_callback_data(*, decision: str, tool_use_id: str) -> str:
    return f"{_PERMISSION_CALLBACK_PREFIX}:{decision}:{tool_use_id}"


def parse_permission_callback_data(data: str | None) -> tuple[str, str] | None:
    if not data:
        return None
    prefix, sep, rest = data.partition(":")
    if prefix != _PERMISSION_CALLBACK_PREFIX or not sep:
        return None
    decision, sep, tool_use_id = rest.partition(":")
    if not sep or decision not in {"allow", "deny"} or not tool_use_id:
        return None
    return decision, tool_use_id


def build_permission_keyboard(*, tool_use_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="允许",
                    callback_data=build_permission_callback_data(decision="allow", tool_use_id=tool_use_id),
                ),
                InlineKeyboardButton(
                    text="拒绝",
                    callback_data=build_permission_callback_data(decision="deny", tool_use_id=tool_use_id),
                ),
            ]
        ]
    )


def register_permission_handlers(router, *, task_service: TaskService):
    @router.message(Command("approve"))
    async def command_approve(message: Message) -> None:
        user_id = message.from_user.id if message.from_user else 0
        ok, text = await task_service.respond_to_pending_permission(user_id=user_id, decision="allow")
        if ok:
            await message.answer(text)
        else:
            await message.answer(f"批准失败: {text}")

    @router.message(Command("deny"))
    async def command_deny(message: Message, command: CommandObject) -> None:
        user_id = message.from_user.id if message.from_user else 0
        reason = (command.args or "").strip() or None
        ok, text = await task_service.respond_to_pending_permission(user_id=user_id, decision="deny", reason=reason)
        if ok:
            await message.answer(text)
        else:
            await message.answer(f"拒绝失败: {text}")

    @router.callback_query(F.data.startswith(f"{_PERMISSION_CALLBACK_PREFIX}:"))
    async def callback_permission(callback: CallbackQuery) -> None:
        user_id = callback.from_user.id if callback.from_user else 0
        parsed = parse_permission_callback_data(callback.data)
        if parsed is None:
            await callback.answer("无效的权限操作", show_alert=True)
            return
        decision, tool_use_id = parsed
        ok, text = await task_service.respond_to_pending_permission(
            user_id=user_id,
            decision=decision,
            expected_tool_use_id=tool_use_id,
        )
        if callback.message is not None:
            if ok:
                try:
                    await callback.message.edit_reply_markup(reply_markup=None)
                except Exception:
                    logger.exception("failed to clear permission inline keyboard", extra={"user_id": user_id, "tool_use_id": tool_use_id})
                await callback.message.answer(text)
            else:
                await callback.message.answer(f"权限操作失败: {text}")
        await callback.answer(text, show_alert=not ok)
