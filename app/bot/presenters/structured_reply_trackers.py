from __future__ import annotations

import re

from app.bot.presenters.structured_reply_messages import _format_tool_input_detail, _is_user_question_tool, build_user_question_prompt
from app.bot.presenters.structured_reply_models import (
    FileToolAggregateStatusOutput,
    SubagentAggregateStatusOutput,
    SubagentToolStatusOutput,
    TaskListItemStatusOutput,
    TaskListStatusOutput,
    ToolStatusOutput,
    UserQuestionOutput,
    _ToolStateSnapshot,
)
from app.domain.session_models import ToolStatus
from app.domain.user_question_models import UserQuestionPrompt
from app.services.session_store import parse_user_question_key

_TASK_LIST_MESSAGE_KEY = "task-list"
_SUBAGENT_AGGREGATE_MESSAGE_KEY = "subagent-aggregate"
_FILE_TOOL_AGGREGATE_MESSAGE_KEY = "file-tool-aggregate"
_FILE_TOOL_NAMES = {"read", "grep", "glob"}
_TERMINAL_TOOL_STATUSES = {
    ToolStatus.SUCCESS.value,
    ToolStatus.ERROR.value,
    ToolStatus.INTERRUPTED.value,
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
            task_id = _task_list_text_value(task_input, "taskId") or _task_list_text_value(task_input, "task_id")  # type: ignore[assignment]
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
        _subagent_container_output(tool) for tool in tools if tool.status is not None and _is_subagent_container_tool(tool.tool_name)
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


class UserQuestionTracker:
    def __init__(self) -> None:
        self._last_question_key: str | None = None
        self._question_keys_by_tool_id: dict[str, tuple[str, ...]] = {}

    @property
    def last_question_key(self) -> str | None:
        return self._last_question_key

    def reset(self) -> None:
        self._last_question_key = None
        self._question_keys_by_tool_id = {}

    def set_cursor(self, question_key: str | None) -> None:
        self._last_question_key = question_key

    def baseline(
        self,
        *,
        tool_question_prompts: dict[str, tuple[UserQuestionPrompt, ...]],
        pending_prompts: tuple[UserQuestionPrompt, ...],
    ) -> None:
        self._question_keys_by_tool_id = {
            tool_use_id: tuple(prompt.key for prompt in prompts) for tool_use_id, prompts in tool_question_prompts.items() if prompts
        }
        if pending_prompts:
            self._question_keys_by_tool_id[pending_prompts[0].tool_use_id] = tuple(prompt.key for prompt in pending_prompts)

    def acknowledge(self, question: UserQuestionPrompt) -> None:
        self._last_question_key = question.key
        previous_keys = self._question_keys_by_tool_id.get(question.tool_use_id, ())
        if question.key not in previous_keys:
            self._question_keys_by_tool_id[question.tool_use_id] = (*previous_keys, question.key)

    def collect_updates(
        self,
        *,
        tool_states: tuple[_ToolStateSnapshot, ...],
        tool_question_prompts: dict[str, tuple[UserQuestionPrompt, ...]],
        pending_question_prompts: tuple[UserQuestionPrompt, ...] = (),
    ) -> list[UserQuestionOutput]:
        messages: list[UserQuestionOutput] = []

        active_prompt_group: tuple[UserQuestionPrompt, ...] = pending_question_prompts
        active_tool_use_id: str | None = pending_question_prompts[0].tool_use_id if pending_question_prompts else None
        if not active_prompt_group:
            for tool in tool_states:
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
            current_question_cursor = parse_user_question_key(self._last_question_key)
            if current_question_cursor is not None and current_question_cursor[0] == active_tool_use_id:
                matched_prompt = next((prompt for prompt in active_prompt_group if prompt.key == self._last_question_key), None)
                if matched_prompt is not None:
                    selected_prompt = matched_prompt
            if selected_prompt.key not in previous_keys and selected_prompt.key != self._last_question_key:
                messages.append(
                    UserQuestionOutput(
                        text=build_user_question_prompt(selected_prompt),
                        question=selected_prompt,
                    )
                )

        return messages


class FlatToolTracker:
    def __init__(self) -> None:
        self._fingerprint_by_id: dict[str, tuple] = {}
        self._emitted_tool_ids: set[str] = set()

    def reset(self) -> None:
        self._fingerprint_by_id = {}
        self._emitted_tool_ids = set()

    def baseline(self, tool_states: tuple[_ToolStateSnapshot, ...]) -> None:
        self._fingerprint_by_id = {tool.tool_use_id: _tool_state_fingerprint(tool) for tool in tool_states if tool.status is not None}
        self._emitted_tool_ids = set()

    def update(
        self,
        *,
        all_tool_states: tuple[_ToolStateSnapshot, ...],
        flat_tools: tuple[_ToolStateSnapshot, ...],
        suppress_new: bool,
    ) -> tuple[ToolStatusOutput, ...]:
        current_fingerprint_by_id = {tool.tool_use_id: _tool_state_fingerprint(tool) for tool in all_tool_states if tool.status is not None}
        outputs: list[ToolStatusOutput] = []
        for tool in flat_tools:
            if tool.status is None:
                continue
            if suppress_new and tool.tool_use_id not in self._emitted_tool_ids:
                continue
            fingerprint = current_fingerprint_by_id.get(tool.tool_use_id)
            if self._fingerprint_by_id.get(tool.tool_use_id) == fingerprint:
                continue
            outputs.append(_tool_status_output(tool))
            self._emitted_tool_ids.add(tool.tool_use_id)
        self._fingerprint_by_id = current_fingerprint_by_id
        return tuple(outputs)


class TaskListTracker:
    def __init__(self) -> None:
        self._fingerprint: tuple | None = None

    def reset(self) -> None:
        self._fingerprint = None

    def baseline(self, tool_states: tuple[_ToolStateSnapshot, ...]) -> None:
        self._fingerprint = _task_list_status_fingerprint(_task_list_status_output(tool_states))

    def update(self, tool_states: tuple[_ToolStateSnapshot, ...]) -> tuple[TaskListStatusOutput | None, bool]:
        output = _task_list_status_output(tool_states)
        fingerprint = _task_list_status_fingerprint(output)
        has_task_list = output is not None
        changed_output = output if output is not None and fingerprint != self._fingerprint else None
        self._fingerprint = fingerprint
        return changed_output, has_task_list


class SubagentAggregateTracker:
    def __init__(self) -> None:
        self._fingerprint: tuple | None = None
        self._container_by_id: dict[str, ToolStatusOutput] = {}

    def reset(self) -> None:
        self._fingerprint = None
        self._container_by_id = {}

    def baseline(self, tool_states: tuple[_ToolStateSnapshot, ...]) -> None:
        containers = _subagent_container_outputs(tool_states)
        self._container_by_id = {container.tool_use_id: container for container in containers}
        self._fingerprint = _subagent_aggregate_fingerprint(containers)

    def known_nested_tool_ids(self) -> set[str]:
        return {subagent_tool.tool_use_id for container in self._container_by_id.values() for subagent_tool in container.subagent_tools}

    def update(self, current_containers: tuple[ToolStatusOutput, ...]) -> SubagentAggregateStatusOutput | None:
        containers = self._merge(current_containers)
        fingerprint = _subagent_aggregate_fingerprint(containers)
        changed = bool(containers) and fingerprint != self._fingerprint
        self._fingerprint = fingerprint if containers else None
        if not changed:
            return None
        return SubagentAggregateStatusOutput(
            message_key=_SUBAGENT_AGGREGATE_MESSAGE_KEY,
            containers=containers,
        )

    def _merge(self, current_containers: tuple[ToolStatusOutput, ...]) -> tuple[ToolStatusOutput, ...]:
        current_ids = {container.tool_use_id for container in current_containers}
        if not current_ids:
            self._container_by_id = {}
            return ()

        merged_containers: list[ToolStatusOutput] = []
        for container in current_containers:
            merged = _merge_subagent_container_output(
                self._container_by_id.get(container.tool_use_id),
                container,
            )
            self._container_by_id[container.tool_use_id] = merged
            merged_containers.append(merged)

        self._container_by_id = {
            tool_use_id: container for tool_use_id, container in self._container_by_id.items() if tool_use_id in current_ids
        }
        return tuple(merged_containers)


class FileToolAggregateTracker:
    def __init__(self) -> None:
        self._fingerprint: tuple | None = None

    def reset(self) -> None:
        self._fingerprint = None

    def baseline(self, file_tools: tuple[ToolStatusOutput, ...]) -> None:
        self._fingerprint = _file_tool_aggregate_fingerprint(file_tools)

    def update(self, file_tools: tuple[ToolStatusOutput, ...]) -> FileToolAggregateStatusOutput | None:
        fingerprint = _file_tool_aggregate_fingerprint(file_tools)
        changed = bool(file_tools) and fingerprint != self._fingerprint
        self._fingerprint = fingerprint if file_tools else None
        if not changed:
            return None
        return FileToolAggregateStatusOutput(
            message_key=_FILE_TOOL_AGGREGATE_MESSAGE_KEY,
            tools=file_tools,
        )
