from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

from app.domain.hook_models import HookEvent, HookResponse, PendingPermission

HookEventHandler = Callable[[HookEvent], Awaitable[None] | None]
PermissionFailureHandler = Callable[[str, str], Awaitable[None] | None]


class HookSocketServer:
    def __init__(self, socket_path: str) -> None:
        self._socket_path = Path(socket_path)
        self._server: asyncio.AbstractServer | None = None
        self._event_handler: HookEventHandler | None = None
        self._permission_failure_handler: PermissionFailureHandler | None = None
        self._pending_permissions: dict[str, PendingPermission] = {}
        self._tool_use_id_cache: dict[str, list[str]] = {}
        self._lock = asyncio.Lock()

    async def start(
        self,
        on_event: HookEventHandler,
        on_permission_failure: PermissionFailureHandler | None = None,
    ) -> None:
        if self._server is not None:
            return
        self._event_handler = on_event
        self._permission_failure_handler = on_permission_failure
        self._socket_path.parent.mkdir(parents=True, exist_ok=True)
        with suppress(FileNotFoundError):
            self._socket_path.unlink()
        self._server = await asyncio.start_unix_server(self._handle_client, path=str(self._socket_path))

    async def stop(self) -> None:
        server = self._server
        self._server = None
        if server is not None:
            server.close()
            await server.wait_closed()
        async with self._lock:
            pending = list(self._pending_permissions.values())
            self._pending_permissions.clear()
            self._tool_use_id_cache.clear()
        for item in pending:
            item.writer.close()
            with suppress(Exception):
                await item.writer.wait_closed()
        with suppress(FileNotFoundError):
            self._socket_path.unlink()

    async def respond_to_permission(self, *, tool_use_id: str, decision: str, reason: str | None = None) -> None:
        async with self._lock:
            pending = self._pending_permissions.pop(tool_use_id, None)
        if pending is None:
            return
        await self._write_response(pending=pending, decision=decision, reason=reason)

    async def respond_to_permission_by_session(self, *, session_id: str, decision: str, reason: str | None = None) -> None:
        async with self._lock:
            candidates = [item for item in self._pending_permissions.values() if item.session_id == session_id]
            pending = max(candidates, key=lambda item: item.received_at, default=None)
            if pending is not None:
                self._pending_permissions.pop(pending.tool_use_id, None)
        if pending is None:
            return
        await self._write_response(pending=pending, decision=decision, reason=reason)

    async def cancel_pending_permissions(self, *, session_id: str) -> None:
        async with self._lock:
            matching = [item for item in self._pending_permissions.values() if item.session_id == session_id]
            for item in matching:
                self._pending_permissions.pop(item.tool_use_id, None)
        for item in matching:
            item.writer.close()
            with suppress(Exception):
                await item.writer.wait_closed()

    async def has_pending_permission(self, *, session_id: str) -> bool:
        async with self._lock:
            return any(item.session_id == session_id for item in self._pending_permissions.values())

    async def get_pending_permission(self, *, session_id: str) -> tuple[str | None, str | None, dict[str, Any] | None] | None:
        async with self._lock:
            for item in self._pending_permissions.values():
                if item.session_id == session_id:
                    return item.event.tool, item.tool_use_id, item.event.tool_input
        return None

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        raw = await reader.read()
        if not raw:
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()
            return

        try:
            event = HookEvent.from_dict(json.loads(raw.decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, ValueError):
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()
            return

        if event.event == "PreToolUse" and event.tool_use_id:
            await self._cache_tool_use_id(event)
        if event.event == "SessionEnd":
            await self._cleanup_cache(event.session_id)

        keep_open = False
        emit_event = event
        if event.expects_response:
            tool_use_id = event.tool_use_id or await self._pop_cached_tool_use_id(event)
            if tool_use_id:
                emit_event = event.with_tool_use_id(tool_use_id)
                async with self._lock:
                    self._pending_permissions[tool_use_id] = PendingPermission(
                        session_id=emit_event.session_id,
                        tool_use_id=tool_use_id,
                        writer=writer,
                        event=emit_event,
                    )
                keep_open = True

        if not keep_open:
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()

        await self._emit_event(emit_event)

    async def _write_response(self, *, pending: PendingPermission, decision: str, reason: str | None) -> None:
        response = HookResponse(decision=decision, reason=reason)
        data = json.dumps(response.to_dict(), ensure_ascii=False).encode("utf-8")
        try:
            pending.writer.write(data)
            await pending.writer.drain()
        except Exception:
            await self._emit_permission_failure(pending.session_id, pending.tool_use_id)
        finally:
            pending.writer.close()
            with suppress(Exception):
                await pending.writer.wait_closed()

    async def _emit_event(self, event: HookEvent) -> None:
        if self._event_handler is None:
            return
        result = self._event_handler(event)
        if inspect.isawaitable(result):
            await result

    async def _emit_permission_failure(self, session_id: str, tool_use_id: str) -> None:
        if self._permission_failure_handler is None:
            return
        result = self._permission_failure_handler(session_id, tool_use_id)
        if inspect.isawaitable(result):
            await result

    async def _cache_tool_use_id(self, event: HookEvent) -> None:
        key = self._cache_key(event)
        async with self._lock:
            self._tool_use_id_cache.setdefault(key, []).append(event.tool_use_id or "")

    async def _pop_cached_tool_use_id(self, event: HookEvent) -> str | None:
        key = self._cache_key(event)
        async with self._lock:
            queue = self._tool_use_id_cache.get(key)
            if not queue:
                return None
            tool_use_id = queue.pop(0)
            if not queue:
                self._tool_use_id_cache.pop(key, None)
            return tool_use_id or None

    async def _cleanup_cache(self, session_id: str) -> None:
        async with self._lock:
            keys = [key for key in self._tool_use_id_cache if key.startswith(f"{session_id}:")]
            for key in keys:
                self._tool_use_id_cache.pop(key, None)

    def _cache_key(self, event: HookEvent) -> str:
        tool_input = event.tool_input or {}
        serialized = json.dumps(tool_input, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return f"{event.session_id}:{event.tool or 'unknown'}:{serialized}"
