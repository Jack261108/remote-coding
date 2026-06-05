from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable
from pathlib import Path
from typing import TYPE_CHECKING

from app.bootstrap_base import AppContainerBase
from app.config.settings import is_workdir_allowed
from app.domain.external_session_models import SessionOrigin as ExternalSessionOrigin
from app.domain.hook_models import HookEvent
from app.domain.models import SessionContext, TaskStatus, utc_now
from app.domain.session_models import SessionEvent, SessionEventType, SessionPhase, SessionState
from app.domain.user_question_models import extract_user_question_prompts
from app.services.permission_callback_registry import AutoApproveOutcome, SessionOrigin

if TYPE_CHECKING:
    from app.domain.external_session_models import OwnershipResult

logger = logging.getLogger(__name__)


def _is_session_end_event(event: HookEvent) -> bool:
    return event.event == "SessionEnd" or event.status == "ended"


class _StageShortCircuit(Exception):
    """Raised by a pipeline stage to terminate the rest of the stage list.

    The orchestration loop catches this, logs at INFO level, closes unawaited
    coroutines, and stops further stage execution. Not treated as an error.
    """

    def __init__(self, *, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class JsonlSyncMixin(AppContainerBase):
    """JSONL sync: debounced incremental parsing and event dispatch."""

    async def sync_claude_session(self, session_id: str, cwd: str) -> None:
        async with self._jsonl_sync_locks.lock(session_id):
            snapshot = self.claude_jsonl_parser.parse_incremental(session_id=session_id, cwd=cwd)
            logger.info(
                "claude session synced",
                extra={
                    "session_id": session_id,
                    "cwd": cwd,
                    "turn_count": len(snapshot.turns),
                    "tool_call_count": len(snapshot.tool_calls),
                    "last_reply": snapshot.last_reply,
                    "last_reply_role": snapshot.last_reply_role,
                    "last_offset": snapshot.last_offset,
                    "clear_detected": snapshot.clear_detected,
                },
            )
            await self._dispatch_session_event(  # type: ignore[attr-defined]
                SessionEvent(
                    session_id=session_id,
                    type=SessionEventType.FILE_SYNCED,
                    payload=snapshot.to_payload(),
                )
            )

    def _schedule_jsonl_sync(self, session_id: str, cwd: str) -> None:
        self.session_supervisor.watch(session_id=session_id, workdir=cwd)
        self.session_supervisor.schedule_jsonl_sync(session_id, cwd)


class HookHandlingMixin(AppContainerBase):
    """Hook event handling: validate, bind session, dispatch events."""

    async def _handle_hook_event(self, event: HookEvent) -> None:
        logger.debug(
            "hook event received",
            extra={
                "session_id": event.session_id,
                "event": event.event,
                "status": event.status,
                "tool": event.tool,
            },
        )

        # Stage 1: Ownership resolution (gate — failure halts pipeline)
        ownership = await self._resolve_ownership_stage(event)
        if ownership is None:
            return

        # Stages 2+: each wrapped independently in error boundaries.
        # A stage may raise _StageShortCircuit to terminate the pipeline early.
        stages = self._build_stage_list(event, ownership)
        executed_up_to = -1
        for i, (stage_name, stage_coro) in enumerate(stages):
            try:
                await stage_coro
                executed_up_to = i
            except _StageShortCircuit as sc:
                logger.info(
                    "hook pipeline short-circuited",
                    extra={
                        "stage_name": stage_name,
                        "reason": sc.reason,
                        "session_id": event.session_id,
                        "event_type": event.event,
                    },
                )
                executed_up_to = i
                break
            except Exception:
                logger.exception(
                    "hook pipeline stage failed",
                    extra={
                        "stage_name": stage_name,
                        "session_id": getattr(event, "session_id", None),
                        "event_type": getattr(event, "event", None),
                        "hook_cwd": getattr(event, "cwd", None),
                    },
                )

        # Close un-awaited coroutines from skipped stages
        for j in range(executed_up_to + 1, len(stages)):
            coro = stages[j][1]
            if hasattr(coro, "close"):
                coro.close()

    async def _resolve_ownership_stage(self, event: HookEvent) -> OwnershipResult | None:
        """Gate stage: workdir check, SessionEnd cleanup, and ownership resolution.

        Returns the OwnershipResult on success, or None if the event should be
        skipped (rejected by workdir allowlist or handled via legacy fallback).
        Exceptions are logged as ERROR with stage_name="ownership_resolution" and
        the method returns None so the pipeline halts gracefully.
        """
        try:
            # Workdir allowlist check
            if not is_workdir_allowed(event.cwd, self.settings.allowed_workdirs):
                logger.warning(
                    "hook event rejected by workdir allowlist",
                    extra={"session_id": event.session_id, "cwd": event.cwd, "event": event.event},
                )
                return None

            is_session_end = _is_session_end_event(event)

            # Clear unified permission state on session end before removing bindings.
            if is_session_end:
                if hasattr(self, "auto_approve_service"):
                    await self.auto_approve_service.deactivate_all_for_session(event.session_id)
                    await self.auto_approve_service.release_all_slots_for_session(event.session_id)
                if hasattr(self, "permission_callback_registry"):
                    await self.permission_callback_registry.invalidate_session(event.session_id)
                if hasattr(self, "unbound_permission_handler"):
                    await self.unbound_permission_handler.invalidate_session(event.session_id)

            # If ownership_resolver is not wired (e.g. in tests), fall back to old behavior
            if not hasattr(self, "ownership_resolver"):
                if is_session_end and hasattr(self, "external_binding_store"):
                    self.external_binding_store.remove_binding(event.session_id)
                await self._bind_hook_session(event)
                await self._dispatch_session_event(  # type: ignore[attr-defined]
                    SessionEvent(
                        session_id=event.session_id,
                        type=SessionEventType.HOOK_RECEIVED,
                        payload=event.to_dict(),
                    )
                )
                self._schedule_jsonl_sync(event.session_id, event.cwd)  # type: ignore[attr-defined]
                return None

            # Resolve ownership before removing a SessionEnd binding so the ended
            # event still follows the external-bound pipeline it belonged to.
            ownership = await self.ownership_resolver.resolve(event.session_id)

            if not is_session_end and ownership.origin == ExternalSessionOrigin.EXTERNAL and hasattr(self, "external_discovery"):
                is_ended = getattr(self.external_discovery, "is_session_ended", None)
                if callable(is_ended) and is_ended(event.session_id):
                    if event.expects_response and hasattr(self, "hook_socket_server"):
                        await self.hook_socket_server.cancel_pending_permissions(session_id=event.session_id)
                    return None

            # Remove external binding on session end so /list doesn't show stale entries.
            if is_session_end and ownership.origin == ExternalSessionOrigin.EXTERNAL:
                if hasattr(self, "external_discovery"):
                    marker = getattr(self.external_discovery, "mark_session_ended", None)
                    if callable(marker):
                        marker(event.session_id)
                    else:
                        self.external_discovery.remove_session(event.session_id)
                if hasattr(self, "external_uq_state"):
                    invalidator = getattr(self.external_uq_state, "invalidate_session", None)
                    if callable(invalidator):
                        invalidator(event.session_id)
                if hasattr(self, "external_binding_store"):
                    self.external_binding_store.remove_binding(event.session_id)
            logger.info(
                "hook event ownership resolved",
                extra={
                    "session_id": event.session_id,
                    "ownership_state": ownership.ownership_state,
                    "origin": ownership.origin.value,
                    "owner_user_id": ownership.owner_user_id,
                },
            )

            # Refresh activity timestamp on bound external hook events so that
            # the periodic stale-binding cleanup keeps active sessions alive.
            # Skipped for SessionEnd (which removes the binding above) and for
            # tmux-owned or unbound events.
            if (
                ownership.origin == ExternalSessionOrigin.EXTERNAL
                and ownership.ownership_state == "bound"
                and not is_session_end
                and hasattr(self, "external_binding_store")
                and self.external_binding_store.get_binding(event.session_id) is not None
            ):
                self.external_binding_store.touch_activity(event.session_id, utc_now(), pid=event.pid)

            return ownership
        except Exception:
            logger.exception(
                "hook pipeline stage failed",
                extra={
                    "stage_name": "ownership_resolution",
                    "session_id": event.session_id,
                    "event_type": event.event,
                    "hook_cwd": event.cwd,
                },
            )
            return None

    def _build_stage_list(self, event: HookEvent, ownership: OwnershipResult) -> list[tuple[str, Awaitable[None]]]:
        """Build the ordered list of pipeline stages based on ownership state.

        Returns a list of (stage_name, coroutine) tuples. Each coroutine is a
        zero-arg awaitable that captures the needed context from event/ownership.
        """
        stages: list[tuple[str, Awaitable[None]]] = []

        if ownership.ownership_state == "owned":
            # Session binding MUST run before auto-approve check so that
            # structured_session_store is updated even when short-circuited.
            stages.append(
                (
                    "session_binding",
                    self._bind_hook_session(event),
                )
            )
            # Event dispatch
            stages.append(
                (
                    "event_dispatch",
                    self._dispatch_session_event(  # type: ignore[attr-defined]
                        SessionEvent(
                            session_id=event.session_id,
                            type=SessionEventType.HOOK_RECEIVED,
                            payload=event.to_dict(),
                        )
                    ),
                )
            )

            # JSONL sync scheduling (sync, not async — wrap in a trivial coroutine)
            async def _schedule_jsonl_owned() -> None:
                self._schedule_jsonl_sync(event.session_id, event.cwd)  # type: ignore[attr-defined]

            stages.append(("jsonl_sync_scheduling", _schedule_jsonl_owned()))

            # Auto-approve check — may short-circuit, skipping only auto_file_send
            stages.append(
                (  # type: ignore[arg-type]
                    "auto_approve_check",
                    self._run_auto_approve_check(
                        event,
                        origin=SessionOrigin.OWNED,
                        candidate_user_id=ownership.owner_user_id,
                    ),
                )
            )

            # Auto-file-send (sync — wrap in a trivial coroutine)
            async def _auto_file_send_owned() -> None:
                self._maybe_auto_file_send(event, ownership.owner_user_id)

            stages.append(("auto_file_send", _auto_file_send_owned()))

        elif ownership.ownership_state == "bound":
            # Event dispatch MUST run before auto-approve check
            stages.append(
                (
                    "event_dispatch",
                    self._dispatch_session_event(  # type: ignore[attr-defined]
                        SessionEvent(
                            session_id=event.session_id,
                            type=SessionEventType.HOOK_RECEIVED,
                            payload=event.to_dict(),
                        )
                    ),
                )
            )

            # JSONL sync scheduling
            async def _schedule_jsonl_bound() -> None:
                self._schedule_jsonl_sync(event.session_id, event.cwd)  # type: ignore[attr-defined]

            stages.append(("jsonl_sync_scheduling", _schedule_jsonl_bound()))

            # Auto-approve check — may short-circuit, skipping push_notification
            stages.append(
                (  # type: ignore[arg-type]
                    "auto_approve_check",
                    self._run_auto_approve_check(
                        event,
                        origin=SessionOrigin.EXTERNAL_BOUND,
                        candidate_user_id=ownership.owner_user_id,
                    ),
                )
            )

            # Push notification
            async def _push_notification_bound() -> None:
                if hasattr(self, "push_notifier") and ownership.owner_user_id is not None:
                    await self._notify_bound_external_event(event, ownership.owner_user_id)

            stages.append(("push_notification", _push_notification_bound()))

            # Auto-file-send
            async def _auto_file_send_bound() -> None:
                self._maybe_auto_file_send(event, ownership.owner_user_id)

            stages.append(("auto_file_send", _auto_file_send_bound()))

        else:
            # Unbound
            # External discovery record
            async def _external_discovery() -> None:
                if hasattr(self, "external_discovery"):
                    if _is_session_end_event(event):
                        return
                    self.external_discovery.record_event(event)

            stages.append(("external_discovery", _external_discovery()))

            # Permission handling
            async def _permission_handling() -> None:
                if event.expects_response and hasattr(self, "unbound_permission_handler"):
                    candidate_user_id = None
                    if hasattr(self, "auto_approve_service"):
                        candidate_user_id = self.auto_approve_service.get_active_user_for_session(event.session_id)
                    outcome = await self._run_auto_approve_check(
                        event,
                        origin=SessionOrigin.EXTERNAL_UNBOUND,
                        candidate_user_id=candidate_user_id,
                    )
                    if outcome in {AutoApproveOutcome.APPROVED, AutoApproveOutcome.APPROVAL_UNKNOWN}:
                        return
                    await self.unbound_permission_handler.handle_unbound_permission(event)

            stages.append(("permission_handling", _permission_handling()))

        return stages

    async def _run_auto_approve_check(
        self,
        event: HookEvent,
        *,
        origin: SessionOrigin = SessionOrigin.EXTERNAL_BOUND,
        candidate_user_id: int | None = None,
    ) -> AutoApproveOutcome:
        """Check if the event should be auto-approved through PermissionGateway.

        Successful or unknown auto-approval short-circuits downstream prompt stages.
        Failed auto-approval falls back to normal interactive notification.
        """
        if not event.expects_response or event.tool == "AskUserQuestion":
            return AutoApproveOutcome.NOT_APPROVED
        if not event.tool_use_id or not hasattr(self, "permission_gateway"):
            return AutoApproveOutcome.NOT_APPROVED

        outcome = await self.permission_gateway.maybe_auto_approve(
            session_id=event.session_id,
            origin=origin,
            candidate_user_id=candidate_user_id,
            tool_use_id=event.tool_use_id,
            tool_name=event.tool or "unknown tool",
            tool_input=event.tool_input,
        )
        if outcome in {AutoApproveOutcome.APPROVED, AutoApproveOutcome.APPROVAL_UNKNOWN}:
            raise _StageShortCircuit(reason="auto-approved")
        return outcome

    def _maybe_auto_file_send(self, event: HookEvent, owner_user_id: int | None) -> None:
        if event.event == "PostToolUse" and event.tool == "Write" and owner_user_id is not None and hasattr(self, "file_sender"):
            file_path_raw = event.tool_input.get("file_path", "") if event.tool_input else ""
            self._background_tasks.spawn(
                self.file_sender.send_if_eligible(
                    file_path_raw=file_path_raw,
                    cwd=event.cwd,
                    chat_id=owner_user_id,
                )
            )

    async def _stop_background_tasks(self) -> None:
        await self._background_tasks.cancel_all()

    async def _notify_bound_external_event(self, event: HookEvent, user_id: int) -> None:
        """Send push notifications for bound external session events."""
        if not hasattr(self, "push_notifier"):
            return
        if event.expects_response:
            # AskUserQuestion: try PTY injection flow if tmux pane is available
            if event.tool == "AskUserQuestion":
                prompts = extract_user_question_prompts(
                    tool_use_id=event.tool_use_id or "",
                    tool_name=event.tool,
                    tool_input=event.tool_input,
                )
                if prompts and hasattr(self, "external_uq_state") and event.pid is not None:
                    # Try to find tmux pane for interactive injection
                    from app.adapters.process.pty_injector import find_tmux_pane_for_pid

                    pane_id = await find_tmux_pane_for_pid(event.pid, self.settings.tmux_bin)
                    if pane_id is not None:
                        # Store pending state and show interactive buttons
                        # Do NOT auto-allow — hold the permission until user clicks
                        from app.services.external_user_question_state import PendingExternalUserQuestion

                        pending = PendingExternalUserQuestion(
                            tool_use_id=event.tool_use_id or "",
                            session_id=event.session_id,
                            user_id=user_id,
                            pid=event.pid,
                            prompts=prompts,
                            pane_id=pane_id,
                            tmux_bin=self.settings.tmux_bin,
                        )
                        self.external_uq_state.store(pending)
                        await self.push_notifier.notify_user_question(
                            user_id=user_id,
                            session_id=event.session_id,
                            prompts=prompts,
                            interactive=True,
                        )
                        return

                # Fallback: no tmux pane found or no PID — fall through to normal
                # permission flow (notify_permission_request below). The user sees
                # the permission request in Telegram and clicks allow; Claude Code
                # then shows the question UI in the terminal.
                pass
            # Resolve title for permission notification
            _title: str | None = None
            if hasattr(self, "claude_jsonl_parser"):
                try:
                    _title = self.claude_jsonl_parser.extract_session_title(session_id=event.session_id, cwd=event.cwd)
                except Exception:
                    pass
            await self.push_notifier.notify_permission_request(
                user_id=user_id,
                session_id=event.session_id,
                tool_name=event.tool or "",
                tool_input=event.tool_input,
                tool_use_id=event.tool_use_id or "",
                cwd=event.cwd,
                title=_title,
            )
        elif event.event == "Stop":
            await self.push_notifier.notify_session_end(
                user_id=user_id,
                session_id=event.session_id,
                cwd=event.cwd,
            )

    async def _handle_permission_failure(self, session_id: str, tool_use_id: str) -> None:
        logger.warning(
            "permission response failed",
            extra={"session_id": session_id, "tool_use_id": tool_use_id},
        )
        await self._dispatch_session_event(  # type: ignore[attr-defined]
            SessionEvent(
                session_id=session_id,
                type=SessionEventType.PERMISSION_RESPONSE_FAILED,
                payload={"tool_use_id": tool_use_id},
            )
        )
        # Update permission callback registry and edit Telegram message
        if hasattr(self, "permission_callback_registry"):
            # Get the record before invalidating to preserve message info
            record = await self.permission_callback_registry.get_record_by_tool_use_id(session_id, tool_use_id)
            await self.permission_callback_registry.invalidate_pending_for_tool(
                session_id=session_id,
                tool_use_id=tool_use_id,
                reason="permission_response_failed",
            )
            # Edit the Telegram message if we have the message info
            if record and record.telegram_chat_id and record.telegram_message_id:
                from app.bot.handlers.callback_utils import build_approval_message

                approval_text = "⚠️ 响应失败（超时或连接断开）"
                try:
                    original_text = record.telegram_message_text or ""
                    new_text = build_approval_message(original_text, approval_text)
                    await self.message_sender.edit_message(
                        chat_id=record.telegram_chat_id,
                        message_id=record.telegram_message_id,
                        text=new_text,
                        parse_mode="HTML",
                    )
                    logger.info(
                        "telegram message updated for permission failure",
                        extra={"session_id": session_id, "tool_use_id": tool_use_id},
                    )
                except Exception:
                    logger.warning(
                        "failed to edit Telegram message for permission failure",
                        extra={"session_id": session_id, "tool_use_id": tool_use_id},
                        exc_info=True,
                    )

    async def _handle_permission_resolved(self, session_id: str, tool_use_id: str, reason: str) -> None:
        """Handle permission resolved in terminal (not via Telegram)."""
        logger.info(
            "permission resolved in terminal session_id=%s tool_use_id=%s reason=%s",
            session_id,
            tool_use_id,
            reason,
        )
        is_approved = reason == "terminal_approved"
        # Dispatch permission event to update session state
        await self._dispatch_session_event(  # type: ignore[attr-defined]
            SessionEvent(
                session_id=session_id,
                type=SessionEventType.PERMISSION_APPROVED if is_approved else SessionEventType.PERMISSION_DENIED,
                payload={"tool_use_id": tool_use_id, "source": "terminal"},
            )
        )
        # Update permission callback registry and edit Telegram message
        if hasattr(self, "permission_callback_registry"):
            # Get the record before invalidating to preserve message info
            record = await self.permission_callback_registry.get_record_by_tool_use_id(session_id, tool_use_id)
            if record is None:
                logger.warning(
                    "permission record not found for terminal resolution session_id=%s tool_use_id=%s reason=%s",
                    session_id,
                    tool_use_id,
                    reason,
                )
            else:
                logger.info(
                    "permission record found for terminal resolution session_id=%s tool_use_id=%s reason=%s has_telegram_message=%s record_status=%s record_decision=%s chat_id=%s message_id=%s",
                    session_id,
                    tool_use_id,
                    reason,
                    bool(record.telegram_chat_id and record.telegram_message_id),
                    record.status,
                    record.decision,
                    record.telegram_chat_id,
                    record.telegram_message_id,
                )
            await self.permission_callback_registry.invalidate_pending_for_tool(
                session_id=session_id,
                tool_use_id=tool_use_id,
                reason=reason,
            )
            # Edit the Telegram message if we have the message info
            if record and record.telegram_chat_id and record.telegram_message_id:
                from app.bot.handlers.callback_utils import build_approval_message

                approval_text = "✅ 已在终端批准" if is_approved else "❌ 已在终端拒绝"
                try:
                    original_text = record.telegram_message_text or ""
                    new_text = build_approval_message(original_text, approval_text)
                    await self.message_sender.edit_message(
                        chat_id=record.telegram_chat_id,
                        message_id=record.telegram_message_id,
                        text=new_text,
                        parse_mode="HTML",
                    )
                    logger.info(
                        "telegram message updated for terminal resolution session_id=%s tool_use_id=%s approval_text=%s chat_id=%s message_id=%s",
                        session_id,
                        tool_use_id,
                        approval_text,
                        record.telegram_chat_id,
                        record.telegram_message_id,
                    )
                except Exception:
                    logger.warning(
                        "failed to edit Telegram message for terminal resolution",
                        extra={"session_id": session_id, "tool_use_id": tool_use_id},
                        exc_info=True,
                    )

    async def _bind_hook_session(self, event: HookEvent) -> None:
        if not event.session_id:
            return
        matched = await self._match_session_context(event)  # type: ignore[attr-defined]
        logger.info(
            "hook session match result",
            extra={
                "hook_session_id": event.session_id,
                "hook_event": event.event,
                "hook_status": event.status,
                "hook_cwd": event.cwd,
                "matched_user_id": matched.user_id if matched is not None else None,
                "matched_workdir": matched.workdir if matched is not None else None,
                "matched_terminal_id": matched.terminal_id if matched is not None else None,
                "matched_claude_session_id": matched.claude_session_id if matched is not None else None,
            },
        )
        if matched is None:
            return
        workdir = event.cwd or matched.workdir
        await self.task_service.bind_claude_session(
            user_id=matched.user_id,
            claude_session_id=event.session_id,
            workdir=workdir,
        )
        state = self.structured_session_store.get_or_create(
            session_id=event.session_id,
            provider="claude_code",
            workdir=workdir,
            terminal_id=matched.terminal_id,
            user_id=matched.user_id,
            claude_session_id=event.session_id,
        )
        state.terminal_id = matched.terminal_id
        state.user_id = matched.user_id
        state.workdir = workdir
        state.claude_session_id = event.session_id
        self.structured_session_store.save(state)


class SessionMatchingMixin(AppContainerBase):
    """Session matching: bind hook events to user sessions."""

    async def _match_session_context(self, event: HookEvent) -> SessionContext | None:
        # O(1) index lookup by claude_session_id (most common match path)
        indexed = await self.session_service.lookup_by_claude_session_id(event.session_id)
        if indexed is not None:
            logger.info(
                "matched hook session by claude_session_id (index)",
                extra={
                    "hook_session_id": event.session_id,
                    "user_id": indexed.user_id,
                    "workdir": indexed.workdir,
                    "terminal_id": indexed.terminal_id,
                },
            )
            return indexed

        # Index miss — fall back to full-scan matching logic
        sessions = await self.session_service.list_all()
        logger.info(
            "matching hook session context (fallback)",
            extra={
                "hook_session_id": event.session_id,
                "hook_cwd": event.cwd,
                "session_count": len(sessions),
            },
        )

        state = self.structured_session_store.get(event.session_id)
        if state is not None:
            for session in sessions:
                if session.user_id != state.user_id:
                    continue
                if session.terminal_id and state.terminal_id and session.terminal_id == state.terminal_id:
                    logger.info(
                        "matched hook session by terminal_id",
                        extra={
                            "hook_session_id": event.session_id,
                            "user_id": session.user_id,
                            "terminal_id": session.terminal_id,
                        },
                    )
                    return session

        event_workdir = str(Path(event.cwd).resolve()) if event.cwd else None
        eligible_sessions: list[SessionContext] = []
        workdir_matches: list[SessionContext] = []
        for session in sessions:
            session_workdir = str(Path(session.workdir).resolve()) if session.workdir else None
            logger.info(
                "evaluating hook session candidate",
                extra={
                    "hook_session_id": event.session_id,
                    "user_id": session.user_id,
                    "provider": session.provider,
                    "claude_chat_active": session.claude_chat_active,
                    "session_workdir": session.workdir,
                    "resolved_session_workdir": session_workdir,
                    "resolved_event_workdir": event_workdir,
                    "session_claude_session_id": session.claude_session_id,
                    "session_terminal_id": session.terminal_id,
                },
            )
            if session.provider != "claude_code" or not session.claude_chat_active:
                continue
            eligible_sessions.append(session)
            if event_workdir and session_workdir == event_workdir:
                workdir_matches.append(session)

        if len(workdir_matches) == 1:
            session = workdir_matches[0]
            has_active_task = await self._has_active_interactive_task(user_id=session.user_id, workdir=session.workdir)
            if has_active_task:
                logger.info(
                    "matched hook session by active interactive task",
                    extra={
                        "hook_session_id": event.session_id,
                        "user_id": session.user_id,
                        "terminal_id": session.terminal_id,
                        "resolved_event_workdir": event_workdir,
                    },
                )
                return session
            can_bind_chat, bind_reason, terminal_state = self._can_bind_unique_workdir_claude_chat(
                session=session,
                resolved_event_workdir=event_workdir,
            )
            if can_bind_chat:
                logger.info(
                    "matched hook session by active claude chat",
                    extra={
                        "hook_session_id": event.session_id,
                        "user_id": session.user_id,
                        "terminal_id": session.terminal_id,
                        "resolved_event_workdir": event_workdir,
                        "terminal_state_id": terminal_state.session_id if terminal_state is not None else None,
                        "terminal_state_phase": terminal_state.phase.value if terminal_state is not None else None,
                        "reason": bind_reason,
                    },
                )
                return session
            logger.warning(
                "failed to match hook session context",
                extra={
                    "hook_session_id": event.session_id,
                    "hook_cwd": event.cwd,
                    "reason": "workdir_only_match_blocked",
                    "candidate_user_ids": [session.user_id],
                    "resolved_event_workdir": event_workdir,
                    "terminal_id": session.terminal_id,
                    "has_active_interactive_task": has_active_task,
                    "claude_chat_bind_reason": bind_reason,
                    "terminal_state_id": terminal_state.session_id if terminal_state is not None else None,
                    "terminal_state_phase": terminal_state.phase.value if terminal_state is not None else None,
                },
            )
            return None

        if len(workdir_matches) > 1:
            logger.warning(
                "failed to match hook session context",
                extra={
                    "hook_session_id": event.session_id,
                    "hook_cwd": event.cwd,
                    "reason": "ambiguous_workdir",
                    "candidate_user_ids": [session.user_id for session in workdir_matches],
                },
            )
            return None

        logger.warning(
            "failed to match hook session context",
            extra={
                "hook_session_id": event.session_id,
                "hook_cwd": event.cwd,
                "reason": "no_match",
                "eligible_user_ids": [session.user_id for session in eligible_sessions],
            },
        )
        return None

    async def _has_active_interactive_task(self, *, user_id: int, workdir: str) -> bool:
        tasks = await self.task_store.iter_all()
        for task in tasks:
            if task.user_id != user_id:
                continue
            if task.provider != "claude_code":
                continue
            if task.workdir != workdir:
                continue
            if task.status in {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.TIMEOUT, TaskStatus.CANCELED}:
                continue
            return True
        return False

    def _can_bind_unique_workdir_claude_chat(
        self,
        *,
        session: SessionContext,
        resolved_event_workdir: str | None,
    ) -> tuple[bool, str, SessionState | None]:
        if session.provider != "claude_code" or not session.claude_chat_active:
            return False, "inactive_claude_chat", None
        if not session.terminal_mode or not session.terminal_id:
            return False, "terminal_not_ready", None

        terminal_state = self.structured_session_store.find_by_terminal_id(session.terminal_id)
        if terminal_state is None:
            return True, "terminal_missing_state", None

        if terminal_state.user_id is not None and terminal_state.user_id != session.user_id:
            return False, "terminal_user_mismatch", terminal_state

        terminal_workdir = str(Path(terminal_state.workdir).resolve()) if terminal_state.workdir else None
        if resolved_event_workdir and terminal_workdir and terminal_workdir != resolved_event_workdir:
            return False, "terminal_workdir_mismatch", terminal_state

        has_content = bool(terminal_state.turns or terminal_state.tool_calls or terminal_state.pending_permission is not None)
        if has_content:
            return True, "terminal_has_content", terminal_state

        if terminal_state.phase in {SessionPhase.IDLE, SessionPhase.WAITING_FOR_INPUT}:
            return True, "terminal_waiting", terminal_state

        return False, "terminal_empty_not_waiting", terminal_state


class WatcherMixin(AppContainerBase):
    """Session watcher management (unified interrupt + file + JSONL sync)."""

    def _start_session_watchers(self) -> None:
        """Start session supervisor watchers for all claude_code sessions."""
        sessions = self.structured_session_store.values()
        for state in sessions:
            if state.provider != "claude_code":
                continue
            self.session_supervisor.watch(session_id=state.session_id, workdir=state.workdir)

    def _start_session_watchers_by_session_id(self, session_id: str, workdir: str) -> None:
        """Start session supervisor watcher for a specific session."""
        self.session_supervisor.watch(session_id=session_id, workdir=workdir)


class PeriodicRecheckMixin(AppContainerBase):
    """Periodic recheck of active Claude sessions."""

    async def _periodic_recheck_loop(self) -> None:
        interval_sec = self.settings.claude_periodic_recheck_ms / 1000
        try:
            while True:
                await asyncio.sleep(interval_sec)
                await self._recheck_active_claude_sessions()
        except asyncio.CancelledError:
            raise

    async def _recheck_active_claude_sessions(self) -> None:
        sessions = await self.session_service.list_all()
        for session in sessions:
            if session.provider != "claude_code" or not session.claude_chat_active:
                continue
            if not session.claude_session_id:
                continue
            state = self.structured_session_store.get(session.claude_session_id)
            if state is None:
                continue
            if state.phase not in {SessionPhase.PROCESSING, SessionPhase.WAITING_FOR_APPROVAL}:
                continue
            logger.info(
                "periodic recheck syncing",
                extra={
                    "user_id": session.user_id,
                    "claude_session_id": session.claude_session_id,
                    "phase": state.phase.value,
                    "workdir": session.workdir,
                },
            )
            await self.sync_claude_session(session.claude_session_id, session.workdir)  # type: ignore[attr-defined]


class SessionRestoreMixin(AppContainerBase):
    """Session restoration on startup."""

    async def _restore_session_bindings(self) -> None:
        sessions = await self.session_service.list_all()
        for session in sessions:
            claude_session_id = session.claude_session_id
            if not claude_session_id:
                continue
            state = self.structured_session_store.get_or_create(
                session_id=claude_session_id,
                provider="claude_code",
                workdir=session.workdir,
                terminal_id=session.terminal_id,
                user_id=session.user_id,
                claude_session_id=claude_session_id,
            )
            if state.turns or state.tool_calls or state.pending_permission is not None:
                self.session_supervisor.watch(session_id=state.session_id, workdir=state.workdir)
                continue
            session_file = self.claude_jsonl_parser.session_file_path(session_id=claude_session_id, cwd=session.workdir)
            if session_file.exists():
                await self.sync_claude_session(claude_session_id, session.workdir)  # type: ignore[attr-defined]
                self.session_supervisor.watch(session_id=state.session_id, workdir=state.workdir)
                continue
            terminal_state = self.structured_session_store.find_by_terminal_id(session.terminal_id) if session.terminal_id else None
            if (
                terminal_state is not None
                and terminal_state.phase in {SessionPhase.PROCESSING, SessionPhase.WAITING_FOR_APPROVAL}
                and (terminal_state.turns or terminal_state.tool_calls or terminal_state.pending_permission is not None)
            ):
                self.session_supervisor.watch(session_id=terminal_state.session_id, workdir=terminal_state.workdir)
                continue
            await self.session_service.clear_claude_session(user_id=session.user_id)


class EventDispatchMixin(AppContainerBase):
    """Session event dispatch with per-session locking."""

    async def _dispatch_session_event(self, event: SessionEvent) -> None:
        async with self._session_event_locks.lock(event.session_id):
            self.structured_session_store.get_or_create(
                session_id=event.session_id,
                provider="claude_code",
                workdir=str(event.payload.get("cwd", ".")),
                claude_session_id=event.session_id,
            )
            self.structured_session_store.process(event)
        if event.type == SessionEventType.SESSION_ENDED:
            await self._session_event_locks.cleanup_key(event.session_id, require_expired=False)
