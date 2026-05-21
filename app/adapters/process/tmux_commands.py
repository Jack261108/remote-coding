from __future__ import annotations

import shlex
from pathlib import Path

_INTERACTIVE_SYSTEM_PROMPT = "你是 Telegram CLI 网关的后端。直接输出回复正文，不要输出 TGCLI_BEGIN/TGCLI_DONE 等标签。"


class TmuxCommandMixin:
    _claude_cli_bin: str

    def _build_session_name(self, terminal_key: str) -> str:
        sanitized = "".join(ch for ch in terminal_key if ch.isalnum() or ch in {"-", "_"})
        if not sanitized:
            sanitized = "terminal"
        return f"tgcli_{sanitized}"[:64]

    def _build_shell_command(
        self, *, argv: list[str], workdir: str, log_file: Path, exit_file: Path, command_file: Path, hide_launcher_line: bool
    ) -> str:
        cli_command = shlex.join(argv)
        workdir_target = shlex.quote(str(Path(workdir).resolve()))
        log_target = shlex.quote(str(log_file))
        exit_target = shlex.quote(str(exit_file))
        script = (
            "#!/usr/bin/env bash\n"
            "set -o pipefail\n"
            f"cd {workdir_target}\n"
            f"{cli_command} 2>&1 | tee -a {log_target}\n"
            f"code=${{PIPESTATUS[0]}}\n"
            f"printf '%s' \"$code\" > {exit_target}\n"
        )
        command_file.write_text(script, encoding="utf-8")
        if not hide_launcher_line:
            return f"bash {shlex.quote(str(command_file))}"
        script_target = shlex.quote(str(command_file))
        return f'bash {script_target}; exec "${{SHELL:-bash}}" -l'

    def _build_interactive_claude_command(self, *, workdir: str) -> str:
        workdir_target = shlex.quote(str(Path(workdir).resolve()))
        claude_bin = shlex.quote(self._claude_cli_bin)
        system_prompt = shlex.quote(_INTERACTIVE_SYSTEM_PROMPT)
        return f"cd {workdir_target} && exec {claude_bin} --append-system-prompt {system_prompt}"

    def _wrap_interactive_prompt(self, *, prompt: str) -> str:
        safe_prompt = prompt.replace("\r", "").strip()
        if not safe_prompt:
            raise ValueError("prompt 不能为空")
        # Claude Code TUI may not submit multi-line pastes with C-m;
        # collapse to a single line to ensure reliable submission.
        safe_prompt = " ".join(safe_prompt.split("\n"))
        return safe_prompt
