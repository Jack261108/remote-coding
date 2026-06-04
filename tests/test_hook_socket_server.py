from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

import pytest

from app.adapters.claude.hook_socket_server import HookSocketServer
from app.domain.hook_models import HookEvent, PendingPermissionRequest


def _socket_path() -> Path:
    return Path("/tmp") / f"rc-hook-{uuid.uuid4().hex}.sock"


async def _send_raw(socket_path, payload: bytes) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    reader, writer = await asyncio.open_unix_connection(str(socket_path))
    writer.write(payload)
    await writer.drain()
    if writer.can_write_eof():
        writer.write_eof()
    return reader, writer


async def _send_event(socket_path, payload: dict) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    return await _send_raw(socket_path, json.dumps(payload).encode("utf-8"))


class BrokenWriter:
    def write(self, data: bytes) -> None:
        return None

    async def drain(self) -> None:
        raise RuntimeError("writer closed")

    def close(self) -> None:
        return None

    async def wait_closed(self) -> None:
        return None


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
    assert json.loads(response.decode("utf-8")) == {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {
                "behavior": "allow",
            },
        }
    }


@pytest.mark.asyncio
async def test_hook_socket_server_matches_permission_denied_without_tool_use_id_to_pending_request(tmp_path) -> None:
    delivered = asyncio.Event()
    resolved = asyncio.Event()
    resolutions: list[tuple[str, str, str]] = []
    socket_path = _socket_path()
    server = HookSocketServer(str(socket_path))

    async def on_event(event: HookEvent) -> None:
        if event.event == "PermissionRequest":
            delivered.set()

    async def on_permission_resolved(session_id: str, tool_use_id: str, reason: str) -> None:
        resolutions.append((session_id, tool_use_id, reason))
        resolved.set()

    await server.start(on_event, on_permission_resolved=on_permission_resolved)
    try:
        reader, writer = await _send_event(
            socket_path,
            {
                "session_id": "s1",
                "cwd": "/tmp/project",
                "event": "PermissionRequest",
                "status": "waiting_for_approval",
                "tool": "Bash",
                "tool_input": {"command": "pwd"},
                "tool_use_id": "tool-1",
            },
        )
        await asyncio.wait_for(delivered.wait(), timeout=1)
        assert await server.get_pending_permission(session_id="s1") == ("Bash", "tool-1", {"command": "pwd"})

        denied_reader, denied_writer = await _send_event(
            socket_path,
            {
                "session_id": "s1",
                "cwd": "/tmp/project",
                "event": "PermissionDenied",
                "status": "processing",
                "tool": "Bash",
                "tool_input": {"command": "pwd"},
            },
        )
        assert await denied_reader.read() == b""
        denied_writer.close()
        await denied_writer.wait_closed()

        await asyncio.wait_for(resolved.wait(), timeout=1)
        assert resolutions == [("s1", "tool-1", "terminal_denied")]
        assert await server.get_pending_permission(session_id="s1") is None
        assert await reader.read() == b""
        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_hook_socket_server_relaxed_matches_permission_tool_use_id_when_input_differs(tmp_path) -> None:
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
            "tool_input": {
                "command": "bash test.sh",
                "description": "执行测试脚本",
                "timeout": 120000,
            },
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
            "tool_input": {
                "command": "bash test.sh",
            },
        },
    )

    await asyncio.wait_for(delivered.wait(), timeout=1)
    pending = await server.get_pending_permission(session_id="s1")
    assert pending == ("Bash", "tool-1", {"command": "bash test.sh"})
    assert seen[-1].tool_use_id == "tool-1"

    sent = await server.respond_to_permission(tool_use_id="tool-1", decision="allow")
    response = await asyncio.wait_for(reader.read(), timeout=1)
    writer.close()
    await writer.wait_closed()
    await server.stop()

    assert sent is True
    assert json.loads(response.decode("utf-8")) == {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {
                "behavior": "allow",
            },
        }
    }


@pytest.mark.asyncio
async def test_hook_socket_server_does_not_match_cache_after_post_tool_use(tmp_path) -> None:
    seen: list[HookEvent] = []
    delivered = asyncio.Event()
    socket_path = _socket_path()
    server = HookSocketServer(str(socket_path))

    async def on_event(event: HookEvent) -> None:
        seen.append(event)
        if event.event == "PermissionRequest":
            delivered.set()

    await server.start(on_event)
    try:
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

        post_reader, post_writer = await _send_event(
            socket_path,
            {
                "session_id": "s1",
                "cwd": "/tmp/project",
                "event": "PostToolUse",
                "status": "running_tool",
                "tool": "Bash",
                "tool_input": {"command": "pwd"},
                "tool_use_id": "tool-1",
            },
        )
        assert await post_reader.read() == b""
        post_writer.close()
        await post_writer.wait_closed()

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
        assert pending is not None
        _, tool_use_id, _ = pending
        assert tool_use_id.startswith("hookperm-")
        assert seen[-1].tool_use_id == tool_use_id

        assert await server.respond_to_permission(tool_use_id=tool_use_id, decision="allow") is True
        assert await asyncio.wait_for(reader.read(), timeout=1)
        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_hook_socket_server_trims_oldest_cached_tool_use_id_when_limit_reached(tmp_path) -> None:
    seen: list[HookEvent] = []
    delivered = asyncio.Event()
    socket_path = _socket_path()
    server = HookSocketServer(str(socket_path), max_tool_use_id_cache_entries=1)

    async def on_event(event: HookEvent) -> None:
        seen.append(event)
        if event.event == "PermissionRequest":
            delivered.set()

    await server.start(on_event)
    try:
        first_pre_reader, first_pre_writer = await _send_event(
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
        assert await first_pre_reader.read() == b""
        first_pre_writer.close()
        await first_pre_writer.wait_closed()

        second_pre_reader, second_pre_writer = await _send_event(
            socket_path,
            {
                "session_id": "s1",
                "cwd": "/tmp/project",
                "event": "PreToolUse",
                "status": "running_tool",
                "tool": "Bash",
                "tool_input": {"command": "ls"},
                "tool_use_id": "tool-2",
            },
        )
        assert await second_pre_reader.read() == b""
        second_pre_writer.close()
        await second_pre_writer.wait_closed()

        first_reader, first_writer = await _send_event(
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
        first_pending = await server.get_pending_permission(session_id="s1")
        assert first_pending is not None
        _, first_tool_use_id, _ = first_pending
        assert first_tool_use_id.startswith("hookperm-")
        assert first_tool_use_id != "tool-1"
        assert await server.respond_to_permission(tool_use_id=first_tool_use_id, decision="allow") is True
        assert await asyncio.wait_for(first_reader.read(), timeout=1)
        first_writer.close()
        await first_writer.wait_closed()

        delivered.clear()
        second_reader, second_writer = await _send_event(
            socket_path,
            {
                "session_id": "s1",
                "cwd": "/tmp/project",
                "event": "PermissionRequest",
                "status": "waiting_for_approval",
                "tool": "Bash",
                "tool_input": {"command": "ls"},
            },
        )
        await asyncio.wait_for(delivered.wait(), timeout=1)
        assert await server.get_pending_permission(session_id="s1") == ("Bash", "tool-2", {"command": "ls"})
        assert seen[-1].tool_use_id == "tool-2"
        assert await server.respond_to_permission(tool_use_id="tool-2", decision="allow") is True
        assert await asyncio.wait_for(second_reader.read(), timeout=1)
        second_writer.close()
        await second_writer.wait_closed()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_hook_socket_server_keeps_permission_request_open_with_synthetic_id_when_cache_missing(tmp_path) -> None:
    seen: list[HookEvent] = []
    delivered = asyncio.Event()
    socket_path = _socket_path()
    server = HookSocketServer(str(socket_path))

    async def on_event(event: HookEvent) -> None:
        seen.append(event)
        if event.event == "PermissionRequest":
            delivered.set()

    await server.start(on_event)

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
    assert pending is not None
    tool_name, tool_use_id, tool_input = pending
    assert tool_name == "Bash"
    assert tool_use_id.startswith("hookperm-")
    assert tool_input == {"command": "pwd"}
    assert seen[-1].tool_use_id == tool_use_id

    sent = await server.respond_to_permission(tool_use_id=tool_use_id, decision="allow")
    response = await asyncio.wait_for(reader.read(), timeout=1)
    writer.close()
    await writer.wait_closed()
    await server.stop()

    assert sent is True
    assert json.loads(response.decode("utf-8")) == {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {
                "behavior": "allow",
            },
        }
    }


@pytest.mark.asyncio
async def test_hook_socket_server_permission_deny_includes_message(tmp_path) -> None:
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
            "tool_input": {"command": "rm -rf /tmp/demo"},
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
            "tool_input": {"command": "rm -rf /tmp/demo"},
        },
    )

    await asyncio.wait_for(delivered.wait(), timeout=1)

    sent = await server.respond_to_permission(tool_use_id="tool-1", decision="deny", reason="用户拒绝了这次操作")
    response = await asyncio.wait_for(reader.read(), timeout=1)
    writer.close()
    await writer.wait_closed()
    await server.stop()

    assert sent is True
    assert json.loads(response.decode("utf-8")) == {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {
                "behavior": "deny",
                "message": "用户拒绝了这次操作",
            },
        }
    }


@pytest.mark.asyncio
async def test_hook_socket_server_returns_false_and_emits_failure_when_writer_breaks(tmp_path) -> None:
    server = HookSocketServer(str(_socket_path()))
    failures: list[tuple[str, str]] = []

    async def on_failure(session_id: str, tool_use_id: str) -> None:
        failures.append((session_id, tool_use_id))

    server._permission_failure_handler = on_failure
    event = HookEvent(
        session_id="s1",
        cwd="/tmp/project",
        event="PermissionRequest",
        status="waiting_for_approval",
        tool="Bash",
        tool_input={"command": "pwd"},
        tool_use_id="tool-1",
    )
    server._pending_permissions["tool-1"] = PendingPermissionRequest(
        session_id="s1",
        tool_use_id="tool-1",
        writer=BrokenWriter(),
        event=event,
    )

    sent = await server.respond_to_permission(tool_use_id="tool-1", decision="allow")

    assert sent is False
    assert failures == [("s1", "tool-1")]
    assert await server.get_pending_permission(session_id="s1") is None


@pytest.mark.asyncio
async def test_hook_socket_server_sets_socket_permissions(tmp_path) -> None:
    socket_path = _socket_path()
    server = HookSocketServer(str(socket_path))

    async def on_event(event: HookEvent) -> None:
        return None

    await server.start(on_event)
    try:
        assert socket_path.exists()
        assert socket_path.stat().st_mode & 0o777 == 0o600
    finally:
        await server.stop()

    assert not socket_path.exists()


@pytest.mark.asyncio
async def test_hook_socket_server_rejects_invalid_hook_payloads(tmp_path) -> None:
    seen: list[HookEvent] = []
    socket_path = _socket_path()
    server = HookSocketServer(str(socket_path))

    async def on_event(event: HookEvent) -> None:
        seen.append(event)

    await server.start(on_event)
    invalid_payloads = [
        {"session_id": "../evil", "cwd": "/tmp/project", "event": "SessionStart", "status": "starting"},
        {"session_id": "s1", "cwd": "relative/path", "event": "SessionStart", "status": "starting"},
        {"session_id": "s1", "cwd": "/tmp/project", "event": "Unknown", "status": "starting"},
        {"session_id": "s1", "cwd": "/tmp/project", "event": "SessionStart", "status": "unknown"},
        {"session_id": "s1", "cwd": "/tmp/project", "event": "SessionStart", "status": "starting", "pid": "123"},
    ]
    try:
        for payload in invalid_payloads:
            reader, writer = await _send_event(socket_path, payload)
            assert await asyncio.wait_for(reader.read(), timeout=1) == b""
            writer.close()
            await writer.wait_closed()
    finally:
        await server.stop()

    assert seen == []


@pytest.mark.asyncio
async def test_hook_socket_server_rejects_oversized_message(tmp_path) -> None:
    seen: list[HookEvent] = []
    socket_path = _socket_path()
    server = HookSocketServer(str(socket_path), max_message_bytes=32)

    async def on_event(event: HookEvent) -> None:
        seen.append(event)

    await server.start(on_event)
    try:
        reader, writer = await _send_raw(
            socket_path, b'{"session_id":"s1","cwd":"/tmp/project","event":"SessionStart","status":"starting"}'
        )
        assert await asyncio.wait_for(reader.read(), timeout=1) == b""
        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()

    assert seen == []


@pytest.mark.asyncio
async def test_hook_socket_server_rejects_workdir_outside_allowlist(tmp_path) -> None:
    seen: list[HookEvent] = []
    delivered = asyncio.Event()
    socket_path = _socket_path()
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    server = HookSocketServer(str(socket_path), allowed_workdirs=[str(allowed)])

    async def on_event(event: HookEvent) -> None:
        seen.append(event)
        delivered.set()

    await server.start(on_event)
    try:
        reader, writer = await _send_event(
            socket_path,
            {"session_id": "s1", "cwd": str(allowed), "event": "SessionStart", "status": "starting"},
        )
        await asyncio.wait_for(delivered.wait(), timeout=1)
        assert await reader.read() == b""
        writer.close()
        await writer.wait_closed()

        reader, writer = await _send_event(
            socket_path,
            {"session_id": "s2", "cwd": str(outside), "event": "SessionStart", "status": "starting"},
        )
        assert await asyncio.wait_for(reader.read(), timeout=1) == b""
        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()

    assert [event.session_id for event in seen] == ["s1"]


@pytest.mark.asyncio
async def test_hook_socket_server_expires_pending_permission(tmp_path) -> None:
    failures: list[tuple[str, str]] = []
    delivered = asyncio.Event()
    socket_path = _socket_path()
    server = HookSocketServer(str(socket_path), pending_permission_ttl_sec=1)

    async def on_event(event: HookEvent) -> None:
        if event.event == "PermissionRequest":
            delivered.set()

    async def on_failure(session_id: str, tool_use_id: str) -> None:
        failures.append((session_id, tool_use_id))

    await server.start(on_event, on_failure)
    try:
        reader, writer = await _send_event(
            socket_path,
            {
                "session_id": "s1",
                "cwd": "/tmp/project",
                "event": "PermissionRequest",
                "status": "waiting_for_approval",
                "tool": "Bash",
                "tool_input": {"command": "pwd"},
                "tool_use_id": "tool-ttl",
            },
        )
        await asyncio.wait_for(delivered.wait(), timeout=1)

        response = await asyncio.wait_for(reader.read(), timeout=2)
        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()

    assert failures == [("s1", "tool-ttl")]
    assert json.loads(response.decode("utf-8"))["hookSpecificOutput"]["decision"] == {
        "behavior": "deny",
        "message": "permission request expired",
    }


@pytest.mark.asyncio
async def test_hook_socket_server_rejects_permission_when_pending_limit_reached(tmp_path) -> None:
    seen: list[str | None] = []
    first_delivered = asyncio.Event()
    socket_path = _socket_path()
    server = HookSocketServer(str(socket_path), max_pending_permissions=1)

    async def on_event(event: HookEvent) -> None:
        seen.append(event.tool_use_id)
        if event.tool_use_id == "tool-1":
            first_delivered.set()

    await server.start(on_event)
    try:
        first_reader, first_writer = await _send_event(
            socket_path,
            {
                "session_id": "s1",
                "cwd": "/tmp/project",
                "event": "PermissionRequest",
                "status": "waiting_for_approval",
                "tool": "Bash",
                "tool_input": {"command": "pwd"},
                "tool_use_id": "tool-1",
            },
        )
        await asyncio.wait_for(first_delivered.wait(), timeout=1)

        second_reader, second_writer = await _send_event(
            socket_path,
            {
                "session_id": "s1",
                "cwd": "/tmp/project",
                "event": "PermissionRequest",
                "status": "waiting_for_approval",
                "tool": "Bash",
                "tool_input": {"command": "ls"},
                "tool_use_id": "tool-2",
            },
        )
        second_response = await asyncio.wait_for(second_reader.read(), timeout=1)
        second_writer.close()
        await second_writer.wait_closed()
        assert json.loads(second_response.decode("utf-8"))["hookSpecificOutput"]["decision"] == {
            "behavior": "deny",
            "message": "pending permission limit reached",
        }
        assert await server.get_pending_permission(session_id="s1") == ("Bash", "tool-1", {"command": "pwd"})

        assert await server.respond_to_permission(tool_use_id="tool-1", decision="allow") is True
        assert await asyncio.wait_for(first_reader.read(), timeout=1)
        first_writer.close()
        await first_writer.wait_closed()
    finally:
        await server.stop()

    assert seen == ["tool-1"]
