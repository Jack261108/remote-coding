from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.bot.handlers.user_utils import extract_user_id
from app.domain.models import SessionListItem
from app.services.external_session_binder import ExternalSessionBinder
from app.services.external_session_discovery import ExternalSessionDiscoveryService
from app.services.process_liveness import process_is_alive
from app.services.session_registry import SessionRegistryService

if TYPE_CHECKING:
    from app.services.external_binding_reaper import ExternalBindingReaper

logger = logging.getLogger(__name__)

_PHASE_ICONS = {
    "idle": "⏸",
    "processing": "⚙️",
    "waiting_for_input": "\U0001f4ac",
    "waiting_for_approval": "\U0001f510",
    "compacting": "\U0001f504",
    "ended": "⏹️",
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
        user_id = extract_user_id(message)

        # ── collect all session types ────────────────────────────────────────
        items: list[SessionListItem] = []

        # 1. tmux sessions
        sessions = await registry_service.list_active_sessions()
        for s in sessions:
            icon = _PHASE_ICONS.get(s.phase, "❓")
            owner_tag = f" (owner:{s.owner_user_id})" if s.owner_user_id else ""
            attached = f" +{len(s.attached_user_ids)}人" if s.attached_user_ids else ""
            alive_tag = "" if s.is_alive else " [已断开]"
            sid = s.terminal_id
            label = sid if len(sid) <= 20 else sid[:18] + "…"
            items.append(
                SessionListItem(
                    session_id=sid,
                    cwd=s.workdir,
                    status_icon=icon,
                    status_text=f"{s.phase}{owner_tag}{attached}{alive_tag}",
                    source="tmux",
                    buttons=[
                        (f"🔗 绑定 {label}", f"sess:attach:{sid[:16]}"),
                        (f"❌ 关闭 {label}", f"sess:close:{sid[:16]}"),
                    ],
                )
            )

        # 2. external unbound sessions
        external_sessions = []
        if external_discovery is not None:
            external_sessions = external_discovery.list_unbound()
        for ext in external_sessions:
            short = _short_cwd(ext.cwd)
            sid_tag = ext.session_id[:8]
            label = f"{ext.title} ({sid_tag})" if ext.title else f"{short} ({sid_tag})"
            if len(label) > 60:
                label = label[:59] + "…"
            items.append(
                SessionListItem(
                    session_id=ext.session_id,
                    cwd=ext.cwd,
                    status_icon="\U0001f4e1",
                    status_text="external",
                    source="external",
                    buttons=[(f"📋 {label}", f"sess:select:{ext.session_id[:16]}")],
                )
            )

        # 3. bound sessions
        bound_sessions = []
        if external_binder is not None:
            bound_sessions = external_binder._binding_store.get_bindings_for_user(user_id)

            # Liveness partition: reap dead bindings
            if liveness_enabled and reaper is not None:
                visible = []
                dead_ids: list[str] = []
                for binding in bound_sessions:
                    pid = binding.pid
                    if pid is not None and pid > 0 and not process_is_alive(pid):
                        dead_ids.append(binding.session_id)
                        continue
                    visible.append(binding)
                bound_sessions = visible
                for session_id in dead_ids:
                    try:
                        await reaper.remove_with_cleanup(session_id, reason="pid_dead")
                    except Exception:
                        logger.warning(
                            "failed to reap dead binding during /list",
                            extra={"session_id": session_id},
                            exc_info=True,
                        )

        for b in bound_sessions:
            short = _short_cwd(b.cwd)
            sid_tag = b.session_id[:8]
            label = f"{short} ({sid_tag})"
            items.append(
                SessionListItem(
                    session_id=b.session_id,
                    cwd=b.cwd,
                    status_icon="\U0001f517",
                    status_text="bound",
                    source="bound",
                    buttons=[(f"📋 {label}", f"sess:select:{b.session_id[:16]}")],
                )
            )

        # ── render ───────────────────────────────────────────────────────────
        if not items:
            await message.answer("当前无活跃会话。")
            return

        lines = ["活跃会话:"]
        buttons: list[list[InlineKeyboardButton]] = []
        for item in items:
            lines.append(f"\n{item.status_icon} `{item.session_id}`\n   {item.cwd} · {item.status_text}")
            buttons.append([InlineKeyboardButton(text=btn_text, callback_data=cb_data) for btn_text, cb_data in item.buttons])

        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
        await message.answer("\n".join(lines), reply_markup=keyboard)
