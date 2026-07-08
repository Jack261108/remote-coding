import pytest

from app.adapters.process.claude_terminal_facade import DisabledClaudeTerminalFacade, TmuxClaudeTerminalFacade
from app.domain.session_models import SessionState


class DummyTmuxRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []
        self.state = SessionState(session_id="claude-session-1", workdir="/tmp")

    async def close_terminal(self, terminal_key: str) -> tuple[bool, str]:
        self.calls.append(("close_terminal", (terminal_key,), {}))
        return True, "closed"

    async def ensure_terminal(self, *, terminal_key: str, workdir: str) -> tuple[bool, str]:
        self.calls.append(("ensure_terminal", (), {"terminal_key": terminal_key, "workdir": workdir}))
        return True, "ensured"

    async def ensure_claude_interactive_session(self, *, terminal_key: str, workdir: str) -> tuple[bool, str]:
        self.calls.append(("ensure_claude_interactive_session", (), {"terminal_key": terminal_key, "workdir": workdir}))
        return True, "interactive"

    async def ensure_claude_resume_session(self, *, terminal_key: str, workdir: str, session_id: str) -> tuple[bool, str]:
        self.calls.append(
            ("ensure_claude_resume_session", (), {"terminal_key": terminal_key, "workdir": workdir, "session_id": session_id})
        )
        return True, "resumed"

    async def reveal_terminal(self, terminal_key: str) -> tuple[bool, str]:
        self.calls.append(("reveal_terminal", (terminal_key,), {}))
        return True, "revealed"

    async def send_interactive_input(self, *, terminal_key: str, workdir: str, text: str) -> tuple[bool, str]:
        self.calls.append(("send_interactive_input", (), {"terminal_key": terminal_key, "workdir": workdir, "text": text}))
        return True, "sent"

    async def select_user_question_option(
        self,
        *,
        terminal_key: str,
        workdir: str,
        option_index: int,
        submit_after: bool = False,
    ) -> tuple[bool, str]:
        self.calls.append(
            (
                "select_user_question_option",
                (),
                {"terminal_key": terminal_key, "workdir": workdir, "option_index": option_index, "submit_after": submit_after},
            )
        )
        return True, "selected"

    async def answer_user_question_with_text(
        self,
        *,
        terminal_key: str,
        workdir: str,
        option_count: int,
        text: str,
        submit_after: bool = False,
    ) -> tuple[bool, str]:
        self.calls.append(
            (
                "answer_user_question_with_text",
                (),
                {
                    "terminal_key": terminal_key,
                    "workdir": workdir,
                    "option_count": option_count,
                    "text": text,
                    "submit_after": submit_after,
                },
            )
        )
        return True, "answered"

    async def advance_user_question_after_multi_select(
        self,
        *,
        terminal_key: str,
        workdir: str,
        final_question: bool,
    ) -> tuple[bool, str]:
        self.calls.append(
            (
                "advance_user_question_after_multi_select",
                (),
                {"terminal_key": terminal_key, "workdir": workdir, "final_question": final_question},
            )
        )
        return True, "advanced"

    def get_session_state(self, terminal_key: str) -> SessionState | None:
        self.calls.append(("get_session_state", (terminal_key,), {}))
        return self.state


@pytest.mark.asyncio
async def test_disabled_facade_returns_legacy_unavailable_message() -> None:
    facade = DisabledClaudeTerminalFacade()

    ok, text = await facade.ensure_terminal(terminal_key="user_1", workdir="/tmp")

    assert ok is False
    assert text == "CLAUDE_TMUX_MODE 未开启或 tmux 未配置"
    assert facade.get_session_state("user_1") is None
    assert facade.get_claude_session_state("claude-session-1") is None


@pytest.mark.asyncio
async def test_tmux_facade_delegates_terminal_runtime_methods() -> None:
    tmux = DummyTmuxRunner()
    facade = TmuxClaudeTerminalFacade(tmux)  # type: ignore[arg-type]

    assert await facade.close_terminal("user_1") == (True, "closed")
    assert await facade.ensure_terminal(terminal_key="user_1", workdir="/tmp") == (True, "ensured")
    assert await facade.ensure_interactive_session(terminal_key="user_1", workdir="/tmp") == (True, "interactive")
    assert await facade.ensure_resume_session(terminal_key="user_1", workdir="/tmp", session_id="claude-session-1") == (True, "resumed")
    assert await facade.reveal_terminal("user_1") == (True, "revealed")
    assert await facade.send_interactive_input(terminal_key="user_1", workdir="/tmp", text="hello") == (True, "sent")

    assert [call[0] for call in tmux.calls] == [
        "close_terminal",
        "ensure_terminal",
        "ensure_claude_interactive_session",
        "ensure_claude_resume_session",
        "reveal_terminal",
        "send_interactive_input",
    ]


@pytest.mark.asyncio
async def test_tmux_facade_delegates_user_question_transport_methods() -> None:
    tmux = DummyTmuxRunner()
    facade = TmuxClaudeTerminalFacade(tmux)  # type: ignore[arg-type]

    assert await facade.select_option(terminal_key="user_1", workdir="/tmp", option_index=2, submit_after=True) == (True, "selected")
    assert await facade.answer_with_text(
        terminal_key="user_1",
        workdir="/tmp",
        option_count=3,
        text="自定义",
        submit_after=False,
    ) == (True, "answered")
    assert await facade.advance_after_multi_select(terminal_key="user_1", workdir="/tmp", final_question=True) == (True, "advanced")

    assert tmux.calls == [
        (
            "select_user_question_option",
            (),
            {"terminal_key": "user_1", "workdir": "/tmp", "option_index": 2, "submit_after": True},
        ),
        (
            "answer_user_question_with_text",
            (),
            {
                "terminal_key": "user_1",
                "workdir": "/tmp",
                "option_count": 3,
                "text": "自定义",
                "submit_after": False,
            },
        ),
        (
            "advance_user_question_after_multi_select",
            (),
            {"terminal_key": "user_1", "workdir": "/tmp", "final_question": True},
        ),
    ]


def test_tmux_facade_delegates_session_state_reader() -> None:
    tmux = DummyTmuxRunner()
    facade = TmuxClaudeTerminalFacade(tmux)  # type: ignore[arg-type]

    assert facade.get_session_state("user_1") is tmux.state
    assert facade.get_claude_session_state("claude-session-1") is tmux.state
    assert tmux.calls == [
        ("get_session_state", ("user_1",), {}),
        ("get_session_state", ("claude-session-1",), {}),
    ]
