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


def test_empty_invalid_list_still_shows_cleanup_button() -> None:
    result = build_session_list_message([], now=NOW, has_invalid_sessions=True)

    assert result.text == "当前无活跃会话。"
    assert result.keyboard is not None
    assert [button.callback_data for row in result.keyboard.inline_keyboard for button in row] == ["sess:cleanup"]


def test_tmux_attention_uses_attach_callback_with_terminal_id_prefix() -> None:
    terminal_id = "user_42_123456789abc"

    result = build_session_list_message(
        [_item(terminal_id, None, 1, source=ListSessionSource.TMUX, state="waiting_for_input")],
        now=NOW,
    )

    assert f"sess:attach:{terminal_id[:16]}" in _callbacks(result)


def test_tmux_attention_uses_unique_attach_callback_prefixes_for_same_user() -> None:
    terminal_ids = [
        "user_1234567890_aaaaaaaaaaaa",
        "user_1234567890_bbbbbbbbbbbb",
    ]

    result = build_session_list_message(
        [
            _item(terminal_id, None, index, source=ListSessionSource.TMUX, state="waiting_for_input")
            for index, terminal_id in enumerate(terminal_ids, start=1)
        ],
        now=NOW,
    )

    suffixes = [callback.removeprefix("sess:attach:") for callback in _callbacks(result) if callback.startswith("sess:attach:")]
    assert len(suffixes) == 2
    assert len(set(suffixes)) == 2
    for suffix in suffixes:
        assert sum(terminal_id.startswith(suffix) for terminal_id in terminal_ids) == 1


def test_tmux_attention_prefix_is_unique_against_hidden_tmux_sessions() -> None:
    terminal_ids = [
        "user_1234567890_aaaaaaaaaaaa",
        "user_1234567890_bbbbbbbbbbbb",
    ]

    result = build_session_list_message(
        [
            _item(terminal_ids[0], None, 1, source=ListSessionSource.TMUX, state="waiting_for_input"),
            _item(terminal_ids[1], None, 2, source=ListSessionSource.TMUX, state="idle"),
        ],
        now=NOW,
    )

    suffixes = [callback.removeprefix("sess:attach:") for callback in _callbacks(result) if callback.startswith("sess:attach:")]
    assert len(suffixes) == 1
    assert sum(terminal_id.startswith(suffixes[0]) for terminal_id in terminal_ids) == 1


def test_cleanup_button_shown_when_has_invalid_sessions() -> None:
    result = build_session_list_message(
        [_item("sess-00000001", "Test", 1)],
        now=NOW,
        has_invalid_sessions=True,
    )

    assert result.keyboard is not None
    last_button = result.keyboard.inline_keyboard[-1][-1]
    assert last_button.text == "🧹 清理无效会话"
    assert last_button.callback_data == "sess:cleanup"


def test_cleanup_button_hidden_when_no_invalid_sessions() -> None:
    result = build_session_list_message(
        [_item("sess-00000001", "Test", 1)],
        now=NOW,
        has_invalid_sessions=False,
    )

    assert result.keyboard is not None
    # 没有清理按钮
    all_callbacks = _callbacks(result)
    assert "sess:cleanup" not in all_callbacks
