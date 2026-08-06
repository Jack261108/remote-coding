"""External Ghostty session input service.

The single orchestration point for everything described in
``docs/specs/2026-08-03-external-ghostty-input-design.md`` §4-9: pairing a
bound external Claude session to a Ghostty terminal surface, activating an
input target from ``/list``, and sending ordinary Telegram text (or an
unregistered slash command) into the verified Claude TUI — or enqueuing it
while Claude is busy and draining later.

Why a service: handlers must NOT touch ``GhosttyTerminalAdapter``,
``LocalProcessProbe``, ``ExternalBindingStore`` internals, the pairing
registry, or ``osascript`` directly. They route through this service, which
owns the per-session input lock, the in-flight marker, the drain task and the
security-ordered checks (owner → binding generation → process/TTY → terminal).

Concurrency invariants (design §6 / §9):
  * Per-session sending is serialised by ``_input_locks`` (a
    ``RefCountedLockRegistry``), independent of the external reply-delivery
    and session-event locks.
  * An in-flight marker (``_in_flight``) — NOT ``SessionState.phase`` — guards
    against a rapid double-send landing between our injection and the next
    Hook ``Stop``/``TurnStarted`` event updating phase.
  * Draining reuses the same per-session lock, so drain and immediate send
    never race within one session.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import StrEnum

from app.adapters.process.ghostty_terminal_adapter import (
    GhosttyTerminal,
    GhosttyTerminalAdapter,
    InjectionOutcome,
)
from app.domain.external_session_models import ExternalBinding, GhosttyInputTarget
from app.domain.models import utc_now
from app.domain.session_models import SessionPhase
from app.infra.lock_registry import RefCountedLockRegistry
from app.services.external_binding_store import ExternalBindingStore
from app.services.external_input_mode_state import ExternalInputTargetStore
from app.services.external_input_queue import (
    ExternalInputQueue,
    QueuedInput,
    QueueEnqueueOverflow,
)
from app.services.local_process_probe import LocalProcessProbe
from app.services.pairing_callback_registry import (
    PairConsumeOk,
    PairConsumeResult,
    PairConsumeUnauthorized,
    PairingCallbackRegistry,
)
from app.services.session_store import SessionStore

logger = logging.getLogger(__name__)


class SendOutcome(StrEnum):
    """User-visible result of a send_text attempt."""

    SENT = "sent"
    QUEUED = "queued"
    NOT_PAIRED = "not_paired"
    NO_TARGET = "no_target"  # user has not selected an input session
    NOT_OWNER = "not_owner"
    BINDING_STALE = "binding_stale"  # selected binding_id no longer matches live
    SESSION_ENDED = "session_ended"
    PROCESS_INVALID = "process_invalid"
    TERMINAL_INVALID = "terminal_invalid"
    ADAPTER_UNAVAILABLE = "adapter_unavailable"
    QUEUE_FULL = "queue_full"
    INJECTION_FAILED = "injection_failed"
    INJECTION_INDETERMINATE = "injection_indeterminate"


class PairOutcome(StrEnum):
    """User-visible result of pairing/activating."""

    NEEDS_PAIRING = "needs_pairing"  # no target yet; handler should show candidates
    PAIRED = "paired"
    ACTIVATED = "activated"  # had a valid target; set as current input session
    NOT_OWNER = "not_owner"
    BINDING_STALE = "binding_stale"
    SESSION_ENDED = "session_ended"
    PROCESS_INVALID = "process_invalid"
    TERMINAL_INVALID = "terminal_invalid"
    ADAPTER_UNAVAILABLE = "adapter_unavailable"
    NO_TERMINALS = "no_terminals"
    TOKEN_INVALID = "token_invalid"
    TOKEN_UNAUTHORIZED = "token_unauthorized"
    PAIRING_NOT_ENABLED = "pairing_not_enabled"


class _DrainStep(StrEnum):
    INJECTED = "injected"
    WAIT = "wait"
    EMPTY = "empty"
    ABORT = "abort"


# Phases in which direct injection is allowed (design §7). Any other phase
# queues instead. AskUserQuestion and pending permission gate via the state
# fields, not the phase enum.
_SENDABLE_PHASES = frozenset({SessionPhase.IDLE, SessionPhase.WAITING_FOR_INPUT})


@dataclass(frozen=True, slots=True)
class PairingCandidates:
    """Read-only candidate list for the pairing UI."""

    binding_id: str
    paired_tty: str  # the trust anchor resolved from the Claude PID
    terminals: list[GhosttyTerminal]


@dataclass(frozen=True, slots=True)
class _DrainSlot:
    """Per-session long-lived drain task handle."""

    session_id: str
    task: asyncio.Task[None]
    wake: asyncio.Event


class ExternalSessionInputService:
    """Pairing + activation + send/drain for external Ghostty input.

    Construct with a feature flag ``enabled`` (mirrors
    ``Settings.GHOSTTY_INPUT_ENABLED``); when False, every method short-circuits
    to ``PairingNotEnabled`` / ``SendOutcome.ADAPTER_UNAVAILABLE`` so the rest of
    the binding/reply system keeps working.
    """

    def __init__(
        self,
        *,
        enabled: bool,
        binding_store: ExternalBindingStore,
        session_store: SessionStore,
        ghostty_adapter: GhosttyTerminalAdapter,
        process_probe: LocalProcessProbe,
        pairing_registry: PairingCallbackRegistry,
        input_mode_store: ExternalInputTargetStore,
        input_queue: ExternalInputQueue,
        input_locks: RefCountedLockRegistry,
        drain_publish_wait_timeout_sec: float = 30.0,
    ) -> None:
        self._enabled = enabled
        self._binding_store = binding_store
        self._session_store = session_store
        self._adapter = ghostty_adapter
        self._probe = process_probe
        self._pairing = pairing_registry
        self._mode_store = input_mode_store
        self._queue = input_queue
        self._input_locks = input_locks
        self._drain_wait_timeout = drain_publish_wait_timeout_sec
        self._injecting: set[str] = set()
        self._in_flight: set[str] = set()
        self._drain_slots: dict[str, _DrainSlot] = {}
        self._lifecycle_lock = asyncio.Lock()

    # ─── public: pairing ────────────────────────────────────────────

    async def pair_candidates(
        self,
        *,
        user_id: int,
        session_id: str,
    ) -> tuple[PairOutcome, PairingCandidates | None]:
        """Resolve the pairing trust anchor and enumerate Ghostty terminals.

        Returns ``(PairingCandidates)`` for display, or a refusal outcome.
        Caller (handler) then registers a token per chosen terminal and sends
        inline buttons.
        """
        if not self._enabled:
            return PairOutcome.PAIRING_NOT_ENABLED, None
        binding = self._binding_store.get_binding(session_id)
        if binding is None or binding.user_id != user_id:
            return PairOutcome.NOT_OWNER, None
        if binding.ended_at is not None:
            return PairOutcome.SESSION_ENDED, None
        if not self._adapter.is_available():
            return PairOutcome.ADAPTER_UNAVAILABLE, None

        paired_tty = self._resolve_paired_tty(binding)
        if paired_tty is None:
            return PairOutcome.PROCESS_INVALID, None
        process = self._probe.validate_claude_foreground(
            pid=binding.pid or 0,
            paired_tty=paired_tty,
        )
        if not process.ok:
            return PairOutcome.PROCESS_INVALID, None

        terminals, _err = await self._adapter.list_terminals()
        if terminals is None:
            # Ghostty down / TCC / disabled / not on darwin: pairing is
            # currently impossible, but the external binding remains usable.
            return PairOutcome.ADAPTER_UNAVAILABLE, None
        if not terminals:
            return PairOutcome.NO_TERMINALS, None

        # Ghostty 1.3 does not expose terminal PID/TTY, so cwd/title are display
        # hints only and MUST NOT auto-select a target. Put exact-cwd candidates
        # first while preserving adapter order within each group; the handler
        # shows enough identity for the owner to choose explicitly.
        terminals = sorted(terminals, key=lambda terminal: terminal.cwd != binding.cwd)
        return (
            PairOutcome.NEEDS_PAIRING,
            PairingCandidates(
                binding_id=binding.binding_id,
                paired_tty=paired_tty,
                terminals=terminals,
            ),
        )

    async def register_pair_token(
        self,
        *,
        user_id: int,
        session_id: str,
        expected_binding_id: str,
        terminal_id: str,
    ) -> str | None:
        """Issue a short-lived token for one displayed terminal candidate.

        ``expected_binding_id`` comes from ``PairingCandidates``. If the
        binding was removed/recreated between listing and token registration,
        registration fails instead of anchoring an old UI to the new binding.
        """
        if not self._enabled:
            return None
        binding = self._binding_store.get_binding(session_id)
        if binding is None or binding.user_id != user_id or binding.ended_at is not None or binding.binding_id != expected_binding_id:
            return None
        return await self._pairing.register_token(
            user_id=user_id,
            session_id=session_id,
            binding_id=binding.binding_id,
            terminal_id=terminal_id,
        )

    async def consume_pair_token(
        self,
        *,
        token: str,
        user_id: int,
    ) -> PairOutcome:
        """Consume a callback token and finalise pairing.

        Order: token resolve → owner match (registry-enforced) → binding still
        owner & not ended & same generation → terminal UUID still unique →
        process foreground check → persist target → set input mode. Every
        failure is fail-closed.
        """
        if not self._enabled:
            return PairOutcome.PAIRING_NOT_ENABLED
        if not self._adapter.is_available():
            return PairOutcome.ADAPTER_UNAVAILABLE
        result: PairConsumeResult = await self._pairing.consume(token, user_id)
        if not isinstance(result, PairConsumeOk):
            if isinstance(result, PairConsumeUnauthorized):
                return PairOutcome.TOKEN_UNAUTHORIZED
            return PairOutcome.TOKEN_INVALID
        snap = result.snapshot
        binding = self._binding_store.get_binding(snap.session_id)
        if binding is None or binding.user_id != user_id:
            return PairOutcome.NOT_OWNER
        if binding.ended_at is not None:
            return PairOutcome.SESSION_ENDED
        if binding.binding_id != snap.binding_id:
            # ABA: token was issued under a generation that no longer matches.
            return PairOutcome.BINDING_STALE

        ok, terminal, terminal_error = await self._adapter.validate_terminal(snap.terminal_id)
        if not ok or terminal is None:
            return (
                PairOutcome.TERMINAL_INVALID
                if terminal_error in {InjectionOutcome.NOT_FOUND, InjectionOutcome.NOT_UNIQUE}
                else PairOutcome.ADAPTER_UNAVAILABLE
            )

        paired_tty = self._resolve_paired_tty(binding)
        if paired_tty is None:
            return PairOutcome.PROCESS_INVALID
        process = self._probe.validate_claude_foreground(
            pid=binding.pid or 0,
            paired_tty=paired_tty,
        )
        if not process.ok:
            return PairOutcome.PROCESS_INVALID

        saved = self._binding_store.set_ghostty_target(
            snap.session_id,
            binding.binding_id,
            terminal_id=snap.terminal_id,
            paired_tty=paired_tty,
            paired_at=utc_now(),
            name=terminal.name,
            cwd=terminal.cwd,
        )
        if not saved:
            return PairOutcome.BINDING_STALE
        activated = await self._activate_target(
            user_id=user_id,
            session_id=snap.session_id,
            binding_id=binding.binding_id,
        )
        return PairOutcome.PAIRED if activated else PairOutcome.BINDING_STALE

    # ─── public: activation ──────────────────────────────────────────

    async def activate_select(
        self,
        *,
        user_id: int,
        session_id: str,
    ) -> PairOutcome:
        """Activated from ``/list`` for an already-bound session.

        If a valid persisted Ghostty target exists, sets it as the user's
        current input session and returns ``ACTIVATED``. If no target or the
        target/process no longer validating, returns ``NEEDS_PAIRING`` so the
        handler kicks off pairing. Returns ``NOT_OWNER`` for foreign bindings
        and ``SESSION_ENDED`` for ended ones.
        """
        if not self._enabled:
            return PairOutcome.PAIRING_NOT_ENABLED
        binding = self._binding_store.get_binding(session_id)
        if binding is None or binding.user_id != user_id:
            return PairOutcome.NOT_OWNER
        if binding.ended_at is not None:
            return PairOutcome.SESSION_ENDED
        target = binding.ghostty_target
        if target is None:
            return PairOutcome.NEEDS_PAIRING
        if target.binding_id != binding.binding_id:
            self._binding_store.clear_ghostty_target(session_id, binding.binding_id)
            return PairOutcome.BINDING_STALE
        if not self._adapter.is_available():
            return PairOutcome.ADAPTER_UNAVAILABLE

        ok, _terminal, terminal_error = await self._adapter.validate_terminal(target.terminal_id)
        if not ok:
            if terminal_error in {InjectionOutcome.NOT_FOUND, InjectionOutcome.NOT_UNIQUE}:
                self._binding_store.clear_ghostty_target(session_id, binding.binding_id)
                return PairOutcome.NEEDS_PAIRING
            return PairOutcome.ADAPTER_UNAVAILABLE

        process = self._probe.validate_claude_foreground(
            pid=binding.pid or 0,
            paired_tty=target.paired_tty,
        )
        if not process.ok:
            # A process/TTY mismatch can be transient (e.g. a foreground tool),
            # so retain the persisted pairing but do not enter input mode.
            return PairOutcome.PROCESS_INVALID
        activated = await self._activate_target(
            user_id=user_id,
            session_id=session_id,
            binding_id=binding.binding_id,
        )
        return PairOutcome.ACTIVATED if activated else PairOutcome.BINDING_STALE

    async def has_target(self, user_id: int) -> bool:
        """Whether this user currently has an active external input target (no side effects)."""
        return await self._mode_store.get_target(user_id) is not None

    async def leave(self, *, user_id: int) -> bool:
        """Exit external input mode and discard queued input for that target."""
        target = await self._mode_store.get_target(user_id)
        if target is None:
            return False
        async with self._input_locks.lock(target.session_id):
            current = await self._mode_store.get_target(user_id)
            if current is None or current.session_id != target.session_id:
                return False
            await self._mode_store.clear_target(user_id)
            await self._queue.clear(target.session_id)
            self._injecting.discard(target.session_id)
            self._in_flight.discard(target.session_id)
            await self._stop_drain(target.session_id)
        return True

    # ─── public: send / drain ────────────────────────────────────────

    async def send_text(
        self,
        *,
        user_id: int,
        text: str,
    ) -> SendOutcome:
        """Send ordinary Telegram text to the user's active external session.

        CRLF/CR normalised to LF (design §8). On a non-sendable phase, enqueues
        (§9); on a target/process failure, refuses and does not enqueue.
        ``INDETERMINATE`` is never retried and is not enqueued.
        """
        if not self._enabled:
            return SendOutcome.ADAPTER_UNAVAILABLE
        payload = _normalise_text(text)
        if payload == "":
            # Empty after normalisation: nothing meaningful to inject.
            return SendOutcome.SENT

        target = await self._mode_store.get_target(user_id)
        if target is None:
            return SendOutcome.NO_TARGET
        session_id = target.session_id

        async with self._input_locks.lock(session_id):
            binding = self._binding_store.get_binding(session_id)
            if binding is None or binding.user_id != user_id:
                await self._mode_store.clear_target(user_id)
                return SendOutcome.NOT_OWNER
            state = self._session_store.get(session_id)
            if binding.ended_at is not None or (state is not None and state.phase is SessionPhase.ENDED):
                await self._mode_store.clear_target_for_session(session_id)
                await self._queue.clear(session_id)
                return SendOutcome.SESSION_ENDED
            if binding.binding_id != target.binding_id:
                # Stale selection from before a rebind.
                await self._mode_store.clear_target(user_id)
                return SendOutcome.BINDING_STALE
            ghostty_target = binding.ghostty_target
            if ghostty_target is None:
                await self._mode_store.clear_target(user_id)
                return SendOutcome.NOT_PAIRED
            if ghostty_target.binding_id != binding.binding_id:
                self._binding_store.clear_ghostty_target(session_id, binding.binding_id)
                await self._mode_store.clear_target(user_id)
                return SendOutcome.BINDING_STALE
            if not self._adapter.is_available():
                return SendOutcome.ADAPTER_UNAVAILABLE

            process = self._probe.validate_claude_foreground(
                pid=binding.pid or 0,
                paired_tty=ghostty_target.paired_tty,
            )
            if not process.ok:
                return SendOutcome.PROCESS_INVALID

            ok, _terminal, terminal_error = await self._adapter.validate_terminal(ghostty_target.terminal_id)
            if not ok:
                if terminal_error in {InjectionOutcome.NOT_FOUND, InjectionOutcome.NOT_UNIQUE}:
                    self._binding_store.clear_ghostty_target(session_id, binding.binding_id)
                    await self._mode_store.clear_target_for_session(session_id)
                    await self._queue.clear(session_id)
                    return SendOutcome.TERMINAL_INVALID
                return SendOutcome.ADAPTER_UNAVAILABLE

            current = self._current_binding_target(
                session_id=session_id,
                user_id=user_id,
                expected_binding_id=binding.binding_id,
                expected_target=ghostty_target,
            )
            if current is None:
                return SendOutcome.BINDING_STALE
            binding, ghostty_target = current

            if session_id in self._in_flight:
                # A previous send is mid-flight (between our inject and the
                # next Hook phase update); do not pile on — enqueue instead.
                return await self._enqueue(session_id, binding.binding_id, payload)

            sendable = self._is_sendable(session_id)
            if not sendable:
                return await self._enqueue(session_id, binding.binding_id, payload)
            queue_pending = await self._queue.has_pending(
                session_id,
                binding_id=binding.binding_id,
            )
            current = self._current_binding_target(
                session_id=session_id,
                user_id=user_id,
                expected_binding_id=binding.binding_id,
                expected_target=ghostty_target,
            )
            if current is None:
                return SendOutcome.BINDING_STALE
            binding, ghostty_target = current
            if queue_pending:
                # Preserve FIFO even if a new message wins the input lock just
                # as the session becomes ready before the existing drain does.
                return await self._enqueue(session_id, binding.binding_id, payload)

            # Terminal validation above can block on AppleScript for seconds.
            # Re-check the process trust anchor immediately before injection so
            # a shell that took over during that await never receives text+Enter.
            process = self._probe.validate_claude_foreground(
                pid=binding.pid or 0,
                paired_tty=ghostty_target.paired_tty,
            )
            if not process.ok:
                return SendOutcome.PROCESS_INVALID

            self._injecting.add(session_id)
            try:
                outcome = await self._adapter.inject_text(ghostty_target.terminal_id, payload)
            except Exception:
                logger.exception("ghostty inject raised", extra={"session_id": session_id})
                self._injecting.discard(session_id)
                return SendOutcome.INJECTION_FAILED
            self._injecting.discard(session_id)
            if outcome == InjectionOutcome.OK:
                # Mark in-flight only after AppleScript completed. A stale Stop
                # arriving during injection is ignored by notify_hook_event.
                self._in_flight.add(session_id)
                return SendOutcome.SENT
            if outcome == InjectionOutcome.INDETERMINATE:
                return SendOutcome.INJECTION_INDETERMINATE
            if outcome == InjectionOutcome.GHOSTTY_NOT_RUNNING or outcome in {
                "ghostty_not_running",
                "applescript_disabled",
                "non_darwin",
                "osascript_missing",
                InjectionOutcome.TCC_DENIED,
            }:
                return SendOutcome.ADAPTER_UNAVAILABLE
            return SendOutcome.INJECTION_FAILED

    async def notify_hook_event(
        self,
        *,
        session_id: str,
        event_kind: str,
    ) -> None:
        """Called by the Hook pipeline on phase-affecting events.

        ``event_kind`` is the Claude hook event name (``Stop``, ``TurnStarted``,
        ``SessionEnd``, etc.) — we do not depend on exact strings beyond the
        ones that should clear in-flight / schedule a drain.
        """
        if not self._enabled:
            return
        kind = event_kind.strip().lower().replace("-", "_")
        if kind in {
            "stop",
            "turn_completed",
            "permission_approved",
            "permission_denied",
            "permission_resolved",
            "permissionresolved",
            "user_question_resolved",
            "userquestionresolved",
        }:
            if session_id in self._injecting:
                # A completion from an older turn can race with the AppleScript
                # call for a new turn. It must not release the new turn's guard.
                return
            self._in_flight.discard(session_id)
            await self._schedule_drain(session_id)
        elif kind in {"session_end", "sessionend", "sessionended", "clear"}:
            await self.invalidate_binding(session_id, reason="session_end")

    async def invalidate_binding(self, session_id: str, *, reason: str) -> None:
        """Tear down all input state for a session (unbind / SessionEnd / reaper / target failure)."""
        if not self._enabled:
            return
        async with self._input_locks.lock(session_id):
            cleared_targets = await self._mode_store.clear_target_for_session(session_id)
            dropped = await self._queue.clear(session_id)
            await self._pairing.invalidate_session(session_id)
            self._injecting.discard(session_id)
            self._in_flight.discard(session_id)
            await self._stop_drain(session_id)
        if cleared_targets or dropped:
            logger.info(
                "external input invalidated",
                extra={
                    "session_id": session_id,
                    "reason": reason,
                    "targets_cleared": len(cleared_targets),
                    "queued_dropped": len(dropped),
                },
            )

    async def rebind_aba(self, session_id: str, new_binding_id: str) -> None:
        """Called after an unbind+rebind produced a new generation."""
        if not self._enabled:
            return
        async with self._input_locks.lock(session_id):
            await self._mode_store.invalidate_for_binding_aba(session_id, new_binding_id)
            await self._pairing.invalidate_binding(session_id, new_binding_id)
            await self._queue.clear(session_id)
            self._injecting.discard(session_id)
            self._in_flight.discard(session_id)
            await self._stop_drain(session_id)

    async def shutdown(self) -> None:
        """Cancel all drain tasks. Call on container shutdown."""
        async with self._lifecycle_lock:
            slots = list(self._drain_slots.values())
            self._drain_slots.clear()
        for slot in slots:
            slot.task.cancel()
        await asyncio.gather(*(slot.task for slot in slots), return_exceptions=True)

    # ─── internals ──────────────────────────────────────────────────

    def _current_binding_target(
        self,
        *,
        session_id: str,
        user_id: int,
        expected_binding_id: str,
        expected_target: GhosttyInputTarget,
    ) -> tuple[ExternalBinding, GhosttyInputTarget] | None:
        """Re-read binding/target after an await and enforce the ABA snapshot."""
        binding = self._binding_store.get_binding(session_id)
        if binding is None or binding.user_id != user_id or binding.ended_at is not None or binding.binding_id != expected_binding_id:
            return None
        target = binding.ghostty_target
        if target is None or target.binding_id != binding.binding_id or target != expected_target:
            return None
        return binding, target

    def _is_sendable(self, session_id: str) -> bool:
        state = self._session_store.get(session_id)
        if state is None:
            # The design only permits known idle/waiting_for_input states.
            # Unknown state is no positive proof, so fail closed and queue.
            return False
        if state.phase not in _SENDABLE_PHASES:
            return False
        if state.pending_permission is not None:
            return False
        if state.structured_user_question_key:
            return False
        return True

    async def _enqueue(self, session_id: str, binding_id: str, payload: str) -> SendOutcome:
        result = await self._queue.enqueue(session_id, text=payload, binding_id=binding_id)
        if isinstance(result, QueueEnqueueOverflow):
            return SendOutcome.QUEUE_FULL
        await self._ensure_drain(session_id)
        return SendOutcome.QUEUED

    async def _activate_target(
        self,
        *,
        user_id: int,
        session_id: str,
        binding_id: str,
    ) -> bool:
        """Switch input intent if the binding generation is still current."""
        binding = self._binding_store.get_binding(session_id)
        if binding is None or binding.user_id != user_id or binding.ended_at is not None or binding.binding_id != binding_id:
            return False

        previous = await self._mode_store.get_target(user_id)
        if previous is not None and previous.session_id != session_id:
            async with self._input_locks.lock(previous.session_id):
                current = await self._mode_store.get_target(user_id)
                if current is not None and current.session_id == previous.session_id:
                    await self._queue.clear(previous.session_id)
                    self._injecting.discard(previous.session_id)
                    self._in_flight.discard(previous.session_id)
                    await self._stop_drain(previous.session_id)
        async with self._input_locks.lock(session_id):
            binding = self._binding_store.get_binding(session_id)
            if binding is None or binding.user_id != user_id or binding.ended_at is not None or binding.binding_id != binding_id:
                return False
            await self._mode_store.set_target(
                user_id=user_id,
                session_id=session_id,
                binding_id=binding_id,
            )
        return True

    def _resolve_paired_tty(self, binding: ExternalBinding) -> str | None:
        """Resolve the trust-anchor TTY for a binding.

        Prefer the binding's already-recorded tty (carried by hooks); fall
        back to querying the controlling tty of the live pid. Never guesses.
        """
        if binding.tty:
            return binding.tty
        if not binding.pid or binding.pid <= 0:
            return None
        return self._probe.pid_controlling_tty(binding.pid)

    # --- drain task ----------------------------------------------------------

    async def _schedule_drain(self, session_id: str) -> None:
        """Wake an existing drain loop so it re-checks phase + dequeues."""
        slot = self._drain_slots.get(session_id)
        if slot is not None and not slot.task.done():
            slot.wake.set()
            return
        # No slot yet but a queue may have entries from before the slot spun up.
        if await self._queue.peek_size(session_id) > 0:
            await self._ensure_drain(session_id)

    async def _ensure_drain(self, session_id: str) -> None:
        async with self._lifecycle_lock:
            existing = self._drain_slots.get(session_id)
            if existing is not None and not existing.task.done():
                existing.wake.set()
                return
            wake = asyncio.Event()
            task = asyncio.create_task(self._drain_loop(session_id, wake))
            self._drain_slots[session_id] = _DrainSlot(session_id=session_id, task=task, wake=wake)

    async def _stop_drain(self, session_id: str) -> None:
        async with self._lifecycle_lock:
            slot = self._drain_slots.pop(session_id, None)
        if slot is not None and not slot.task.done():
            slot.wake.set()
            slot.task.cancel()
            try:
                await slot.task
            except (asyncio.CancelledError, Exception):
                pass

    async def _drain_loop(self, session_id: str, wake: asyncio.Event) -> None:
        """Drain one entry per ready turn, waiting on wake or state publish.

        A queued entry remains queued while phase/process/adapter is temporarily
        not ready. It is dequeued only after every precondition succeeds, so a
        phase flip never silently drops user input.
        """
        restart_if_queued = False
        aborted = False
        try:
            while True:
                step = await self._try_drain_one(session_id)
                if step is _DrainStep.INJECTED:
                    # _in_flight now blocks the next item until Stop/resolution.
                    continue
                if step is _DrainStep.ABORT:
                    # _try_drain_one cleared the old queue while still holding
                    # the input lock; messages arriving afterwards are new work.
                    aborted = True
                    return
                if step is _DrainStep.EMPTY:
                    restart_if_queued = True
                    return
                await self._wait_for_drain_activity(session_id, wake)
        finally:
            async with self._lifecycle_lock:
                current = self._drain_slots.get(session_id)
                if current is not None and current.task is asyncio.current_task():
                    self._drain_slots.pop(session_id, None)
            if (aborted or restart_if_queued) and await self._queue.peek_size(session_id) > 0:
                # Enqueue may have raced after the abort/empty clear while this
                # slot was still visible. The old slot is now removed, so start
                # a replacement instead of silently swallowing/stranding it.
                await self._ensure_drain(session_id)

    async def _wait_for_drain_activity(self, session_id: str, wake: asyncio.Event) -> None:
        """Wait for an explicit Hook wake or a SessionStore publish."""
        if wake.is_set():
            wake.clear()
            return
        since_cursor = self._session_store.get_publish_cursor(session_id)
        wake_task = asyncio.create_task(wake.wait())
        publish_task = asyncio.create_task(
            self._session_store.wait_for_publish(
                session_id,
                since_cursor=since_cursor,
                timeout_sec=self._drain_wait_timeout,
            )
        )
        try:
            await asyncio.wait({wake_task, publish_task}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (wake_task, publish_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(wake_task, publish_task, return_exceptions=True)
            if wake.is_set():
                wake.clear()

    async def _abort_drain(self, session_id: str) -> _DrainStep:
        """Clear old queued work while the caller still owns the input lock."""
        await self._queue.clear(session_id)
        return _DrainStep.ABORT

    async def _try_drain_one(self, session_id: str) -> _DrainStep:
        """Inject one ready queue head under the per-session input lock."""
        async with self._input_locks.lock(session_id):
            if await self._queue.peek_size(session_id) == 0:
                return _DrainStep.EMPTY
            if session_id in self._in_flight or not self._is_sendable(session_id):
                return _DrainStep.WAIT

            binding = self._binding_store.get_binding(session_id)
            if binding is None or binding.ended_at is not None:
                return await self._abort_drain(session_id)
            target = binding.ghostty_target
            if target is None or target.binding_id != binding.binding_id:
                return await self._abort_drain(session_id)

            process = self._probe.validate_claude_foreground(
                pid=binding.pid or 0,
                paired_tty=target.paired_tty,
            )
            if not process.ok:
                return _DrainStep.WAIT
            if not self._adapter.is_available():
                return _DrainStep.WAIT
            terminal_ok, _terminal, terminal_error = await self._adapter.validate_terminal(target.terminal_id)
            if not terminal_ok:
                if terminal_error in {InjectionOutcome.NOT_FOUND, InjectionOutcome.NOT_UNIQUE}:
                    self._binding_store.clear_ghostty_target(session_id, binding.binding_id)
                    await self._mode_store.clear_target_for_session(session_id)
                    return await self._abort_drain(session_id)
                return _DrainStep.WAIT

            current = self._current_binding_target(
                session_id=session_id,
                user_id=binding.user_id,
                expected_binding_id=binding.binding_id,
                expected_target=target,
            )
            if current is None:
                return await self._abort_drain(session_id)
            binding, target = current

            entry: QueuedInput | None = await self._queue.dequeue(
                session_id,
                binding_id=binding.binding_id,
            )
            if entry is None:
                return _DrainStep.EMPTY

            current = self._current_binding_target(
                session_id=session_id,
                user_id=binding.user_id,
                expected_binding_id=binding.binding_id,
                expected_target=target,
            )
            if current is None:
                # The entry belongs to the old generation/target; abort clears
                # the remaining old queue rather than injecting after rebind.
                return await self._abort_drain(session_id)
            binding, target = current

            # As in the immediate-send path, close the AppleScript validation
            # TOCTOU window by checking the process again at the last possible
            # point. Restore the FIFO head if the process changed meanwhile.
            process = self._probe.validate_claude_foreground(
                pid=binding.pid or 0,
                paired_tty=target.paired_tty,
            )
            if not process.ok:
                restored = await self._queue.prepend(session_id, entry)
                if not restored:
                    logger.warning(
                        "failed to restore queued input after process revalidation",
                        extra={"session_id": session_id},
                    )
                return _DrainStep.WAIT

            self._injecting.add(session_id)
            try:
                outcome = await self._adapter.inject_text(target.terminal_id, entry.text)
            except Exception:
                logger.exception("ghostty drain inject raised", extra={"session_id": session_id})
                self._injecting.discard(session_id)
                return await self._abort_drain(session_id)
            self._injecting.discard(session_id)
            if outcome == InjectionOutcome.OK:
                self._in_flight.add(session_id)
                return _DrainStep.INJECTED
            logger.warning(
                "ghostty drain inject failed",
                extra={"session_id": session_id, "outcome": outcome},
            )
            # The dequeued entry cannot be retried safely when the script may
            # have pasted text. Abort and clear remaining entries.
            return await self._abort_drain(session_id)


def _normalise_text(text: str) -> str:
    """Normalise CRLF/CR to LF (design §8). No shell escaping."""
    return text.replace("\r\n", "\n").replace("\r", "\n")
