from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from app.bot.presenters.structured_reply_models import (
    FileToolAggregateStatusOutput,
    SubagentAggregateStatusOutput,
    SubagentToolStatusOutput,
    TaskListItemStatusOutput,
    TaskListStatusOutput,
    ToolStatusOutput,
)
from app.bot.presenters.structured_reply_text import _truncate_permission_text, _truncate_question_text
from app.domain.session_models import ToolStatus
from app.domain.user_question_models import UserQuestionPrompt, extract_user_question_prompts

_TASK_LIST_VISIBLE_LIMIT = 20


def _format_omitted(omitted: int) -> str:
    return f"...另有 {omitted} 项未显示"


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
        lines.append(
            f"{prefix}{_tool_status_icon(tool.status)} {index + 1}. {tool.tool_name or 'Unknown'} - {_tool_status_label(tool.status)}{detail_text}"
        )

    omitted = len(visible_tools) - len(display_indexes)
    if omitted > 0:
        lines.append(_format_omitted(omitted))

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
        lines.append(_format_omitted(omitted))

    return "\n".join(lines)


def build_subagent_aggregate_status_message(output: SubagentAggregateStatusOutput) -> str:
    containers = output.containers
    noun = _subagent_aggregate_noun(containers)
    icon = _aggregate_status_icon(_subagent_container_status_values(containers))
    status_text = _subagent_aggregate_status_text(containers)
    status_count = _subagent_aggregate_status_count(containers, status_text=status_text)
    count_text = str(len(containers)) if status_count == len(containers) else f"{status_count}/{len(containers)}"
    lines = [f"{icon} {count_text} {noun} {status_text}"]
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
        lines.append(
            f"{_tool_status_icon(tool.status)} {index}. {tool.tool_name or 'Unknown'} - {_tool_status_label(tool.status)}{detail_text}"
        )

    omitted = len(tools) - len(display_tools)
    if omitted > 0:
        lines.append(_format_omitted(omitted))

    return "\n".join(lines)


def _visible_subagent_tools(subagent_tools: tuple[SubagentToolStatusOutput, ...]) -> tuple[SubagentToolStatusOutput, ...]:
    return tuple(tool for tool in subagent_tools if not _is_user_question_tool(tool.tool_name, tool.tool_input))


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


def _select_active_index(
    items: tuple[Any, ...], *, get_status: Callable[[Any], str | None], priority_statuses: tuple[str, ...]
) -> int | None:
    for status in priority_statuses:
        for index, item in enumerate(items):
            if get_status(item) == status:
                return index
    return None


def _select_active_file_tool_index(tools: tuple[ToolStatusOutput, ...]) -> int | None:
    return _select_active_index(
        tools,
        get_status=lambda t: t.status,
        priority_statuses=(
            ToolStatus.RUNNING.value,
            ToolStatus.WAITING_FOR_APPROVAL.value,
            ToolStatus.ERROR.value,
            ToolStatus.INTERRUPTED.value,
        ),
    )


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


def _subagent_aggregate_status_count(containers: tuple[ToolStatusOutput, ...], *, status_text: str) -> int:
    return sum(1 for container in containers if _subagent_aggregate_status_text((container,)) == status_text)


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


_TOOL_STATUS_ICON: dict[str | None, str] = {
    ToolStatus.RUNNING.value: "🔄",
    ToolStatus.SUCCESS.value: "✅",
    ToolStatus.ERROR.value: "❌",
    ToolStatus.INTERRUPTED.value: "⏹️",
    ToolStatus.WAITING_FOR_APPROVAL.value: "⏳",
}


def _tool_status_icon(status: str | None) -> str:
    return _TOOL_STATUS_ICON.get(status, "⏳")


_TOOL_STATUS_LABEL: dict[str | None, str] = {
    ToolStatus.RUNNING.value: "执行中",
    ToolStatus.SUCCESS.value: "完成",
    ToolStatus.ERROR.value: "失败",
    ToolStatus.INTERRUPTED.value: "已中断",
    ToolStatus.WAITING_FOR_APPROVAL.value: "等待权限",
}


def _tool_status_label(status: str | None) -> str:
    return _TOOL_STATUS_LABEL.get(status, "未知")


def _select_active_subagent_index(tools: tuple[SubagentToolStatusOutput, ...]) -> int | None:
    return _select_active_index(
        tools,
        get_status=lambda t: t.status,
        priority_statuses=(
            ToolStatus.RUNNING.value,
            ToolStatus.WAITING_FOR_APPROVAL.value,
            ToolStatus.ERROR.value,
            ToolStatus.INTERRUPTED.value,
        ),
    )


def _select_visible_indexes(count: int, *, active_index: int | None, limit: int = _TASK_LIST_VISIBLE_LIMIT) -> tuple[int, ...]:
    if count <= limit:
        return tuple(range(count))
    indexes = list(range(limit))
    if active_index is not None and active_index not in indexes:
        indexes[-1] = active_index
    return tuple(indexes)


def _select_visible_subagent_indexes(tools: tuple[SubagentToolStatusOutput, ...], *, active_index: int | None) -> tuple[int, ...]:
    return _select_visible_indexes(len(tools), active_index=active_index)


def _select_active_task_list_item_index(items: tuple[TaskListItemStatusOutput, ...]) -> int | None:
    return _select_active_index(
        items,
        get_status=lambda t: t.status,
        priority_statuses=("in_progress", "failed", "interrupted", "pending"),
    )


def _select_visible_task_list_item_indexes(items: tuple[TaskListItemStatusOutput, ...], *, active_index: int | None) -> tuple[int, ...]:
    return _select_visible_indexes(len(items), active_index=active_index)


_TASK_LIST_ITEM_STATUS_ICON: dict[str | None, str] = {
    "in_progress": "🔄",
    "completed": "✅",
    "failed": "❌",
    "interrupted": "⏹️",
    "deleted": "🗑️",
}


def _task_list_item_status_icon(status: str | None) -> str:
    return _TASK_LIST_ITEM_STATUS_ICON.get(status, "⏳")


_TASK_LIST_ITEM_STATUS_LABEL: dict[str | None, str] = {
    "in_progress": "执行中",
    "completed": "完成",
    "failed": "失败",
    "interrupted": "已中断",
    "deleted": "已删除",
}


def _task_list_item_status_label(status: str | None) -> str:
    return _TASK_LIST_ITEM_STATUS_LABEL.get(status, "待执行")


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
