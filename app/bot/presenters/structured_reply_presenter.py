from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from app.domain.user_question_models import UserQuestionPrompt, extract_user_question_prompts
from app.domain.session_models import SessionPhase, ToolStatus
from app.services.session_store import parse_user_question_key
from app.services.task_service import TaskService

logger = logging.getLogger(__name__)

_FALLBACK_PROMPT = "结构化回复暂不可用，已回退为原始输出。"
_MARKER_LINE_RE = re.compile(r"^\s*_*(?:TGCLI_BEGIN|TGCLI_DONE)_*(?:\s*[:：]?\s*[A-Za-z0-9_-]+)?\s*$", re.IGNORECASE)
_BLANK_LINE_BURST_RE = re.compile(r"\n{3,}")
_STREAM_PREVIEW_CHAR_LIMIT = 1800
_STREAM_PREVIEW_LINE_LIMIT = 60
_PERMISSION_INPUT_CHAR_LIMIT = 280
_PERMISSION_INPUT_LINE_LIMIT = 8
_QUESTION_TEXT_CHAR_LIMIT = 360
_QUESTION_TEXT_LINE_LIMIT = 10


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


@dataclass(frozen=True)
class UserQuestionOutput:
    text: str
    question: UserQuestionPrompt


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


def _truncate_question_text(text: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return ""

    lines = normalized.split("\n")
    needs_line_truncation = len(lines) > _QUESTION_TEXT_LINE_LIMIT
    preview_lines = lines[:_QUESTION_TEXT_LINE_LIMIT]
    preview = "\n".join(preview_lines)

    needs_char_truncation = len(preview) > _QUESTION_TEXT_CHAR_LIMIT
    if needs_char_truncation:
        preview = preview[:_QUESTION_TEXT_CHAR_LIMIT].rstrip()

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


def build_user_question_prompt(question: UserQuestionPrompt) -> str:
    lines = ["需要你选择"]
    if question.total_questions > 1:
        lines.append(f"问题: {question.question_index + 1}/{question.total_questions}")
    if question.header:
        lines.append(f"主题: {question.header}")
    lines.append(f"内容: {_truncate_question_text(question.question)}")

    if question.options:
        lines.append("")
        for index, option in enumerate(question.options, start=1):
            lines.append(f"{index}. {_truncate_question_text(option.label)}")
            if option.description:
                lines.append(f"   {_truncate_question_text(option.description)}")

    lines.append("")
    if question.multi_select:
        lines.append("可多选，请先勾选需要的选项，再点击“提交选择”；如果要自己补充说明，也可以直接回复文字。")
    else:
        lines.append("请点击下方按钮；如果要自己补充说明，也可以直接回复文字。")
    return "\n".join(lines)


def _extract_tool_question_prompts(tool: _ToolStateSnapshot) -> tuple[UserQuestionPrompt, ...]:
    return extract_user_question_prompts(
        tool_use_id=tool.tool_use_id,
        tool_name=tool.tool_name,
        tool_input=tool.tool_input,
    )


def _extract_tool_question_prompts_by_id(snapshot: _StructuredSnapshot) -> dict[str, tuple[UserQuestionPrompt, ...]]:
    return {
        tool.tool_use_id: _extract_tool_question_prompts(tool)
        for tool in snapshot.tool_states
    }


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
        self._last_user_question_key: str | None = None
        self._last_phase: str | None = None
        self._tool_status_by_id: dict[str, str | None] = {}
        self._question_keys_by_tool_id: dict[str, tuple[str, ...]] = {}

    @property
    def structured_session_available(self) -> bool:
        return self._structured_session_available

    async def prime(self, *, log_missing: bool = True, baseline_current_snapshot: bool = False) -> None:
        snapshot = await self._load_snapshot(log_missing=log_missing)
        self._structured_session_available = snapshot.session_available
        self._current_session_id = snapshot.session_id
        self._last_phase = snapshot.phase

        persisted_turn_id, persisted_permission_key = await self._task_service.get_structured_reply_cursor(self._user_id)
        self._last_structured_turn_id = persisted_turn_id
        if self._last_structured_turn_id is None and baseline_current_snapshot:
            self._last_structured_turn_id = snapshot.turn_id
        self._last_pending_permission_key = persisted_permission_key

        if baseline_current_snapshot:
            tool_question_prompts = _extract_tool_question_prompts_by_id(snapshot)
            self._tool_status_by_id = {tool.tool_use_id: tool.status for tool in snapshot.tool_states}
            self._question_keys_by_tool_id = {
                tool_use_id: tuple(prompt.key for prompt in prompts)
                for tool_use_id, prompts in tool_question_prompts.items()
                if prompts
            }
            pending_prompts = self._extract_pending_user_question_prompts(snapshot, tool_question_prompts=tool_question_prompts)
            if pending_prompts:
                self._question_keys_by_tool_id[pending_prompts[0].tool_use_id] = tuple(prompt.key for prompt in pending_prompts)
        else:
            self._tool_status_by_id = {}
            self._question_keys_by_tool_id = {}

        self._last_user_question_key = await self._task_service.get_structured_user_question_cursor(self._user_id)
        if self._last_user_question_key is None and baseline_current_snapshot and pending_prompts:
            self._last_user_question_key = pending_prompts[0].key

        self._revision = await self._task_service.get_structured_session_cursor(self._user_id)

    async def wait_for_update(self, *, timeout_sec: float) -> bool:
        current_session = await self._task_service.get_structured_session(self._user_id, log_missing=False)
        current_session_id = current_session.session_id if current_session is not None else None
        if current_session_id != self._current_session_id:
            self._current_session_id = current_session_id
            self._last_phase = None
            self._last_pending_permission_key = None
            self._tool_status_by_id = {}
            self._question_keys_by_tool_id = {}
            self._revision = await self._task_service.get_structured_session_cursor(self._user_id)
            return True
        changed = await self._task_service.wait_for_structured_session_update(
            user_id=self._user_id,
            since_cursor=self._revision,
            timeout_sec=timeout_sec,
        )
        if changed:
            self._revision = await self._task_service.get_structured_session_cursor(self._user_id)
        return changed

    async def poll(self, *, task_id: str, final: bool = False, log_missing: bool = False) -> list[str | PermissionRequestOutput | ProgressUpdateOutput | UserQuestionOutput]:
        snapshot = await self._load_snapshot(log_missing=log_missing)
        self._structured_session_available = self._structured_session_available or snapshot.session_available
        tool_question_prompts = _extract_tool_question_prompts_by_id(snapshot)
        self._last_user_question_key = await self._task_service.get_structured_user_question_cursor(self._user_id)

        messages: list[str | PermissionRequestOutput | ProgressUpdateOutput | UserQuestionOutput] = []
        pending_question_prompts = self._extract_pending_user_question_prompts(snapshot, tool_question_prompts=tool_question_prompts)
        question_updates = self._collect_user_question_updates(
            snapshot=snapshot,
            tool_question_prompts=tool_question_prompts,
            pending_question_prompts=pending_question_prompts,
        )
        messages.extend(question_updates)
        for output in question_updates:
            await self._task_service.acknowledge_structured_user_question(self._user_id, question_key=output.question.key)
        messages.extend(self._collect_progress_updates(snapshot=snapshot, tool_question_prompts=tool_question_prompts))
        if (
            snapshot.phase == SessionPhase.WAITING_FOR_APPROVAL.value
            and snapshot.pending_permission_key
            and snapshot.pending_permission_key != self._last_pending_permission_key
            and not pending_question_prompts
        ):
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
            await self._task_service.acknowledge_structured_reply(
                self._user_id,
                permission_key=snapshot.pending_permission_key,
            )
        elif snapshot.phase != SessionPhase.WAITING_FOR_APPROVAL.value:
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
        await self._task_service.acknowledge_structured_reply(self._user_id, turn_id=snapshot.turn_id)
        logger.info("[task %s][structured] %s", task_id, snapshot.reply.rstrip("\n"))
        return snapshot.reply

    def _collect_progress_updates(
        self,
        *,
        snapshot: _StructuredSnapshot,
        tool_question_prompts: dict[str, tuple[UserQuestionPrompt, ...]],
    ) -> list[ProgressUpdateOutput]:
        messages: list[ProgressUpdateOutput] = []
        if snapshot.phase == SessionPhase.COMPACTING.value and self._last_phase != SessionPhase.COMPACTING.value:
            messages.append(ProgressUpdateOutput(text=build_compacting_progress_message()))
        self._last_phase = snapshot.phase

        current_status_by_id: dict[str, str | None] = {}
        for tool in snapshot.tool_states:
            current_status_by_id[tool.tool_use_id] = tool.status
            if tool_question_prompts.get(tool.tool_use_id):
                continue
            if tool.status != ToolStatus.RUNNING.value:
                continue
            previous_status = self._tool_status_by_id.get(tool.tool_use_id)
            if previous_status == ToolStatus.RUNNING.value:
                continue
            messages.append(
                ProgressUpdateOutput(
                    text=build_tool_progress_message(
                        tool_name=tool.tool_name,
                        tool_input=tool.tool_input,
                        resumed=previous_status == ToolStatus.WAITING_FOR_APPROVAL.value,
                    )
                )
            )
        self._tool_status_by_id = current_status_by_id
        return messages

    def _collect_user_question_updates(
        self,
        *,
        snapshot: _StructuredSnapshot,
        tool_question_prompts: dict[str, tuple[UserQuestionPrompt, ...]],
        pending_question_prompts: tuple[UserQuestionPrompt, ...] = (),
    ) -> list[UserQuestionOutput]:
        messages: list[UserQuestionOutput] = []
        current_keys_by_tool_id: dict[str, tuple[str, ...]] = {}

        active_prompt_group: tuple[UserQuestionPrompt, ...] = pending_question_prompts
        active_tool_use_id: str | None = pending_question_prompts[0].tool_use_id if pending_question_prompts else None
        if not active_prompt_group:
            for tool in snapshot.tool_states:
                if tool.status != ToolStatus.RUNNING.value:
                    continue
                prompts = tool_question_prompts.get(tool.tool_use_id, ())
                if not prompts:
                    continue
                active_prompt_group = prompts
                active_tool_use_id = tool.tool_use_id

        if active_tool_use_id is not None and active_prompt_group:
            current_keys = tuple(prompt.key for prompt in active_prompt_group)
            current_keys_by_tool_id[active_tool_use_id] = current_keys
            previous_keys = self._question_keys_by_tool_id.get(active_tool_use_id, ())
            selected_prompt = active_prompt_group[0]
            current_question_cursor = parse_user_question_key(self._last_user_question_key)
            if current_question_cursor is not None and current_question_cursor[0] == active_tool_use_id:
                matched_prompt = next((prompt for prompt in active_prompt_group if prompt.key == self._last_user_question_key), None)
                if matched_prompt is not None:
                    selected_prompt = matched_prompt
            if selected_prompt.key not in previous_keys and selected_prompt.key != self._last_user_question_key:
                messages.append(
                    UserQuestionOutput(
                        text=build_user_question_prompt(selected_prompt),
                        question=selected_prompt,
                    )
                )
                self._last_user_question_key = selected_prompt.key

        self._question_keys_by_tool_id = current_keys_by_tool_id
        return messages

    def _extract_pending_user_question_prompts(
        self,
        snapshot: _StructuredSnapshot,
        *,
        tool_question_prompts: dict[str, tuple[UserQuestionPrompt, ...]],
    ) -> tuple[UserQuestionPrompt, ...]:
        if not snapshot.pending_permission_tool_use_id:
            for tool in snapshot.tool_states:
                if tool.status != ToolStatus.WAITING_FOR_APPROVAL.value:
                    continue
                prompts = tool_question_prompts.get(tool.tool_use_id, ())
                if prompts:
                    return prompts
            return ()
        prompts = tool_question_prompts.get(snapshot.pending_permission_tool_use_id)
        if prompts is not None:
            return prompts
        return extract_user_question_prompts(
            tool_use_id=snapshot.pending_permission_tool_use_id,
            tool_name=snapshot.pending_permission_tool_name,
            tool_input=snapshot.pending_permission_tool_input,
        )

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
            normalized_reply = normalize_stream_text(turn.text)
            if not normalized_reply:
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
                reply=normalized_reply,
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
