from __future__ import annotations

import logging

from app.bot.presenters.structured_reply_messages import (
    build_compacting_progress_message,
    build_file_tool_aggregate_status_message,
    build_permission_prompt,
    build_subagent_aggregate_status_message,
    build_task_list_status_message,
    build_tool_progress_message,
    build_tool_status_message,
    build_tool_task_list_message,
    build_user_question_prompt,
)
from app.bot.presenters.structured_reply_models import (
    _StructuredSnapshot,
    _SubagentToolStateSnapshot,
    _ToolStateSnapshot,
    FileToolAggregateStatusOutput,
    PermissionRequestOutput,
    ProgressUpdateOutput,
    StructuredReplyOutput,
    SubagentAggregateStatusOutput,
    SubagentToolStatusOutput,
    TaskListItemStatusOutput,
    TaskListStatusOutput,
    ToolStatusOutput,
    UserQuestionOutput,
)
from app.bot.presenters.structured_reply_snapshot_loader import StructuredReplySnapshotLoader
from app.bot.presenters.structured_reply_text import (
    _MARKER_LINE_RE,
    normalize_stream_text,
    preview_stream_text,
    strip_bridge_markers,
)
from app.bot.presenters.structured_reply_trackers import (
    FileToolAggregateTracker,
    FlatToolTracker,
    SubagentAggregateTracker,
    TaskListTracker,
    UserQuestionTracker,
    _is_file_tool,
    _is_subagent_container_tool,
    _is_task_list_tool,
    _subagent_container_output,
    _tool_status_output,
)
from app.domain.session_models import SessionPhase, ToolStatus
from app.domain.user_question_models import UserQuestionPrompt, extract_user_question_prompts
from app.services.task_service import TaskService

logger = logging.getLogger(__name__)

_FALLBACK_PROMPT = "结构化回复暂不可用，已回退为原始输出。"


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
    def __init__(self, *, task_service: TaskService, user_id: int, task_id: str | None = None) -> None:
        self._task_service = task_service
        self._user_id = user_id
        self._task_id = task_id
        self._snapshot_loader = StructuredReplySnapshotLoader(
            task_service=task_service,
            user_id=user_id,
            task_id=task_id,
        )
        self._last_structured_turn_id: str | None = None
        self._last_pending_permission_key: str | None = None
        self._structured_session_available = False
        self._structured_reply_emitted_in_run = False
        self._fallback_announced = False
        self._revision = 0
        self._current_session_id: str | None = None
        self._last_phase: str | None = None
        self._user_question_tracker = UserQuestionTracker()
        self._flat_tool_tracker = FlatToolTracker()
        self._task_list_tracker = TaskListTracker()
        self._subagent_tracker = SubagentAggregateTracker()
        self._file_tool_tracker = FileToolAggregateTracker()

    @property
    def structured_session_available(self) -> bool:
        return self._structured_session_available

    async def prime(self, *, log_missing: bool = True, baseline_current_snapshot: bool = False) -> None:
        snapshot = await self._snapshot_loader.load_snapshot(log_missing=log_missing)
        self._structured_session_available = snapshot.session_available
        self._current_session_id = snapshot.session_id
        self._last_phase = snapshot.phase

        persisted_turn_id, persisted_permission_key = await self._task_service.get_structured_reply_cursor(self._user_id, task_id=self._task_id)
        self._last_structured_turn_id = persisted_turn_id
        if self._last_structured_turn_id is None and baseline_current_snapshot:
            self._last_structured_turn_id = snapshot.turn_id
        self._last_pending_permission_key = persisted_permission_key
        pending_prompts: tuple[UserQuestionPrompt, ...] = ()

        if baseline_current_snapshot:
            tool_question_prompts = _extract_tool_question_prompts_by_id(snapshot)
            self._flat_tool_tracker.baseline(snapshot.tool_states)
            self._subagent_tracker.baseline(snapshot.tool_states)
            file_tools = tuple(
                _tool_status_output(tool)
                for tool in snapshot.tool_states
                if tool.status is not None and _is_file_tool(tool.tool_name)
            )
            self._file_tool_tracker.baseline(file_tools)
            self._task_list_tracker.baseline(snapshot.tool_states)
            pending_prompts = self._extract_pending_user_question_prompts(snapshot, tool_question_prompts=tool_question_prompts)
            self._user_question_tracker.baseline(
                tool_question_prompts=tool_question_prompts,
                pending_prompts=pending_prompts,
            )
        else:
            self._flat_tool_tracker.reset()
            self._task_list_tracker.reset()
            self._subagent_tracker.reset()
            self._file_tool_tracker.reset()
            self._user_question_tracker.reset()

        question_cursor = await self._task_service.get_structured_user_question_cursor(self._user_id, task_id=self._task_id)
        if question_cursor is None and baseline_current_snapshot and pending_prompts:
            question_cursor = pending_prompts[0].key
        self._user_question_tracker.set_cursor(question_cursor)

        self._revision = await self._task_service.get_structured_session_cursor(self._user_id, task_id=self._task_id)

    async def wait_for_update(self, *, timeout_sec: float) -> bool:
        current_session = await self._snapshot_loader.load_session(log_missing=False)
        current_session_id = current_session.session_id if current_session is not None else None
        if current_session_id != self._current_session_id:
            self._current_session_id = current_session_id
            self._last_phase = None
            self._last_pending_permission_key = None
            self._flat_tool_tracker.reset()
            self._task_list_tracker.reset()
            self._subagent_tracker.reset()
            self._file_tool_tracker.reset()
            self._user_question_tracker.reset()
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
        snapshot = await self._snapshot_loader.load_snapshot(log_missing=log_missing)
        self._structured_session_available = self._structured_session_available or snapshot.session_available
        tool_question_prompts = _extract_tool_question_prompts_by_id(snapshot)
        self._user_question_tracker.set_cursor(
            await self._task_service.get_structured_user_question_cursor(self._user_id, task_id=self._task_id)
        )

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
        question_updates = self._user_question_tracker.collect_updates(
            tool_states=snapshot.tool_states,
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
        self._user_question_tracker.acknowledge(output.question)

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

        task_list_output, suppress_flat_tools = self._task_list_tracker.update(snapshot.tool_states)
        if task_list_output is not None:
            messages.append(task_list_output)

        nested_tool_ids = {
            subagent_tool.tool_use_id
            for tool in snapshot.tool_states
            for subagent_tool in tool.subagent_tools
        }
        nested_tool_ids.update(self._subagent_tracker.known_nested_tool_ids())
        subagent_containers: list[ToolStatusOutput] = []
        file_tools: list[ToolStatusOutput] = []
        flat_tools: list[_ToolStateSnapshot] = []
        for tool in snapshot.tool_states:
            if tool.status is None:
                continue
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
            flat_tools.append(tool)

        messages.extend(
            self._flat_tool_tracker.update(
                all_tool_states=snapshot.tool_states,
                flat_tools=tuple(flat_tools),
                suppress_new=suppress_flat_tools,
            )
        )

        file_tool_output = self._file_tool_tracker.update(tuple(file_tools))
        if file_tool_output is not None:
            messages.append(file_tool_output)

        subagent_output = self._subagent_tracker.update(tuple(subagent_containers))
        if subagent_output is not None:
            messages.append(subagent_output)

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
