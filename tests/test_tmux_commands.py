"""Unit tests for TmuxCommandMixin."""

from app.adapters.process.tmux_commands import TmuxCommandMixin


class _TestableCommandMixin(TmuxCommandMixin):
    def __init__(self, claude_cli_bin: str = "claude"):
        self._claude_cli_bin = claude_cli_bin


class TestBuildInteractiveClaudeResumeCommand:
    def test_appends_resume_flag_with_session_id(self):
        mixin = _TestableCommandMixin()
        result = mixin._build_interactive_claude_resume_command(workdir="/tmp/project", session_id="abc-123-def")
        assert "--resume" in result
        assert "abc-123-def" in result

    def test_contains_same_base_as_interactive_command(self):
        mixin = _TestableCommandMixin()
        base = mixin._build_interactive_claude_command(workdir="/tmp/project")
        resume = mixin._build_interactive_claude_resume_command(workdir="/tmp/project", session_id="sess-1")
        # The resume command should start with the same prefix as the base command
        assert resume.startswith(base)

    def test_session_id_with_special_chars_is_quoted(self):
        mixin = _TestableCommandMixin()
        result = mixin._build_interactive_claude_resume_command(workdir="/tmp/project", session_id="id with spaces")
        # shlex.quote should wrap the session_id
        assert "'id with spaces'" in result

    def test_workdir_is_resolved(self):
        mixin = _TestableCommandMixin()
        result = mixin._build_interactive_claude_resume_command(workdir="/tmp/project", session_id="sess-1")
        assert "cd " in result
        assert "/tmp/project" in result

    def test_custom_claude_bin(self):
        mixin = _TestableCommandMixin(claude_cli_bin="/usr/local/bin/claude")
        result = mixin._build_interactive_claude_resume_command(workdir="/tmp/project", session_id="sess-1")
        assert "/usr/local/bin/claude" in result
