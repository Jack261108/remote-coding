"""Tests for Claude command discovery.

Covers: discover_commands, _read_first_line, built-in commands,
user-level commands/skills, project-level commands/skills.
"""

from __future__ import annotations

import pytest

from app.services.claude_command_discovery import (
    BUILTIN_COMMANDS,
    _read_first_line,
    discover_commands,
)

# ---------------------------------------------------------------------------
# _read_first_line
# ---------------------------------------------------------------------------


class TestReadFirstLine:
    def test_reads_first_nonempty_line(self, tmp_path):
        f = tmp_path / "test.md"
        f.write_text("\n\nHello World\nMore text\n")
        assert _read_first_line(f) == "Hello World"

    def test_skips_heading_marker(self, tmp_path):
        f = tmp_path / "test.md"
        f.write_text("# My Command\nDescription here\n")
        assert _read_first_line(f) == "My Command"

    def test_skips_yaml_frontmatter(self, tmp_path):
        f = tmp_path / "test.md"
        f.write_text("---\nkey: value\n---\nActual content\n")
        assert _read_first_line(f) == "Actual content"

    def test_truncates_to_60_chars(self, tmp_path):
        f = tmp_path / "test.md"
        long_line = "A" * 100
        f.write_text(long_line + "\n")
        assert _read_first_line(f) == "A" * 60

    def test_returns_empty_for_empty_file(self, tmp_path):
        f = tmp_path / "empty.md"
        f.write_text("")
        assert _read_first_line(f) == ""

    def test_returns_empty_for_missing_file(self, tmp_path):
        f = tmp_path / "missing.md"
        assert _read_first_line(f) == ""

    def test_returns_empty_for_all_blank_lines(self, tmp_path):
        f = tmp_path / "blank.md"
        f.write_text("\n\n\n")
        assert _read_first_line(f) == ""


# ---------------------------------------------------------------------------
# discover_commands
# ---------------------------------------------------------------------------


class TestDiscoverCommands:
    def test_includes_builtin_commands(self, tmp_path):
        commands = discover_commands(workdir=str(tmp_path), claude_home=tmp_path / ".claude")
        builtin_names = [c.name for c in commands if c.source == "builtin"]
        assert builtin_names == [name for name, _ in BUILTIN_COMMANDS]

    def test_discovers_user_commands(self, tmp_path):
        home = tmp_path / ".claude"
        user_cmds = home / "commands"
        user_cmds.mkdir(parents=True)
        (user_cmds / "security-review.md").write_text("# Security Review\nScan for vulns\n")
        (user_cmds / "optimize.md").write_text("Optimize performance\n")

        commands = discover_commands(workdir=str(tmp_path), claude_home=home)
        user_found = [c for c in commands if c.source == "user"]
        names = {c.name for c in user_found}
        assert "/user:security-review" in names
        assert "/user:optimize" in names

    def test_discovers_project_commands(self, tmp_path):
        proj_cmds = tmp_path / ".claude" / "commands"
        proj_cmds.mkdir(parents=True)
        (proj_cmds / "deploy.md").write_text("Deploy to prod\n")

        commands = discover_commands(workdir=str(tmp_path), claude_home=tmp_path / ".cla")
        proj_found = [c for c in commands if c.source == "project"]
        names = {c.name for c in proj_found}
        assert "/project:deploy" in names

    def test_discovers_user_skills(self, tmp_path):
        home = tmp_path / ".claude"
        skill_dir = home / "skills" / "code-review"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# Code Review\nReview code quality\n")

        commands = discover_commands(workdir=str(tmp_path), claude_home=home)
        skill_found = [c for c in commands if c.source == "skill" and c.name == "/code-review"]
        assert len(skill_found) == 1
        assert skill_found[0].slash_text == "/code-review"

    def test_discovers_project_skills(self, tmp_path):
        skill_dir = tmp_path / ".claude" / "skills" / "test-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("Test skill\n")

        commands = discover_commands(workdir=str(tmp_path), claude_home=tmp_path / ".cla")
        skill_found = [c for c in commands if c.source == "project" and c.name == "/test-skill"]
        assert len(skill_found) == 1

    def test_ignores_skill_dir_without_skill_md(self, tmp_path):
        home = tmp_path / ".claude"
        skill_dir = home / "skills" / "incomplete"
        skill_dir.mkdir(parents=True)
        (skill_dir / "README.md").write_text("No SKILL.md here\n")

        commands = discover_commands(workdir=str(tmp_path), claude_home=home)
        skill_found = [c for c in commands if c.name == "/incomplete"]
        assert len(skill_found) == 0

    def test_ignores_non_md_files_in_commands(self, tmp_path):
        home = tmp_path / ".claude"
        user_cmds = home / "commands"
        user_cmds.mkdir(parents=True)
        (user_cmds / "valid.md").write_text("Valid command\n")
        (user_cmds / "ignore.txt").write_text("Not a command\n")

        commands = discover_commands(workdir=str(tmp_path), claude_home=home)
        user_found = [c for c in commands if c.source == "user"]
        names = {c.name for c in user_found}
        assert "/user:valid" in names
        assert "/user:ignore" not in names

    def test_sorted_output(self, tmp_path):
        home = tmp_path / ".claude"
        user_cmds = home / "commands"
        user_cmds.mkdir(parents=True)
        (user_cmds / "zebra.md").write_text("Z\n")
        (user_cmds / "alpha.md").write_text("A\n")

        commands = discover_commands(workdir=str(tmp_path), claude_home=home)
        user_found = [c for c in commands if c.source == "user"]
        names = [c.name for c in user_found]
        assert names == sorted(names)

    def test_handles_missing_directories(self, tmp_path):
        home = tmp_path / "nonexistent"
        commands = discover_commands(workdir=str(tmp_path), claude_home=home)
        # Should only have builtins
        assert len(commands) == len(BUILTIN_COMMANDS)


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
