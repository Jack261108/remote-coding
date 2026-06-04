from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from html import escape

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

_RECENT_LIMIT = 3
_SID_PREFIX_LEN = 16
_DISPLAY_ID_LEN = 8
_TITLE_MAX_CHARS = 36


class ListSessionSource(StrEnum):
    BOUND = "bound"
    TMUX = "tmux"
    UNBOUND = "unbound"


@dataclass(frozen=True, slots=True)
class ListSessionView:
    session_id: str
    title: str | None
    cwd: str
    source: ListSessionSource
    state: str
    activity_at: datetime


@dataclass(frozen=True, slots=True)
class SessionListRenderResult:
    text: str
    keyboard: InlineKeyboardMarkup | None


def build_session_list_message(items: Sequence[ListSessionView], *, now: datetime) -> SessionListRenderResult:
    now_utc = _ensure_aware_utc(now)
    all_items = list(items)
    if not all_items:
        return SessionListRenderResult(text="当前无活跃会话。", keyboard=None)

    recent = sorted(
        (item for item in all_items if item.source == ListSessionSource.BOUND),
        key=lambda item: _activity_timestamp(item.activity_at),
        reverse=True,
    )[:_RECENT_LIMIT]
    recent_ids = {item.session_id for item in recent}

    attention = sorted(
        (item for item in all_items if item.session_id not in recent_ids and _needs_attention(item)),
        key=lambda item: (_attention_priority(item), -_activity_timestamp(item.activity_at)),
    )
    attention_ids = {item.session_id for item in attention}
    hidden_count = sum(1 for item in all_items if item.session_id not in recent_ids and item.session_id not in attention_ids)

    parts: list[str] = ["📋 <b>会话</b>"]
    buttons: list[list[InlineKeyboardButton]] = []

    if recent:
        parts.extend(["", "🚀 <b>最近可继续</b>"])
        recent_buttons: list[InlineKeyboardButton] = []
        for index, item in enumerate(recent, start=1):
            title = _html(_truncate(_display_title(item), _TITLE_MAX_CHARS))
            cwd = _html(_short_cwd(item.cwd))
            relative = _relative_time(item.activity_at, now_utc)
            parts.append(f"{index}. 🔗 {title}")
            parts.append(f"   {cwd} · {relative} · 已绑定")
            recent_buttons.append(
                InlineKeyboardButton(
                    text=f"{index} 继续",
                    callback_data=f"sess:select:{_sid_prefix(item)}",
                )
            )
        buttons.append(recent_buttons)

    if attention:
        parts.extend(["", "⚠️ <b>需要处理</b>"])
        for item in attention:
            label = _attention_label(item)
            icon = _attention_icon(item)
            cwd = _html(_short_cwd(item.cwd))
            sid = _html(_display_sid(item))
            relative = _relative_time(item.activity_at, now_utc)
            parts.append(f"{icon} {label} · {cwd} · <code>{sid}</code> · {relative}")
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=_attention_button_text(item),
                        callback_data=_attention_callback_data(item),
                    )
                ]
            )

    if hidden_count > 0:
        parts.extend(["", "📦 <b>其他</b>", f"还有 {hidden_count} 个旧会话未显示"])
        buttons.append([InlineKeyboardButton(text="查看全部", callback_data="sess:list:all")])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
    return SessionListRenderResult(text="\n".join(parts), keyboard=keyboard)


def _needs_attention(item: ListSessionView) -> bool:
    return item.source == ListSessionSource.UNBOUND or item.state in {
        "waiting_for_approval",
        "waiting_for_input",
        "processing",
    }


def _attention_priority(item: ListSessionView) -> int:
    if item.state == "waiting_for_approval":
        return 0
    if item.state == "waiting_for_input":
        return 1
    if item.state == "processing":
        return 2
    if item.source == ListSessionSource.UNBOUND:
        return 3
    return 4


def _attention_label(item: ListSessionView) -> str:
    if item.state == "waiting_for_approval":
        return "等待审批"
    if item.state == "waiting_for_input":
        return "等待输入"
    if item.state == "processing":
        return "正在处理"
    if item.source == ListSessionSource.UNBOUND:
        return "可绑定新会话"
    return "需要处理"


def _attention_icon(item: ListSessionView) -> str:
    if item.state == "waiting_for_approval":
        return "🔐"
    if item.state == "waiting_for_input":
        return "💬"
    if item.state == "processing":
        return "⚙️"
    if item.source == ListSessionSource.UNBOUND:
        return "📡"
    return "⚠️"


def _attention_button_text(item: ListSessionView) -> str:
    sid = _display_sid(item)
    if item.state == "waiting_for_approval":
        return f"处理审批 {sid}"
    if item.state == "waiting_for_input":
        return f"继续输入 {sid}"
    if item.source == ListSessionSource.UNBOUND:
        return f"绑定 {sid}"
    return f"查看 {sid}"


def _attention_callback_data(item: ListSessionView) -> str:
    action = "bind" if item.source == ListSessionSource.UNBOUND else "select"
    return f"sess:{action}:{_sid_prefix(item)}"


def _display_title(item: ListSessionView) -> str:
    title = (item.title or "").strip()
    return title or _short_cwd(item.cwd)


def _short_cwd(cwd: str) -> str:
    stripped = cwd.rstrip("/")
    if not stripped:
        return "unknown"
    parts = stripped.split("/")
    return "/".join(parts[-2:]) if len(parts) >= 2 else stripped


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1] + "…"


def _relative_time(activity_at: datetime, now: datetime) -> str:
    delta_sec = max(0, int((now - _ensure_aware_utc(activity_at)).total_seconds()))
    if delta_sec < 60:
        return "刚刚"
    minutes = delta_sec // 60
    if minutes < 60:
        return f"{minutes} 分钟前"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} 小时前"
    days = hours // 24
    if days == 1:
        return "昨天"
    return f"{days} 天前"


def _activity_timestamp(value: datetime) -> float:
    return _ensure_aware_utc(value).timestamp()


def _ensure_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _sid_prefix(item: ListSessionView) -> str:
    return item.session_id[:_SID_PREFIX_LEN]


def _display_sid(item: ListSessionView) -> str:
    return item.session_id[:_DISPLAY_ID_LEN]


def _html(text: str) -> str:
    return escape(text, quote=False)
