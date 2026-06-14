from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.handlers.user_utils import extract_user_id
from app.bot.middleware.callback_validator import CallbackValidatorMiddleware
from app.infra.text_formatting import format_external_session_bound_message, format_external_session_unbound_message, short_id
from app.services.external_session_binder import ExternalSessionBinder
from app.services.external_session_discovery import ExternalSessionDiscoveryService
from app.services.session_action_validator import validate_external_session_select
from app.services.session_id_resolver import BindResult, UnbindResult, resolve_and_bind, resolve_and_unbind, resolve_unique_prefix
from app.services.session_registry import SessionRegistryService

logger = logging.getLogger(__name__)


async def _resolve_terminal_id_prefix(
    terminal_id_prefix: str,
    registry_service: SessionRegistryService,
) -> tuple[str | None, str | None]:
    candidates = [session.terminal_id for session in await registry_service.list_active_sessions() if session.is_alive]
    return resolve_unique_prefix(terminal_id_prefix, candidates)


def register_session_action_handlers(
    router: Router,
    *,
    discovery: ExternalSessionDiscoveryService,
    binder: ExternalSessionBinder,
    registry_service: SessionRegistryService | None = None,
) -> None:
    router.callback_query.middleware(CallbackValidatorMiddleware(prefix="sess"))

    @router.callback_query(F.data.startswith("sess:select:"))
    async def handle_session_select(callback: CallbackQuery, callback_parts: tuple[str, ...]) -> None:
        user_id = extract_user_id(callback)
        session_id_prefix = callback_parts[2]

        validation = validate_external_session_select(
            session_id_prefix,
            user_id=user_id,
            discovery=discovery,
            binder=binder,
        )
        if validation.denial_message or not validation.session_id or not validation.action:
            await callback.answer(validation.denial_message or "Session not found")
            return

        detail_text = f"📂 Session: {short_id(validation.session_id, 12)}...\n  cwd: {validation.cwd}"

        callback_token = validation.callback_token
        if validation.action == "unbind":
            buttons = [[InlineKeyboardButton(text="取消绑定", callback_data=f"sess:unbind:{callback_token}")]]
        else:
            buttons = [[InlineKeyboardButton(text="绑定", callback_data=f"sess:bind:{callback_token}")]]

        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

        await callback.answer()
        if callback.message:
            await callback.message.answer(detail_text, reply_markup=keyboard)

    async def _handle_bind_unbind_action(callback: CallbackQuery, action_type: str, callback_parts: tuple[str, ...]) -> None:
        user_id = extract_user_id(callback)
        session_id_prefix = callback_parts[2]

        result: BindResult | UnbindResult
        if action_type == "bind":
            result = await resolve_and_bind(session_id_prefix, user_id=user_id, discovery=discovery, binder=binder)
        else:
            result = await resolve_and_unbind(session_id_prefix, user_id=user_id, discovery=discovery, binder=binder)

        if result.success:
            success_text = "绑定成功" if action_type == "bind" else "取消绑定成功"
            await callback.answer(success_text)
            if callback.message:
                if action_type == "bind":
                    await callback.message.answer(format_external_session_bound_message(result.session_id, result.message))
                else:
                    await callback.message.answer(format_external_session_unbound_message(result.session_id))
        else:
            await callback.answer(f"❌ {result.message}")

    @router.callback_query(F.data.startswith("sess:bind:"))
    async def handle_session_bind(callback: CallbackQuery, callback_parts: tuple[str, ...]) -> None:
        await _handle_bind_unbind_action(callback, "bind", callback_parts)

    @router.callback_query(F.data.startswith("sess:unbind:"))
    async def handle_session_unbind(callback: CallbackQuery, callback_parts: tuple[str, ...]) -> None:
        await _handle_bind_unbind_action(callback, "unbind", callback_parts)

    # ── tmux session actions ─────────────────────────────────────────────────

    @router.callback_query(F.data.startswith("sess:attach:"))
    async def handle_session_attach(callback: CallbackQuery, callback_parts: tuple[str, ...]) -> None:
        if registry_service is None:
            await callback.answer("功能不可用")
            return
        user_id = extract_user_id(callback)
        terminal_id_prefix = callback_parts[2]
        terminal_id, error = await _resolve_terminal_id_prefix(terminal_id_prefix, registry_service)
        if error or not terminal_id:
            await callback.answer(error or "Session not found")
            return
        ok, text = await registry_service.attach_user(user_id=user_id, terminal_id=terminal_id)
        await callback.answer(text if ok else f"❌ {text}")
        if callback.message:
            await callback.message.answer(text)

    @router.callback_query(F.data.startswith("sess:close:"))
    async def handle_session_close(callback: CallbackQuery, callback_parts: tuple[str, ...]) -> None:
        if registry_service is None:
            await callback.answer("功能不可用")
            return
        terminal_id_prefix = callback_parts[2]
        terminal_id, error = await _resolve_terminal_id_prefix(terminal_id_prefix, registry_service)
        if error or not terminal_id:
            await callback.answer(error or "Session not found")
            return
        ok = await registry_service.close_session(terminal_id)
        await callback.answer("会话已关闭" if ok else "关闭失败")
        if callback.message:
            await callback.message.answer(f"{'✅' if ok else '❌'} 会话 `{terminal_id}` {'已关闭' if ok else '关闭失败'}")
