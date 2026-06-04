from __future__ import annotations

import logging
from collections.abc import Callable
from html import escape
from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.bot.handlers.user_utils import extract_user_id
from app.bot.session_list_renderer import ListSessionSource, ListSessionView, build_session_list_message
from app.domain.models import SessionListItem, utc_now
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
    "waiting_for_input": "💬",
    "waiting_for_approval": "🔐",
    "compacting": "🔄",
    "ended": "⏹️",
}


def _short_cwd(cwd: str) -> str:
    """Return last 2 path segments as a short display name."""
    parts = cwd.rstrip("/").split("/")
    return "/".join(parts[-2:]) if len(parts) >= 2 else cwd


def _html(text: str) -> str:
    return escape(text, quote=False)


def _render_full_list(items: list[SessionListItem]) -> tuple[str, InlineKeyboardMarkup | None]:
    if not items:
        return "当前无活跃会话。", None

    parts: list[str] = ["📋 <b>活跃会话</b>\n"]
    buttons: list[list[InlineKeyboardButton]] = []
    for item in items:
        short_cwd = _html(_short_cwd(item.cwd))
        sid_short = _html(item.session_id[:8])
        status_text = _html(item.status_text)
        parts.append(f"{item.status_icon} <b>{short_cwd}</b>")
        parts.append(f"   <code>{sid_short}</code> · {status_text}")
        buttons.append([InlineKeyboardButton(text=btn_text, callback_data=cb_data) for btn_text, cb_data in item.buttons])
        parts.append("")

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
    return "\n".join(parts), keyboard


def register_list_handler(
    router: Router,
    *,
    registry_service: SessionRegistryService,
    external_discovery: ExternalSessionDiscoveryService | None = None,
    external_binder: ExternalSessionBinder | None = None,
    liveness_enabled: bool = False,
    reaper: ExternalBindingReaper | None = None,
    title_resolver: Callable[[str, str], str | None] | None = None,
) -> None:
    async def collect_items(user_id: int) -> tuple[list[SessionListItem], list[ListSessionView]]:
        now = utc_now()
        legacy_items: list[SessionListItem] = []
        summary_items: list[ListSessionView] = []

        sessions = await registry_service.list_active_sessions()
        for s in sessions:
            icon = _PHASE_ICONS.get(s.phase, "❓")
            tags: list[str] = [s.phase]
            if s.owner_user_id:
                tags.append(f"owner:{s.owner_user_id}")
            if s.attached_user_ids:
                tags.append(f"+{len(s.attached_user_ids)}人")
            if not s.is_alive:
                tags.append("已断开")
            sid = s.terminal_id
            legacy_items.append(
                SessionListItem(
                    session_id=sid,
                    cwd=s.workdir,
                    status_icon=icon,
                    status_text=" · ".join(tags),
                    source="tmux",
                    buttons=[
                        ("🔗 绑定", f"sess:attach:{sid[:16]}"),
                        ("❌ 关闭", f"sess:close:{sid[:16]}"),
                    ],
                )
            )
            summary_items.append(
                ListSessionView(
                    session_id=sid,
                    title=None,
                    cwd=s.workdir,
                    source=ListSessionSource.TMUX,
                    state=s.phase,
                    activity_at=s.last_activity or now,
                )
            )

        external_sessions = []
        if external_discovery is not None:
            external_sessions = external_discovery.list_unbound()
        for ext in external_sessions:
            status = ext.title or "未绑定"
            legacy_items.append(
                SessionListItem(
                    session_id=ext.session_id,
                    cwd=ext.cwd,
                    status_icon="📡",
                    status_text=status,
                    source="external",
                    buttons=[(ext.title or _short_cwd(ext.cwd), f"sess:select:{ext.session_id[:16]}")],
                )
            )
            summary_items.append(
                ListSessionView(
                    session_id=ext.session_id,
                    title=ext.title,
                    cwd=ext.cwd,
                    source=ListSessionSource.UNBOUND,
                    state="unbound",
                    activity_at=ext.last_seen,
                )
            )

        bound_sessions = []
        if external_binder is not None:
            bound_sessions = external_binder._binding_store.get_bindings_for_user(user_id)

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
            title = b.title
            if title is None and title_resolver is not None:
                try:
                    title = title_resolver(b.session_id, b.cwd)
                    if title is not None and external_binder is not None:
                        b.title = title
                        external_binder._binding_store.save_binding(b)
                except Exception:
                    logger.debug("title resolver failed for bound session", extra={"session_id": b.session_id})
            legacy_items.append(
                SessionListItem(
                    session_id=b.session_id,
                    cwd=b.cwd,
                    status_icon="🔗",
                    status_text="已绑定",
                    source="bound",
                    buttons=[(title or _short_cwd(b.cwd), f"sess:select:{b.session_id[:16]}")],
                )
            )
            summary_items.append(
                ListSessionView(
                    session_id=b.session_id,
                    title=title,
                    cwd=b.cwd,
                    source=ListSessionSource.BOUND,
                    state="bound",
                    activity_at=b.last_activity_at,
                )
            )

        return legacy_items, summary_items

    @router.message(Command("list"))
    async def command_list(message: Message) -> None:
        user_id = extract_user_id(message)
        _, summary_items = await collect_items(user_id)
        result = build_session_list_message(summary_items, now=utc_now())
        await message.answer(result.text, parse_mode="HTML", reply_markup=result.keyboard)

    @router.callback_query(F.data == "sess:list:all")
    async def handle_list_all(callback: CallbackQuery) -> None:
        user_id = extract_user_id(callback)
        legacy_items, _ = await collect_items(user_id)
        text, keyboard = _render_full_list(legacy_items)
        await callback.answer()
        if callback.message:
            await callback.message.answer(text, parse_mode="HTML", reply_markup=keyboard)
