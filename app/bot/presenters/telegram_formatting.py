from __future__ import annotations

import html
import re


_FENCED_CODE_RE = re.compile(r"```[ \t]*([A-Za-z0-9_+\-]*)[ \t]*\n?(.*?)```", re.DOTALL)
_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_STRIKE_RE = re.compile(r"~~(.+?)~~", re.DOTALL)
_ITALIC_RE = re.compile(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)", re.DOTALL)
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$")
_HTML_TOKEN_RE = re.compile(r"(<[^>]+>)")
_HTML_TAG_NAME_RE = re.compile(r"^</?([a-zA-Z0-9]+)")
_TABLE_SEPARATOR_RE = re.compile(r"^\|[\s\-:]+(\|[\s\-:]+)+\|?\s*$")
_TABLE_ROW_RE = re.compile(r"^\|.+\|")


def render_markdownish_to_telegram_html(text: str) -> str:
    if not text:
        return ""

    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    parts: list[str] = []
    cursor = 0
    for match in _FENCED_CODE_RE.finditer(normalized):
        if match.start() > cursor:
            parts.append(_render_non_code_block(normalized[cursor : match.start()]))
        code = match.group(2).strip("\n")
        parts.append(f"<pre><code>{html.escape(code)}</code></pre>")
        cursor = match.end()
    if cursor < len(normalized):
        parts.append(_render_non_code_block(normalized[cursor:]))
    return "".join(parts)


def _render_non_code_block(text: str) -> str:
    """Render a block that is not inside fenced code, splitting out tables."""
    segments = _extract_table_segments(text)
    parts: list[str] = []
    for is_table, content in segments:
        if is_table:
            parts.append(f"<pre><code>{html.escape(content)}</code></pre>")
        else:
            parts.append(_render_normal_block(content))
    return "".join(parts)


_CONCAT_TABLE_SEP_RE = re.compile(r"^\|[\s\-:]+(\|[\s\-:]+)+\|?\s*$")
_TEXT_BEFORE_TABLE_RE = re.compile(r"^(.*?)((?:\|[^|]+)+\|)$")


def _extract_table_segments(text: str) -> list[tuple[bool, str]]:
    """Split text into (is_table, content) segments.

    A table is a contiguous block of lines containing pipe-rows and at least
    one separator row (dashes). Handles concatenated rows joined by '||'.
    """
    expanded_lines = _expand_concatenated_lines(text.split("\n"))
    segments: list[tuple[bool, str]] = []
    table_lines: list[str] = []
    non_table_lines: list[str] = []

    def flush_non_table() -> None:
        if non_table_lines:
            segments.append((False, "\n".join(non_table_lines)))
            non_table_lines.clear()

    def flush_table() -> None:
        if table_lines:
            segments.append((True, "\n".join(table_lines)))
            table_lines.clear()

    for line in expanded_lines:
        if _TABLE_ROW_RE.match(line):
            table_lines.append(line)
        else:
            if table_lines and _has_separator_line(table_lines):
                flush_non_table()
                flush_table()
            elif table_lines:
                non_table_lines.extend(table_lines)
                table_lines.clear()
            non_table_lines.append(line)

    if table_lines and _has_separator_line(table_lines):
        flush_non_table()
        flush_table()
    elif table_lines:
        non_table_lines.extend(table_lines)
    flush_non_table()

    return segments


def _has_separator_line(lines: list[str]) -> bool:
    """Check if any line contains a table separator."""
    return any(_TABLE_SEPARATOR_RE.match(line) for line in lines)


def _expand_concatenated_lines(lines: list[str]) -> list[str]:
    """Pre-process lines: split '||' concatenation and text-embedded table rows."""
    result: list[str] = []
    for line in lines:
        # Split 'header||separator' into separate lines
        if "||" in line:
            parts = line.split("||", 1)
            after = f"|{parts[1]}" if not parts[1].startswith("|") else parts[1]
            if _CONCAT_TABLE_SEP_RE.match(after):
                before = parts[0] + "|"
                # Split text prefix from table row in the 'before' part
                text_match = _TEXT_BEFORE_TABLE_RE.match(before)
                if text_match:
                    if text_match.group(1).strip():
                        result.append(text_match.group(1).rstrip())
                    result.append(text_match.group(2))
                else:
                    result.append(before)
                result.append(after)
                continue
        # Split 'text:| A | B |' into text and table row
        text_match = _TEXT_BEFORE_TABLE_RE.match(line)
        if text_match and "|" in text_match.group(2):
            if text_match.group(1).strip():
                result.append(text_match.group(1).rstrip())
            result.append(text_match.group(2))
            continue
        result.append(line)
    return result


def _render_normal_block(text: str) -> str:
    rendered_lines: list[str] = []
    for line in text.split("\n"):
        rendered_lines.append(_render_line(line))
    return "\n".join(rendered_lines)


def _render_line(line: str) -> str:
    stripped = line.lstrip()
    indent = html.escape(line[: len(line) - len(stripped)])
    if not stripped:
        return indent

    heading = _HEADING_RE.match(stripped)
    if heading is not None:
        return f"{indent}<b>{_render_inline(heading.group(2).strip())}</b>"
    if stripped.startswith("> "):
        return f"{indent}&gt; {_render_inline(stripped[2:])}"
    return f"{indent}{_render_inline(stripped)}"


def _render_inline(text: str) -> str:
    if not text:
        return ""

    parts: list[str] = []
    cursor = 0
    token_re = re.compile(
        rf"{_LINK_RE.pattern}|{_INLINE_CODE_RE.pattern}|{_BOLD_RE.pattern}|{_STRIKE_RE.pattern}|{_ITALIC_RE.pattern}"
    )
    for match in token_re.finditer(text):
        if match.start() > cursor:
            parts.append(html.escape(text[cursor : match.start()]))
        token = match.group(0)
        if _LINK_RE.fullmatch(token):
            link_match = _LINK_RE.fullmatch(token)
            assert link_match is not None
            label, url = link_match.groups()
            safe_url = html.escape(url, quote=True)
            parts.append(f'<a href="{safe_url}">{_render_inline(label)}</a>')
        elif _INLINE_CODE_RE.fullmatch(token):
            code_match = _INLINE_CODE_RE.fullmatch(token)
            assert code_match is not None
            parts.append(f"<code>{html.escape(code_match.group(1))}</code>")
        elif _BOLD_RE.fullmatch(token):
            bold_match = _BOLD_RE.fullmatch(token)
            assert bold_match is not None
            parts.append(f"<b>{_render_inline(bold_match.group(1))}</b>")
        elif _STRIKE_RE.fullmatch(token):
            strike_match = _STRIKE_RE.fullmatch(token)
            assert strike_match is not None
            parts.append(f"<s>{_render_inline(strike_match.group(1))}</s>")
        else:
            italic_match = _ITALIC_RE.fullmatch(token)
            if italic_match is not None:
                parts.append(f"<i>{_render_inline(italic_match.group(1))}</i>")
            else:
                parts.append(html.escape(token))
        cursor = match.end()
    if cursor < len(text):
        parts.append(html.escape(text[cursor:]))
    return "".join(parts)


def split_markdownish_for_telegram(text: str, max_len: int) -> list[str]:
    if not text:
        return []
    if max_len <= 0:
        return [text]

    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    parts: list[str] = []
    cursor = 0
    for match in _FENCED_CODE_RE.finditer(normalized):
        if match.start() > cursor:
            parts.extend(_split_non_code_segment(normalized[cursor : match.start()], max_len))
        parts.extend(
            _split_fenced_code_block(
                language=match.group(1) or "",
                code=match.group(2),
                raw_block=match.group(0),
                max_len=max_len,
            )
        )
        cursor = match.end()
    if cursor < len(normalized):
        parts.extend(_split_non_code_segment(normalized[cursor:], max_len))
    return _merge_markdownish_chunks(parts, max_len)


def _split_non_code_segment(text: str, max_len: int) -> list[str]:
    """Split a non-code segment, keeping tables together like code blocks."""
    segments = _extract_table_segments(text)
    parts: list[str] = []
    for is_table, content in segments:
        if is_table:
            parts.extend(_split_table_block(content, max_len))
        else:
            parts.extend(_split_plain_text(content, max_len))
    return parts


def _split_table_block(table: str, max_len: int) -> list[str]:
    """Split a table block, keeping rows together if possible."""
    if len(table) <= max_len or max_len <= 0:
        return [table]
    # Split by rows, keep contiguous groups within max_len
    lines = table.split("\n")
    chunks: list[str] = []
    current = ""
    for line in lines:
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > max_len and current:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def split_telegram_html(text: str, max_len: int) -> list[str]:
    if not text:
        return []
    if len(text) <= max_len or max_len <= 0:
        return [text]

    tokens = _HTML_TOKEN_RE.split(text)
    chunks: list[str] = []
    current = ""
    stack: list[tuple[str, str]] = []

    def close_tags() -> str:
        return "".join(closing for _, closing in reversed(stack))

    def reopen_tags() -> str:
        return "".join(opening for opening, _ in stack)

    def flush_current() -> None:
        nonlocal current
        if not current:
            return
        chunk = current + close_tags()
        if chunk.strip():
            chunks.append(chunk)
        current = reopen_tags()

    for token in tokens:
        if not token:
            continue
        if token.startswith("<") and token.endswith(">"):
            if not token.startswith("</"):
                prospective_stack = stack + [("x", f"</{_extract_html_tag_name(token)}>")] if _extract_html_tag_name(token) else stack
                prospective_closings = "".join(closing for _, closing in reversed(prospective_stack))
                prospective = current + token + prospective_closings
            else:
                prospective = current + token + close_tags()
            if not token.startswith("</") and len(prospective) > max_len and _html_fragment_has_text(current):
                flush_current()
            current += token
            _update_html_stack(stack, token)
            continue

        remaining = token
        while remaining:
            available = max_len - len(current) - len(close_tags())
            if available <= 0 and current:
                flush_current()
                available = max_len - len(current) - len(close_tags())
            if available <= 0:
                available = max_len
            if len(remaining) <= available:
                current += remaining
                break
            split_at = _find_text_split_index(remaining, available)
            if split_at <= 0:
                split_at = available
            current += remaining[:split_at]
            flush_current()
            remaining = remaining[split_at:]

    if current:
        final_chunk = current + close_tags()
        if final_chunk.strip():
            chunks.append(final_chunk)
    return chunks


def _split_plain_text(text: str, max_len: int) -> list[str]:
    if not text:
        return []
    if len(text) <= max_len or max_len <= 0:
        return [text]

    remaining = text
    chunks: list[str] = []
    while remaining:
        if len(remaining) <= max_len:
            chunks.append(remaining)
            break
        split_at = _find_plain_text_split_index(remaining, max_len)
        if split_at <= 0:
            split_at = max_len
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:]
    return chunks


def _merge_markdownish_chunks(parts: list[str], max_len: int) -> list[str]:
    if not parts:
        return []
    if max_len <= 0:
        return [part for part in parts if part]

    merged: list[str] = []
    current = ""
    for part in parts:
        if not part:
            continue
        if not current:
            current = part
            continue
        if len(current) + len(part) <= max_len:
            current += part
            continue
        merged.append(current)
        current = part
    if current:
        merged.append(current)
    return merged


def _split_fenced_code_block(*, language: str, code: str, raw_block: str, max_len: int) -> list[str]:
    if len(raw_block) <= max_len or max_len <= 0:
        return [raw_block]

    opening = f"```{language}\n"
    closing = "```"
    available = max_len - len(opening) - len(closing) - 1
    if available <= 0:
        return _split_plain_text(raw_block, max_len)

    code_chunks = _split_plain_text(code, available)
    wrapped: list[str] = []
    for chunk in code_chunks:
        suffix = "" if chunk.endswith("\n") else "\n"
        wrapped.append(f"{opening}{chunk}{suffix}{closing}")
    return wrapped


def _find_plain_text_split_index(text: str, max_len: int) -> int:
    for marker in ("\n\n", "\n", " "):
        index = text.rfind(marker, 0, max_len + 1)
        if index > 0:
            return index + len(marker)
    return max_len


def _find_text_split_index(text: str, max_len: int) -> int:
    split_at = _find_plain_text_split_index(text, max_len)
    if split_at <= 0:
        split_at = max_len

    last_amp = text.rfind("&", 0, split_at)
    if last_amp != -1:
        semicolon = text.find(";", last_amp, split_at)
        if semicolon == -1:
            if last_amp > 0:
                split_at = last_amp
            else:
                semicolon = text.find(";", split_at)
                if semicolon != -1 and semicolon + 1 <= max_len:
                    split_at = semicolon + 1
    return split_at


def _update_html_stack(stack: list[tuple[str, str]], token: str) -> None:
    if token.startswith("</"):
        match = _HTML_TAG_NAME_RE.match(token)
        if match is None:
            return
        tag_name = match.group(1)
        for index in range(len(stack) - 1, -1, -1):
            opening, closing = stack[index]
            if closing == f"</{tag_name}>":
                del stack[index]
                break
        return

    if token.endswith("/>"):
        return

    match = _HTML_TAG_NAME_RE.match(token)
    if match is None:
        return
    tag_name = match.group(1)
    stack.append((token, f"</{tag_name}>"))


def _extract_html_tag_name(token: str) -> str | None:
    match = _HTML_TAG_NAME_RE.match(token)
    if match is None:
        return None
    return match.group(1)


def _html_fragment_has_text(fragment: str) -> bool:
    plain = _HTML_TOKEN_RE.sub("", fragment)
    return bool(plain.strip())
