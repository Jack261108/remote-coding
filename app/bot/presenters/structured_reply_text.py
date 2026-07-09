from __future__ import annotations

from app.infra import source_text_normalization as _source_text

_MARKER_LINE_RE = _source_text.BRIDGE_MARKER_LINE_RE
_STREAM_PREVIEW_CHAR_LIMIT = 1800
_STREAM_PREVIEW_LINE_LIMIT = 60
_PERMISSION_INPUT_CHAR_LIMIT = 280
_PERMISSION_INPUT_LINE_LIMIT = 8
_QUESTION_TEXT_CHAR_LIMIT = 360
_QUESTION_TEXT_LINE_LIMIT = 10


def strip_bridge_markers(text: str) -> str:
    return _source_text.strip_bridge_markers(text)


def strip_ansi_escapes(text: str) -> str:
    return _source_text.strip_ansi_escapes(text)


def normalize_stream_text(text: str) -> str:
    return _source_text.normalize_source_text(text)


def _truncate_text(text: str, *, char_limit: int, line_limit: int, suffix: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return ""

    lines = normalized.split("\n")
    needs_line_truncation = len(lines) > line_limit
    preview_lines = lines[:line_limit]
    preview = "\n".join(preview_lines)

    needs_char_truncation = len(preview) > char_limit
    if needs_char_truncation:
        preview = preview[:char_limit].rstrip()

    if needs_line_truncation or needs_char_truncation:
        preview = f"{preview}{suffix}"
    return preview


def preview_stream_text(text: str) -> str:
    return _truncate_text(
        normalize_stream_text(text),
        char_limit=_STREAM_PREVIEW_CHAR_LIMIT,
        line_limit=_STREAM_PREVIEW_LINE_LIMIT,
        suffix="\n...[输出片段过长，已截断本条消息]",
    )


def _truncate_permission_text(text: str) -> str:
    return _truncate_text(
        text,
        char_limit=_PERMISSION_INPUT_CHAR_LIMIT,
        line_limit=_PERMISSION_INPUT_LINE_LIMIT,
        suffix="...",
    )


def _truncate_question_text(text: str) -> str:
    return _truncate_text(
        text,
        char_limit=_QUESTION_TEXT_CHAR_LIMIT,
        line_limit=_QUESTION_TEXT_LINE_LIMIT,
        suffix="...",
    )
