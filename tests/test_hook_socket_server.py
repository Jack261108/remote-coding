from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

import pytest

from app.adapters.claude.hook_socket_server import HookSocketServer
from app.domain.hook_models import HookEvent


def _socket_path() -> Path:
    return Path("/tmp") / f"rc-hook-{uuid.uuid4().hex}.sock"


async def _send_event(socket_path, payload: dict) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    reader, writer = await asyncio.open_unix_connection(str(socket_path))
    writer.write(json.dumps(payload).encode("utf-8"))
    await writer.drain()
    if writer.can_write_eof():
        writer.write_eof()
    return reader, writer


@pytest.mark.asyncio
async def test_hook_socket_server_emits_plain_event(tmp_path) -> None:
    seen: list[HookEvent] = []
    delivered = asyncio.Event()
    socket_path = _socket_path()
    server = HookSocketServer(str(socket_path))

    async def on_event(event: HookEvent) -> None:
        seen.append(event)
        delivered.set()

    await server.start(on_event)
    reader, writer = await _send_event(
        socket_path,
        {
            "session_id": "s1",
            "cwd": "/tmp/project",
            "event": "SessionStart",
            "status": "starting",
        },
    )
    await asyncio.wait_for(delivered.wait(), timeout=1)
    assert await reader.read() == b""
    writer.close()
    await writer.wait_closed()
    await server.stop()

    assert seen[0].event == "SessionStart"
    assert seen[0].session_id == "s1"


@pytest.mark.asyncio
async def test_hook_socket_server_accepts_normalized_claude_payload(tmp_path) -> None:
    seen: list[HookEvent] = []
    delivered = asyncio.Event()
    socket_path = _socket_path()
    server = HookSocketServer(str(socket_path))

    async def on_event(event: HookEvent) -> None:
        seen.append(event)
        delivered.set()

    await server.start(on_event)
    reader, writer = await _send_event(
        socket_path,
        {
            "session_id": "claude-session-1",
            "cwd": "/tmp/project",
            "event": "PreToolUse",
            "status": "running_tool",
            "tool": "Bash",
            "tool_input": {"command": "pwd"},
            "tool_use_id": "tool-1",
            "pid": 123,
            "tty": "/dev/ttys001",
            "notification_type": None,
            "message": None,
        },
    )
    await asyncio.wait_for(delivered.wait(), timeout=1)
    assert await reader.read() == b""
    writer.close()
    await writer.wait_closed()
    await server.stop()

    assert seen[0].event == "PreToolUse"
    assert seen[0].status == "running_tool"
    assert seen[0].tool == "Bash"
    assert seen[0].tool_input == {"command": "pwd"}
    assert seen[0].tool_use_id == "tool-1"
    assert seen[0].pid == 123
    assert seen[0].tty == "/dev/ttys001"


@pytest.mark.asyncio
async def test_hook_socket_server_resolves_permission_tool_use_id_and_replies(tmp_path) -> None:
    seen: list[HookEvent] = []
    delivered = asyncio.Event()
    socket_path = _socket_path()
    server = HookSocketServer(str(socket_path))

    async def on_event(event: HookEvent) -> None:
        seen.append(event)
        if event.event == "PermissionRequest":
            delivered.set()

    await server.start(on_event)

    pre_reader, pre_writer = await _send_event(
        socket_path,
        {
            "session_id": "s1",
            "cwd": "/tmp/project",
            "event": "PreToolUse",
            "status": "running_tool",
            "tool": "Bash",
            "tool_input": {"command": "pwd"},
            "tool_use_id": "tool-1",
        },
    )
    assert await pre_reader.read() == b""
    pre_writer.close()
    await pre_writer.wait_closed()

    reader, writer = await _send_event(
        socket_path,
        {
            "session_id": "s1",
            "cwd": "/tmp/project",
            "event": "PermissionRequest",
            "status": "waiting_for_approval",
            "tool": "Bash",
            "tool_input": {"command": "pwd"},
        },
    )

    await asyncio.wait_for(delivered.wait(), timeout=1)
    pending = await server.get_pending_permission(session_id="s1")
    assert pending == ("Bash", "tool-1", {"command": "pwd"})
    assert seen[-1].tool_use_id == "tool-1"

    sent = await server.respond_to_permission(tool_use_id="tool-1", decision="allow", reason="ok")
    response = await asyncio.wait_for(reader.read(), timeout=1)
    writer.close()
    await writer.wait_closed()
    await server.stop()

    assert sent is True
    assert json.loads(response.decode("utf-8")) == {"decision": "allow", "reason": "ok"}


@pytest.mark.asyncio
async def test_hook_socket_server_emits_permission_failure_when_writer_breaks(tmp_path) -> None:
    socket_path = _socket_path()
    server = HookSocketServer(str(socket_path))
    failures: list[tuple[str, str]] = []
    delivered = asyncio.Event()

    async def on_event(event: HookEvent) -> None:
        if event.event == "PermissionRequest":
            delivered.set()

    async def on_failure(session_id: str, tool_use_id: str) -> None:
        failures.append((session_id, tool_use_id))

    await server.start(on_event, on_failure)

    pre_reader, pre_writer = await _send_event(
        socket_path,
        {
            "session_id": "s1",
            "cwd": "/tmp/project",
            "event": "PreToolUse",
            "status": "running_tool",
            "tool": "Bash",
            "tool_input": {"command": "pwd"},
            "tool_use_id": "tool-1",
        },
    )
    assert await pre_reader.read() == b""
    pre_writer.close()
    await pre_writer.wait_closed()

    reader, writer = await _send_event(
        socket_path,
        {
            "session_id": "s1",
            "cwd": "/tmp/project",
            "event": "PermissionRequest",
            "status": "waiting_for_approval",
            "tool": "Bash",
            "tool_input": {"command": "pwd"},
        },
    )
    await asyncio.wait_for(delivered.wait(), timeout=1)
    writer.close()
    await writer.wait_closed()
    await server.respond_to_permission(tool_use_id="tool-1", decision="allow")
    await asyncio.sleep(0)
    await server.stop()

    assert reader.at_eof() or failures == [("s1", "tool-1")]
