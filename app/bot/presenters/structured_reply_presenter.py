from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

from app.services.task_service import TaskService

logger = logging.getLogger(__name__)

_PERMISSION_PROMPT = "检测到权限请求，请发送 /approve 或 /deny [reason]。"
_FALLBACK_PROMPT = "结构化回复暂不可用，已回退为原始输出。"
_MARKER_LINE_RE = re.compile(r"^\s*_*(?:TGCLI_BEGIN|TGCLI_DONE)_*(?:\s*[:：]?\s*[A-Za-z0-9_-]+)?\s*$", re.IGNORECASE)
_BLANK_LINE_BURST_RE = re.compile(r"\n{3,}")
_STREAM_PREVIEW_CHAR_LIMIT = 1800
_STREAM_PREVIEW_LINE_LIMIT = 60


@dataclass
class _StructuredSnapshot:
    session_id: str | None
    turn_id: str | None
    reply: str
    session_available: bool
    phase: str | None = None
    pending_permission_key: str | None = None


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

    @property
    def structured_session_available(self) -> bool:
        return self._structured_session_available

    async def prime(self, *, log_missing: bool = True, baseline_current_snapshot: bool = False) -> None:
        snapshot = await self._load_snapshot(log_missing=log_missing)
        self._structured_session_available = snapshot.session_available
        self._current_session_id = snapshot.session_id

        cursor_getter = getattr(self._task_service, "get_structured_reply_cursor", None)
        if cursor_getter is not None:
            persisted_turn_id, persisted_permission_key = await cursor_getter(self._user_id)
            self._last_structured_turn_id = persisted_turn_id
            if self._last_structured_turn_id is None and baseline_current_snapshot:
                self._last_structured_turn_id = snapshot.turn_id
            self._last_pending_permission_key = persisted_permission_key
        else:
            self._last_structured_turn_id = snapshot.turn_id

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

    async def poll(self, *, task_id: str, final: bool = False, log_missing: bool = False) -> list[str]:
        snapshot = await self._load_snapshot(log_missing=log_missing)
        self._structured_session_available = self._structured_session_available or snapshot.session_available

        messages: list[str] = []
        acknowledger = getattr(self._task_service, "acknowledge_structured_reply", None)
        if snapshot.phase == "waiting_for_approval" and snapshot.pending_permission_key and snapshot.pending_permission_key != self._last_pending_permission_key:
            self._last_pending_permission_key = snapshot.pending_permission_key
            messages.append(_PERMISSION_PROMPT)
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

    async def _load_snapshot(self, *, log_missing: bool) -> _StructuredSnapshot:
        session = await self._task_service.get_structured_session(self._user_id, log_missing=log_missing)
        if session is None:
            if log_missing:
                logger.info("structured reply unavailable", extra={"user_id": self._user_id, "reason": "no_structured_session"})
            return _StructuredSnapshot(session_id=None, turn_id=None, reply="", session_available=False)

        phase = session.phase.value
        pending = getattr(session, "pending_permission", None)
        pending_permission_key = None
        if pending is not None:
            pending_permission_key = f"{pending.tool_use_id}:{pending.tool_name}"

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
        )
