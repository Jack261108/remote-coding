"""Discovers Claude Code slash commands and skills from the filesystem."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Claude Code built-in slash commands
BUILTIN_COMMANDS: list[tuple[str, str]] = [
    ("/compact", "压缩上下文窗口"),
    ("/clear", "清空会话历史"),
    ("/model", "切换模型"),
]


@dataclass
class ClaudeCommand:
    """A discovered Claude slash command."""

    name: str  # e.g. "/user:security-review" or "/project:optimize" or "/skill-name"
    description: str  # First line of the .md file or folder name
    source: str  # "builtin", "user", "project", "skill"
    slash_text: str  # The actual text to send to Claude (e.g. "/compact" or "/user:security-review")


def discover_commands(*, workdir: str, claude_home: Path | None = None) -> list[ClaudeCommand]:
    """Discover all available Claude commands for the given workdir.

    Scans:
    - Built-in commands
    - ~/.claude/commands/ (user-level, prefix: /user:)
    - ~/.claude/skills/ (user-level skills, prefix: /)
    - <workdir>/.claude/commands/ (project-level, prefix: /project:)
    - <workdir>/.claude/skills/ (project-level skills, prefix: /)
    """
    home = claude_home or Path.home() / ".claude"
    workdir_path = Path(workdir)
    commands: list[ClaudeCommand] = []

    # Built-in commands
    for name, desc in BUILTIN_COMMANDS:
        commands.append(ClaudeCommand(name=name, description=desc, source="builtin", slash_text=name))

    # User-level commands (~/.claude/commands/)
    user_commands_dir = home / "commands"
    if user_commands_dir.is_dir():
        for md_file in sorted(user_commands_dir.glob("*.md")):
            cmd_name = md_file.stem
            desc = _read_first_line(md_file)
            commands.append(
                ClaudeCommand(
                    name=f"/user:{cmd_name}",
                    description=desc or cmd_name,
                    source="user",
                    slash_text=f"/user:{cmd_name}",
                )
            )

    # User-level skills (~/.claude/skills/)
    user_skills_dir = home / "skills"
    if user_skills_dir.is_dir():
        for skill_dir in sorted(user_skills_dir.iterdir()):
            if skill_dir.is_dir() and (skill_dir / "SKILL.md").exists():
                skill_name = skill_dir.name
                desc = _read_first_line(skill_dir / "SKILL.md")
                commands.append(
                    ClaudeCommand(
                        name=f"/{skill_name}",
                        description=desc or skill_name,
                        source="skill",
                        slash_text=f"/{skill_name}",
                    )
                )

    # Project-level commands (<workdir>/.claude/commands/)
    project_commands_dir = workdir_path / ".claude" / "commands"
    if project_commands_dir.is_dir():
        for md_file in sorted(project_commands_dir.glob("*.md")):
            cmd_name = md_file.stem
            desc = _read_first_line(md_file)
            commands.append(
                ClaudeCommand(
                    name=f"/project:{cmd_name}",
                    description=desc or cmd_name,
                    source="project",
                    slash_text=f"/project:{cmd_name}",
                )
            )

    # Project-level skills (<workdir>/.claude/skills/)
    project_skills_dir = workdir_path / ".claude" / "skills"
    if project_skills_dir.is_dir():
        for skill_dir in sorted(project_skills_dir.iterdir()):
            if skill_dir.is_dir() and (skill_dir / "SKILL.md").exists():
                skill_name = skill_dir.name
                desc = _read_first_line(skill_dir / "SKILL.md")
                commands.append(
                    ClaudeCommand(
                        name=f"/{skill_name}",
                        description=desc or skill_name,
                        source="project",
                        slash_text=f"/{skill_name}",
                    )
                )

    return commands


def _read_first_line(path: Path) -> str:
    """Read the first non-empty, non-heading, non-frontmatter line from a markdown file."""
    try:
        in_frontmatter = False
        with open(path, encoding="utf-8", errors="ignore") as f:
            for line in f:
                stripped = line.strip()
                if not stripped:
                    continue
                # Skip YAML front matter (--- ... ---)
                if stripped == "---":
                    in_frontmatter = not in_frontmatter
                    continue
                if in_frontmatter:
                    continue
                # Skip markdown headings but extract their text
                if stripped.startswith("#"):
                    stripped = stripped.lstrip("#").strip()
                if stripped:
                    return stripped[:60]
        return ""
    except OSError:
        return ""
