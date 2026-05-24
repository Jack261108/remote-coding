from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path

from app.config.settings import is_workdir_allowed
from app.domain.hook_models import HookEvent
from app.domain.models import SessionContext, TaskStatus
from app.domain.session_models import SessionEvent, SessionEventType, SessionPhase, SessionState
from app.domain.user_question_models import extract_user_question_prompts
from app.bootstrap_base import AppContainerBase

logger = logging.getLogger(__name__)


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
            await self._dispatch_session_event(
                SessionEvent(
                    session_id=session_id,
                    type=SessionEventType.FILE_SYNCED,
                    payload=snapshot.to_payload(),
                )
            )

    async def _stop_jsonl_sync_tasks(self) -> None:
        tasks = list(self._jsonl_sync_tasks.values())
        self._jsonl_sync_tasks.clear()
        self._jsonl_sync_requests.clear()
        await self._jsonl_sync_locks.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task

    def _schedule_jsonl_sync(self, session_id: str, cwd: str) -> None:
        self._jsonl_sync_requests[session_id] = cwd
        self._start_interrupt_watchers_by_session_id(session_id, cwd)
        self._start_agent_file_watchers_by_session_id(session_id, cwd)
        existing = self._jsonl_sync_tasks.get(session_id)
        if existing is None or existing.done():
            self._jsonl_sync_tasks[session_id] = asyncio.create_task(self._debounced_sync_claude_session(session_id))

    async def _debounced_sync_claude_session(self, session_id: str) -> None:
        current_cwd: str | None = None
        try:
            while True:
                await asyncio.sleep(self.settings.claude_jsonl_sync_debounce_ms / 1000)
                current_cwd = self._jsonl_sync_requests.pop(session_id, None)
                if current_cwd is None:
                    return
                await self.sync_claude_session(session_id, current_cwd)
                current_cwd = None
                if session_id not in self._jsonl_sync_requests:
                    return
        except asyncio.CancelledError:
            if current_cwd is not None and session_id not in self._jsonl_sync_requests:
                self._jsonl_sync_requests[session_id] = current_cwd
            raise
        except Exception:
            if current_cwd is not None and session_id not in self._jsonl_sync_requests:
                self._jsonl_sync_requests[session_id] = current_cwd
            logger.exception("debounced jsonl sync failed", extra={"session_id": session_id})
        finally:
            current = self._jsonl_sync_tasks.get(session_id)
            if current is asyncio.current_task():
                self._jsonl_sync_tasks.pop(session_id, None)
                if session_id in self._jsonl_sync_requests:
                    self._jsonl_sync_tasks[session_id] = asyncio.create_task(self._debounced_sync_claude_session(session_id))
                else:
                    await self._jsonl_sync_locks.cleanup_key(session_id)


class HookHandlingMixin(AppContainerBase):
    """Hook event handling: validate, bind session, dispatch events."""

    async def _handle_hook_event(self, event: HookEvent) -> None:
        if not is_workdir_allowed(event.cwd, self.settings.allowed_workdirs):
            logger.warning(
                "hook event rejected by workdir allowlist",
                extra={"session_id": event.session_id, "cwd": event.cwd, "event": event.event},
            )
            return
        logger.debug(
            "hook event received",
            extra={
                "session_id": event.session_id,
                "event": event.event,
                "status": event.status,
                "tool": event.tool,
            },
        )

        # Clear auto-approve state on session end
        if event.event == "SessionEnd" and hasattr(self, "auto_approve_service"):
            self.auto_approve_service.clear_session(event.session_id)

        # If ownership_resolver is not wired (e.g. in tests), fall back to old behavior
        if not hasattr(self, "ownership_resolver"):
            await self._bind_hook_session(event)
            await self._dispatch_session_event(
                SessionEvent(
                    session_id=event.session_id,
                    type=SessionEventType.HOOK_RECEIVED,
                    payload=event.to_dict(),
                )
            )
            self._schedule_jsonl_sync(event.session_id, event.cwd)
            return

        # Ownership resolver as first gate
        ownership = await self.ownership_resolver.resolve(event.session_id)
        logger.info(
            "hook event ownership resolved",
            extra={
                "session_id": event.session_id,
                "ownership_state": ownership.ownership_state,
                "origin": ownership.origin.value,
                "owner_user_id": ownership.owner_user_id,
            },
        )

        if ownership.ownership_state == "owned":
            # Tmux-launched session: existing bind + dispatch + sync flow
            # Auto-approve check: intercept before notification flow
            if event.expects_response and event.tool != "AskUserQuestion":
                if hasattr(self, "auto_approve_service") and self.auto_approve_service.is_active(event.session_id):
                    await self._handle_auto_approved_permission(event)
                    return
            await self._bind_hook_session(event)
            await self._dispatch_session_event(
                SessionEvent(
                    session_id=event.session_id,
                    type=SessionEventType.HOOK_RECEIVED,
                    payload=event.to_dict(),
                )
            )
            self._schedule_jsonl_sync(event.session_id, event.cwd)
            self._maybe_auto_file_send(event, ownership.owner_user_id)

        elif ownership.ownership_state == "bound":
            # Externally-bound session: dispatch event + schedule JSONL sync + push notifications
            # Auto-approve check: intercept before push notification
            if event.expects_response and event.tool != "AskUserQuestion":
                if hasattr(self, "auto_approve_service") and self.auto_approve_service.is_active(event.session_id):
                    await self._handle_auto_approved_permission(event)
                    return
            await self._dispatch_session_event(
                SessionEvent(
                    session_id=event.session_id,
                    type=SessionEventType.HOOK_RECEIVED,
                    payload=event.to_dict(),
                )
            )
            self._schedule_jsonl_sync(event.session_id, event.cwd)
            # Push notifications for bound external sessions (notifier may not be wired yet)
            if hasattr(self, "push_notifier") and ownership.owner_user_id is not None:
                await self._notify_bound_external_event(event, ownership.owner_user_id)
            self._maybe_auto_file_send(event, ownership.owner_user_id)

        else:
            # Unbound: record in discovery, handle permissions if needed
            if hasattr(self, "external_discovery"):
                self.external_discovery.record_event(event)
            if event.expects_response and hasattr(self, "unbound_permission_handler"):
                # AskUserQuestion: auto-allow, don't broadcast Approve/Deny
                if event.tool == "AskUserQuestion":
                    await self._auto_allow_ask_user_question(event)
                else:
                    # Auto-approve check for unbound sessions
                    if hasattr(self, "auto_approve_service") and self.auto_approve_service.is_active(event.session_id):
                        await self._handle_auto_approved_permission(event)
                        return
                    await self.unbound_permission_handler.handle_unbound_permission(event)

    def _maybe_auto_file_send(self, event: HookEvent, owner_user_id: int | None) -> None:
        if event.event == "PostToolUse" and event.tool == "Write" and owner_user_id is not None and hasattr(self, "file_sender"):
            file_path_raw = event.tool_input.get("file_path", "") if event.tool_input else ""
            task = asyncio.create_task(
                self.file_sender.send_if_eligible(
                    file_path_raw=file_path_raw,
                    cwd=event.cwd,
                    chat_id=owner_user_id,
                )
            )
            self._background_tasks.add(task)
            task.add_done_callback(self._on_background_task_done)

    def _on_background_task_done(self, task: asyncio.Task[None]) -> None:
        self._background_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.warning("background task failed", exc_info=exc)

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

                    pane_id = await find_tmux_pane_for_pid(event.pid)
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
                        )
                        self.external_uq_state.store(pending)
                        await self.push_notifier.notify_user_question(
                            user_id=user_id,
                            session_id=event.session_id,
                            prompts=prompts,
                            interactive=True,
                        )
                        return

                # Fallback: no tmux pane found or no PID — auto-allow and show read-only
                await self._auto_allow_ask_user_question(event)
                if prompts:
                    await self.push_notifier.notify_user_question(
                        user_id=user_id,
                        session_id=event.session_id,
                        prompts=prompts,
                        interactive=False,
                    )
                else:
                    # Fallback: couldn't parse structured prompts
                    short_id = event.session_id[:8]
                    question = ""
                    if event.tool_input:
                        question = event.tool_input.get("question", "")
                    text = f"❓ [{short_id}] 等待用户输入 — 请在终端中选择"
                    if question:
                        truncated = question[:150] + ("..." if len(question) > 150 else "")
                        text += f"\n{truncated}"
                    await self.push_notifier.notify_info(user_id=user_id, text=text)
                return
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
                tool_input=None,
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

    async def _auto_allow_ask_user_question(self, event: HookEvent) -> None:
        """Auto-allow AskUserQuestion permission requests — user answers in terminal."""
        tool_use_id = event.tool_use_id or ""
        if not tool_use_id:
            return
        await self.hook_socket_server.respond_to_permission(
            tool_use_id=tool_use_id,
            decision="allow",
            reason="AskUserQuestion auto-allowed",
        )

    async def _handle_auto_approved_permission(self, event: HookEvent) -> None:
        """Auto-approve a permission request and send silent notification."""
        tool_use_id = event.tool_use_id or ""
        if not tool_use_id:
            return

        # Respond with allow immediately
        await self.hook_socket_server.respond_to_permission(
            tool_use_id=tool_use_id,
            decision="allow",
            reason="auto-approved",
        )

        # Send silent notification to the user who activated auto-approve
        entry = self.auto_approve_service._sessions.get(event.session_id)
        if entry is not None:
            tool_name = event.tool or "Unknown"
            input_summary = self._format_auto_approve_input_summary(event)
            message = f"🟢 Auto-approved: {tool_name} {input_summary}".strip()
            try:
                await self.bot.send_message(chat_id=entry.user_id, text=message)
            except Exception:
                logger.warning(
                    "Failed to send auto-approve notification",
                    extra={"session_id": event.session_id, "tool_use_id": tool_use_id},
                )

        # Audit log
        logger.info(
            "permission auto-approved",
            extra={
                "session_id": event.session_id,
                "tool": event.tool,
                "tool_use_id": tool_use_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )

    def _format_auto_approve_input_summary(self, event: HookEvent) -> str:
        """Format a brief summary of tool_input for auto-approve notifications."""
        if not event.tool_input:
            return ""
        # Common patterns for tool inputs
        for key in ("file_path", "path", "command", "url", "query", "description"):
            value = event.tool_input.get(key)
            if value and isinstance(value, str):
                truncated = value[:80] + ("..." if len(value) > 80 else "")
                return truncated
        return ""

    async def _handle_permission_failure(self, session_id: str, tool_use_id: str) -> None:
        logger.warning(
            "permission response failed",
            extra={"session_id": session_id, "tool_use_id": tool_use_id},
        )
        await self._dispatch_session_event(
            SessionEvent(
                session_id=session_id,
                type=SessionEventType.PERMISSION_RESPONSE_FAILED,
                payload={"tool_use_id": tool_use_id},
            )
        )

    async def _bind_hook_session(self, event: HookEvent) -> None:
        if not event.session_id:
            return
        matched = await self._match_session_context(event)
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
        sessions = await self.session_service.list_all()
        logger.info(
            "matching hook session context",
            extra={
                "hook_session_id": event.session_id,
                "hook_cwd": event.cwd,
                "session_count": len(sessions),
            },
        )

        # O(1) lookup by claude_session_id (most common match path)
        by_claude_session_id: dict[str, SessionContext] = {}
        for session in sessions:
            if session.claude_session_id:
                by_claude_session_id[session.claude_session_id] = session

        matched = by_claude_session_id.get(event.session_id)
        if matched is not None:
            logger.info(
                "matched hook session by claude_session_id",
                extra={
                    "hook_session_id": event.session_id,
                    "user_id": matched.user_id,
                    "workdir": matched.workdir,
                    "terminal_id": matched.terminal_id,
                },
            )
            return matched

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
    """Interrupt and agent file watcher management."""

    def _start_interrupt_watchers(self) -> None:
        sessions = self.structured_session_store.values()
        for state in sessions:
            self._start_interrupt_watcher(state)

    def _start_interrupt_watchers_by_session_id(self, session_id: str, workdir: str) -> None:
        state = self.structured_session_store.get(session_id)
        if state is None:
            self.interrupt_watcher.watch(session_id=session_id, workdir=workdir)
            return
        self._start_interrupt_watcher(state)

    def _start_interrupt_watcher(self, state: SessionState) -> None:
        if state.provider != "claude_code":
            return
        if state.phase not in {SessionPhase.PROCESSING, SessionPhase.WAITING_FOR_APPROVAL}:
            return
        self.interrupt_watcher.watch(session_id=state.session_id, workdir=state.workdir)

    def _start_agent_file_watchers(self) -> None:
        sessions = self.structured_session_store.values()
        for state in sessions:
            self._start_agent_file_watcher(state)

    def _start_agent_file_watchers_by_session_id(self, session_id: str, workdir: str) -> None:
        state = self.structured_session_store.get(session_id)
        if state is None:
            self.agent_file_watcher.watch(session_id=session_id, workdir=workdir)
            return
        self._start_agent_file_watcher(state)

    def _start_agent_file_watcher(self, state: SessionState) -> None:
        if state.provider != "claude_code":
            return
        self.agent_file_watcher.watch(session_id=state.session_id, workdir=state.workdir)


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
            await self.sync_claude_session(session.claude_session_id, session.workdir)

    async def _stop_periodic_recheck_task(self) -> None:
        task = self._periodic_recheck_task
        self._periodic_recheck_task = None
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


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
                self.interrupt_watcher.watch(session_id=state.session_id, workdir=state.workdir)
                self.agent_file_watcher.watch(session_id=state.session_id, workdir=state.workdir)
                continue
            session_file = self.claude_jsonl_parser.session_file_path(session_id=claude_session_id, cwd=session.workdir)
            if session_file.exists():
                await self.sync_claude_session(claude_session_id, session.workdir)
                self.interrupt_watcher.watch(session_id=state.session_id, workdir=state.workdir)
                self.agent_file_watcher.watch(session_id=state.session_id, workdir=state.workdir)
                continue
            terminal_state = self.structured_session_store.find_by_terminal_id(session.terminal_id) if session.terminal_id else None
            if (
                terminal_state is not None
                and terminal_state.phase in {SessionPhase.PROCESSING, SessionPhase.WAITING_FOR_APPROVAL}
                and (terminal_state.turns or terminal_state.tool_calls or terminal_state.pending_permission is not None)
            ):
                self.interrupt_watcher.watch(session_id=terminal_state.session_id, workdir=terminal_state.workdir)
                self.agent_file_watcher.watch(session_id=terminal_state.session_id, workdir=terminal_state.workdir)
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
