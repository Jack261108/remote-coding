from __future__ import annotations

import html
import re
from html.parser import HTMLParser

from hypothesis import given, settings
from hypothesis import strategies as st

from app.bot.presenters.permission_message_builder import PermissionMessageBuilder, PermissionPromptInput
from app.bot.presenters.telegram_formatting import render_markdownish_to_telegram_html

ZWNJ = "\u200c"


class _BalancedTagParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.stack: list[str] = []
        self.errors: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(tag)
            return
        self.stack.pop()


class _CodeTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._code_depth = 0
        self._current: list[str] | None = None
        self.code_texts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "code":
            self._code_depth += 1
            self._current = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "code" and self._code_depth:
            self._code_depth -= 1
            if self._current is not None:
                self.code_texts.append("".join(self._current))
            self._current = None

    def handle_data(self, data: str) -> None:
        if self._code_depth and self._current is not None:
            self._current.append(data)


def _assert_balanced_html(rendered: str) -> None:
    parser = _BalancedTagParser()
    parser.feed(rendered)
    parser.close()
    assert parser.errors == []
    assert parser.stack == []


def _code_texts(rendered: str) -> list[str]:
    parser = _CodeTextParser()
    parser.feed(rendered)
    parser.close()
    return parser.code_texts


def _raw_code_fragments(rendered: str) -> list[str]:
    return re.findall(r"(?:<pre><code>|<code>)(.*?)(?:</code></pre>|</code>)", rendered, flags=re.DOTALL)


def _renderer_normalized(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _has_edge_newline_equivalent(value: str) -> bool:
    return value.startswith(("\n", "\r")) or value.endswith(("\n", "\r"))


def _sanitize_fenced_value(value: str) -> str:
    sanitized = re.sub(r"`{3,}", lambda match: ZWNJ.join("`" for _ in match.group(0)), value)
    if sanitized and _has_edge_newline_equivalent(sanitized):
        return f"{ZWNJ}{sanitized}{ZWNJ}"
    return sanitized


def _field_is_fenced(value: str, *, always_fenced: bool = False) -> bool:
    return always_fenced or "`" in value or "\n" in value or "\r" in value


def _assert_field_reachable(rendered: str, value: str, *, always_fenced: bool = False) -> None:
    if value == "":
        return
    expected_value = _sanitize_fenced_value(value) if _field_is_fenced(value, always_fenced=always_fenced) else value
    expected_value = _renderer_normalized(expected_value)
    raw_fragments = _raw_code_fragments(rendered)
    text_fragments = _code_texts(rendered)

    assert any(html.escape(expected_value) in fragment for fragment in raw_fragments)
    assert any(expected_value in fragment for fragment in text_fragments)
    if expected_value != value:
        normalized_value = _renderer_normalized(value)
        assert any(normalized_value in fragment.replace(ZWNJ, "") for fragment in text_fragments)


def _truncate(value: str, max_chars: int) -> str:
    return value[:max_chars]


_control_char = st.characters(blacklist_categories=("Cc", "Cs"), blacklist_characters=ZWNJ)
_adversarial_chunk = st.one_of(
    st.text(_control_char, min_size=0, max_size=8),
    st.sampled_from(["<", ">", "&", "*", "_", "~", "[", "]", "`", "```", "````", "\n", "\r", "\r\n", "汉字", "🚀"]),
)
_adversarial_text = st.one_of(
    st.just(""),
    st.lists(_adversarial_chunk, min_size=1, max_size=12).map("".join),
)
_prompt_inputs = st.builds(
    lambda tool_name, command, file_path, description, cwd, session_title: PermissionPromptInput(
        tool_name=tool_name,
        tool_input={"command": command, "file_path": file_path, "description": description},
        cwd=cwd,
        session_id="12345678-1234-5678-1234-567812345678",
        session_title=session_title,
    ),
    tool_name=_adversarial_text,
    command=_adversarial_text,
    file_path=_adversarial_text,
    description=_adversarial_text,
    cwd=_adversarial_text,
    session_title=st.one_of(st.none(), _adversarial_text),
)


def _render(prompt: PermissionPromptInput) -> str:
    message = PermissionMessageBuilder().build_permission_prompt(prompt)
    return render_markdownish_to_telegram_html(message)


@settings(max_examples=100, deadline=None)
@given(prompt=_prompt_inputs)
def test_builder_output_is_valid_renderer_input_for_adversarial_fields(prompt: PermissionPromptInput) -> None:
    rendered = _render(prompt)

    assert rendered
    _assert_balanced_html(rendered)

    tool_input = prompt.tool_input or {}
    _assert_field_reachable(rendered, prompt.tool_name)
    _assert_field_reachable(rendered, _truncate(str(tool_input["command"]), 300), always_fenced=True)
    _assert_field_reachable(rendered, str(tool_input["file_path"]))
    _assert_field_reachable(rendered, _truncate(str(tool_input["description"]), 200))
    if prompt.session_title is not None:
        _assert_field_reachable(rendered, prompt.session_title)
    if prompt.cwd:
        _assert_field_reachable(rendered, prompt.cwd)


def test_backtick_fence_collision_is_sanitized_and_renderable() -> None:
    command = "printf ``` then ````"
    prompt = PermissionPromptInput(
        tool_name="Bash",
        tool_input={"command": command},
        cwd="/tmp/project",
        session_id="abcdef12-0000-0000-0000-000000000000",
        session_title="session",
    )

    message = PermissionMessageBuilder().build_permission_prompt(prompt)
    rendered = render_markdownish_to_telegram_html(message)

    assert ZWNJ in message
    assert "``` then ````" not in message
    assert any(command in fragment.replace(ZWNJ, "") for fragment in _code_texts(rendered))
    _assert_balanced_html(rendered)


def test_inline_field_with_single_backtick_and_newline_promotes_to_fence() -> None:
    file_path = "a`b\nc"
    prompt = PermissionPromptInput(
        tool_name="Edit",
        tool_input={"command": "pwd", "file_path": file_path},
        cwd="/tmp/project",
        session_id="abcdef12-0000-0000-0000-000000000000",
        session_title="session",
    )

    rendered = render_markdownish_to_telegram_html(PermissionMessageBuilder().build_permission_prompt(prompt))

    assert rendered.count("<pre><code>") >= 2
    assert any(file_path in fragment.replace(ZWNJ, "") for fragment in _code_texts(rendered))
    assert f"<pre><code>{html.escape(file_path)}</code></pre>" in rendered


def test_inline_field_with_carriage_return_promotes_to_fence() -> None:
    file_path = "a\rb"
    expected_file_path = file_path.replace("\r", "\n")
    prompt = PermissionPromptInput(
        tool_name="Edit",
        tool_input={"command": "pwd", "file_path": file_path},
        cwd="/tmp/project",
        session_id="abcdef12-0000-0000-0000-000000000000",
        session_title="session",
    )

    rendered = render_markdownish_to_telegram_html(PermissionMessageBuilder().build_permission_prompt(prompt))

    assert rendered.count("<pre><code>") >= 2
    assert any(expected_file_path in fragment.replace(ZWNJ, "") for fragment in _code_texts(rendered))
    assert f"<pre><code>{html.escape(expected_file_path)}</code></pre>" in rendered


def test_fenced_command_preserves_edge_carriage_returns_after_renderer_normalization() -> None:
    command = "\ralpha\r"
    expected_command = command.replace("\r", "\n")
    prompt = PermissionPromptInput(
        tool_name="Bash",
        tool_input={"command": command},
        cwd="/tmp/project",
        session_id="abcdef12-0000-0000-0000-000000000000",
        session_title="session",
    )

    rendered = render_markdownish_to_telegram_html(PermissionMessageBuilder().build_permission_prompt(prompt))

    assert any(expected_command in fragment.replace(ZWNJ, "") for fragment in _code_texts(rendered))
    assert any(html.escape(expected_command) in fragment.replace(ZWNJ, "") for fragment in _raw_code_fragments(rendered))


def test_super_long_command_is_truncated_before_wrapping() -> None:
    command = "x" * 350
    prompt = PermissionPromptInput(
        tool_name="Bash",
        tool_input={"command": command},
        cwd="/tmp/project",
        session_id="abcdef12-0000-0000-0000-000000000000",
        session_title="session",
    )

    rendered = render_markdownish_to_telegram_html(PermissionMessageBuilder().build_permission_prompt(prompt))

    assert "x" * 300 in rendered
    assert "x" * 301 not in rendered


def test_empty_session_title_is_still_wrapped_in_header() -> None:
    prompt = PermissionPromptInput(
        tool_name="Bash",
        tool_input={"command": "pwd"},
        cwd="/tmp/project",
        session_id="abcdef12-0000-0000-0000-000000000000",
        session_title="",
    )

    message = PermissionMessageBuilder().build_permission_prompt(prompt)

    assert message.startswith("🔐 [```\n\n```] 请求权限: `Bash`")
    assert "abcdef12" not in message.split("\n", maxsplit=1)[0]


def test_missing_cwd_shows_plain_text_unknown() -> None:
    prompt = PermissionPromptInput(
        tool_name="Bash",
        tool_input={"command": "pwd"},
        cwd="",
        session_id="abcdef12-0000-0000-0000-000000000000",
        session_title=None,
    )

    rendered = render_markdownish_to_telegram_html(PermissionMessageBuilder().build_permission_prompt(prompt))

    assert "📂 unknown" in rendered
    assert "<code>unknown</code>" not in rendered
