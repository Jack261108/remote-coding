from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from app.services.external_session_binder import ExternalSessionBinder
from app.services.external_session_discovery import ExternalSessionDiscoveryService

logger = logging.getLogger(__name__)


def _resolve_session_id(
    session_id_prefix: str,
    discovery: ExternalSessionDiscoveryService,
    binder: ExternalSessionBinder,
) -> tuple[str | None, str | None]:
    """Resolve a partial session_id prefix to a full session_id.

    Searches both unbound discovery list and bound sessions.
    Returns (full_session_id, error_message).
    """
    prefix = session_id_prefix.rstrip(".")
    candidates: list[str] = []

    for s in discovery.list_unbound():
        if s.session_id == prefix or s.session_id.startswith(prefix):
            candidates.append(s.session_id)

    for b in binder._binding_store.load_all().values():
        if b.session_id == prefix or b.session_id.startswith(prefix):
            if b.session_id not in candidates:
                candidates.append(b.session_id)

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
) -> None:
    @router.callback_query(F.data.startswith("sess:select:"))
    async def handle_session_select(callback: CallbackQuery) -> None:
        user_id = callback.from_user.id if callback.from_user else 0
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
        short_id = resolved[:12]
        # Try to get cwd from discovery or binding
        cwd = ""
        unbound_session = discovery.get(resolved)
        if unbound_session:
            cwd = unbound_session.cwd
        elif binding:
            cwd = binding.cwd

        detail_text = f"📂 Session: {short_id}...\n  cwd: {cwd}"

        # Build action buttons conditionally
        sid_prefix = resolved[:16]
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
        user_id = callback.from_user.id if callback.from_user else 0
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

        result = await binder.bind(user_id=user_id, session_id=resolved)
        if result.success:
            conv_status = "✅ conversation available" if result.conversation_available else "⏳ waiting for JSONL"
            await callback.answer("绑定成功")
            if callback.message:
                await callback.message.answer(f"🔗 Bound session {resolved[:12]}...\n{conv_status}")
        else:
            await callback.answer(f"❌ {result.message}")

    @router.callback_query(F.data.startswith("sess:unbind:"))
    async def handle_session_unbind(callback: CallbackQuery) -> None:
        user_id = callback.from_user.id if callback.from_user else 0
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

        result = await binder.unbind(user_id=user_id, session_id=resolved)
        if result.success:
            await callback.answer("取消绑定成功")
            if callback.message:
                await callback.message.answer(f"🔓 Unbound session {resolved[:12]}...")
        else:
            await callback.answer(f"❌ {result.message}")
