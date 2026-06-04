from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.handlers.user_utils import extract_user_id
from app.infra.text_formatting import short_id
from app.services.external_session_binder import ExternalSessionBinder
from app.services.external_session_discovery import ExternalSessionDiscoveryService
from app.services.session_id_resolver import _resolve_session_id, resolve_and_bind, resolve_and_unbind
from app.services.session_registry import SessionRegistryService

logger = logging.getLogger(__name__)


async def _resolve_terminal_id_prefix(
    terminal_id_prefix: str,
    registry_service: SessionRegistryService,
) -> tuple[str | None, str | None]:
    prefix = terminal_id_prefix.rstrip(".")
    candidates = [
        session.terminal_id
        for session in await registry_service.list_active_sessions()
        if session.terminal_id == prefix or session.terminal_id.startswith(prefix)
    ]
    if len(candidates) == 1:
        return candidates[0], None
    if len(candidates) == 0:
        return None, "Session not found"
    return None, f"Ambiguous prefix, {len(candidates)} matches. Be more specific."


def register_session_action_handlers(
    router: Router,
    *,
    discovery: ExternalSessionDiscoveryService,
    binder: ExternalSessionBinder,
    registry_service: SessionRegistryService | None = None,
) -> None:
    @router.callback_query(F.data.startswith("sess:select:"))
    async def handle_session_select(callback: CallbackQuery) -> None:
        user_id = extract_user_id(callback)
        data = callback.data or ""
        parts = data.split(":", 2)
        if len(parts) < 3:
            await callback.answer("Invalid callback data")
            return

        session_id_prefix = parts[2]
        resolved, error = _resolve_session_id(session_id_prefix, discovery, binder)
        if error or not resolved:
            await callback.answer(error or "Session not found")
            return

        # Determine binding state for this user
        binding = binder._binding_store.get_binding(resolved)
        is_bound_to_user = binding is not None and binding.user_id == user_id

        # Build detail message
        # Try to get cwd from discovery or binding
        cwd = ""
        unbound_session = discovery.get(resolved)
        if unbound_session:
            cwd = unbound_session.cwd
        elif binding:
            cwd = binding.cwd

        detail_text = f"📂 Session: {short_id(resolved, 12)}...\n  cwd: {cwd}"

        # Build action buttons conditionally
        sid_prefix = short_id(resolved, 16)
        if is_bound_to_user:
            buttons = [[InlineKeyboardButton(text="取消绑定", callback_data=f"sess:unbind:{sid_prefix}")]]
        else:
            buttons = [[InlineKeyboardButton(text="绑定", callback_data=f"sess:bind:{sid_prefix}")]]

        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

        await callback.answer()
        if callback.message:
            await callback.message.answer(detail_text, reply_markup=keyboard)

    @router.callback_query(F.data.startswith("sess:bind:"))
    async def handle_session_bind(callback: CallbackQuery) -> None:
        user_id = extract_user_id(callback)
        data = callback.data or ""
        parts = data.split(":", 2)
        if len(parts) < 3:
            await callback.answer("Invalid callback data")
            return

        result = await resolve_and_bind(parts[2], user_id=user_id, discovery=discovery, binder=binder)
        if result.success:
            await callback.answer("绑定成功")
            if callback.message:
                await callback.message.answer(f"🔗 Bound session {short_id(result.session_id or '', 12)}...\n{result.message}")
        else:
            await callback.answer(f"❌ {result.message}")

    @router.callback_query(F.data.startswith("sess:unbind:"))
    async def handle_session_unbind(callback: CallbackQuery) -> None:
        user_id = extract_user_id(callback)
        data = callback.data or ""
        parts = data.split(":", 2)
        if len(parts) < 3:
            await callback.answer("Invalid callback data")
            return

        result = await resolve_and_unbind(parts[2], user_id=user_id, discovery=discovery, binder=binder)
        if result.success:
            await callback.answer("取消绑定成功")
            if callback.message:
                await callback.message.answer(f"🔓 Unbound session {short_id(result.session_id or '', 12)}...")
        else:
            await callback.answer(f"❌ {result.message}")

    # ── tmux session actions ─────────────────────────────────────────────────

    @router.callback_query(F.data.startswith("sess:attach:"))
    async def handle_session_attach(callback: CallbackQuery) -> None:
        if registry_service is None:
            await callback.answer("功能不可用")
            return
        user_id = extract_user_id(callback)
        terminal_id_prefix = (callback.data or "").removeprefix("sess:attach:")
        if not terminal_id_prefix:
            await callback.answer("Invalid callback data")
            return
        terminal_id, error = await _resolve_terminal_id_prefix(terminal_id_prefix, registry_service)
        if error or not terminal_id:
            await callback.answer(error or "Session not found")
            return
        ok, text = await registry_service.attach_user(user_id=user_id, terminal_id=terminal_id)
        await callback.answer(text if ok else f"❌ {text}")
        if callback.message:
            await callback.message.answer(text)

    @router.callback_query(F.data.startswith("sess:close:"))
    async def handle_session_close(callback: CallbackQuery) -> None:
        if registry_service is None:
            await callback.answer("功能不可用")
            return
        terminal_id_prefix = (callback.data or "").removeprefix("sess:close:")
        if not terminal_id_prefix:
            await callback.answer("Invalid callback data")
            return
        terminal_id, error = await _resolve_terminal_id_prefix(terminal_id_prefix, registry_service)
        if error or not terminal_id:
            await callback.answer(error or "Session not found")
            return
        ok = await registry_service.close_session(terminal_id)
        await callback.answer("会话已关闭" if ok else "关闭失败")
        if callback.message:
            await callback.message.answer(f"{'✅' if ok else '❌'} 会话 `{terminal_id}` {'已关闭' if ok else '关闭失败'}")
