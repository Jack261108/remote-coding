from __future__ import annotations

from app.bot.presenters.telegram_formatting import (
    render_markdownish_to_telegram_html,
    split_markdownish_for_telegram,
)


class TestTableRendering:
    def test_markdown_table_wrapped_in_pre_code(self) -> None:
        text = (
            "| Name  | Value |\n"
            "|-------|-------|\n"
            "| foo   | 42    |\n"
            "| bar   | 108   |"
        )
        result = render_markdownish_to_telegram_html(text)
        assert result == (
            "<pre><code>| Name  | Value |\n"
            "|-------|-------|\n"
            "| foo   | 42    |\n"
            "| bar   | 108   |</code></pre>"
        )

    def test_table_with_text_before_and_after(self) -> None:
        text = (
            "Here is a table:\n"
            "\n"
            "| A | B |\n"
            "|---|---|\n"
            "| 1 | 2 |\n"
            "\n"
            "And some text after."
        )
        result = render_markdownish_to_telegram_html(text)
        assert "<pre><code>" in result
        assert "| A | B |" in result
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
        text = (
            "| Left | Center | Right |\n"
            "|:-----|:------:|------:|\n"
            "| a    | b      | c     |"
        )
        result = render_markdownish_to_telegram_html(text)
        assert result.startswith("<pre><code>")
        assert result.endswith("</code></pre>")

    def test_multiple_tables(self) -> None:
        text = (
            "| A | B |\n"
            "|---|---|\n"
            "| 1 | 2 |\n"
            "\n"
            "Some text\n"
            "\n"
            "| X | Y |\n"
            "|---|---|\n"
            "| 3 | 4 |"
        )
        result = render_markdownish_to_telegram_html(text)
        # Should have two pre/code blocks
        assert result.count("<pre><code>") == 2
        assert result.count("</code></pre>") == 2

    def test_table_inside_fenced_code_unchanged(self) -> None:
        text = (
            "```\n"
            "| A | B |\n"
            "|---|---|\n"
            "| 1 | 2 |\n"
            "```"
        )
        result = render_markdownish_to_telegram_html(text)
        # Should be wrapped once (by fenced code), not double-wrapped
        assert result.count("<pre><code>") == 1
        assert "| A | B |" in result

    def test_table_html_escaped(self) -> None:
        text = (
            "| A & B | C |\n"
            "|-------|---|\n"
            "| <tag> | 1 |"
        )
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
