from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.bot.presenters.telegram_formatting import (
    render_markdownish_to_telegram_html,
    split_markdownish_for_telegram,
)
from app.infra.text_formatting import html_escape, relative_time_compact_en, relative_time_zh, short_cwd, truncate_text


class TestCommonTextFormatting:
    def test_short_cwd_returns_last_two_segments(self) -> None:
        assert short_cwd("/Users/jack/project") == "jack/project"
        assert short_cwd("") == "unknown"
        assert short_cwd("", fallback="") == ""

    def test_html_escape_matches_telegram_html_escaping(self) -> None:
        assert html_escape("<a&b>") == "&lt;a&amp;b&gt;"
        assert html_escape('a "quote"') == 'a "quote"'

    def test_truncate_text_uses_suffix(self) -> None:
        assert truncate_text("abcdef", 4) == "abc…"
        assert truncate_text("abc", 4) == "abc"

    def test_relative_time_zh(self) -> None:
        now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        assert relative_time_zh(now - timedelta(seconds=5), now) == "刚刚"
        assert relative_time_zh(now - timedelta(minutes=3), now) == "3 分钟前"
        assert relative_time_zh(now - timedelta(hours=2), now) == "2 小时前"
        assert relative_time_zh(now - timedelta(days=1), now) == "昨天"

    def test_relative_time_compact_en(self) -> None:
        now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        assert relative_time_compact_en(now - timedelta(seconds=5), now=now) == "5s ago"
        assert relative_time_compact_en(now - timedelta(minutes=3), now=now) == "3m ago"
        assert relative_time_compact_en(now - timedelta(hours=2), now=now) == "2h ago"
        assert relative_time_compact_en(now - timedelta(days=2), now=now) == "2d ago"


class TestTableRendering:
    def test_markdown_table_wrapped_in_pre_code(self) -> None:
        text = "| Name  | Value |\n|-------|-------|\n| foo   | 42    |\n| bar   | 108   |"
        result = render_markdownish_to_telegram_html(text)
        assert "<pre><code>" in result
        assert "</code></pre>" in result
        # Should contain aligned content without separator dashes
        assert "Name" in result
        assert "foo" in result
        assert "│" in result  # Uses box-drawing character for separator
        assert "|-------|" not in result  # Separator row removed

    def test_table_with_text_before_and_after(self) -> None:
        text = "Here is a table:\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\nAnd some text after."
        result = render_markdownish_to_telegram_html(text)
        assert "<pre><code>" in result
        assert "A" in result
        assert "1" in result
        assert "Here is a table:" in result
        assert "And some text after." in result
        # Table part should be in pre/code
        assert result.index("Here is a table:") < result.index("<pre><code>")
        assert result.index("</code></pre>") < result.index("And some text after.")

    def test_no_false_positive_on_non_table_pipes(self) -> None:
        # Lines with | but no separator row should NOT be treated as table
        text = "use the | command | to pipe\noutput | to | file"
        result = render_markdownish_to_telegram_html(text)
        assert "<pre><code>" not in result

    def test_table_with_alignment_colons(self) -> None:
        text = "| Left | Center | Right |\n|:-----|:------:|------:|\n| a    | b      | c     |"
        result = render_markdownish_to_telegram_html(text)
        assert result.startswith("<pre><code>")
        assert result.endswith("</code></pre>")

    def test_multiple_tables(self) -> None:
        text = "| A | B |\n|---|---|\n| 1 | 2 |\n\nSome text\n\n| X | Y |\n|---|---|\n| 3 | 4 |"
        result = render_markdownish_to_telegram_html(text)
        # Should have two pre/code blocks
        assert result.count("<pre><code>") == 2
        assert result.count("</code></pre>") == 2

    def test_table_inside_fenced_code_unchanged(self) -> None:
        text = "```\n| A | B |\n|---|---|\n| 1 | 2 |\n```"
        result = render_markdownish_to_telegram_html(text)
        # Should be wrapped once (by fenced code), not double-wrapped
        assert result.count("<pre><code>") == 1
        assert "| A | B |" in result

    def test_table_html_escaped(self) -> None:
        text = "| A & B | C |\n|-------|---|\n| <tag> | 1 |"
        result = render_markdownish_to_telegram_html(text)
        assert "A &amp; B" in result
        assert "&lt;tag&gt;" in result


class TestTableSplitting:
    def test_short_table_not_split(self) -> None:
        text = "| A | B |\n|---|---|\n| 1 | 2 |"
        parts = split_markdownish_for_telegram(text, max_len=4096)
        assert len(parts) == 1
        assert "| A | B |" in parts[0]

    def test_long_table_split_by_rows(self) -> None:
        rows = "\n".join(f"| row {i} | value {i} |" for i in range(100))
        text = f"| Name | Value |\n|------|-------|\n{rows}"
        parts = split_markdownish_for_telegram(text, max_len=200)
        assert len(parts) > 1
        # Each part should still look like table rows
        for part in parts:
            assert "| Name | Value |" in part or "| row" in part

    def test_table_with_surrounding_text(self) -> None:
        text = "Before\n\n| A |\n|---|\n| 1 |\n\nAfter"
        parts = split_markdownish_for_telegram(text, max_len=4096)
        assert len(parts) == 1
        assert "Before" in parts[0]
        assert "| A |" in parts[0]
        assert "After" in parts[0]
