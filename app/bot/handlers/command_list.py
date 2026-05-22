from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.services.external_session_binder import ExternalSessionBinder
from app.services.external_session_discovery import ExternalSessionDiscoveryService
from app.services.session_registry import SessionRegistryService

logger = logging.getLogger(__name__)

_PHASE_ICONS = {
    "idle": "\u23f8",
    "processing": "\u2699\ufe0f",
    "waiting_for_input": "\U0001f4ac",
    "waiting_for_approval": "\U0001f510",
    "compacting": "\U0001f504",
    "ended": "\u23f9\ufe0f",
}


def _short_cwd(cwd: str) -> str:
    """Return last 2 path segments as a short display name."""
    parts = cwd.rstrip("/").split("/")
    return "/".join(parts[-2:]) if len(parts) >= 2 else cwd


def register_list_handler(
    router: Router,
    *,
    registry_service: SessionRegistryService,
    external_discovery: ExternalSessionDiscoveryService | None = None,
    external_binder: ExternalSessionBinder | None = None,
) -> None:
    @router.message(Command("list"))
    async def command_list(message: Message) -> None:
        user_id = message.from_user.id if message.from_user else 0
        sessions = await registry_service.list_active_sessions()

        # Gather external sessions if discovery service is available
        external_sessions = []
        bound_sessions = []
        if external_discovery is not None:
            external_sessions = external_discovery.list_unbound()
        if external_binder is not None:
            bound_sessions = external_binder._binding_store.get_bindings_for_user(user_id)

        if not sessions and not external_sessions and not bound_sessions:
            await message.answer("当前无活跃会话。")
            return

        lines = ["活跃会话:"]
        for s in sessions:
            icon = _PHASE_ICONS.get(s.phase, "\u2753")
            owner_tag = f" (owner:{s.owner_user_id})" if s.owner_user_id else ""
            attached = f" +{len(s.attached_user_ids)}人" if s.attached_user_ids else ""
            alive_tag = "" if s.is_alive else " [已断开]"
            lines.append(f"\n{icon} `{s.terminal_id}`{owner_tag}{attached}{alive_tag}\n   workdir: {s.workdir}\n   phase: {s.phase}")

        lines.append("\n使用 /attach <terminal_id> 连接到会话")

        # Build inline keyboard for external sessions (unbound + bound)
        buttons: list[list[InlineKeyboardButton]] = []
        if external_sessions:
            lines.append("\n📡 External sessions (unbound):")
            for ext in external_sessions:
                short = _short_cwd(ext.cwd)
                sid_tag = ext.session_id[:8]
                buttons.append(
                    [
                        InlineKeyboardButton(
                            text=f"📂 {short} ({sid_tag})",
                            callback_data=f"sess:select:{ext.session_id[:16]}",
                        )
                    ]
                )
        if bound_sessions:
            lines.append("\n🔗 Bound sessions:")
            for b in bound_sessions:
                short = _short_cwd(b.cwd)
                sid_tag = b.session_id[:8]
                buttons.append(
                    [
                        InlineKeyboardButton(
                            text=f"🔗 {short} ({sid_tag})",
                            callback_data=f"sess:select:{b.session_id[:16]}",
                        )
                    ]
                )

        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
        await message.answer("\n".join(lines), reply_markup=keyboard)
