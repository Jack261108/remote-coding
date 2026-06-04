# `/list` 会话摘要显示 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 Telegram bot 的 `/list` 从平铺会话列表改为“最近 3 个已绑定会话优先 + 需要处理项可见 + 查看全部兜底”的摘要视图。

**Architecture:** 新增 `app/bot/session_list_renderer.py` 作为纯渲染层，接收统一 view model 并输出 Telegram HTML 与 inline keyboard。`app/bot/handlers/command_list.py` 只负责收集 tmux、未绑定外部会话、已绑定外部会话，转换为 renderer 输入，并保留旧版完整列表作为 `sess:list:all` callback 的兜底视图。

**Tech Stack:** Python 3.11+、aiogram inline keyboard、pytest、Hypothesis property tests、现有 pyenv/pyenv-virtualenv 项目环境。

---

## File Structure

- Create: `app/bot/session_list_renderer.py`
  - 纯函数渲染模块。
  - 定义 `ListSessionSource`、`ListSessionView`、`SessionListRenderResult`。
  - 提供 `build_session_list_message(items, now=...)`。
  - 负责分区、排序、HTML 转义、相对时间和按钮生成。

- Modify: `app/domain/models.py`
  - 给 `TerminalSessionInfo` 增加 `last_activity: datetime | None = None`，让 tmux 会话也能传递活跃时间。

- Modify: `app/services/session_registry.py`
  - 在 `list_active_sessions()` 和 `get_session_info()` 构造 `TerminalSessionInfo` 时填充 `last_activity=state.last_activity if state else None`。

- Modify: `app/bot/handlers/command_list.py`
  - 保留现有收集逻辑和 liveness 清理逻辑。
  - 增加统一 view model 转换。
  - `/list` 默认调用新 renderer。
  - 旧版完整列表抽成 `_render_full_list()`。
  - 增加 `sess:list:all` callback，输出完整列表兜底视图。

- Create: `tests/test_session_list_renderer.py`
  - 覆盖 renderer 的排序、分区、隐藏数量、HTML 转义和 callback。

- Modify: `tests/test_command_list.py`
  - 覆盖 tmux `last_activity` 透传。
  - 覆盖 `/list` 摘要视图。
  - 覆盖 `sess:list:all` 完整列表兜底。

- Modify: `tests/property/test_external_list_liveness_properties.py`
  - 调整旧断言：摘要视图只渲染最多 3 个 bound select 按钮，因此不再要求所有可见 binding 都出现在 `/list` 首页按钮中。
  - 仍要求 dead pid binding 不进入摘要按钮，并且 liveness reaper 调用保持精确。

---

## Task 0: Preflight

**Files:**
- No file changes.

- [ ] **Step 1: Confirm git state is clean before code changes**

Run:

```bash
git -C /Users/jack/project/remote-coding status --short
```

Expected: no output. If there is output, stop and ask the user whether to commit, stash, or inspect those changes before proceeding.

- [ ] **Step 2: Confirm the Python environment is pyenv-managed before pytest**

Run:

```bash
cd /Users/jack/project/remote-coding && pyenv version && python -c "import sys; print(sys.executable)"
```

Expected: `pyenv version` reports a project-bound pyenv/pyenv-virtualenv environment. `sys.executable` must point inside a pyenv-managed path. If the project is not bound to a pyenv virtualenv, stop and use the `pyenv-virtualenv` skill before running Python tests.

---

## Task 1: Add renderer tests

**Files:**
- Create: `tests/test_session_list_renderer.py`

- [ ] **Step 1: Write the failing renderer tests**

Create `tests/test_session_list_renderer.py` with this complete content:

```python
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.bot.session_list_renderer import (
    ListSessionSource,
    ListSessionView,
    build_session_list_message,
)

NOW = datetime(2026, 6, 4, 12, 0, tzinfo=UTC)


def _at(minutes_ago: int) -> datetime:
    return NOW - timedelta(minutes=minutes_ago)


def _item(
    session_id: str,
    title: str | None,
    minutes_ago: int,
    *,
    source: ListSessionSource = ListSessionSource.BOUND,
    state: str = "bound",
    cwd: str = "/Users/jack/project/remote-coding",
) -> ListSessionView:
    return ListSessionView(
        session_id=session_id,
        title=title,
        cwd=cwd,
        source=source,
        state=state,
        activity_at=_at(minutes_ago),
    )


def _callbacks(result) -> list[str]:
    assert result.keyboard is not None
    return [button.callback_data or "" for row in result.keyboard.inline_keyboard for button in row]


def test_recent_bound_sessions_show_top_three_and_hide_the_rest() -> None:
    result = build_session_list_message(
        [
            _item("sess-newest-0001", "Newest", 1),
            _item("sess-second-0002", "Second", 2),
            _item("sess-third-0003", "Third", 3),
            _item("sess-hidden-a004", "Hidden A", 4),
            _item("sess-hidden-b005", "Hidden B", 5),
        ],
        now=NOW,
    )

    assert "🚀 <b>最近可继续</b>" in result.text
    assert "1. 🔗 Newest" in result.text
    assert "2. 🔗 Second" in result.text
    assert "3. 🔗 Third" in result.text
    assert "Hidden A" not in result.text
    assert "Hidden B" not in result.text
    assert "还有 2 个旧会话未显示" in result.text

    assert result.keyboard is not None
    first_row = result.keyboard.inline_keyboard[0]
    assert [button.text for button in first_row] == ["1 继续", "2 继续", "3 继续"]
    assert [button.callback_data for button in first_row] == [
        "sess:select:sess-newest-0001",
        "sess:select:sess-second-0002",
        "sess:select:sess-third-0003",
    ]
    assert _callbacks(result)[-1] == "sess:list:all"


def test_unbound_session_stays_in_attention_even_when_newer_than_bound() -> None:
    result = build_session_list_message(
        [
            _item("sess-bound-old01", "Bound old", 30),
            _item(
                "unbound-session-0001",
                None,
                1,
                source=ListSessionSource.UNBOUND,
                state="unbound",
                cwd="/Users/jack/project/new-app",
            ),
        ],
        now=NOW,
    )

    assert "🚀 <b>最近可继续</b>" in result.text
    assert "Bound old" in result.text
    assert "⚠️ <b>需要处理</b>" in result.text
    assert "📡 可绑定新会话" in result.text
    assert "project/new-app" in result.text
    assert "sess:bind:unbound-session-" in _callbacks(result)


def test_attention_items_sort_by_priority_before_activity_time() -> None:
    result = build_session_list_message(
        [
            _item("tmux-processing01", None, 1, source=ListSessionSource.TMUX, state="processing"),
            _item("tmux-input00002", None, 10, source=ListSessionSource.TMUX, state="waiting_for_input"),
            _item("tmux-approval03", None, 20, source=ListSessionSource.TMUX, state="waiting_for_approval"),
            _item("unbound-session-0002", None, 0, source=ListSessionSource.UNBOUND, state="unbound"),
        ],
        now=NOW,
    )

    assert result.text.index("等待审批") < result.text.index("等待输入")
    assert result.text.index("等待输入") < result.text.index("正在处理")
    assert result.text.index("正在处理") < result.text.index("可绑定新会话")


def test_html_escapes_title_and_cwd() -> None:
    result = build_session_list_message(
        [
            _item(
                "sess-html-000001",
                "A <B> & C",
                1,
                cwd="/Users/jack/project/a&b",
            )
        ],
        now=NOW,
    )

    assert "A &lt;B&gt; &amp; C" in result.text
    assert "project/a&amp;b" in result.text
    assert "A <B> & C" not in result.text


def test_empty_list_returns_no_active_sessions_message() -> None:
    result = build_session_list_message([], now=NOW)

    assert result.text == "当前无活跃会话。"
    assert result.keyboard is None
```

- [ ] **Step 2: Run renderer tests to verify they fail before implementation**

Run:

```bash
cd /Users/jack/project/remote-coding && pytest tests/test_session_list_renderer.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'app.bot.session_list_renderer'`.

---

## Task 2: Implement the session list renderer

**Files:**
- Create: `app/bot/session_list_renderer.py`
- Test: `tests/test_session_list_renderer.py`

- [ ] **Step 1: Create the renderer module**

Create `app/bot/session_list_renderer.py` with this complete content:

```python
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
```

- [ ] **Step 2: Run renderer tests**

Run:

```bash
cd /Users/jack/project/remote-coding && pytest tests/test_session_list_renderer.py -q
```

Expected: PASS.

- [ ] **Step 3: Commit renderer and tests**

Run:

```bash
git -C /Users/jack/project/remote-coding add app/bot/session_list_renderer.py tests/test_session_list_renderer.py
git -C /Users/jack/project/remote-coding commit -m "feat: add /list session summary renderer"
```

Expected: commit succeeds.

---

## Task 3: Pass tmux last activity through the registry

**Files:**
- Modify: `app/domain/models.py`
- Modify: `app/services/session_registry.py`
- Modify: `tests/test_command_list.py`

- [ ] **Step 1: Add a failing test for `TerminalSessionInfo.last_activity`**

Modify `tests/test_command_list.py` imports:

```python
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from aiogram import Router
```

Replace the body of `test_list_shows_active_session` with this version:

```python
@pytest.mark.asyncio
async def test_list_shows_active_session(tmp_path) -> None:
    registry, session_service, cache = _setup(tmp_path, alive_sessions={"tgcli_user_1_abc123"})
    await session_service.switch(
        user_id=1,
        provider="claude_code",
        workdir="/proj",
        terminal_mode=True,
        claude_chat_active=True,
    )
    ctx = await session_service.get(1)
    ctx.terminal_id = "user_1_abc123"
    await session_service._store.save(ctx)
    state = cache.get_or_create(
        session_id="s1",
        provider="claude_code",
        workdir="/proj",
        terminal_id="user_1_abc123",
        user_id=1,
    )
    activity_at = datetime(2026, 6, 4, 12, 0, tzinfo=UTC)
    state.last_activity = activity_at
    cache.put(state)

    sessions = await registry.list_active_sessions()
    assert len(sessions) == 1
    assert sessions[0].terminal_id == "user_1_abc123"
    assert sessions[0].workdir == "/proj"
    assert sessions[0].owner_user_id == 1
    assert sessions[0].last_activity == activity_at
```

- [ ] **Step 2: Run the targeted test to verify it fails**

Run:

```bash
cd /Users/jack/project/remote-coding && pytest tests/test_command_list.py::test_list_shows_active_session -q
```

Expected: FAIL with `AttributeError: 'TerminalSessionInfo' object has no attribute 'last_activity'`.

- [ ] **Step 3: Add `last_activity` to the model**

Modify `app/domain/models.py` so `TerminalSessionInfo` becomes:

```python
@dataclass
class TerminalSessionInfo:
    """View-model for session listing (/list command)."""

    terminal_id: str
    tmux_session_name: str
    workdir: str
    phase: str
    owner_user_id: int | None
    attached_user_ids: list[int]
    is_alive: bool
    last_activity: datetime | None = None
```

- [ ] **Step 4: Fill `last_activity` in the registry**

In `app/services/session_registry.py`, modify both `TerminalSessionInfo(...)` constructions.

Inside `list_active_sessions()`, use:

```python
results.append(
    TerminalSessionInfo(
        terminal_id=terminal_id,
        tmux_session_name=tmux_name,
        workdir=workdir,
        phase=phase,
        owner_user_id=owner.user_id if owner else None,
        attached_user_ids=attached,
        is_alive=alive,
        last_activity=state.last_activity if state else None,
    )
)
```

Inside `get_session_info()`, use:

```python
return TerminalSessionInfo(
    terminal_id=terminal_id,
    tmux_session_name=tmux_name,
    workdir=workdir,
    phase=phase,
    owner_user_id=owner.user_id if owner else None,
    attached_user_ids=attached_ids,
    is_alive=alive,
    last_activity=state.last_activity if state else None,
)
```

- [ ] **Step 5: Run the targeted test**

Run:

```bash
cd /Users/jack/project/remote-coding && pytest tests/test_command_list.py::test_list_shows_active_session -q
```

Expected: PASS.

- [ ] **Step 6: Commit tmux activity propagation**

Run:

```bash
git -C /Users/jack/project/remote-coding add app/domain/models.py app/services/session_registry.py tests/test_command_list.py
git -C /Users/jack/project/remote-coding commit -m "feat: expose tmux session activity in list data"
```

Expected: commit succeeds.

---

## Task 4: Switch `/list` to the summary renderer

**Files:**
- Modify: `app/bot/handlers/command_list.py`
- Modify: `tests/test_command_list.py`

- [ ] **Step 1: Add a failing command-level summary test**

Append these imports to `tests/test_command_list.py`:

```python
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from app.domain.external_session_models import ExternalBinding
from app.services.external_binding_store import ExternalBindingStore
from app.services.external_session_binder import ExternalSessionBinder
from app.services.external_session_discovery import ExternalSessionDiscoveryService
```

If `Path`, `AsyncMock`, or datetime imports already exist after Task 3, keep one import per symbol and remove duplicates.

Append these helpers and test to `tests/test_command_list.py`:

```python
def _message(user_id: int = 42) -> MagicMock:
    message = MagicMock()
    message.from_user = SimpleNamespace(id=user_id)
    message.answer = AsyncMock()
    return message


def _callback_data_from_answer(message: MagicMock) -> list[str]:
    keyboard = message.answer.call_args.kwargs.get("reply_markup")
    if keyboard is None:
        return []
    return [button.callback_data or "" for row in keyboard.inline_keyboard for button in row]


def _save_external_binding(
    store: ExternalBindingStore,
    *,
    session_id: str,
    user_id: int,
    title: str,
    activity_at: datetime,
) -> None:
    store.save_binding(
        ExternalBinding(
            session_id=session_id,
            user_id=user_id,
            cwd="/Users/jack/project/remote-coding",
            bound_at=activity_at - timedelta(hours=1),
            jsonl_path=None,
            title=title,
            last_activity_at_init=activity_at,
        )
    )


@pytest.mark.asyncio
async def test_command_list_renders_recent_bound_summary(tmp_path: Path) -> None:
    store = ExternalBindingStore(data_dir=tmp_path)
    user_id = 42
    now = datetime(2026, 6, 4, 12, 0, tzinfo=UTC)
    _save_external_binding(store, session_id="sess-newest-0001", user_id=user_id, title="Newest", activity_at=now)
    _save_external_binding(store, session_id="sess-second-0002", user_id=user_id, title="Second", activity_at=now - timedelta(minutes=2))
    _save_external_binding(store, session_id="sess-third-0003", user_id=user_id, title="Third", activity_at=now - timedelta(minutes=3))
    _save_external_binding(store, session_id="sess-hidden-0004", user_id=user_id, title="Hidden", activity_at=now - timedelta(minutes=4))

    registry = AsyncMock()
    registry.list_active_sessions = AsyncMock(return_value=[])
    binder = ExternalSessionBinder(
        discovery=ExternalSessionDiscoveryService(),
        binding_store=store,
        projects_dir=tmp_path / "projects",
    )
    router = Router()
    register_list_handler(router, registry_service=registry, external_binder=binder)
    handler = router.message.handlers[-1].callback
    message = _message(user_id)

    await handler(message)

    text = message.answer.call_args.args[0]
    assert "📋 <b>会话</b>" in text
    assert "🚀 <b>最近可继续</b>" in text
    assert "1. 🔗 Newest" in text
    assert "2. 🔗 Second" in text
    assert "3. 🔗 Third" in text
    assert "Hidden" not in text
    assert "还有 1 个旧会话未显示" in text
    assert _callback_data_from_answer(message) == [
        "sess:select:sess-newest-0001",
        "sess:select:sess-second-0002",
        "sess:select:sess-third-0003",
        "sess:list:all",
    ]
```

- [ ] **Step 2: Run the new command-level test to verify it fails**

Run:

```bash
cd /Users/jack/project/remote-coding && pytest tests/test_command_list.py::test_command_list_renders_recent_bound_summary -q
```

Expected: FAIL because `/list` still renders the old `📋 <b>活跃会话</b>` flat list.

- [ ] **Step 3: Replace `command_list.py` with summary integration**

Replace the complete content of `app/bot/handlers/command_list.py` with:

```python
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
```

- [ ] **Step 4: Run the command-level summary test**

Run:

```bash
cd /Users/jack/project/remote-coding && pytest tests/test_command_list.py::test_command_list_renders_recent_bound_summary -q
```

Expected: PASS.

- [ ] **Step 5: Run existing command list tests**

Run:

```bash
cd /Users/jack/project/remote-coding && pytest tests/test_command_list.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit `/list` summary integration**

Run:

```bash
git -C /Users/jack/project/remote-coding add app/bot/handlers/command_list.py tests/test_command_list.py
git -C /Users/jack/project/remote-coding commit -m "feat: show recent sessions in /list summary"
```

Expected: commit succeeds.

---

## Task 5: Add and verify the full-list callback

**Files:**
- Modify: `tests/test_command_list.py`
- Verify: `app/bot/handlers/command_list.py`

- [ ] **Step 1: Add a callback test for `sess:list:all`**

Append this test to `tests/test_command_list.py`:

```python
@pytest.mark.asyncio
async def test_list_all_callback_renders_full_legacy_list(tmp_path: Path) -> None:
    store = ExternalBindingStore(data_dir=tmp_path)
    user_id = 42
    now = datetime(2026, 6, 4, 12, 0, tzinfo=UTC)
    _save_external_binding(store, session_id="sess-newest-0001", user_id=user_id, title="Newest", activity_at=now)
    _save_external_binding(store, session_id="sess-second-0002", user_id=user_id, title="Second", activity_at=now - timedelta(minutes=2))
    _save_external_binding(store, session_id="sess-third-0003", user_id=user_id, title="Third", activity_at=now - timedelta(minutes=3))
    _save_external_binding(store, session_id="sess-hidden-0004", user_id=user_id, title="Hidden", activity_at=now - timedelta(minutes=4))

    registry = AsyncMock()
    registry.list_active_sessions = AsyncMock(return_value=[])
    binder = ExternalSessionBinder(
        discovery=ExternalSessionDiscoveryService(),
        binding_store=store,
        projects_dir=tmp_path / "projects",
    )
    router = Router()
    register_list_handler(router, registry_service=registry, external_binder=binder)
    callback_handler = router.callback_query.handlers[-1].callback

    callback = MagicMock()
    callback.from_user = SimpleNamespace(id=user_id)
    callback.data = "sess:list:all"
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    callback.message.answer = AsyncMock()

    await callback_handler(callback)

    callback.answer.assert_awaited_once()
    text = callback.message.answer.call_args.args[0]
    assert "📋 <b>活跃会话</b>" in text
    assert "Newest" in text
    assert "Second" in text
    assert "Third" in text
    assert "Hidden" in text
    callbacks = [
        button.callback_data or ""
        for row in callback.message.answer.call_args.kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    assert "sess:select:sess-hidden-0004" in callbacks
```

- [ ] **Step 2: Run the callback test**

Run:

```bash
cd /Users/jack/project/remote-coding && pytest tests/test_command_list.py::test_list_all_callback_renders_full_legacy_list -q
```

Expected: PASS because Task 4 already added the callback.

- [ ] **Step 3: Run command list tests**

Run:

```bash
cd /Users/jack/project/remote-coding && pytest tests/test_command_list.py -q
```

Expected: PASS.

- [ ] **Step 4: Commit callback coverage**

Run:

```bash
git -C /Users/jack/project/remote-coding add tests/test_command_list.py
git -C /Users/jack/project/remote-coding commit -m "test: cover full session list callback"
```

Expected: commit succeeds.

---

## Task 6: Update liveness property expectations for the summary view

**Files:**
- Modify: `tests/property/test_external_list_liveness_properties.py`

- [ ] **Step 1: Update the helper docstring to describe summary semantics**

In `tests/property/test_external_list_liveness_properties.py`, replace `_rendered_session_id_prefixes()` with:

```python
def _rendered_session_id_prefixes(message: _DummyMessage) -> set[str]:
    """Extract bound-session select callback prefixes rendered in the summary view.

    The new /list summary shows at most three recent bound sessions on the first
    page. This helper intentionally returns only the visible summary buttons; the
    full exact visibility partition is covered by the reaper assertions and the
    `sess:list:all` command-level test.
    """
    if not message.reply_markups:
        return set()
    keyboard = message.reply_markups[-1]
    if keyboard is None:
        return set()
    rendered: set[str] = set()
    for row in keyboard.inline_keyboard:
        for button in row:
            data = button.callback_data or ""
            if data.startswith(_CALLBACK_PREFIX):
                rendered.add(data[len(_CALLBACK_PREFIX) :])
    return rendered
```

- [ ] **Step 2: Update the property assertions**

In the property test body, replace the assertion block from `if liveness_enabled:` through the `else:` block with:

```python
        if liveness_enabled:
            # Summary rendering shows at most the three newest visible bindings.
            # It must never show a binding whose pid was classified as dead.
            visible_prefixes = {sid[:16] for sid in visible_expected}
            dead_prefixes = {sid[:16] for sid in dead_expected}
            assert rendered <= visible_prefixes, (
                f"liveness on: rendered prefixes must be a subset of visible bindings; "
                f"expected_visible={visible_expected!r}, got prefixes={rendered!r}"
            )
            assert rendered.isdisjoint(dead_prefixes), (
                f"liveness on: dead bindings must not be rendered; dead_expected={dead_expected!r}, got prefixes={rendered!r}"
            )
            assert len(rendered) == min(3, len(visible_prefixes))

            # Each Pid_Known-and-dead binding is reaped exactly once with
            # reason='pid_dead' (Req 9.2); no alive/unknown binding is reaped.
            reaped_ids = {c.args[0] for c in reaper.remove_with_cleanup.await_args_list}
            reaped_reasons = {c.kwargs.get("reason") for c in reaper.remove_with_cleanup.await_args_list}
            assert reaped_ids == dead_expected, f"expected reaped={dead_expected!r}, got {reaped_ids!r}"
            assert reaper.remove_with_cleanup.await_count == len(dead_expected)
            if dead_expected:
                assert reaped_reasons == {"pid_dead"}
            assert reaped_ids.isdisjoint(visible_expected), "no alive/unknown binding may be reaped"
        else:
            # Liveness disabled: pid is ignored and no binding is reaped. The
            # summary still shows only up to three recent bindings.
            all_prefixes = {sid[:16] for sid in all_ids}
            assert rendered <= all_prefixes, (
                f"liveness off: rendered prefixes must be a subset of all bindings; all_ids={all_ids!r}, got {rendered!r}"
            )
            assert len(rendered) == min(3, len(all_prefixes))
            reaper.remove_with_cleanup.assert_not_awaited()
            probe_mock.assert_not_called()
```

- [ ] **Step 3: Run the liveness property test**

Run:

```bash
cd /Users/jack/project/remote-coding && pytest tests/property/test_external_list_liveness_properties.py -q
```

Expected: PASS.

- [ ] **Step 4: Run the integration liveness test that checks one visible binding still renders**

Run:

```bash
cd /Users/jack/project/remote-coding && pytest tests/integration/test_external_binding_pid_liveness.py::test_liveness_disabled_retains_and_lists_dead_pid -q
```

Expected: PASS because a single retained binding still appears in the top-three summary buttons.

- [ ] **Step 5: Commit liveness test update**

Run:

```bash
git -C /Users/jack/project/remote-coding add tests/property/test_external_list_liveness_properties.py
git -C /Users/jack/project/remote-coding commit -m "test: update list liveness expectations for summary view"
```

Expected: commit succeeds.

---

## Task 7: Final verification

**Files:**
- Verify all changed files.

- [ ] **Step 1: Run focused tests**

Run:

```bash
cd /Users/jack/project/remote-coding && pytest \
  tests/test_session_list_renderer.py \
  tests/test_command_list.py \
  tests/property/test_external_list_liveness_properties.py \
  tests/integration/test_external_binding_pid_liveness.py \
  -q
```

Expected: PASS.

- [ ] **Step 2: Run lint and formatting checks**

Run:

```bash
cd /Users/jack/project/remote-coding && ruff check app tests && ruff format --check app tests
```

Expected: PASS.

- [ ] **Step 3: Run the full test suite**

Run:

```bash
cd /Users/jack/project/remote-coding && pytest -q
```

Expected: PASS. If any unrelated pre-existing test fails, capture the failing test name and output before deciding whether to fix or report it.

- [ ] **Step 4: Confirm git status**

Run:

```bash
git -C /Users/jack/project/remote-coding status --short
```

Expected: no output. If generated cache files or temporary files appear, remove only files created by this task and rerun the status command.

---

## Self-review checklist for implementers

- The renderer has tests for top-three bound sessions, unbound attention, attention priority, HTML escaping, callbacks, hidden count, and empty input.
- `/list` uses the new summary renderer and keeps `sess:list:all` as the full-list escape hatch.
- Default `/list` no longer shows per-session close buttons; close remains available through existing session-specific flows.
- Liveness filtering still happens before rendering, so dead pid bindings are not counted as visible summary items.
- No new persistent fields are introduced; tmux `last_activity` is passed through the in-memory `TerminalSessionInfo` view model only.
