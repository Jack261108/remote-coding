from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram import F
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message

from app.bot.handlers.callback_utils import apply_callback_response
from app.bot.handlers.user_utils import extract_user_id

if TYPE_CHECKING:
    from app.services.permission_gateway import PermissionGateway

_PERMISSION_CALLBACK_PREFIX = "perm"


def register_permission_handlers(
    router,
    *,
    permission_gateway: PermissionGateway,
):
    @router.message(Command("approve"))
    async def command_approve(message: Message) -> None:
        user_id = extract_user_id(message)
        await message.answer(await permission_gateway.handle_approve_command(user_id=user_id))

    @router.message(Command("deny"))
    async def command_deny(message: Message, command: CommandObject) -> None:
        user_id = extract_user_id(message)
        reason = (command.args or "").strip() or None
        await message.answer(await permission_gateway.handle_deny_command(user_id=user_id, reason=reason))

    @router.callback_query(F.data.startswith(f"{_PERMISSION_CALLBACK_PREFIX}:"))
    async def callback_permission(callback: CallbackQuery) -> None:
        user_id = extract_user_id(callback)
        response = await permission_gateway.handle_callback(data=callback.data or "", user_id=user_id)
        await apply_callback_response(
            callback,
            edit_text=response.edit_message_text,
            clear_keyboard=response.clear_keyboard,
            alert_text=response.alert_text,
            show_alert=response.show_alert,
            log_prefix="permission",
        )
