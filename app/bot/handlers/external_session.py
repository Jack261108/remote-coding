from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from app.bot.handlers.user_utils import extract_user_id
from app.domain.models import utc_now
from app.infra.text_formatting import format_external_session_bound_message, format_external_session_unbound_message, short_id
from app.services.external_session_binder import ExternalSessionBinder
from app.services.external_session_discovery import ExternalSessionDiscoveryService
from app.services.session_id_resolver import _resolve_session_id, resolve_and_bind, resolve_and_unbind
from app.services.session_store import SessionStore

logger = logging.getLogger(__name__)


def _time_ago(dt) -> str:  # noqa: ANN001
    """Format a datetime as a human-readable 'X ago' string."""
    delta = utc_now() - dt
    total_sec = int(delta.total_seconds())
    if total_sec < 60:
        return f"{total_sec}s ago"
    minutes = total_sec // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"


def register_external_session_handler(
    router: Router,
    *,
    discovery: ExternalSessionDiscoveryService,
    binder: ExternalSessionBinder,
    session_store: SessionStore,
) -> None:
    @router.message(Command("external"))
    async def command_external(message: Message) -> None:
        user_id = extract_user_id(message)
        text = (message.text or "").strip()
        # Parse: /external <subcommand> [args]
        parts = text.split(maxsplit=2)
        # parts[0] = "/external"
        if len(parts) < 2:
            await message.answer(
                "用法:\n/external list\n/external bind <session_id>\n/external unbind <session_id>\n/external status <session_id>"
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
            ago = _time_ago(s.first_seen)
            lines.append(f"  • {short_id(s.session_id, 12)}... | {s.cwd} | first seen {ago}")

    if bound:
        lines.append("\n🔗 Your bound sessions:")
        for b in bound:
            ago = _time_ago(b.bound_at)
            lines.append(f"  • {short_id(b.session_id, 12)}... | {b.cwd} | bound {ago}")

    await message.answer("\n".join(lines))


async def _handle_bind(
    message: Message,
    *,
    user_id: int,
    session_id: str,
    binder: ExternalSessionBinder,
    discovery: ExternalSessionDiscoveryService,
) -> None:
    if not session_id:
        await message.answer("用法: /external bind <session_id>")
        return

    result = await resolve_and_bind(session_id, user_id=user_id, discovery=discovery, binder=binder)
    if result.success:
        await message.answer(format_external_session_bound_message(result.session_id, result.message))
    else:
        await message.answer(f"❌ {result.message}")


async def _handle_unbind(
    message: Message,
    *,
    user_id: int,
    session_id: str,
    binder: ExternalSessionBinder,
    discovery: ExternalSessionDiscoveryService,
) -> None:
    if not session_id:
        await message.answer("用法: /external unbind <session_id>")
        return

    result = await resolve_and_unbind(session_id, user_id=user_id, discovery=discovery, binder=binder)
    if result.success:
        await message.answer(format_external_session_unbound_message(result.session_id))
    else:
        await message.answer(f"❌ {result.message}")


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
