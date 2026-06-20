from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.bot.handlers.user_utils import extract_user_id
from app.bot.session_list_renderer import ListSessionSource, ListSessionView, build_session_list_message
from app.domain.models import SessionListItem, utc_now
from app.infra.text_formatting import html_escape, short_cwd
from app.services.external_session_binder import ExternalSessionBinder
from app.services.external_session_discovery import ExternalSessionDiscoveryService
from app.services.process_liveness import process_is_alive
from app.services.session_id_resolver import unique_prefixes
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


def _is_dead_pid(pid: int | None, *, session_id: str, source: str) -> bool:
    if pid is None or pid <= 0:
        return False
    try:
        return not process_is_alive(pid)
    except Exception:
        logger.warning(
            "failed to check external session pid during /list",
            extra={"session_id": session_id, "pid": pid, "source": source},
            exc_info=True,
        )
        return False


def _render_full_list(items: list[SessionListItem]) -> tuple[str, InlineKeyboardMarkup | None]:
    if not items:
        return "当前无活跃会话。", None

    parts: list[str] = ["📋 <b>活跃会话</b>\n"]
    buttons: list[list[InlineKeyboardButton]] = []
    for item in items:
        cwd_label = html_escape(short_cwd(item.cwd, fallback=""))
        sid_short = html_escape(item.session_id[:8])
        status_text = html_escape(item.status_text)
        parts.append(f"{item.status_icon} <b>{cwd_label}</b>")
        parts.append(f"   <code>{sid_short}</code> · {status_text}")
        buttons.append([InlineKeyboardButton(text=btn_text, callback_data=cb_data) for btn_text, cb_data in item.buttons])
        parts.append("")

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
    return "\n".join(parts), keyboard


def _collect_tmux_items(
    sessions: list,
    now: datetime,
) -> tuple[list[SessionListItem], list[ListSessionView], int]:
    """Collect tmux session items. Returns (legacy_items, summary_items, invalid_count)."""
    legacy_items: list[SessionListItem] = []
    summary_items: list[ListSessionView] = []
    invalid_count = 0
    tmux_prefixes = unique_prefixes((s.terminal_id for s in sessions if s.is_alive), min_length=16)
    for s in sessions:
        icon = _PHASE_ICONS.get(s.phase, "❓")
        tags: list[str] = [s.phase]
        if s.owner_user_id:
            tags.append(f"owner:{s.owner_user_id}")
        if s.attached_user_ids:
            tags.append(f"+{len(s.attached_user_ids)}人")
        if not s.is_alive:
            invalid_count += 1
            continue  # 跳过已断开的 tmux sessions，不显示
        sid = s.terminal_id
        sid_prefix = tmux_prefixes[sid]
        legacy_items.append(
            SessionListItem(
                session_id=sid,
                cwd=s.workdir,
                status_icon=icon,
                status_text=" · ".join(tags),
                source="tmux",
                buttons=[
                    ("🔗 绑定", f"sess:attach:{sid_prefix}"),
                    ("❌ 关闭", f"sess:close:{sid_prefix}"),
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
    return legacy_items, summary_items, invalid_count


def _collect_external_items(
    external_sessions: list,
    *,
    external_discovery: ExternalSessionDiscoveryService | None,
    external_prefixes: dict[str, str],
) -> tuple[list[SessionListItem], list[ListSessionView], int]:
    """Collect external unbound session items. Returns (legacy_items, summary_items, invalid_count)."""
    legacy_items: list[SessionListItem] = []
    summary_items: list[ListSessionView] = []
    invalid_count = 0
    for ext in external_sessions:
        # 检测 stale unbound sessions（pid 已死或基于时间）
        is_dead_pid = _is_dead_pid(ext.pid, session_id=ext.session_id, source="unbound")
        is_stale_time = (
            external_discovery.is_session_stale(ext.session_id)
            if external_discovery is not None and hasattr(external_discovery, "is_session_stale")
            else False
        )
        if is_dead_pid or is_stale_time:
            invalid_count += 1
            continue  # 跳过 stale sessions，不显示
        status = ext.title or "未绑定"
        legacy_items.append(
            SessionListItem(
                session_id=ext.session_id,
                cwd=ext.cwd,
                status_icon="📡",
                status_text=status,
                source="external",
                buttons=[(ext.title or short_cwd(ext.cwd, fallback=""), f"sess:select:{external_prefixes[ext.session_id]}")],
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
    return legacy_items, summary_items, invalid_count


async def _collect_bound_items(
    bound_sessions: list,
    *,
    external_binder: ExternalSessionBinder | None,
    liveness_enabled: bool,
    reaper: ExternalBindingReaper | None,
    title_resolver: Callable[[str, str], str | None] | None,
    external_prefixes: dict[str, str],
) -> tuple[list[SessionListItem], list[ListSessionView], int]:
    """Collect bound session items. Returns (legacy_items, summary_items, invalid_count)."""
    legacy_items: list[SessionListItem] = []
    summary_items: list[ListSessionView] = []
    invalid_count = 0

    if liveness_enabled and reaper is not None:
        visible = []
        dead_ids: list[str] = []
        for binding in bound_sessions:
            if _is_dead_pid(binding.pid, session_id=binding.session_id, source="bound"):
                dead_ids.append(binding.session_id)
                invalid_count += 1
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
                buttons=[(title or short_cwd(b.cwd, fallback=""), f"sess:select:{external_prefixes[b.session_id]}")],
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
    return legacy_items, summary_items, invalid_count


async def _cleanup_dead_sessions(
    *,
    user_id: int,
    external_binder: ExternalSessionBinder | None,
    liveness_enabled: bool,
    reaper: ExternalBindingReaper | None,
    registry_service: SessionRegistryService,
    external_discovery: ExternalSessionDiscoveryService | None,
    dead_unbound_cleanup: Callable[[str], Awaitable[object]] | None,
) -> int:
    """Clean up dead/stale sessions. Returns count of cleaned sessions."""
    cleaned = 0

    # 1. 清理 dead pid binding
    if external_binder is not None and liveness_enabled and reaper is not None:
        bound_sessions = external_binder._binding_store.get_bindings_for_user(user_id)
        for binding in bound_sessions:
            if _is_dead_pid(binding.pid, session_id=binding.session_id, source="bound"):
                try:
                    if await reaper.remove_with_cleanup(binding.session_id, reason="pid_dead"):
                        cleaned += 1
                except Exception:
                    logger.warning("cleanup failed for binding", extra={"session_id": binding.session_id})

    # 2. 清理 dead tmux session
    sessions = await registry_service.list_active_sessions()
    for s in sessions:
        if not s.is_alive:
            try:
                if await registry_service.close_session(s.terminal_id):
                    cleaned += 1
            except Exception:
                logger.warning("cleanup failed for tmux session", extra={"terminal_id": s.terminal_id})

    # 3. 清理 stale unbound sessions（pid 已死的）
    if external_discovery is not None:
        dead_unbound_ids = external_discovery.prune_dead()
        for session_id in dead_unbound_ids:
            if dead_unbound_cleanup is not None:
                try:
                    await dead_unbound_cleanup(session_id)
                except Exception:
                    logger.warning("cleanup failed for unbound session", extra={"session_id": session_id}, exc_info=True)
            cleaned += 1
        # 清理基于时间的 stale sessions
        stale_ids = external_discovery.prune_stale()
        cleaned += len(stale_ids)

    return cleaned


def register_list_handler(
    router: Router,
    *,
    registry_service: SessionRegistryService,
    external_discovery: ExternalSessionDiscoveryService | None = None,
    external_binder: ExternalSessionBinder | None = None,
    liveness_enabled: bool = False,
    reaper: ExternalBindingReaper | None = None,
    title_resolver: Callable[[str, str], str | None] | None = None,
    dead_unbound_cleanup: Callable[[str], Awaitable[object]] | None = None,
) -> None:
    async def collect_items(user_id: int) -> tuple[list[SessionListItem], list[ListSessionView], int, list[str]]:
        now = utc_now()
        legacy_items: list[SessionListItem] = []
        summary_items: list[ListSessionView] = []
        invalid_count = 0

        # Tmux sessions
        sessions = await registry_service.list_active_sessions()
        tmux_legacy, tmux_summary, tmux_invalid = _collect_tmux_items(sessions, now)
        legacy_items.extend(tmux_legacy)
        summary_items.extend(tmux_summary)
        invalid_count += tmux_invalid

        # External token IDs for prefix resolution
        external_sessions = []
        if external_discovery is not None:
            external_sessions = external_discovery.list_unbound()
        external_token_ids = [ext.session_id for ext in external_sessions]
        if external_binder is not None:
            external_token_ids.extend(binding.session_id for binding in external_binder._binding_store.list_all())
        if external_discovery is not None:
            external_token_ids.extend(external_discovery.unavailable_session_ids())
        external_prefixes = unique_prefixes(external_token_ids, min_length=16, max_length=52)

        # External unbound sessions
        ext_legacy, ext_summary, ext_invalid = _collect_external_items(
            external_sessions,
            external_discovery=external_discovery,
            external_prefixes=external_prefixes,
        )
        legacy_items.extend(ext_legacy)
        summary_items.extend(ext_summary)
        invalid_count += ext_invalid

        # Bound sessions
        bound_sessions = []
        if external_binder is not None:
            bound_sessions = external_binder._binding_store.get_bindings_for_user(user_id)

        bound_legacy, bound_summary, bound_invalid = await _collect_bound_items(
            bound_sessions,
            external_binder=external_binder,
            liveness_enabled=liveness_enabled,
            reaper=reaper,
            title_resolver=title_resolver,
            external_prefixes=external_prefixes,
        )
        legacy_items.extend(bound_legacy)
        summary_items.extend(bound_summary)
        invalid_count += bound_invalid

        return legacy_items, summary_items, invalid_count, external_token_ids

    @router.message(Command("list"))
    async def command_list(message: Message) -> None:
        user_id = extract_user_id(message)
        _, summary_items, invalid_count, external_token_ids = await collect_items(user_id)
        result = build_session_list_message(
            summary_items,
            now=utc_now(),
            has_invalid_sessions=invalid_count > 0,
            external_token_ids=external_token_ids,
        )
        await message.answer(result.text, parse_mode="HTML", reply_markup=result.keyboard)

    @router.callback_query(F.data == "sess:list:all")
    async def handle_list_all(callback: CallbackQuery) -> None:
        user_id = extract_user_id(callback)
        legacy_items, _, _, _ = await collect_items(user_id)
        text, keyboard = _render_full_list(legacy_items)
        await callback.answer()
        if callback.message:
            await callback.message.answer(text, parse_mode="HTML", reply_markup=keyboard)

    @router.callback_query(F.data == "sess:cleanup")
    async def handle_cleanup(callback: CallbackQuery) -> None:
        user_id = extract_user_id(callback)
        cleaned = await _cleanup_dead_sessions(
            user_id=user_id,
            external_binder=external_binder,
            liveness_enabled=liveness_enabled,
            reaper=reaper,
            registry_service=registry_service,
            external_discovery=external_discovery,
            dead_unbound_cleanup=dead_unbound_cleanup,
        )
        await callback.answer(f"已清理 {cleaned} 个无效会话")

        # 刷新摘要视图
        if callback.message:
            _, summary_items, invalid_count, external_token_ids = await collect_items(user_id)
            result = build_session_list_message(
                summary_items,
                now=utc_now(),
                has_invalid_sessions=invalid_count > 0,
                external_token_ids=external_token_ids,
            )
            await callback.message.answer(result.text, parse_mode="HTML", reply_markup=result.keyboard)
