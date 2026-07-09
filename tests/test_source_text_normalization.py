from __future__ import annotations

from app.infra.source_text_normalization import normalize_source_text, strip_ansi_escapes, strip_bridge_markers


def test_strip_bridge_markers_removes_only_marker_lines() -> None:
    raw = "TGCLI_BEGIN\n正文包含 TGCLI_DONE 但不是标记\nTGCLI_DONE: abc-123\n__TGCLI_BEGIN__ task-1\n完成"

    assert strip_bridge_markers(raw) == "正文包含 TGCLI_DONE 但不是标记\n完成"


def test_strip_ansi_escapes_removes_common_terminal_sequences() -> None:
    raw = "\x1b[31m红色\x1b[0m\n\x1b]0;title\x07标题"

    assert strip_ansi_escapes(raw) == "红色\n标题"


def test_normalize_source_text_normalizes_newlines_trims_and_collapses_blank_bursts() -> None:
    raw = "\r\nTGCLI_BEGIN\r\n第一行  \r第二行\t  \n\n\n\nTGCLI_DONE\n"

    assert normalize_source_text(raw) == "第一行\n第二行"


def test_normalize_source_text_preserves_single_blank_line_between_content() -> None:
    assert normalize_source_text("第一行\n\n\n第二行") == "第一行\n\n第二行"


def test_normalize_source_text_returns_empty_for_blank_marker_only_and_none() -> None:
    assert normalize_source_text(None) == ""
    assert normalize_source_text("  \n\t") == ""
    assert normalize_source_text("TGCLI_BEGIN\nTGCLI_DONE") == ""


def test_normalize_source_text_is_idempotent() -> None:
    raw = "TGCLI_BEGIN\r\n\x1b[32m第一行\x1b[0m  \r\n\r\n\r\n第二行\nTGCLI_DONE"
    normalized = normalize_source_text(raw)

    assert normalize_source_text(normalized) == normalized
