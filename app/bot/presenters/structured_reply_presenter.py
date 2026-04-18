from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass

from app.services.task_service import TaskService

logger = logging.getLogger(__name__)

_FALLBACK_PROMPT = "结构化回复暂不可用，已回退为原始输出。"
_MARKER_LINE_RE = re.compile(r"^\s*_*(?:TGCLI_BEGIN|TGCLI_DONE)_*(?:\s*[:：]?\s*[A-Za-z0-9_-]+)?\s*$", re.IGNORECASE)
_BLANK_LINE_BURST_RE = re.compile(r"\n{3,}")
_STREAM_PREVIEW_CHAR_LIMIT = 1800
_STREAM_PREVIEW_LINE_LIMIT = 60
_PERMISSION_INPUT_CHAR_LIMIT = 280
_PERMISSION_INPUT_LINE_LIMIT = 8


@dataclass(frozen=True)
class _ToolStateSnapshot:
    tool_use_id: str
    tool_name: str | None
    tool_input: dict | None
    status: str | None


@dataclass
class _StructuredSnapshot:
    session_id: str | None
    turn_id: str | None
    reply: str
    session_available: bool
    phase: str | None = None
    pending_permission_key: str | None = None
    pending_permission_tool_use_id: str | None = None
    pending_permission_tool_name: str | None = None
    pending_permission_tool_input: dict | None = None
    tool_states: tuple[_ToolStateSnapshot, ...] = ()


@dataclass(frozen=True)
class PermissionRequestOutput:
    text: str
    tool_use_id: str
    tool_name: str | None = None


@dataclass(frozen=True)
class ProgressUpdateOutput:
    text: str


def strip_bridge_markers(text: str) -> str:
    if not text:
        return ""
    lines = text.split("\n")
    kept: list[str] = []
    for raw_line in lines:
        if _MARKER_LINE_RE.match(raw_line):
            continue
        kept.append(raw_line)
    return "\n".join(kept)


def normalize_stream_text(text: str) -> str:
    cleaned = strip_bridge_markers(text).replace("\r\n", "\n").replace("\r", "\n")
    if not cleaned.strip():
        return ""

    normalized_lines = [line.rstrip() for line in cleaned.split("\n")]
    normalized = "\n".join(normalized_lines).strip("\n")
    normalized = _BLANK_LINE_BURST_RE.sub("\n\n", normalized)
    return normalized.strip()


def preview_stream_text(text: str) -> str:
    normalized = normalize_stream_text(text)
    if not normalized:
        return ""

    lines = normalized.split("\n")
    needs_line_truncation = len(lines) > _STREAM_PREVIEW_LINE_LIMIT
    preview_lines = lines[:_STREAM_PREVIEW_LINE_LIMIT]
    preview = "\n".join(preview_lines)

    needs_char_truncation = len(preview) > _STREAM_PREVIEW_CHAR_LIMIT
    if needs_char_truncation:
        preview = preview[:_STREAM_PREVIEW_CHAR_LIMIT].rstrip()

    if needs_line_truncation or needs_char_truncation:
        preview = f"{preview}\n...[输出片段过长，已截断本条消息]"

    return preview


def _truncate_permission_text(text: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return ""

    lines = normalized.split("\n")
    needs_line_truncation = len(lines) > _PERMISSION_INPUT_LINE_LIMIT
    preview_lines = lines[:_PERMISSION_INPUT_LINE_LIMIT]
    preview = "\n".join(preview_lines)

    needs_char_truncation = len(preview) > _PERMISSION_INPUT_CHAR_LIMIT
    if needs_char_truncation:
        preview = preview[:_PERMISSION_INPUT_CHAR_LIMIT].rstrip()

    if needs_line_truncation or needs_char_truncation:
        preview = f"{preview}..."
    return preview


def _format_tool_input_detail(tool_name: str | None, tool_input: dict | None) -> tuple[str, str] | None:
    if not tool_input:
        return None

    tool = (tool_name or "").strip().lower()
    if tool == "bash":
        command = str(tool_input.get("command") or "").strip()
        if command:
            return "命令", _truncate_permission_text(command)

    if tool == "webfetch":
        url = str(tool_input.get("url") or "").strip()
        if url:
            return "目标", _truncate_permission_text(url)

    if tool in {"task", "agent"}:
        description = str(tool_input.get("description") or "").strip()
        if description:
            return "任务", _truncate_permission_text(description)

    for key, label in (
        ("description", "任务"),
        ("question", "问题"),
        ("query", "搜索"),
        ("file_path", "文件"),
        ("path", "文件"),
        ("url", "目标"),
        ("command", "命令"),
        ("pattern", "内容"),
    ):
        value = tool_input.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return label, _truncate_permission_text(text)

    serialized = json.dumps(tool_input, ensure_ascii=False, sort_keys=True, indent=2)
    return "参数", _truncate_permission_text(serialized)


def build_permission_prompt(*, tool_name: str | None, tool_input: dict | None = None) -> str:
    lines = ["权限请求"]
    if tool_name:
        lines.append(f"工具: {tool_name}")

    detail = _format_tool_input_detail(tool_name, tool_input)
    if detail is not None:
        label, value = detail
        lines.append(f"{label}: {value}")

    lines.append("")
    lines.append("请点击下方按钮选择允许或拒绝。")
    return "\n".join(lines)


def build_tool_progress_message(*, tool_name: str | None, tool_input: dict | None = None, resumed: bool = False) -> str:
    lines = ["继续执行" if resumed else "执行中"]
    if tool_name:
        lines.append(f"工具: {tool_name}")

    detail = _format_tool_input_detail(tool_name, tool_input)
    if detail is not None:
        label, value = detail
        lines.append(f"{label}: {value}")

    return "\n".join(lines)


def build_compacting_progress_message() -> str:
    return "执行进度\n正在整理上下文，稍后继续。"


class StructuredReplyPresenter:
    def __init__(self, *, task_service: TaskService, user_id: int) -> None:
        self._task_service = task_service
        self._user_id = user_id
        self._last_structured_turn_id: str | None = None
        self._last_pending_permission_key: str | None = None
        self._structured_session_available = False
        self._structured_reply_emitted_in_run = False
        self._fallback_announced = False
        self._revision = 0
        self._current_session_id: str | None = None
        self._last_phase: str | None = None
        self._tool_status_by_id: dict[str, str | None] = {}

    @property
    def structured_session_available(self) -> bool:
        return self._structured_session_available

    async def prime(self, *, log_missing: bool = True, baseline_current_snapshot: bool = False) -> None:
        snapshot = await self._load_snapshot(log_missing=log_missing)
        self._structured_session_available = snapshot.session_available
        self._current_session_id = snapshot.session_id
        self._last_phase = snapshot.phase

        cursor_getter = getattr(self._task_service, "get_structured_reply_cursor", None)
        if cursor_getter is not None:
            persisted_turn_id, persisted_permission_key = await cursor_getter(self._user_id)
            self._last_structured_turn_id = persisted_turn_id
            if self._last_structured_turn_id is None and baseline_current_snapshot:
                self._last_structured_turn_id = snapshot.turn_id
            self._last_pending_permission_key = persisted_permission_key
        else:
            self._last_structured_turn_id = snapshot.turn_id

        if baseline_current_snapshot:
            self._tool_status_by_id = {tool.tool_use_id: tool.status for tool in snapshot.tool_states}
        else:
            self._tool_status_by_id = {}

        revision_getter = getattr(self._task_service, "get_structured_session_cursor", None)
        if revision_getter is None:
            self._revision = 0
            return
        self._revision = await revision_getter(self._user_id)

    async def wait_for_update(self, *, timeout_sec: float) -> bool:
        wait_for_update = getattr(self._task_service, "wait_for_structured_session_update", None)
        cursor_getter = getattr(self._task_service, "get_structured_session_cursor", None)
        if wait_for_update is None or cursor_getter is None:
            await asyncio.sleep(timeout_sec)
            return True
        current_session = await self._task_service.get_structured_session(self._user_id, log_missing=False)
        current_session_id = current_session.session_id if current_session is not None else None
        if current_session_id != self._current_session_id:
            self._current_session_id = current_session_id
            self._last_phase = None
            self._last_pending_permission_key = None
            self._tool_status_by_id = {}
            self._revision = await cursor_getter(self._user_id)
            return True
        changed = await wait_for_update(
            user_id=self._user_id,
            since_cursor=self._revision,
            timeout_sec=timeout_sec,
        )
        if changed:
            self._revision = await cursor_getter(self._user_id)
        return changed

    async def poll(self, *, task_id: str, final: bool = False, log_missing: bool = False) -> list[str | PermissionRequestOutput | ProgressUpdateOutput]:
        snapshot = await self._load_snapshot(log_missing=log_missing)
        self._structured_session_available = self._structured_session_available or snapshot.session_available

        messages: list[str | PermissionRequestOutput | ProgressUpdateOutput] = []
        messages.extend(self._collect_progress_updates(snapshot=snapshot))
        acknowledger = getattr(self._task_service, "acknowledge_structured_reply", None)
        if snapshot.phase == "waiting_for_approval" and snapshot.pending_permission_key and snapshot.pending_permission_key != self._last_pending_permission_key:
            self._last_pending_permission_key = snapshot.pending_permission_key
            if snapshot.pending_permission_tool_use_id:
                messages.append(
                    PermissionRequestOutput(
                        text=build_permission_prompt(
                            tool_name=snapshot.pending_permission_tool_name,
                            tool_input=snapshot.pending_permission_tool_input,
                        ),
                        tool_use_id=snapshot.pending_permission_tool_use_id,
                        tool_name=snapshot.pending_permission_tool_name,
                    )
                )
            else:
                messages.append(
                    build_permission_prompt(
                        tool_name=snapshot.pending_permission_tool_name,
                        tool_input=snapshot.pending_permission_tool_input,
                    )
                )
            if acknowledger is not None:
                await acknowledger(
                    self._user_id,
                    permission_key=snapshot.pending_permission_key,
                )
        elif snapshot.phase != "waiting_for_approval":
            self._last_pending_permission_key = snapshot.pending_permission_key

        reply = await self._collect_reply(task_id=task_id, snapshot=snapshot, log_missing=log_missing)
        if reply:
            messages.append(reply)

        if final and self._structured_session_available and not self._structured_reply_emitted_in_run and not self._fallback_announced:
            self._fallback_announced = True
            logger.warning(
                "structured reply fallback emitted",
                extra={"task_id": task_id, "user_id": self._user_id, "phase": snapshot.phase},
            )
            messages.append(_FALLBACK_PROMPT)

        return messages

    async def _collect_reply(self, *, task_id: str, snapshot: _StructuredSnapshot, log_missing: bool) -> str | None:
        if not snapshot.turn_id:
            if log_missing:
                logger.info("structured reply skipped", extra={"task_id": task_id, "user_id": self._user_id, "reason": "no_turn_id"})
            return None
        if not snapshot.reply:
            if log_missing:
                logger.info(
                    "structured reply skipped",
                    extra={"task_id": task_id, "user_id": self._user_id, "turn_id": snapshot.turn_id, "reason": "empty_preview"},
                )
            return None
        if snapshot.turn_id == self._last_structured_turn_id:
            if log_missing:
                logger.info(
                    "structured reply skipped",
                    extra={"task_id": task_id, "user_id": self._user_id, "turn_id": snapshot.turn_id, "reason": "duplicate_turn"},
                )
            return None

        self._last_structured_turn_id = snapshot.turn_id
        self._structured_reply_emitted_in_run = True
        acknowledger = getattr(self._task_service, "acknowledge_structured_reply", None)
        if acknowledger is not None:
            await acknowledger(self._user_id, turn_id=snapshot.turn_id)
        logger.info("[task %s][structured] %s", task_id, snapshot.reply.rstrip("\n"))
        return snapshot.reply

    def _collect_progress_updates(self, *, snapshot: _StructuredSnapshot) -> list[ProgressUpdateOutput]:
        messages: list[ProgressUpdateOutput] = []
        if snapshot.phase == "compacting" and self._last_phase != "compacting":
            messages.append(ProgressUpdateOutput(text=build_compacting_progress_message()))
        self._last_phase = snapshot.phase

        current_status_by_id: dict[str, str | None] = {}
        for tool in snapshot.tool_states:
            current_status_by_id[tool.tool_use_id] = tool.status
            if tool.status != "running":
                continue
            previous_status = self._tool_status_by_id.get(tool.tool_use_id)
            if previous_status == "running":
                continue
            messages.append(
                ProgressUpdateOutput(
                    text=build_tool_progress_message(
                        tool_name=tool.tool_name,
                        tool_input=tool.tool_input,
                        resumed=previous_status == "waiting_for_approval",
                    )
                )
            )
        self._tool_status_by_id = current_status_by_id
        return messages

    async def _load_snapshot(self, *, log_missing: bool) -> _StructuredSnapshot:
        session = await self._task_service.get_structured_session(self._user_id, log_missing=log_missing)
        if session is None:
            if log_missing:
                logger.info("structured reply unavailable", extra={"user_id": self._user_id, "reason": "no_structured_session"})
            return _StructuredSnapshot(session_id=None, turn_id=None, reply="", session_available=False)

        phase = session.phase.value
        tool_states = tuple(self._collect_tool_states(session))
        pending = getattr(session, "pending_permission", None)
        pending_permission_key = None
        pending_permission_tool_use_id = None
        pending_permission_tool_name = None
        pending_permission_tool_input = None
        if pending is not None:
            pending_permission_key = f"{pending.tool_use_id}:{pending.tool_name}"
            pending_permission_tool_use_id = pending.tool_use_id
            pending_permission_tool_name = pending.tool_name
            pending_permission_tool_input = pending.tool_input

        if not session.turns:
            logger.info(
                "structured reply unavailable",
                extra={"user_id": self._user_id, "reason": "no_turns", "phase": phase},
            )
            return _StructuredSnapshot(
                session_id=session.session_id,
                turn_id=None,
                reply="",
                session_available=True,
                phase=phase,
                pending_permission_key=pending_permission_key,
                pending_permission_tool_use_id=pending_permission_tool_use_id,
                pending_permission_tool_name=pending_permission_tool_name,
                pending_permission_tool_input=pending_permission_tool_input,
                tool_states=tool_states,
            )

        for turn in reversed(session.turns):
            if turn.role != "assistant" or not turn.is_complete:
                continue
            preview = preview_stream_text(turn.text)
            logger.info(
                "structured reply loaded",
                extra={
                    "user_id": self._user_id,
                    "turn_id": turn.turn_id,
                    "phase": phase,
                    "turn_count": len(session.turns),
                    "preview_len": len(preview),
                },
            )
            return _StructuredSnapshot(
                session_id=session.session_id,
                turn_id=turn.turn_id,
                reply=preview,
                session_available=True,
                phase=phase,
                pending_permission_key=pending_permission_key,
                pending_permission_tool_use_id=pending_permission_tool_use_id,
                pending_permission_tool_name=pending_permission_tool_name,
                pending_permission_tool_input=pending_permission_tool_input,
                tool_states=tool_states,
            )

        logger.info(
            "structured reply unavailable",
            extra={
                "user_id": self._user_id,
                "reason": "no_completed_assistant_turn",
                "phase": phase,
                "turn_count": len(session.turns),
            },
        )
        return _StructuredSnapshot(
            session_id=session.session_id,
            turn_id=None,
            reply="",
            session_available=True,
            phase=phase,
            pending_permission_key=pending_permission_key,
            pending_permission_tool_use_id=pending_permission_tool_use_id,
            pending_permission_tool_name=pending_permission_tool_name,
            pending_permission_tool_input=pending_permission_tool_input,
            tool_states=tool_states,
        )

    def _collect_tool_states(self, session) -> list[_ToolStateSnapshot]:
        tool_calls = getattr(session, "tool_calls", {}) or {}
        if not isinstance(tool_calls, dict):
            return []

        states: list[_ToolStateSnapshot] = []
        for tool_use_id, tool in tool_calls.items():
            status = getattr(tool, "status", None)
            status_value = getattr(status, "value", status)
            tool_name = getattr(tool, "name", None)
            tool_input = getattr(tool, "input", None)
            if tool_input is not None and not isinstance(tool_input, dict):
                tool_input = None
            states.append(
                _ToolStateSnapshot(
                    tool_use_id=str(tool_use_id),
                    tool_name=str(tool_name) if tool_name is not None else None,
                    tool_input=tool_input,
                    status=str(status_value) if status_value is not None else None,
                )
            )
        return states
