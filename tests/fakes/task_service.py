"""Shared test fakes for TaskService and its supporting record/event dataclasses.

Centralizes the DummyTaskService variants that were duplicated across the
event-streaming test files (test_command_run, test_auto_export_streamer,
test_run_event_streamer, test_run_event_streamer_upload_queue,
test_run_event_streamer_diff) plus TaskRecord / CLIEvent stream factories.

The defaults mirror the "thin" variant: structured session is None, cursors are
(None, None), waits sleep ``timeout_sec`` and return True. The
structured-state-machine and per-event delay behaviour of the fuller variant are
opt-in via constructor kwargs. Subclasses may override ``create_and_run``,
``cancel`` and ``mark_stream_timeout`` to model watchdog/timeout races.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING

from app.domain.models import CLIEvent, EventType, TaskRecord, TaskStatus, utc_now
from app.domain.session_models import ConversationTurn, SessionPhase
from app.services.user_question_callback_registry import QuestionCallbackTokens

if TYPE_CHECKING:
    from app.domain.user_question_models import UserQuestionPrompt


class FakeTaskService:
    """Configurable, easily-subclassed TaskService test double.

    Behaviour is the union of the four streaming-task DummyTaskService variants.
    Defaults reproduce the "thin" variant (structured session always None,
    cursors (None, None), wait sleeps the full timeout and returns True); the
    structured state machine, per-event delays, and non-default wait outcomes
    are opt-in.
    """

    def __init__(
        self,
        events: list[CLIEvent],
        status: TaskRecord | None = None,
        *,
        interactive: bool = False,
        structured_reply: str = "",
        structured_turns: list[ConversationTurn] | None = None,
        structured_sessions: list[object | None] | None = None,
        event_delays: list[float] | None = None,
        wait_update_result: bool = True,
        wait_update_sleep: float | None = None,
        task_id: str = "t1",
        provider: str = "claude_code",
        session_id: str = "s1",
    ) -> None:
        self._events = events
        self._status = status
        self._interactive = interactive
        self._structured_reply = structured_reply
        self._structured_turns = structured_turns
        self._structured_sessions = structured_sessions
        self._structured_session_index = 0
        self._event_delays = event_delays or [0.0] * len(events)
        self._wait_update_result = wait_update_result
        self._wait_update_sleep = wait_update_sleep
        self._task_id = task_id
        self._provider = provider
        self._session_id = session_id
        self._revision = 0
        self._structured_reply_turn_id: str | None = None
        self._structured_permission_key: str | None = None
        self._structured_user_question_key: str | None = None
        self.create_calls: list[tuple[int, str | None, str, str | None]] = []

    async def create_and_run(self, *, user_id: int, provider: str | None, prompt: str, workdir: str | None = None):
        self.create_calls.append((user_id, provider, prompt, workdir))
        task = SimpleNamespace(
            task_id=self._task_id,
            provider=self._provider,
            session_id=self._session_id,
            workdir=workdir or "/tmp",
            started_at=None,
            created_at=utc_now(),
        )
        return SimpleNamespace(task=task, events=self._stream(), interactive=self._interactive)

    async def get_status(self, task_id: str, user_id: int):
        return self._status

    async def get_structured_session(self, user_id: int, *, log_missing: bool = True):
        if self._structured_sessions is not None:
            if self._structured_session_index < len(self._structured_sessions):
                session = self._structured_sessions[self._structured_session_index]
                self._structured_session_index += 1
            else:
                session = self._structured_sessions[-1]
            self._revision += 1
            return session
        if self._structured_turns is not None:
            return SimpleNamespace(
                session_id="claude-session-1",
                phase=SessionPhase.WAITING_FOR_INPUT,
                turns=self._structured_turns,
                pending_permission=None,
            )
        if not self._structured_reply:
            return None
        return SimpleNamespace(
            session_id="claude-session-1",
            phase=SessionPhase.WAITING_FOR_INPUT,
            turns=[ConversationTurn(turn_id="turn-1", role="assistant", text=self._structured_reply, is_complete=True)],
            pending_permission=None,
        )

    async def get_structured_session_for_task(self, *, task_id: str, user_id: int, log_missing: bool = True):
        return await self.get_structured_session(user_id, log_missing=log_missing)

    async def get_structured_session_cursor(self, user_id: int, *, task_id: str | None = None) -> int:
        return self._revision

    async def get_structured_reply_cursor(self, user_id: int, *, task_id: str | None = None):
        return self._structured_reply_turn_id, self._structured_permission_key

    async def acknowledge_structured_reply(
        self, user_id: int, *, turn_id: str | None = None, permission_key: str | None = None, task_id: str | None = None
    ) -> None:
        if turn_id is not None:
            self._structured_reply_turn_id = turn_id
        if permission_key is not None:
            self._structured_permission_key = permission_key

    async def get_structured_user_question_cursor(self, user_id: int, *, task_id: str | None = None):
        return self._structured_user_question_key

    async def acknowledge_structured_user_question(
        self, user_id: int, *, question_key: str | None = None, task_id: str | None = None
    ) -> None:
        self._structured_user_question_key = question_key

    async def register_question_callback_tokens(
        self,
        *,
        user_id: int,
        prompt: UserQuestionPrompt,
    ) -> QuestionCallbackTokens:
        return QuestionCallbackTokens()

    async def wait_for_structured_session_update(
        self, *, user_id: int, since_cursor: int, timeout_sec: float, task_id: str | None = None
    ) -> bool:
        await asyncio.sleep(self._wait_update_sleep if self._wait_update_sleep is not None else timeout_sec)
        return self._wait_update_result

    async def _stream(self):
        for delay, event in zip(self._event_delays, self._events, strict=False):
            if delay > 0:
                await asyncio.sleep(delay)
            yield event


def make_task_record(
    *,
    task_id: str = "t1",
    session_id: str = "s1",
    user_id: int = 1,
    provider: str = "claude_code",
    prompt: str = "hello",
    workdir: str = "/tmp",
    timeout_sec: int = 30,
    status: TaskStatus = TaskStatus.SUCCEEDED,
    output_chars: int = 0,
    output_truncated: bool = False,
    created_at=None,
    started_at=None,
    ended_at=None,
    **overrides,
) -> TaskRecord:
    """Build a TaskRecord with sensible test defaults.

    All TaskRecord fields are accepted as keyword overrides via ``**overrides``
    (e.g. exit_code, failure_reason, output_text, cancel_requested,
    claude_session_id) so callers only name what they need.
    """
    return TaskRecord(
        task_id=task_id,
        session_id=session_id,
        user_id=user_id,
        provider=provider,
        prompt=prompt,
        workdir=workdir,
        timeout_sec=timeout_sec,
        status=status,
        output_chars=output_chars,
        output_truncated=output_truncated,
        created_at=created_at if created_at is not None else utc_now(),
        started_at=started_at,
        ended_at=ended_at,
        **overrides,
    )


def make_cli_event_stream(
    *,
    task_id: str = "t1",
    started_content: str | None = None,
    exit_code: int = 0,
    failed: bool = False,
    error: str | None = None,
    with_tmux_marker: bool = False,
    user_id_marker: int | None = None,
    custom: list[CLIEvent] | None = None,
) -> list[CLIEvent]:
    """Build a typical STARTED(+EXITED|FAILED) two-event stream.

    - ``custom`` non-None: returned verbatim (escape hatch for generator-style
      per-event yields that cannot be templated).
    - ``with_tmux_marker``: STARTED ``content=f"tmux_session=tgcli_user_{n}"``.
    - ``failed``: emits FAILED(task_id, error) instead of EXITED(task_id, exit_code).
    """
    if custom is not None:
        return custom
    content = started_content
    if content is None and with_tmux_marker:
        content = f"tmux_session=tgcli_user_{user_id_marker or 1}"
    started = CLIEvent(type=EventType.STARTED, task_id=task_id, content=content)
    if failed:
        terminal = CLIEvent(type=EventType.FAILED, task_id=task_id, error=error)
    else:
        terminal = CLIEvent(type=EventType.EXITED, task_id=task_id, exit_code=exit_code)
    return [started, terminal]
