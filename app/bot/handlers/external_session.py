from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.bot.handlers.command_utils import split_message_command
from app.bot.handlers.session_actions import EXTERNAL_INPUT_LEFT_MARK
from app.bot.handlers.user_utils import extract_user_id
from app.infra.text_formatting import (
    format_external_session_action_outcome,
    relative_time_compact_en,
    short_id,
)
from app.services.external_session_binder import ExternalSessionBinder
from app.services.external_session_discovery import ExternalSessionDiscoveryService
from app.services.external_session_input_service import ExternalSessionInputService
from app.services.session_id_resolver import (
    BindResult,
    UnbindResult,
    _resolve_session_id,
    external_session_select_token,
    resolve_and_bind,
    resolve_and_unbind,
)
from app.services.session_store import SessionStore

logger = logging.getLogger(__name__)


def register_external_session_handler(
    router: Router,
    *,
    discovery: ExternalSessionDiscoveryService,
    binder: ExternalSessionBinder,
    session_store: SessionStore,
    input_service: ExternalSessionInputService | None = None,
) -> None:
    @router.message(Command("external"))
    async def command_external(message: Message) -> None:
        user_id = extract_user_id(message)
        # Parse: /external <subcommand> [args]
        parts = split_message_command(message, maxsplit=2)
        # parts[0] = "/external"
        if len(parts) < 2:
            await message.answer(
                "用法:\n/external list\n/external bind <session_id>\n/external unbind <session_id>\n/external status <session_id>\n/external leave (退出外部输入模式)"
            )
            return

        subcommand = parts[1].lower()
        arg = parts[2].strip() if len(parts) > 2 else ""

        if subcommand == "list":
            await _handle_list(message, user_id=user_id, discovery=discovery, binder=binder)
        elif subcommand == "bind":
            await _handle_bind(message, user_id=user_id, session_id=arg, binder=binder, discovery=discovery)
        elif subcommand == "unbind":
            await _handle_unbind(message, user_id=user_id, session_id=arg, binder=binder, discovery=discovery)
        elif subcommand == "status":
            await _handle_status(message, user_id=user_id, session_id=arg, binder=binder, discovery=discovery, session_store=session_store)
        elif subcommand == "leave":
            if input_service is None:
                await message.answer("外部输入功能未启用。")
            else:
                left = await input_service.leave(user_id=user_id)
                await message.answer(f"{EXTERNAL_INPUT_LEFT_MARK}。" if left else "当前不在外部输入模式。")
        else:
            await message.answer(f"未知子命令: {subcommand}")


async def _handle_list(
    message: Message,
    *,
    user_id: int,
    discovery: ExternalSessionDiscoveryService,
    binder: ExternalSessionBinder,
) -> None:
    unbound = discovery.list_unbound()
    bound = binder.list_bound_for_user(user_id)

    if not unbound and not bound:
        await message.answer("📋 No external sessions found.")
        return

    lines = ["📋 External Sessions:"]

    if unbound:
        lines.append("\n🆓 Unbound:")
        for s in unbound:
            ago = relative_time_compact_en(s.first_seen)
            lines.append(f"  • {short_id(s.session_id, 12)}... | {s.cwd} | first seen {ago}")

    if bound:
        lines.append("\n🔗 Your bound sessions:")
        for b in bound:
            ago = relative_time_compact_en(b.bound_at)
            lines.append(f"  • {short_id(b.session_id, 12)}... | {b.cwd} | bound {ago}")

    await message.answer("\n".join(lines))


async def _handle_bind_unbind_action(
    message: Message,
    *,
    action_type: str,
    user_id: int,
    session_id: str,
    binder: ExternalSessionBinder,
    discovery: ExternalSessionDiscoveryService,
) -> None:
    if not session_id:
        await message.answer(f"用法: /external {action_type} <session_id>")
        return

    result: BindResult | UnbindResult
    if action_type == "bind":
        result = await resolve_and_bind(session_id, user_id=user_id, discovery=discovery, binder=binder)
    else:
        result = await resolve_and_unbind(session_id, user_id=user_id, discovery=discovery, binder=binder)

    if result.success:
        if action_type == "bind":
            assert result.session_id is not None  # success ⇒ resolved session_id
            token = external_session_select_token(result.session_id, discovery=discovery, binder=binder)
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="进入终端输入", callback_data=f"sess:select:{token}")]]
            )
            await message.answer(
                format_external_session_action_outcome(action_type, True, session_id=result.session_id, message=result.message),
                reply_markup=keyboard,
            )
        else:
            await message.answer(
                format_external_session_action_outcome(action_type, True, session_id=result.session_id, message=result.message)
            )
    else:
        await message.answer(
            format_external_session_action_outcome(action_type, False, session_id=result.session_id, message=result.message)
        )


async def _handle_bind(
    message: Message,
    *,
    user_id: int,
    session_id: str,
    binder: ExternalSessionBinder,
    discovery: ExternalSessionDiscoveryService,
) -> None:
    await _handle_bind_unbind_action(
        message,
        action_type="bind",
        user_id=user_id,
        session_id=session_id,
        binder=binder,
        discovery=discovery,
    )


async def _handle_unbind(
    message: Message,
    *,
    user_id: int,
    session_id: str,
    binder: ExternalSessionBinder,
    discovery: ExternalSessionDiscoveryService,
) -> None:
    await _handle_bind_unbind_action(
        message,
        action_type="unbind",
        user_id=user_id,
        session_id=session_id,
        binder=binder,
        discovery=discovery,
    )


async def _handle_status(
    message: Message,
    *,
    user_id: int,
    session_id: str,
    binder: ExternalSessionBinder,
    discovery: ExternalSessionDiscoveryService,
    session_store: SessionStore,
) -> None:
    if not session_id:
        await message.answer("用法: /external status <session_id>")
        return

    resolved, error = _resolve_session_id(session_id, discovery, binder)
    if error or not resolved:
        await message.answer(f"❌ {error or 'Session not found'}")
        return

    # Verify user owns this binding
    binding = binder.list_bound_for_user(user_id)
    owned = any(b.session_id == resolved for b in binding)
    if not owned:
        await message.answer("❌ Session not bound to you")
        return

    state = session_store.get(resolved)
    if state is None:
        await message.answer(f"📊 Session {short_id(resolved, 12)}...\n  phase: unknown\n  (no state available)")
        return

    lines = [f"📊 Session {short_id(resolved, 12)}..."]
    lines.append(f"  phase: {state.phase.value}")
    if state.last_tool_name:
        lines.append(f"  last tool: {state.last_tool_name}")
    lines.append(f"  cwd: {state.workdir}")

    await message.answer("\n".join(lines))
