from pathlib import Path

from app.bot.handlers.command_claude import resolve_claude_workdir_arg


def test_resolve_claude_workdir_arg_returns_none_when_missing() -> None:
    assert resolve_claude_workdir_arg(None) is None
    assert resolve_claude_workdir_arg("   ") is None


def test_resolve_claude_workdir_arg_resolves_path_with_spaces(tmp_path: Path) -> None:
    workdir = tmp_path / "my project"
    workdir.mkdir()

    resolved = resolve_claude_workdir_arg(f"  {workdir}  ")

    assert resolved == str(workdir.resolve())
