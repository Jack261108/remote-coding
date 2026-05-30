from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.services.external_session_binder import ExternalSessionBinder
from app.services.external_session_discovery import ExternalSessionDiscoveryService
from app.services.process_liveness import process_is_alive
from app.services.session_registry import SessionRegistryService

if TYPE_CHECKING:
    from app.services.external_binding_reaper import ExternalBindingReaper

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
    liveness_enabled: bool = False,
    reaper: ExternalBindingReaper | None = None,
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

            # Liveness partition (Req 9.1-9.4, 10.3): when enabled, hide and
            # reap any binding whose pid is provably dead. Bindings with an
            # unknown pid (Pid_Known = False) fall through to existing
            # idle-TTL behavior unchanged. Probe is sub-millisecond os.kill,
            # so per-render O(n) is acceptable (Req 9.5).
            if liveness_enabled and reaper is not None:
                visible = []
                dead_ids: list[str] = []
                for b in bound_sessions:
                    pid = b.pid
                    if pid is not None and pid > 0:
                        if not process_is_alive(pid):
                            dead_ids.append(b.session_id)
                        else:
                            visible.append(b)
                    else:
                        visible.append(b)
                bound_sessions = visible
                for session_id in dead_ids:
                    try:
                        await reaper.remove_with_cleanup(session_id, reason="pid_dead")
                    except Exception:
                        # Per-binding failure must not break /list rendering.
                        logger.warning(
                            "failed to reap dead binding during /list",
                            extra={"session_id": session_id},
                            exc_info=True,
                        )

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
                if ext.title:
                    btn_text = f"💬 {ext.title} ({sid_tag})"
                else:
                    btn_text = f"📂 {short} ({sid_tag})"
                if len(btn_text) > 64:
                    btn_text = btn_text[:63] + "…"
                buttons.append(
                    [
                        InlineKeyboardButton(
                            text=btn_text,
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
