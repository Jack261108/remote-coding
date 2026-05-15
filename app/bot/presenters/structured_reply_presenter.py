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
_TASK_LIST_VISIBLE_LIMIT = 20
_SUBAGENT_AGGREGATE_MESSAGE_KEY = "subagent-aggregate"
_TASK_LIST_MESSAGE_KEY = "task-list"
_FILE_TOOL_AGGREGATE_MESSAGE_KEY = "file-tool-aggregate"
_FILE_TOOL_NAMES = {"read", "grep", "glob"}
_TERMINAL_TOOL_STATUSES = {
    ToolStatus.SUCCESS.value,
    ToolStatus.ERROR.value,
    ToolStatus.INTERRUPTED.value,
}


@dataclass(frozen=True)
class _SubagentToolStateSnapshot:
    tool_use_id: str
    tool_name: str | None
    tool_input: dict | None
    status: str | None


@dataclass(frozen=True)
class _ToolStateSnapshot:
    tool_use_id: str
    tool_name: str | None
    tool_input: dict | None
    status: str | None
    result: str | None = None
    structured_result: dict | None = None
    subagent_tools: tuple[_SubagentToolStateSnapshot, ...] = ()


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
class StructuredReplyOutput:
    text: str
    turn_id: str


@dataclass(frozen=True)
class PermissionRequestOutput:
    text: str
    tool_use_id: str | None
    permission_key: str
    tool_name: str | None = None


@dataclass(frozen=True)
class ProgressUpdateOutput:
    text: str


@dataclass(frozen=True)
class SubagentToolStatusOutput:
    tool_use_id: str
    tool_name: str | None
    tool_input: dict | None
    status: str


@dataclass(frozen=True)
class ToolStatusOutput:
    tool_use_id: str
    tool_name: str | None
    tool_input: dict | None
    status: str
    subagent_tools: tuple[SubagentToolStatusOutput, ...] = ()


@dataclass(frozen=True)
class SubagentAggregateStatusOutput:
    message_key: str
    containers: tuple[ToolStatusOutput, ...]


@dataclass(frozen=True)
class TaskListItemStatusOutput:
    task_id: str
    subject: str
    status: str
    active_form: str | None = None


@dataclass(frozen=True)
class TaskListStatusOutput:
    message_key: str
    items: tuple[TaskListItemStatusOutput, ...]


@dataclass(frozen=True)
class FileToolAggregateStatusOutput:
    message_key: str
    tools: tuple[ToolStatusOutput, ...]


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


def _truncate_text(text: str, *, char_limit: int, line_limit: int, suffix: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return ""

    lines = normalized.split("\n")
    needs_line_truncation = len(lines) > line_limit
    preview_lines = lines[:line_limit]
    preview = "\n".join(preview_lines)

    needs_char_truncation = len(preview) > char_limit
    if needs_char_truncation:
        preview = preview[:char_limit].rstrip()

    if needs_line_truncation or needs_char_truncation:
        preview = f"{preview}{suffix}"
    return preview


def preview_stream_text(text: str) -> str:
    return _truncate_text(
        normalize_stream_text(text),
        char_limit=_STREAM_PREVIEW_CHAR_LIMIT,
        line_limit=_STREAM_PREVIEW_LINE_LIMIT,
        suffix="\n...[输出片段过长，已截断本条消息]",
    )


def _truncate_permission_text(text: str) -> str:
    return _truncate_text(
        text,
        char_limit=_PERMISSION_INPUT_CHAR_LIMIT,
        line_limit=_PERMISSION_INPUT_LINE_LIMIT,
        suffix="...",
    )


def _truncate_question_text(text: str) -> str:
    return _truncate_text(
        text,
        char_limit=_QUESTION_TEXT_CHAR_LIMIT,
        line_limit=_QUESTION_TEXT_LINE_LIMIT,
        suffix="...",
    )


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


def _tool_status_heading(status: str | None, *, resumed: bool = False) -> str:
    if status == ToolStatus.SUCCESS.value:
        text = "执行完成"
    elif status == ToolStatus.ERROR.value:
        text = "执行失败"
    elif status == ToolStatus.INTERRUPTED.value:
        text = "已中断"
    elif status == ToolStatus.WAITING_FOR_APPROVAL.value:
        text = "等待权限"
    elif status == ToolStatus.RUNNING.value and resumed:
        text = "继续执行"
    else:
        text = "执行中"
    return f"{_tool_status_icon(status)} {text}"


def build_tool_status_message(*, tool_name: str | None, tool_input: dict | None = None, status: str, resumed: bool = False) -> str:
    lines = [_tool_status_heading(status, resumed=resumed)]
    if tool_name:
        lines.append(f"工具: {tool_name}")

    detail = _format_tool_input_detail(tool_name, tool_input)
    if detail is not None:
        label, value = detail
        lines.append(f"{label}: {value}")

    return "\n".join(lines)


def build_tool_progress_message(*, tool_name: str | None, tool_input: dict | None = None, resumed: bool = False) -> str:
    return build_tool_status_message(
        tool_name=tool_name,
        tool_input=tool_input,
        status=ToolStatus.RUNNING.value,
        resumed=resumed,
    )


def build_tool_task_list_message(output: ToolStatusOutput) -> str:
    visible_tools = _visible_subagent_tools(output.subagent_tools)
    lines = ["任务列表"]
    detail = _format_tool_input_detail(output.tool_name, output.tool_input)
    if detail is not None:
        label, value = detail
        lines.append(f"{label}: {value}")
    elif output.tool_name:
        lines.append(f"工具: {output.tool_name}")
    lines.append(f"状态: {_tool_status_label(output.status)}")

    active_index = _select_active_subagent_index(visible_tools)
    if active_index is None:
        lines.append("当前: 无（全部完成）")
    else:
        active_tool = visible_tools[active_index]
        lines.append(f"当前: {_tool_status_icon(active_tool.status)} {active_index + 1}. {active_tool.tool_name or 'Unknown'}")

    lines.append("")
    display_indexes = _select_visible_subagent_indexes(visible_tools, active_index=active_index)
    for index in display_indexes:
        tool = visible_tools[index]
        prefix = "=> " if index == active_index else ""
        detail = _format_tool_input_detail(tool.tool_name, tool.tool_input)
        detail_text = f" - {detail[0]}: {detail[1]}" if detail is not None else ""
        lines.append(f"{prefix}{_tool_status_icon(tool.status)} {index + 1}. {tool.tool_name or 'Unknown'} - {_tool_status_label(tool.status)}{detail_text}")

    omitted = len(visible_tools) - len(display_indexes)
    if omitted > 0:
        lines.append(f"...另有 {omitted} 项未显示")

    return "\n".join(lines)


def build_task_list_status_message(output: TaskListStatusOutput) -> str:
    visible_items = tuple(item for item in output.items if item.status != "deleted")
    lines = ["任务列表"]
    active_index = _select_active_task_list_item_index(visible_items)
    if active_index is None:
        lines.append("当前: 无（全部完成）")
    else:
        active_item = visible_items[active_index]
        active_title = active_item.active_form or active_item.subject
        icon = _task_list_item_status_icon(active_item.status)
        lines.append(f"当前: {icon} {active_index + 1}. {_truncate_permission_text(active_title)}")

    lines.append("")
    display_indexes = _select_visible_task_list_item_indexes(visible_items, active_index=active_index)
    for index in display_indexes:
        item = visible_items[index]
        prefix = "=> " if index == active_index else ""
        subject = _truncate_permission_text(item.subject)
        icon = _task_list_item_status_icon(item.status)
        lines.append(f"{prefix}{icon} {index + 1}. {subject} - {_task_list_item_status_label(item.status)}")

    omitted = len(visible_items) - len(display_indexes)
    if omitted > 0:
        lines.append(f"...另有 {omitted} 项未显示")

    return "\n".join(lines)


def build_subagent_aggregate_status_message(output: SubagentAggregateStatusOutput) -> str:
    containers = output.containers
    noun = _subagent_aggregate_noun(containers)
    icon = _aggregate_status_icon(_subagent_container_status_values(containers))
    lines = [f"{icon} {len(containers)} {noun} {_subagent_aggregate_status_text(containers)}"]
    display_containers = containers[:_TASK_LIST_VISIBLE_LIMIT]
    if display_containers:
        lines.append("")
    for container in display_containers:
        visible_tools = _visible_subagent_tools(container.subagent_tools)
        tool_use_count = len(visible_tools)
        lines.append(
            f"- {_subagent_container_status_icon(container)} {_subagent_container_title(container)} · {tool_use_count} tool uses · {_subagent_container_status_text(container)}"
        )
        tool_names = _subagent_tool_names_summary(visible_tools)
        if tool_names:
            lines.append(f"  名称: {tool_names}")

    omitted = len(containers) - len(display_containers)
    if omitted > 0:
        lines.append(f"...and {omitted} more {noun}")

    return "\n".join(lines)


def build_file_tool_aggregate_status_message(output: FileToolAggregateStatusOutput) -> str:
    tools = output.tools
    icon = _aggregate_status_icon(tuple(tool.status for tool in tools))
    lines = [f"{icon} 文件检索 · {_file_tool_aggregate_status_label(tools)}"]
    summary = _file_tool_aggregate_summary(tools)
    if summary:
        lines.append(summary)

    active_index = _select_active_file_tool_index(tools)
    if active_index is None:
        lines.append("当前: 无（全部完成）")
    else:
        active_tool = tools[active_index]
        detail = _file_tool_detail(active_tool)
        detail_text = f" · {detail}" if detail else ""
        lines.append(f"当前: {_tool_status_icon(active_tool.status)} {active_tool.tool_name or 'Unknown'}{detail_text}")

    lines.append("")
    display_tools = tools[:_TASK_LIST_VISIBLE_LIMIT]
    for index, tool in enumerate(display_tools, start=1):
        detail = _file_tool_detail(tool)
        detail_text = f" · {detail}" if detail else ""
        lines.append(f"{_tool_status_icon(tool.status)} {index}. {tool.tool_name or 'Unknown'} - {_tool_status_label(tool.status)}{detail_text}")

    omitted = len(tools) - len(display_tools)
    if omitted > 0:
        lines.append(f"...另有 {omitted} 项未显示")

    return "\n".join(lines)


def _visible_subagent_tools(subagent_tools: tuple[SubagentToolStatusOutput, ...]) -> tuple[SubagentToolStatusOutput, ...]:
    return tuple(
        tool
        for tool in subagent_tools
        if not _is_user_question_tool(tool.tool_name, tool.tool_input)
    )


def _aggregate_status_icon(statuses: tuple[str, ...]) -> str:
    if ToolStatus.WAITING_FOR_APPROVAL.value in statuses:
        return _tool_status_icon(ToolStatus.WAITING_FOR_APPROVAL.value)
    if ToolStatus.RUNNING.value in statuses:
        return _tool_status_icon(ToolStatus.RUNNING.value)
    if ToolStatus.ERROR.value in statuses:
        return _tool_status_icon(ToolStatus.ERROR.value)
    if ToolStatus.INTERRUPTED.value in statuses:
        return _tool_status_icon(ToolStatus.INTERRUPTED.value)
    if ToolStatus.SUCCESS.value in statuses:
        return _tool_status_icon(ToolStatus.SUCCESS.value)
    return _tool_status_icon(None)


def _file_tool_aggregate_status_label(tools: tuple[ToolStatusOutput, ...]) -> str:
    statuses = tuple(tool.status for tool in tools)
    if ToolStatus.WAITING_FOR_APPROVAL.value in statuses:
        return "等待权限"
    if ToolStatus.RUNNING.value in statuses:
        return "执行中"
    if ToolStatus.ERROR.value in statuses:
        return "失败"
    if ToolStatus.INTERRUPTED.value in statuses:
        return "已中断"
    return "完成"


def _file_tool_aggregate_summary(tools: tuple[ToolStatusOutput, ...]) -> str | None:
    search_count = sum(1 for tool in tools if (tool.tool_name or "").strip().lower() in {"grep", "glob"})
    read_count = sum(1 for tool in tools if (tool.tool_name or "").strip().lower() == "read")
    parts: list[str] = []
    if search_count:
        parts.append(f"搜索 {search_count} 次")
    if read_count:
        parts.append(f"读取 {read_count} 个文件")
    return "，".join(parts) or None


def _select_active_file_tool_index(tools: tuple[ToolStatusOutput, ...]) -> int | None:
    for status in (ToolStatus.RUNNING.value, ToolStatus.WAITING_FOR_APPROVAL.value, ToolStatus.ERROR.value, ToolStatus.INTERRUPTED.value):
        for index, tool in enumerate(tools):
            if tool.status == status:
                return index
    return None


def _file_tool_detail(tool: ToolStatusOutput) -> str | None:
    detail = _format_tool_input_detail(tool.tool_name, tool.tool_input)
    if detail is None:
        return None
    label, value = detail
    return f"{label}: {value}"


def _subagent_aggregate_noun(containers: tuple[ToolStatusOutput, ...]) -> str:
    if containers and all((container.tool_name or "").strip().lower() == "agent" for container in containers):
        return "agents"
    return "tasks"


def _subagent_aggregate_status_text(containers: tuple[ToolStatusOutput, ...]) -> str:
    statuses = _subagent_container_status_values(containers)
    if ToolStatus.WAITING_FOR_APPROVAL.value in statuses:
        return "waiting"
    if ToolStatus.RUNNING.value in statuses:
        return "running"
    if ToolStatus.ERROR.value in statuses:
        return "failed"
    if ToolStatus.INTERRUPTED.value in statuses:
        return "interrupted"
    return "finished"


def _subagent_container_status_text(container: ToolStatusOutput) -> str:
    statuses = _subagent_container_status_values((container,))
    if ToolStatus.WAITING_FOR_APPROVAL.value in statuses:
        return "Waiting"
    if ToolStatus.RUNNING.value in statuses:
        return "Running"
    if ToolStatus.ERROR.value in statuses:
        return "Failed"
    if ToolStatus.INTERRUPTED.value in statuses:
        return "Interrupted"
    return "Done"


def _subagent_container_status_icon(container: ToolStatusOutput) -> str:
    return _aggregate_status_icon(_subagent_container_status_values((container,)))


def _subagent_container_status_values(containers: tuple[ToolStatusOutput, ...]) -> tuple[str, ...]:
    statuses: list[str] = []
    for container in containers:
        statuses.append(container.status)
        statuses.extend(tool.status for tool in _visible_subagent_tools(container.subagent_tools))
    return tuple(statuses)


def _subagent_container_title(container: ToolStatusOutput) -> str:
    tool_input = container.tool_input or {}
    title = _subagent_container_description(tool_input)
    subagent_type = str(tool_input.get("subagent_type") or "").strip()
    if subagent_type:
        if title:
            return _truncate_permission_text(f"{subagent_type}({title})")
        return _truncate_permission_text(subagent_type)
    if title:
        return _truncate_permission_text(title)
    return container.tool_name or "Unknown"


def _subagent_container_description(tool_input: dict) -> str | None:
    for key in ("description", "prompt"):
        value = tool_input.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _subagent_tool_names_summary(tools: tuple[SubagentToolStatusOutput, ...]) -> str | None:
    if not tools:
        return None

    counts: dict[str, int] = {}
    for tool in tools:
        name = tool.tool_name or "Unknown"
        counts[name] = counts.get(name, 0) + 1

    parts = [f"{name} ×{count}" if count > 1 else name for name, count in counts.items()]
    return _truncate_permission_text("、".join(parts))


def _tool_status_icon(status: str | None) -> str:
    if status == ToolStatus.RUNNING.value:
        return "🔄"
    if status == ToolStatus.SUCCESS.value:
        return "✅"
    if status == ToolStatus.ERROR.value:
        return "❌"
    if status == ToolStatus.INTERRUPTED.value:
        return "⏹️"
    if status == ToolStatus.WAITING_FOR_APPROVAL.value:
        return "⏳"
    return "⏳"


def _tool_status_label(status: str | None) -> str:
    if status == ToolStatus.RUNNING.value:
        return "执行中"
    if status == ToolStatus.SUCCESS.value:
        return "完成"
    if status == ToolStatus.ERROR.value:
        return "失败"
    if status == ToolStatus.INTERRUPTED.value:
        return "已中断"
    if status == ToolStatus.WAITING_FOR_APPROVAL.value:
        return "等待权限"
    return "未知"


def _select_active_subagent_index(tools: tuple[SubagentToolStatusOutput, ...]) -> int | None:
    for status in (ToolStatus.RUNNING.value, ToolStatus.WAITING_FOR_APPROVAL.value, ToolStatus.ERROR.value, ToolStatus.INTERRUPTED.value):
        for index, tool in enumerate(tools):
            if tool.status == status:
                return index
    return None


def _select_visible_subagent_indexes(tools: tuple[SubagentToolStatusOutput, ...], *, active_index: int | None) -> tuple[int, ...]:
    if len(tools) <= _TASK_LIST_VISIBLE_LIMIT:
        return tuple(range(len(tools)))
    indexes = list(range(_TASK_LIST_VISIBLE_LIMIT))
    if active_index is not None and active_index not in indexes:
        indexes[-1] = active_index
    return tuple(indexes)


def _select_active_task_list_item_index(items: tuple[TaskListItemStatusOutput, ...]) -> int | None:
    for status in ("in_progress", "failed", "interrupted", "pending"):
        for index, item in enumerate(items):
            if item.status == status:
                return index
    return None


def _select_visible_task_list_item_indexes(items: tuple[TaskListItemStatusOutput, ...], *, active_index: int | None) -> tuple[int, ...]:
    if len(items) <= _TASK_LIST_VISIBLE_LIMIT:
        return tuple(range(len(items)))
    indexes = list(range(_TASK_LIST_VISIBLE_LIMIT))
    if active_index is not None and active_index not in indexes:
        indexes[-1] = active_index
    return tuple(indexes)


def _task_list_item_status_icon(status: str | None) -> str:
    if status == "in_progress":
        return "🔄"
    if status == "completed":
        return "✅"
    if status == "failed":
        return "❌"
    if status == "interrupted":
        return "⏹️"
    if status == "deleted":
        return "🗑️"
    return "⏳"


def _task_list_item_status_label(status: str | None) -> str:
    if status == "in_progress":
        return "执行中"
    if status == "completed":
        return "完成"
    if status == "failed":
        return "失败"
    if status == "interrupted":
        return "已中断"
    if status == "deleted":
        return "已删除"
    return "待执行"


def _is_user_question_tool(tool_name: str | None, tool_input: dict | None) -> bool:
    return bool(extract_user_question_prompts(tool_use_id="tool", tool_name=tool_name, tool_input=tool_input))


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


def _is_subagent_container_tool(tool_name: str | None) -> bool:
    return (tool_name or "").strip().lower() in {"task", "agent"}


def _is_task_list_tool(tool_name: str | None) -> bool:
    return (tool_name or "").strip().lower() in {"taskcreate", "taskupdate"}


def _is_file_tool(tool_name: str | None) -> bool:
    return (tool_name or "").strip().lower() in _FILE_TOOL_NAMES


def _task_list_status_output(tools: tuple[_ToolStateSnapshot, ...]) -> TaskListStatusOutput | None:
    order: list[str] = []
    items: dict[str, TaskListItemStatusOutput] = {}
    for tool in tools:
        tool_name = (tool.tool_name or "").strip().lower()
        if tool_name == "taskcreate":
            task_id = _task_create_task_id(tool) or f"create:{tool.tool_use_id}"
            existing = items.get(task_id)
            if existing is None:
                order.append(task_id)
            subject = _task_create_subject(tool, task_id=task_id)
            active_form = _task_list_text_value(tool.tool_input, "activeForm") or (existing.active_form if existing else None)
            status = existing.status if existing is not None else _task_create_status(tool.status)
            if tool.status in {ToolStatus.ERROR.value, ToolStatus.INTERRUPTED.value}:
                status = _task_create_status(tool.status)
            items[task_id] = TaskListItemStatusOutput(
                task_id=task_id,
                subject=subject,
                status=status,
                active_form=active_form,
            )
            continue
        if tool_name == "taskupdate":
            task_input = tool.tool_input or {}
            task_id = _task_list_text_value(task_input, "taskId") or _task_list_text_value(task_input, "task_id")
            if not task_id:
                continue
            existing = items.get(task_id)
            if existing is None:
                order.append(task_id)
                existing = TaskListItemStatusOutput(task_id=task_id, subject=f"Task {task_id}", status="pending")
            subject = _task_list_text_value(task_input, "subject") or existing.subject
            active_form = _task_list_text_value(task_input, "activeForm") or existing.active_form
            status = existing.status
            if tool.status == ToolStatus.ERROR.value:
                status = "failed"
            elif tool.status == ToolStatus.INTERRUPTED.value:
                status = "interrupted"
            else:
                updated_status = _task_list_text_value(task_input, "status")
                if updated_status:
                    status = _normalize_task_list_item_status(updated_status)
            items[task_id] = TaskListItemStatusOutput(
                task_id=task_id,
                subject=subject,
                status=status,
                active_form=active_form,
            )

    ordered_items = tuple(items[task_id] for task_id in order if task_id in items)
    if not ordered_items:
        return None
    return TaskListStatusOutput(message_key=_TASK_LIST_MESSAGE_KEY, items=ordered_items)


def _task_list_text_value(mapping: dict | None, key: str) -> str | None:
    if not mapping:
        return None
    value = mapping.get(key)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _task_create_task_id(tool: _ToolStateSnapshot) -> str | None:
    structured_result = tool.structured_result or {}
    task = structured_result.get("task")
    if isinstance(task, dict):
        task_id = task.get("id")
        if task_id is not None and str(task_id).strip():
            return str(task_id).strip()
    if tool.result:
        match = re.search(r"Task #([^\s]+)", tool.result)
        if match:
            return match.group(1).strip()
    return None


def _task_create_subject(tool: _ToolStateSnapshot, *, task_id: str) -> str:
    subject = _task_list_text_value(tool.tool_input, "subject")
    if subject:
        return subject
    structured_result = tool.structured_result or {}
    task = structured_result.get("task")
    if isinstance(task, dict):
        result_subject = task.get("subject")
        if result_subject is not None and str(result_subject).strip():
            return str(result_subject).strip()
    return f"Task {task_id}"


def _task_create_status(status: str | None) -> str:
    if status == ToolStatus.ERROR.value:
        return "failed"
    if status == ToolStatus.INTERRUPTED.value:
        return "interrupted"
    return "pending"


def _normalize_task_list_item_status(status: str) -> str:
    normalized = status.strip().lower()
    if normalized in {"pending", "in_progress", "completed", "deleted"}:
        return normalized
    return normalized or "pending"


def _task_list_status_fingerprint(output: TaskListStatusOutput | None) -> tuple | None:
    if output is None:
        return None
    return tuple((item.task_id, item.subject, item.status, item.active_form) for item in output.items)


def _input_detail_fingerprint(tool_name: str | None, tool_input: dict | None) -> tuple:
    detail = _format_tool_input_detail(tool_name, tool_input)
    if detail is None:
        return ()
    return detail


def _tool_state_fingerprint(tool: _ToolStateSnapshot) -> tuple:
    return (
        tool.status,
        _input_detail_fingerprint(tool.tool_name, tool.tool_input),
        tuple(
            (
                subagent_tool.tool_use_id,
                subagent_tool.tool_name,
                subagent_tool.status,
                _input_detail_fingerprint(subagent_tool.tool_name, subagent_tool.tool_input),
            )
            for subagent_tool in tool.subagent_tools
        ),
    )


def _tool_status_output_fingerprint(output: ToolStatusOutput) -> tuple:
    return (
        output.tool_use_id,
        output.tool_name,
        output.status,
        _input_detail_fingerprint(output.tool_name, output.tool_input),
        tuple(
            (
                subagent_tool.tool_use_id,
                subagent_tool.tool_name,
                subagent_tool.status,
                _input_detail_fingerprint(subagent_tool.tool_name, subagent_tool.tool_input),
            )
            for subagent_tool in output.subagent_tools
        ),
    )


def _subagent_aggregate_fingerprint(containers: tuple[ToolStatusOutput, ...]) -> tuple:
    return tuple(_tool_status_output_fingerprint(container) for container in containers)


def _file_tool_aggregate_fingerprint(tools: tuple[ToolStatusOutput, ...]) -> tuple:
    return tuple(_tool_status_output_fingerprint(tool) for tool in tools)


def _tool_status_output(tool: _ToolStateSnapshot) -> ToolStatusOutput:
    assert tool.status is not None
    return ToolStatusOutput(
        tool_use_id=tool.tool_use_id,
        tool_name=tool.tool_name,
        tool_input=tool.tool_input,
        status=tool.status,
    )


def _subagent_container_output(tool: _ToolStateSnapshot) -> ToolStatusOutput:
    assert tool.status is not None
    return ToolStatusOutput(
        tool_use_id=tool.tool_use_id,
        tool_name=tool.tool_name,
        tool_input=tool.tool_input,
        status=tool.status,
        subagent_tools=_subagent_status_outputs(tool),
    )


def _subagent_container_outputs(tools: tuple[_ToolStateSnapshot, ...]) -> tuple[ToolStatusOutput, ...]:
    return tuple(
        _subagent_container_output(tool)
        for tool in tools
        if tool.status is not None and _is_subagent_container_tool(tool.tool_name)
    )


def _merge_subagent_container_output(previous: ToolStatusOutput | None, current: ToolStatusOutput) -> ToolStatusOutput:
    if previous is None:
        return current

    return ToolStatusOutput(
        tool_use_id=current.tool_use_id,
        tool_name=current.tool_name or previous.tool_name,
        tool_input=current.tool_input or previous.tool_input,
        status=current.status,
        subagent_tools=_merge_subagent_tool_outputs(
            previous.subagent_tools,
            current.subagent_tools,
            container_status=current.status,
        ),
    )


def _merge_subagent_tool_outputs(
    previous: tuple[SubagentToolStatusOutput, ...],
    current: tuple[SubagentToolStatusOutput, ...],
    *,
    container_status: str,
) -> tuple[SubagentToolStatusOutput, ...]:
    tools_by_id = {tool.tool_use_id: tool for tool in previous}
    order = [tool.tool_use_id for tool in previous]
    current_ids: set[str] = set()

    for tool in current:
        current_ids.add(tool.tool_use_id)
        if tool.tool_use_id not in tools_by_id:
            order.append(tool.tool_use_id)
        tools_by_id[tool.tool_use_id] = tool

    if container_status in _TERMINAL_TOOL_STATUSES:
        for tool_use_id, tool in tuple(tools_by_id.items()):
            if tool_use_id not in current_ids:
                tools_by_id[tool_use_id] = SubagentToolStatusOutput(
                    tool_use_id=tool.tool_use_id,
                    tool_name=tool.tool_name,
                    tool_input=tool.tool_input,
                    status=container_status,
                )

    return tuple(tools_by_id[tool_use_id] for tool_use_id in order if tool_use_id in tools_by_id)


def _subagent_status_outputs(tool: _ToolStateSnapshot) -> tuple[SubagentToolStatusOutput, ...]:
    return tuple(
        SubagentToolStatusOutput(
            tool_use_id=subagent_tool.tool_use_id,
            tool_name=subagent_tool.tool_name,
            tool_input=subagent_tool.tool_input,
            status=subagent_tool.status,
        )
        for subagent_tool in tool.subagent_tools
        if subagent_tool.status is not None and not _is_user_question_tool(subagent_tool.tool_name, subagent_tool.tool_input)
    )


class StructuredReplyPresenter:
    def __init__(self, *, task_service: TaskService, user_id: int, task_id: str | None = None) -> None:
        self._task_service = task_service
        self._user_id = user_id
        self._task_id = task_id
        self._last_structured_turn_id: str | None = None
        self._last_pending_permission_key: str | None = None
        self._structured_session_available = False
        self._structured_reply_emitted_in_run = False
        self._fallback_announced = False
        self._revision = 0
        self._current_session_id: str | None = None
        self._last_user_question_key: str | None = None
        self._last_phase: str | None = None
        self._tool_fingerprint_by_id: dict[str, tuple] = {}
        self._subagent_aggregate_fingerprint: tuple | None = None
        self._subagent_container_by_id: dict[str, ToolStatusOutput] = {}
        self._file_tool_aggregate_fingerprint: tuple | None = None
        self._task_list_fingerprint: tuple | None = None
        self._emitted_flat_tool_ids: set[str] = set()
        self._question_keys_by_tool_id: dict[str, tuple[str, ...]] = {}

    @property
    def structured_session_available(self) -> bool:
        return self._structured_session_available

    async def prime(self, *, log_missing: bool = True, baseline_current_snapshot: bool = False) -> None:
        snapshot = await self._load_snapshot(log_missing=log_missing)
        self._structured_session_available = snapshot.session_available
        self._current_session_id = snapshot.session_id
        self._last_phase = snapshot.phase

        persisted_turn_id, persisted_permission_key = await self._task_service.get_structured_reply_cursor(self._user_id, task_id=self._task_id)
        self._last_structured_turn_id = persisted_turn_id
        if self._last_structured_turn_id is None and baseline_current_snapshot:
            self._last_structured_turn_id = snapshot.turn_id
        self._last_pending_permission_key = persisted_permission_key

        if baseline_current_snapshot:
            tool_question_prompts = _extract_tool_question_prompts_by_id(snapshot)
            self._tool_fingerprint_by_id = {tool.tool_use_id: _tool_state_fingerprint(tool) for tool in snapshot.tool_states}
            subagent_containers = _subagent_container_outputs(snapshot.tool_states)
            self._subagent_container_by_id = {container.tool_use_id: container for container in subagent_containers}
            self._subagent_aggregate_fingerprint = _subagent_aggregate_fingerprint(subagent_containers)
            file_tools = tuple(
                _tool_status_output(tool)
                for tool in snapshot.tool_states
                if tool.status is not None and _is_file_tool(tool.tool_name)
            )
            self._file_tool_aggregate_fingerprint = _file_tool_aggregate_fingerprint(file_tools)
            self._task_list_fingerprint = _task_list_status_fingerprint(_task_list_status_output(snapshot.tool_states))
            self._emitted_flat_tool_ids = set()
            self._question_keys_by_tool_id = {
                tool_use_id: tuple(prompt.key for prompt in prompts)
                for tool_use_id, prompts in tool_question_prompts.items()
                if prompts
            }
            pending_prompts = self._extract_pending_user_question_prompts(snapshot, tool_question_prompts=tool_question_prompts)
            if pending_prompts:
                self._question_keys_by_tool_id[pending_prompts[0].tool_use_id] = tuple(prompt.key for prompt in pending_prompts)
        else:
            self._tool_fingerprint_by_id = {}
            self._subagent_aggregate_fingerprint = None
            self._subagent_container_by_id = {}
            self._file_tool_aggregate_fingerprint = None
            self._task_list_fingerprint = None
            self._emitted_flat_tool_ids = set()
            self._question_keys_by_tool_id = {}

        self._last_user_question_key = await self._task_service.get_structured_user_question_cursor(self._user_id, task_id=self._task_id)
        if self._last_user_question_key is None and baseline_current_snapshot and pending_prompts:
            self._last_user_question_key = pending_prompts[0].key

        self._revision = await self._task_service.get_structured_session_cursor(self._user_id, task_id=self._task_id)

    async def wait_for_update(self, *, timeout_sec: float) -> bool:
        current_session = await self._load_session(log_missing=False)
        current_session_id = current_session.session_id if current_session is not None else None
        if current_session_id != self._current_session_id:
            self._current_session_id = current_session_id
            self._last_phase = None
            self._last_pending_permission_key = None
            self._tool_fingerprint_by_id = {}
            self._subagent_aggregate_fingerprint = None
            self._subagent_container_by_id = {}
            self._file_tool_aggregate_fingerprint = None
            self._task_list_fingerprint = None
            self._emitted_flat_tool_ids = set()
            self._question_keys_by_tool_id = {}
            self._revision = await self._task_service.get_structured_session_cursor(self._user_id, task_id=self._task_id)
            return True
        changed = await self._task_service.wait_for_structured_session_update(
            user_id=self._user_id,
            since_cursor=self._revision,
            timeout_sec=timeout_sec,
            task_id=self._task_id,
        )
        if changed:
            self._revision = await self._task_service.get_structured_session_cursor(self._user_id, task_id=self._task_id)
        return changed

    async def poll(
        self,
        *,
        task_id: str,
        final: bool = False,
        log_missing: bool = False,
    ) -> list[
        str
        | StructuredReplyOutput
        | PermissionRequestOutput
        | ProgressUpdateOutput
        | ToolStatusOutput
        | SubagentAggregateStatusOutput
        | TaskListStatusOutput
        | FileToolAggregateStatusOutput
        | UserQuestionOutput
    ]:
        snapshot = await self._load_snapshot(log_missing=log_missing)
        self._structured_session_available = self._structured_session_available or snapshot.session_available
        tool_question_prompts = _extract_tool_question_prompts_by_id(snapshot)
        self._last_user_question_key = await self._task_service.get_structured_user_question_cursor(self._user_id, task_id=self._task_id)

        messages: list[
            str
            | StructuredReplyOutput
            | PermissionRequestOutput
            | ProgressUpdateOutput
            | ToolStatusOutput
            | SubagentAggregateStatusOutput
            | TaskListStatusOutput
            | FileToolAggregateStatusOutput
            | UserQuestionOutput
        ] = []
        pending_question_prompts = self._extract_pending_user_question_prompts(snapshot, tool_question_prompts=tool_question_prompts)
        question_updates = self._collect_user_question_updates(
            snapshot=snapshot,
            tool_question_prompts=tool_question_prompts,
            pending_question_prompts=pending_question_prompts,
        )
        messages.extend(question_updates)
        messages.extend(self._collect_progress_updates(snapshot=snapshot, tool_question_prompts=tool_question_prompts))
        if (
            snapshot.phase == SessionPhase.WAITING_FOR_APPROVAL.value
            and snapshot.pending_permission_key
            and snapshot.pending_permission_key != self._last_pending_permission_key
            and not pending_question_prompts
        ):
            messages.append(
                PermissionRequestOutput(
                    text=build_permission_prompt(
                        tool_name=snapshot.pending_permission_tool_name,
                        tool_input=snapshot.pending_permission_tool_input,
                    ),
                    tool_use_id=snapshot.pending_permission_tool_use_id,
                    permission_key=snapshot.pending_permission_key,
                    tool_name=snapshot.pending_permission_tool_name,
                )
            )
        elif snapshot.phase != SessionPhase.WAITING_FOR_APPROVAL.value:
            self._last_pending_permission_key = snapshot.pending_permission_key

        reply = await self._collect_reply(task_id=task_id, snapshot=snapshot, log_missing=log_missing)
        if reply:
            messages.append(reply)

        if final and self._structured_session_available and reply is None and not self._structured_reply_emitted_in_run and not self._fallback_announced:
            self._fallback_announced = True
            logger.warning(
                "structured reply fallback emitted",
                extra={"task_id": task_id, "user_id": self._user_id, "phase": snapshot.phase},
            )
            messages.append(_FALLBACK_PROMPT)

        return messages

    async def acknowledge_delivery(self, output: StructuredReplyOutput | PermissionRequestOutput | UserQuestionOutput) -> None:
        if isinstance(output, StructuredReplyOutput):
            await self._task_service.acknowledge_structured_reply(self._user_id, turn_id=output.turn_id, task_id=self._task_id)
            self._last_structured_turn_id = output.turn_id
            self._structured_reply_emitted_in_run = True
            return

        if isinstance(output, PermissionRequestOutput):
            await self._task_service.acknowledge_structured_reply(self._user_id, permission_key=output.permission_key, task_id=self._task_id)
            self._last_pending_permission_key = output.permission_key
            return

        await self._task_service.acknowledge_structured_user_question(
            self._user_id,
            question_key=output.question.key,
            task_id=self._task_id,
        )
        self._last_user_question_key = output.question.key
        previous_keys = self._question_keys_by_tool_id.get(output.question.tool_use_id, ())
        if output.question.key not in previous_keys:
            self._question_keys_by_tool_id[output.question.tool_use_id] = (*previous_keys, output.question.key)

    async def _collect_reply(self, *, task_id: str, snapshot: _StructuredSnapshot, log_missing: bool) -> StructuredReplyOutput | None:
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

        logger.info("[task %s][structured] %s", task_id, snapshot.reply.rstrip("\n"))
        return StructuredReplyOutput(text=snapshot.reply, turn_id=snapshot.turn_id)

    def _collect_progress_updates(
        self,
        *,
        snapshot: _StructuredSnapshot,
        tool_question_prompts: dict[str, tuple[UserQuestionPrompt, ...]],
    ) -> list[
        ProgressUpdateOutput
        | ToolStatusOutput
        | SubagentAggregateStatusOutput
        | TaskListStatusOutput
        | FileToolAggregateStatusOutput
    ]:
        messages: list[
            ProgressUpdateOutput
            | ToolStatusOutput
            | SubagentAggregateStatusOutput
            | TaskListStatusOutput
            | FileToolAggregateStatusOutput
        ] = []
        if snapshot.phase == SessionPhase.COMPACTING.value and self._last_phase != SessionPhase.COMPACTING.value:
            messages.append(ProgressUpdateOutput(text=build_compacting_progress_message()))
        self._last_phase = snapshot.phase

        task_list_output = _task_list_status_output(snapshot.tool_states)
        task_list_fingerprint = _task_list_status_fingerprint(task_list_output)
        suppress_flat_tools = task_list_output is not None
        if task_list_output is not None and task_list_fingerprint != self._task_list_fingerprint:
            messages.append(task_list_output)

        nested_tool_ids = {
            subagent_tool.tool_use_id
            for tool in snapshot.tool_states
            for subagent_tool in tool.subagent_tools
        }
        nested_tool_ids.update(
            subagent_tool.tool_use_id
            for container in self._subagent_container_by_id.values()
            for subagent_tool in container.subagent_tools
        )
        current_fingerprint_by_id: dict[str, tuple] = {}
        subagent_containers: list[ToolStatusOutput] = []
        file_tools: list[ToolStatusOutput] = []
        for tool in snapshot.tool_states:
            if tool.status is None:
                continue
            fingerprint = _tool_state_fingerprint(tool)
            current_fingerprint_by_id[tool.tool_use_id] = fingerprint
            if tool.tool_use_id in nested_tool_ids:
                continue
            if tool_question_prompts.get(tool.tool_use_id):
                continue
            if _is_task_list_tool(tool.tool_name):
                continue
            if _is_subagent_container_tool(tool.tool_name):
                subagent_containers.append(_subagent_container_output(tool))
                continue
            if _is_file_tool(tool.tool_name) and not suppress_flat_tools:
                file_tools.append(_tool_status_output(tool))
                continue
            if suppress_flat_tools and tool.tool_use_id not in self._emitted_flat_tool_ids:
                continue
            previous_fingerprint = self._tool_fingerprint_by_id.get(tool.tool_use_id)
            if previous_fingerprint == fingerprint:
                continue
            messages.append(
                ToolStatusOutput(
                    tool_use_id=tool.tool_use_id,
                    tool_name=tool.tool_name,
                    tool_input=tool.tool_input,
                    status=tool.status,
                )
            )
            self._emitted_flat_tool_ids.add(tool.tool_use_id)

        file_tool_outputs = tuple(file_tools)
        file_tool_fingerprint = _file_tool_aggregate_fingerprint(file_tool_outputs)
        if file_tool_outputs and file_tool_fingerprint != self._file_tool_aggregate_fingerprint:
            messages.append(
                FileToolAggregateStatusOutput(
                    message_key=_FILE_TOOL_AGGREGATE_MESSAGE_KEY,
                    tools=file_tool_outputs,
                )
            )

        containers = self._merge_subagent_containers(tuple(subagent_containers))
        aggregate_fingerprint = _subagent_aggregate_fingerprint(containers)
        if containers and aggregate_fingerprint != self._subagent_aggregate_fingerprint:
            messages.append(
                SubagentAggregateStatusOutput(
                    message_key=_SUBAGENT_AGGREGATE_MESSAGE_KEY,
                    containers=containers,
                )
            )
        self._task_list_fingerprint = task_list_fingerprint
        self._file_tool_aggregate_fingerprint = file_tool_fingerprint if file_tool_outputs else None
        self._subagent_aggregate_fingerprint = aggregate_fingerprint if containers else None
        self._tool_fingerprint_by_id = current_fingerprint_by_id
        return messages

    def _merge_subagent_containers(self, current_containers: tuple[ToolStatusOutput, ...]) -> tuple[ToolStatusOutput, ...]:
        current_ids = {container.tool_use_id for container in current_containers}
        if not current_ids:
            self._subagent_container_by_id = {}
            return ()

        merged_containers: list[ToolStatusOutput] = []
        for container in current_containers:
            merged = _merge_subagent_container_output(
                self._subagent_container_by_id.get(container.tool_use_id),
                container,
            )
            self._subagent_container_by_id[container.tool_use_id] = merged
            merged_containers.append(merged)

        self._subagent_container_by_id = {
            tool_use_id: container
            for tool_use_id, container in self._subagent_container_by_id.items()
            if tool_use_id in current_ids
        }
        return tuple(merged_containers)

    def _collect_user_question_updates(
        self,
        *,
        snapshot: _StructuredSnapshot,
        tool_question_prompts: dict[str, tuple[UserQuestionPrompt, ...]],
        pending_question_prompts: tuple[UserQuestionPrompt, ...] = (),
    ) -> list[UserQuestionOutput]:
        messages: list[UserQuestionOutput] = []

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

    async def _load_session(self, *, log_missing: bool):
        if self._task_id is not None:
            return await self._task_service.get_structured_session_for_task(
                task_id=self._task_id,
                user_id=self._user_id,
                log_missing=log_missing,
            )
        return await self._task_service.get_structured_session(self._user_id, log_missing=log_missing)

    async def _load_snapshot(self, *, log_missing: bool) -> _StructuredSnapshot:
        session = await self._load_session(log_missing=log_missing)
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
            structured_result = getattr(tool, "structured_result", None)
            if structured_result is not None and not isinstance(structured_result, dict):
                structured_result = None
            result = getattr(tool, "result", None)
            states.append(
                _ToolStateSnapshot(
                    tool_use_id=str(tool_use_id),
                    tool_name=str(tool_name) if tool_name is not None else None,
                    tool_input=tool_input,
                    status=str(status_value) if status_value is not None else None,
                    result=str(result) if result is not None else None,
                    structured_result=structured_result,
                    subagent_tools=tuple(self._collect_subagent_tool_states(tool)),
                )
            )
        return states

    def _collect_subagent_tool_states(self, tool) -> list[_SubagentToolStateSnapshot]:
        subagent_tools = getattr(tool, "subagent_tools", ()) or ()
        states: list[_SubagentToolStateSnapshot] = []
        for subagent_tool in subagent_tools:
            status = getattr(subagent_tool, "status", None)
            status_value = getattr(status, "value", status)
            tool_name = getattr(subagent_tool, "name", None)
            tool_input = getattr(subagent_tool, "input", None)
            if tool_input is not None and not isinstance(tool_input, dict):
                tool_input = None
            states.append(
                _SubagentToolStateSnapshot(
                    tool_use_id=str(getattr(subagent_tool, "tool_use_id", "")),
                    tool_name=str(tool_name) if tool_name is not None else None,
                    tool_input=tool_input,
                    status=str(status_value) if status_value is not None else None,
                )
            )
        return states
