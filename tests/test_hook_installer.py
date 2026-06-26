from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
import uuid
from contextlib import suppress

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


def test_hook_script_normalizes_claude_payload(tmp_path) -> None:
    paths = ClaudePaths.resolve(str(tmp_path / ".claude"))
    socket_path = f"/tmp/rc-hi-{uuid.uuid4().hex}.sock"
    installer = HookInstaller(
        paths=paths,
        socket_path=socket_path,
        python_bin="python3",
    )
    script_path = installer.install(version=ClaudeCodeVersion(2, 1, 88))

    received: list[dict] = []

    def serve_once() -> None:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(socket_path)
            server.listen(1)
            conn, _ = server.accept()
            with conn:
                chunks: list[bytes] = []
                while True:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    if b"\n" in chunk:
                        break
                received.append(json.loads(b"".join(chunks).split(b"\n", 1)[0].decode("utf-8")))

    thread = threading.Thread(target=serve_once)
    thread.start()

    raw_payload = {
        "session_id": "claude-session-123",
        "cwd": "/tmp/project",
        "hook_event_name": "PermissionRequest",
        "tool_name": "Bash",
        "tool_input": {"command": "pwd"},
        "tool_use_id": "tool-1",
    }
    completed = subprocess.run(
        ["python3", str(script_path)],
        input=json.dumps(raw_payload),
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )
    thread.join(timeout=5)
    with suppress(FileNotFoundError):
        os.unlink(socket_path)

    assert completed.returncode == 0
    assert completed.stdout == ""
    assert received == [
        {
            "session_id": "claude-session-123",
            "cwd": "/tmp/project",
            "event": "PermissionRequest",
            "status": "waiting_for_approval",
            "pid": None,
            "tty": None,
            "tool": "Bash",
            "tool_input": {"command": "pwd"},
            "tool_use_id": "tool-1",
            "notification_type": None,
            "message": None,
        }
    ]


def test_hook_script_does_not_wait_for_non_permission_response(tmp_path) -> None:
    paths = ClaudePaths.resolve(str(tmp_path / ".claude"))
    socket_path = f"/tmp/rc-hi-{uuid.uuid4().hex}.sock"
    installer = HookInstaller(
        paths=paths,
        socket_path=socket_path,
        python_bin="python3",
    )
    script_path = installer.install(version=ClaudeCodeVersion(2, 1, 88))

    received: list[dict] = []
    release = threading.Event()

    def serve_once() -> None:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(socket_path)
            server.listen(1)
            conn, _ = server.accept()
            with conn:
                chunks: list[bytes] = []
                while True:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    if b"\n" in chunk:
                        break
                received.append(json.loads(b"".join(chunks).split(b"\n", 1)[0].decode("utf-8")))
                release.wait(timeout=2)

    thread = threading.Thread(target=serve_once)
    thread.start()

    raw_payload = {
        "session_id": "claude-session-123",
        "cwd": "/tmp/project",
        "hook_event_name": "Stop",
    }
    start = time.monotonic()
    completed = subprocess.run(
        ["python3", str(script_path)],
        input=json.dumps(raw_payload),
        text=True,
        capture_output=True,
        check=False,
        timeout=1,
    )
    elapsed = time.monotonic() - start
    release.set()
    thread.join(timeout=5)
    with suppress(FileNotFoundError):
        os.unlink(socket_path)

    assert completed.returncode == 0
    assert completed.stdout == ""
    assert elapsed < 1
    assert received[0]["event"] == "Stop"


def test_parse_claude_code_version() -> None:
    version = HookInstaller.parse_claude_code_version("claude 2.1.88 (Claude Code)")
    assert version == ClaudeCodeVersion(2, 1, 88)
