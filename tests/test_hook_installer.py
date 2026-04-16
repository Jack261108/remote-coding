from __future__ import annotations

import json

from app.adapters.claude.hook_installer import ClaudeCodeVersion, HookInstaller
from app.adapters.claude.paths import ClaudePaths


def test_hook_installer_writes_script_and_settings(tmp_path) -> None:
    paths = ClaudePaths.resolve(str(tmp_path / ".claude"))
    installer = HookInstaller(
        paths=paths,
        socket_path="/tmp/test-remote-coding.sock",
        python_bin="python3",
    )

    script_path = installer.install(version=ClaudeCodeVersion(2, 1, 88))

    settings = json.loads(paths.settings_file.read_text(encoding="utf-8"))
    assert script_path.exists()
    assert "DEFAULT_SOCKET_PATH = '/tmp/test-remote-coding.sock'" in script_path.read_text(encoding="utf-8")
    assert "hooks" in settings
    assert any(entry["matcher"] == "*" for entry in settings["hooks"]["PreToolUse"])
    permission_entries = settings["hooks"]["PermissionRequest"]
    assert permission_entries[0]["hooks"][0]["timeout"] == 86400
    assert "PermissionDenied" in settings["hooks"]


def test_hook_installer_removes_previous_remote_coding_entries(tmp_path) -> None:
    paths = ClaudePaths.resolve(str(tmp_path / ".claude"))
    paths.settings_file.parent.mkdir(parents=True, exist_ok=True)
    paths.settings_file.write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [
                        {"hooks": [{"type": "command", "command": "python3 /tmp/remote-coding-hook.py"}]},
                        {"hooks": [{"type": "command", "command": "echo keep-me"}]},
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    installer = HookInstaller(paths=paths, socket_path="/tmp/test.sock", python_bin="python3")

    installer.install(version=ClaudeCodeVersion(1, 9, 0))

    settings = json.loads(paths.settings_file.read_text(encoding="utf-8"))
    commands = [hook["command"] for entry in settings["hooks"]["Stop"] for hook in entry["hooks"]]
    assert commands.count("echo keep-me") == 1
    assert sum("remote-coding-hook.py" in command for command in commands) == 1


def test_parse_claude_code_version() -> None:
    version = HookInstaller.parse_claude_code_version("claude 2.1.88 (Claude Code)")
    assert version == ClaudeCodeVersion(2, 1, 88)
