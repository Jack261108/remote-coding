from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aiogram import F
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message

if TYPE_CHECKING:
    from app.services.permission_gateway import CallbackResponse, PermissionGateway

logger = logging.getLogger(__name__)
_PERMISSION_CALLBACK_PREFIX = "perm"


async def _apply_callback_response(callback: CallbackQuery, response: CallbackResponse) -> None:
    if callback.message is not None:
        if response.edit_message_text:
            try:
                await callback.message.edit_text(response.edit_message_text)
            except Exception:
                logger.exception("failed to edit permission callback message")
        if response.clear_keyboard:
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except Exception:
                logger.exception("failed to clear permission inline keyboard")
    await callback.answer(response.alert_text, show_alert=response.show_alert)


def register_permission_handlers(
    router,
    *,
    permission_gateway: PermissionGateway,
):
    @router.message(Command("approve"))
    async def command_approve(message: Message) -> None:
        user_id = message.from_user.id if message.from_user else 0
        await message.answer(await permission_gateway.handle_approve_command(user_id=user_id))

    @router.message(Command("deny"))
    async def command_deny(message: Message, command: CommandObject) -> None:
        user_id = message.from_user.id if message.from_user else 0
        reason = (command.args or "").strip() or None
        await message.answer(await permission_gateway.handle_deny_command(user_id=user_id, reason=reason))

    @router.callback_query(F.data.startswith(f"{_PERMISSION_CALLBACK_PREFIX}:"))
    async def callback_permission(callback: CallbackQuery) -> None:
        user_id = callback.from_user.id if callback.from_user else 0
        response = await permission_gateway.handle_callback(data=callback.data or "", user_id=user_id)
        await _apply_callback_response(callback, response)
