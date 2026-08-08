"""Unit tests for ExternalSessionInputService.

Covers pairing/owner/ABA checks, send state gates, argv-safe adapter outcomes,
per-session serialisation, busy FIFO drain, and lifecycle cleanup. Real Ghostty,
TCC, PTYs and process tables are not required; fakes exercise the service
orchestration while the adapter/probe have their own focused tests.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import pytest

from app.adapters.process.ghostty_terminal_adapter import GhosttyTerminal, InjectionOutcome
from app.adapters.storage.file_session_store import FileSessionStore
from app.domain.external_session_models import ExternalBinding, GhosttyInputTarget
from app.domain.models import utc_now
from app.domain.session_models import PendingPermission, SessionPhase
from app.domain.user_question_models import (
    ExternalGhosttyQuestionTarget,
    ExternalQuestionActionStatus,
    ExternalUserQuestionContext,
    ExternalUserQuestionPhase,
    UserQuestionOption,
    UserQuestionPrompt,
)
from app.infra.lock_registry import RefCountedLockRegistry
from app.services.external_binding_store import ExternalBindingStore
from app.services.external_input_mode_state import ExternalInputTargetStore
from app.services.external_input_queue import ExternalInputQueue
from app.services.external_session_input_service import (
    ExternalSessionInputService,
    PairOutcome,
    SendOutcome,
)
from app.services.external_user_question_state import ExternalUserQuestionState, PendingExternalUserQuestion
from app.services.pairing_callback_registry import PairingCallbackRegistry
from app.services.session_store import SessionStore
from tests.fakes.ghostty import FakeGhosttyTerminalAdapter
from tests.fakes.process_probe import FakeLocalProcessProbe


@dataclass
class _Harness:
    service: ExternalSessionInputService
    binding_store: ExternalBindingStore
    session_store: SessionStore
    mode_store: ExternalInputTargetStore
    queue: ExternalInputQueue
    pairing: PairingCallbackRegistry
    adapter: FakeGhosttyTerminalAdapter
    probe: FakeLocalProcessProbe
    external_uq_state: ExternalUserQuestionState
    binding: ExternalBinding


@pytest.fixture
async def make_harness(tmp_path: Path):
    services: list[ExternalSessionInputService] = []
    counter = 0

    def _make(
        *,
        session_id: str = "session-1",
        user_id: int = 42,
        phase: SessionPhase = SessionPhase.IDLE,
        paired: bool = True,
        enabled: bool = True,
        adapter: FakeGhosttyTerminalAdapter | None = None,
        probe: FakeLocalProcessProbe | None = None,
        queue_max_size: int = 5,
        drain_wait: float = 0.05,
    ) -> _Harness:
        nonlocal counter
        counter += 1
        root = tmp_path / f"h-{counter}"
        binding_store = ExternalBindingStore(root / "binding")
        binding = ExternalBinding(
            session_id=session_id,
            user_id=user_id,
            cwd="/project",
            bound_at=utc_now(),
            jsonl_path=None,
            binding_id=f"binding-{counter}",
            pid=1234,
            tty="/dev/ttys005",
        )
        if paired:
            binding.ghostty_target = GhosttyInputTarget(
                terminal_id="term-1",
                paired_tty="/dev/ttys005",
                paired_at=utc_now(),
                binding_id=binding.binding_id,
                name="claude — project",
                cwd="/project",
            )
        binding_store.save_binding(binding)

        session_store = SessionStore(FileSessionStore(str(root / "state")))
        state = session_store.get_or_create(
            session_id=session_id,
            user_id=user_id,
            workdir="/project",
            claude_session_id=session_id,
        )
        state.phase = phase
        session_store.save(state)

        adapter = adapter or FakeGhosttyTerminalAdapter()
        probe = probe or FakeLocalProcessProbe()
        pairing = PairingCallbackRegistry(ttl_sec=60)
        external_uq_state = ExternalUserQuestionState(ttl_sec=60)
        mode_store = ExternalInputTargetStore()
        queue = ExternalInputQueue(max_size=queue_max_size, ttl_sec=60)
        locks = RefCountedLockRegistry(
            ttl_sec=60,
            cleanup_interval_sec=60,
            cleanup_batch_size=50,
        )
        service = ExternalSessionInputService(
            enabled=enabled,
            binding_store=binding_store,
            session_store=session_store,
            ghostty_adapter=adapter,  # type: ignore[arg-type]
            process_probe=probe,  # type: ignore[arg-type]
            pairing_registry=pairing,
            input_mode_store=mode_store,
            input_queue=queue,
            input_locks=locks,
            external_user_question_state=external_uq_state,
            drain_publish_wait_timeout_sec=drain_wait,
        )
        services.append(service)
        return _Harness(
            service=service,
            binding_store=binding_store,
            session_store=session_store,
            mode_store=mode_store,
            queue=queue,
            pairing=pairing,
            adapter=adapter,
            probe=probe,
            external_uq_state=external_uq_state,
            binding=binding,
        )

    yield _make

    await asyncio.gather(*(service.shutdown() for service in services), return_exceptions=True)


async def _activate(harness: _Harness, *, user_id: int = 42) -> None:
    await harness.mode_store.set_target(
        user_id=user_id,
        session_id=harness.binding.session_id,
        binding_id=harness.binding.binding_id,
    )


def _seed_external_question(
    harness: _Harness,
    *,
    tool_use_id: str = "tool-question",
    multi_select: bool = False,
) -> ExternalUserQuestionContext:
    target = harness.binding.ghostty_target
    assert target is not None
    prompt = UserQuestionPrompt(
        tool_use_id=tool_use_id,
        question_index=0,
        total_questions=1,
        question="Pick one",
        options=(
            UserQuestionOption(label="A"),
            UserQuestionOption(label="B"),
        ),
        multi_select=multi_select,
    )
    state = harness.session_store.get(harness.binding.session_id)
    assert state is not None
    state.phase = SessionPhase.WAITING_FOR_APPROVAL
    state.pending_permission = PendingPermission(
        tool_use_id=tool_use_id,
        tool_name="AskUserQuestion",
        tool_input={
            "questions": [
                {
                    "question": prompt.question,
                    "options": [{"label": option.label} for option in prompt.options],
                    "multiSelect": multi_select,
                }
            ]
        },
    )
    harness.session_store.save(state)
    ghostty_target = ExternalGhosttyQuestionTarget(
        binding_id=target.binding_id,
        terminal_id=target.terminal_id,
        paired_tty=target.paired_tty,
        paired_at=target.paired_at,
    )
    harness.external_uq_state.store(
        PendingExternalUserQuestion(
            tool_use_id=tool_use_id,
            session_id=harness.binding.session_id,
            user_id=harness.binding.user_id,
            prompts=(prompt,),
            target=ghostty_target,
        )
    )
    return ExternalUserQuestionContext(
        tool_use_id=tool_use_id,
        session_id=harness.binding.session_id,
        user_id=harness.binding.user_id,
        target=ghostty_target,
    )


async def _wait_until(predicate: Callable[[], bool], *, timeout: float = 1.0) -> None:
    async def _poll() -> None:
        while not predicate():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(_poll(), timeout=timeout)


# ─── pairing ---------------------------------------------------------------


async def test_pair_candidates_requires_owner_live_process_and_adapter(make_harness) -> None:
    harness = make_harness(paired=False)

    outcome, candidates = await harness.service.pair_candidates(user_id=7, session_id="session-1")
    assert outcome is PairOutcome.NOT_OWNER and candidates is None

    harness.adapter.available = False
    outcome, candidates = await harness.service.pair_candidates(user_id=42, session_id="session-1")
    assert outcome is PairOutcome.ADAPTER_UNAVAILABLE and candidates is None

    harness.adapter.available = True
    harness.probe.valid = False
    outcome, candidates = await harness.service.pair_candidates(user_id=42, session_id="session-1")
    assert outcome is PairOutcome.PROCESS_INVALID and candidates is None

    harness.probe.valid = True
    outcome, candidates = await harness.service.pair_candidates(user_id=42, session_id="session-1")
    assert outcome is PairOutcome.NEEDS_PAIRING
    assert candidates is not None
    assert candidates.binding_id == harness.binding.binding_id
    assert candidates.paired_tty == "/dev/ttys005"
    assert [terminal.terminal_id for terminal in candidates.terminals] == ["term-1"]


async def test_pair_candidates_prioritises_exact_cwd_without_auto_selecting(make_harness) -> None:
    adapter = FakeGhosttyTerminalAdapter(
        terminals=[
            GhosttyTerminal(terminal_id="other-1", name="Claude Code", cwd="/other"),
            GhosttyTerminal(terminal_id="match-claude", name="Claude Code", cwd="/project"),
            GhosttyTerminal(terminal_id="match-shell", name="~/project", cwd="/project"),
            GhosttyTerminal(terminal_id="other-2", name="shell", cwd="/elsewhere"),
        ]
    )
    harness = make_harness(paired=False, adapter=adapter)

    outcome, candidates = await harness.service.pair_candidates(user_id=42, session_id="session-1")

    assert outcome is PairOutcome.NEEDS_PAIRING
    assert candidates is not None
    assert [terminal.terminal_id for terminal in candidates.terminals] == [
        "match-claude",
        "match-shell",
        "other-1",
        "other-2",
    ]


async def test_pair_candidates_resolves_tty_from_pid_when_binding_missing_tty(make_harness) -> None:
    harness = make_harness(paired=False)
    harness.binding.tty = None
    harness.binding_store.save_binding(harness.binding)

    outcome, candidates = await harness.service.pair_candidates(user_id=42, session_id="session-1")
    assert outcome is PairOutcome.NEEDS_PAIRING
    assert candidates is not None and candidates.paired_tty == "/dev/ttys005"
    assert harness.probe.tty_calls == [1234]


async def test_pair_token_registration_enforces_displayed_binding_generation(make_harness) -> None:
    harness = make_harness(paired=False)
    stale = await harness.service.register_pair_token(
        user_id=42,
        session_id="session-1",
        expected_binding_id="old-generation",
        terminal_id="term-1",
    )
    assert stale is None

    token = await harness.service.register_pair_token(
        user_id=42,
        session_id="session-1",
        expected_binding_id=harness.binding.binding_id,
        terminal_id="term-1",
    )
    assert token is not None


async def test_consume_pair_token_persists_snapshot_and_activates(make_harness) -> None:
    harness = make_harness(paired=False)
    token = await harness.service.register_pair_token(
        user_id=42,
        session_id="session-1",
        expected_binding_id=harness.binding.binding_id,
        terminal_id="term-1",
    )
    assert token is not None

    outcome = await harness.service.consume_pair_token(token=token, user_id=42)
    assert outcome is PairOutcome.PAIRED
    binding = harness.binding_store.get_binding("session-1")
    assert binding is not None and binding.ghostty_target is not None
    assert binding.ghostty_target.terminal_id == "term-1"
    assert binding.ghostty_target.name == "claude — project"
    assert binding.ghostty_target.cwd == "/project"
    active = await harness.mode_store.get_target(42)
    assert active is not None and active.binding_id == harness.binding.binding_id


async def test_consume_pair_token_refuses_other_user_and_stale_generation(make_harness) -> None:
    harness = make_harness(paired=False)
    token = await harness.service.register_pair_token(
        user_id=42,
        session_id="session-1",
        expected_binding_id=harness.binding.binding_id,
        terminal_id="term-1",
    )
    assert token is not None
    assert await harness.service.consume_pair_token(token=token, user_id=7) is PairOutcome.TOKEN_UNAUTHORIZED

    # Unauthorized consume does not burn the token. Replace the binding before
    # the owner consumes: the token's old binding_id is now stale (ABA).
    replacement = ExternalBinding(
        session_id="session-1",
        user_id=42,
        cwd="/project",
        bound_at=utc_now(),
        jsonl_path=None,
        binding_id="new-generation",
        pid=1234,
        tty="/dev/ttys005",
    )
    harness.binding_store.save_binding(replacement)
    assert await harness.service.consume_pair_token(token=token, user_id=42) is PairOutcome.BINDING_STALE


async def test_consume_pair_token_honors_store_generation_safe_setter(make_harness, monkeypatch: pytest.MonkeyPatch) -> None:
    harness = make_harness(paired=False)
    token = await harness.service.register_pair_token(
        user_id=42,
        session_id="session-1",
        expected_binding_id=harness.binding.binding_id,
        terminal_id="term-1",
    )
    assert token is not None
    monkeypatch.setattr(harness.binding_store, "set_ghostty_target", lambda *_args, **_kwargs: False)

    outcome = await harness.service.consume_pair_token(token=token, user_id=42)
    assert outcome is PairOutcome.BINDING_STALE
    assert await harness.mode_store.get_target(42) is None


async def test_activate_select_validates_persisted_target(make_harness) -> None:
    harness = make_harness()
    assert await harness.service.activate_select(user_id=42, session_id="session-1") is PairOutcome.ACTIVATED
    assert await harness.mode_store.get_target(42) is not None

    await harness.service.leave(user_id=42)
    harness.adapter.validate_error = InjectionOutcome.NOT_FOUND
    assert await harness.service.activate_select(user_id=42, session_id="session-1") is PairOutcome.NEEDS_PAIRING
    binding = harness.binding_store.get_binding("session-1")
    assert binding is not None and binding.ghostty_target is None


async def test_repair_same_terminal_invalidates_old_question_generation(make_harness) -> None:
    harness = make_harness(paired=True)
    context = _seed_external_question(harness)
    token = await harness.service.register_pair_token(
        user_id=42,
        session_id=harness.binding.session_id,
        expected_binding_id=harness.binding.binding_id,
        terminal_id="term-1",
    )
    assert token is not None

    outcome = await harness.service.consume_pair_token(token=token, user_id=42)

    assert outcome is PairOutcome.PAIRED
    assert harness.external_uq_state.get(context.tool_use_id) is None
    rebound = harness.binding_store.get_binding(harness.binding.session_id)
    assert rebound is not None and rebound.ghostty_target is not None
    assert rebound.ghostty_target.paired_at != context.target.paired_at


# ─── AskUserQuestion transport ---------------------------------------------


async def test_question_select_does_not_require_active_input_mode(make_harness) -> None:
    harness = make_harness(paired=True)
    context = _seed_external_question(harness)
    assert await harness.service.has_target(42) is False

    result = await harness.service.select_option(
        context=context,
        question_index=0,
        option_count=2,
        option_index=1,
        submit_after=False,
    )

    assert result.status is ExternalQuestionActionStatus.APPLIED
    assert harness.adapter.question_calls == [("term-1", "select", 2, 1, False, "")]
    assert harness.external_uq_state.get_active(context.tool_use_id) is not None


async def test_final_question_uses_two_phase_completion(make_harness) -> None:
    harness = make_harness(paired=True)
    context = _seed_external_question(harness)

    result = await harness.service.select_option(
        context=context,
        question_index=0,
        option_count=2,
        option_index=0,
        submit_after=True,
    )

    assert result.status is ExternalQuestionActionStatus.APPLIED
    pending = harness.external_uq_state.get(context.tool_use_id)
    assert pending is not None and pending.phase is ExternalUserQuestionPhase.TERMINAL_ACTION_APPLIED

    await harness.service.question_completed(context=context)
    pending = harness.external_uq_state.get(context.tool_use_id)
    assert pending is not None and pending.phase is ExternalUserQuestionPhase.COMPLETED


async def test_question_rejects_owner_and_process_mismatch_without_adapter_action(make_harness) -> None:
    harness = make_harness(paired=True)
    context = _seed_external_question(harness)

    wrong_owner = ExternalUserQuestionContext(
        tool_use_id=context.tool_use_id,
        session_id=context.session_id,
        user_id=7,
        target=context.target,
    )
    result = await harness.service.select_option(
        context=wrong_owner,
        question_index=0,
        option_count=2,
        option_index=0,
        submit_after=False,
    )
    assert result.status is ExternalQuestionActionStatus.REJECTED

    harness.probe.valid = False
    result = await harness.service.select_option(
        context=context,
        question_index=0,
        option_count=2,
        option_index=0,
        submit_after=False,
    )
    assert result.status is ExternalQuestionActionStatus.REJECTED
    assert harness.adapter.question_calls == []


async def test_question_rechecks_target_after_terminal_validation_await(make_harness) -> None:
    harness = make_harness(paired=True)
    context = _seed_external_question(harness)
    harness.adapter.validate_entered = asyncio.Event()
    harness.adapter.validate_release = asyncio.Event()

    task = asyncio.create_task(
        harness.service.select_option(
            context=context,
            question_index=0,
            option_count=2,
            option_index=0,
            submit_after=False,
        )
    )
    await asyncio.wait_for(harness.adapter.validate_entered.wait(), timeout=1)
    old_target = harness.binding.ghostty_target
    assert old_target is not None
    harness.binding.ghostty_target = GhosttyInputTarget(
        terminal_id=old_target.terminal_id,
        paired_tty=old_target.paired_tty,
        paired_at=old_target.paired_at + timedelta(seconds=1),
        binding_id=old_target.binding_id,
        name=old_target.name,
        cwd=old_target.cwd,
    )
    harness.binding_store.save_binding(harness.binding)
    harness.adapter.validate_release.set()

    result = await task
    assert result.status is ExternalQuestionActionStatus.REJECTED
    assert harness.adapter.question_calls == []


async def test_question_indeterminate_blocks_retry(make_harness) -> None:
    harness = make_harness(paired=True)
    context = _seed_external_question(harness)
    harness.adapter.question_outcomes.append(InjectionOutcome.INDETERMINATE)

    first = await harness.service.select_option(
        context=context,
        question_index=0,
        option_count=2,
        option_index=0,
        submit_after=False,
    )
    second = await harness.service.select_option(
        context=context,
        question_index=0,
        option_count=2,
        option_index=0,
        submit_after=False,
    )

    assert first.status is ExternalQuestionActionStatus.INDETERMINATE
    assert second.status is ExternalQuestionActionStatus.REJECTED
    assert len(harness.adapter.question_calls) == 1
    pending = harness.external_uq_state.get(context.tool_use_id)
    assert pending is not None and pending.phase is ExternalUserQuestionPhase.INDETERMINATE


async def test_question_text_and_multi_advance_preserve_typed_actions(make_harness) -> None:
    harness = make_harness(paired=True)
    text_context = _seed_external_question(harness, tool_use_id="tool-text")
    answer = "  自由文本\n第二行  "

    text_result = await harness.service.answer_with_text(
        context=text_context,
        question_index=0,
        option_count=2,
        text=answer,
        submit_after=False,
    )
    assert text_result.status is ExternalQuestionActionStatus.APPLIED
    assert harness.adapter.question_calls[-1] == ("term-1", "answer_text", 2, -1, False, answer)

    harness.external_uq_state.invalidate_tool("tool-text")
    multi_context = _seed_external_question(harness, tool_use_id="tool-multi", multi_select=True)
    multi_result = await harness.service.advance_after_multi_select(
        context=multi_context,
        question_index=0,
        option_count=2,
        final_question=False,
    )
    assert multi_result.status is ExternalQuestionActionStatus.APPLIED
    assert harness.adapter.question_calls[-1] == ("term-1", "advance_multi", 2, -1, False, "")


# ─── send ------------------------------------------------------------------


async def test_send_requires_active_target(make_harness) -> None:
    harness = make_harness()
    assert await harness.service.send_text(user_id=42, text="hello") is SendOutcome.NO_TARGET
    assert harness.adapter.inject_calls == []


async def test_send_normalises_newlines_and_injects(make_harness) -> None:
    harness = make_harness()
    await _activate(harness)

    outcome = await harness.service.send_text(user_id=42, text="line1\r\nline2\rline3")
    assert outcome is SendOutcome.SENT
    assert harness.adapter.inject_calls == [("term-1", "line1\nline2\nline3")]
    assert harness.probe.validation_calls[-1] == (1234, "/dev/ttys005")
    assert len(harness.probe.validation_calls) == 2
    assert harness.adapter.validate_calls[-1] == "term-1"


async def test_send_revalidates_process_after_terminal_await(make_harness) -> None:
    probe = FakeLocalProcessProbe()
    probe.validation_results.extend([True, False])
    harness = make_harness(probe=probe)
    await _activate(harness)

    outcome = await harness.service.send_text(user_id=42, text="must not hit shell")
    assert outcome is SendOutcome.PROCESS_INVALID
    assert len(probe.validation_calls) == 2
    assert harness.adapter.inject_calls == []


async def test_send_rechecks_binding_generation_after_terminal_await(make_harness) -> None:
    adapter = FakeGhosttyTerminalAdapter()
    adapter.validate_entered = asyncio.Event()
    adapter.validate_release = asyncio.Event()
    harness = make_harness(adapter=adapter)
    await _activate(harness)

    send_task = asyncio.create_task(harness.service.send_text(user_id=42, text="stale generation"))
    await asyncio.wait_for(adapter.validate_entered.wait(), timeout=1)
    replacement = ExternalBinding(
        session_id="session-1",
        user_id=7,
        cwd="/other",
        bound_at=utc_now(),
        jsonl_path=None,
        binding_id="replacement-generation",
        pid=9999,
        tty="/dev/ttys099",
    )
    harness.binding_store.save_binding(replacement)
    adapter.validate_release.set()

    assert await send_task is SendOutcome.BINDING_STALE
    assert adapter.inject_calls == []


@pytest.mark.parametrize(
    "phase",
    [
        SessionPhase.PROCESSING,
        SessionPhase.COMPACTING,
        SessionPhase.WAITING_FOR_APPROVAL,
    ],
)
async def test_busy_phases_queue_without_injection(make_harness, phase: SessionPhase) -> None:
    harness = make_harness(phase=phase)
    await _activate(harness)

    outcome = await harness.service.send_text(user_id=42, text="queued")
    assert outcome is SendOutcome.QUEUED
    assert harness.adapter.inject_calls == []
    assert await harness.queue.peek_size("session-1") == 1


async def test_ended_state_refuses_and_clears_mode(make_harness) -> None:
    harness = make_harness(phase=SessionPhase.ENDED)
    await _activate(harness)

    assert await harness.service.send_text(user_id=42, text="late") is SendOutcome.SESSION_ENDED
    assert await harness.mode_store.get_target(42) is None
    assert harness.adapter.inject_calls == []


async def test_pending_permission_and_user_question_queue(make_harness) -> None:
    harness = make_harness()
    await _activate(harness)
    state = harness.session_store.get("session-1")
    assert state is not None

    state.pending_permission = PendingPermission(tool_use_id="tool-1", tool_name="Bash")
    harness.session_store.save(state)
    assert await harness.service.send_text(user_id=42, text="after permission") is SendOutcome.QUEUED

    await harness.queue.clear("session-1")
    state.pending_permission = None
    state.structured_user_question_key = "tool-uq:0"
    harness.session_store.save(state)
    assert await harness.service.send_text(user_id=42, text="after question") is SendOutcome.QUEUED
    assert harness.adapter.inject_calls == []


async def test_rapid_second_send_queues_behind_in_flight(make_harness) -> None:
    harness = make_harness()
    await _activate(harness)

    assert await harness.service.send_text(user_id=42, text="first") is SendOutcome.SENT
    assert await harness.service.send_text(user_id=42, text="second") is SendOutcome.QUEUED
    assert harness.adapter.inject_calls == [("term-1", "first")]
    assert await harness.queue.peek_size("session-1") == 1


async def test_queue_full_refuses_new_message(make_harness) -> None:
    harness = make_harness(phase=SessionPhase.PROCESSING, queue_max_size=1)
    await _activate(harness)
    assert await harness.service.send_text(user_id=42, text="one") is SendOutcome.QUEUED
    assert await harness.service.send_text(user_id=42, text="two") is SendOutcome.QUEUE_FULL
    assert await harness.queue.peek_size("session-1") == 1


async def test_process_failure_refuses_without_queueing(make_harness) -> None:
    probe = FakeLocalProcessProbe(valid=False)
    harness = make_harness(probe=probe)
    await _activate(harness)

    assert await harness.service.send_text(user_id=42, text="unsafe") is SendOutcome.PROCESS_INVALID
    assert await harness.queue.peek_size("session-1") == 0
    assert harness.adapter.inject_calls == []


async def test_missing_terminal_clears_pairing_mode_and_queue(make_harness) -> None:
    harness = make_harness(phase=SessionPhase.PROCESSING)
    await _activate(harness)
    assert await harness.service.send_text(user_id=42, text="queued") is SendOutcome.QUEUED
    harness.adapter.validate_error = InjectionOutcome.NOT_FOUND

    assert await harness.service.send_text(user_id=42, text="new") is SendOutcome.TERMINAL_INVALID
    binding = harness.binding_store.get_binding("session-1")
    assert binding is not None and binding.ghostty_target is None
    assert await harness.mode_store.get_target(42) is None
    assert await harness.queue.peek_size("session-1") == 0


async def test_adapter_unavailable_preserves_pairing(make_harness) -> None:
    harness = make_harness()
    await _activate(harness)
    harness.adapter.available = False

    assert await harness.service.send_text(user_id=42, text="hello") is SendOutcome.ADAPTER_UNAVAILABLE
    binding = harness.binding_store.get_binding("session-1")
    assert binding is not None and binding.ghostty_target is not None
    assert await harness.mode_store.get_target(42) is not None


async def test_indeterminate_injection_is_not_retried(make_harness) -> None:
    harness = make_harness()
    await _activate(harness)
    harness.adapter.inject_outcomes.append(InjectionOutcome.INDETERMINATE)

    assert await harness.service.send_text(user_id=42, text="maybe pasted") is SendOutcome.INJECTION_INDETERMINATE
    assert harness.adapter.inject_calls == [("term-1", "maybe pasted")]
    assert await harness.queue.peek_size("session-1") == 0


async def test_active_binding_aba_refuses_and_clears_mode(make_harness) -> None:
    harness = make_harness()
    await _activate(harness)
    harness.binding.binding_id = "new-generation"

    assert await harness.service.send_text(user_id=42, text="stale") is SendOutcome.BINDING_STALE
    assert await harness.mode_store.get_target(42) is None
    assert harness.adapter.inject_calls == []


# ─── locking and drain ------------------------------------------------------


async def test_same_session_injection_serialized_second_message_queues(make_harness) -> None:
    adapter = FakeGhosttyTerminalAdapter()
    adapter.inject_entered = asyncio.Event()
    adapter.inject_release = asyncio.Event()
    harness = make_harness(adapter=adapter)
    await _activate(harness)

    first = asyncio.create_task(harness.service.send_text(user_id=42, text="first"))
    await asyncio.wait_for(adapter.inject_entered.wait(), timeout=1)
    second = asyncio.create_task(harness.service.send_text(user_id=42, text="second"))
    await asyncio.sleep(0.02)
    assert not second.done(), "second waits on the per-session input lock"

    adapter.inject_release.set()
    assert await first is SendOutcome.SENT
    assert await second is SendOutcome.QUEUED
    assert adapter.max_active_injections == 1


async def test_stop_arriving_during_injection_cannot_release_new_turn_guard(make_harness) -> None:
    adapter = FakeGhosttyTerminalAdapter()
    adapter.inject_entered = asyncio.Event()
    adapter.inject_release = asyncio.Event()
    harness = make_harness(adapter=adapter, drain_wait=0.2)
    await _activate(harness)

    first = asyncio.create_task(harness.service.send_text(user_id=42, text="first"))
    await asyncio.wait_for(adapter.inject_entered.wait(), timeout=1)
    # This may be a late Stop from the prior turn. Because AppleScript for the
    # new turn is still in progress, it must not clear the new guard.
    await harness.service.notify_hook_event(session_id="session-1", event_kind="Stop")
    adapter.inject_release.set()
    assert await first is SendOutcome.SENT

    adapter.inject_entered = None
    adapter.inject_release = None
    assert await harness.service.send_text(user_id=42, text="second") is SendOutcome.QUEUED
    assert adapter.inject_calls == [("term-1", "first")]

    await harness.service.notify_hook_event(session_id="session-1", event_kind="Stop")
    await _wait_until(lambda: len(adapter.inject_calls) == 2)
    assert adapter.inject_calls[-1] == ("term-1", "second")


async def test_different_sessions_inject_concurrently(make_harness) -> None:
    adapter = FakeGhosttyTerminalAdapter()
    adapter.inject_entered = asyncio.Event()
    adapter.inject_release = asyncio.Event()
    one = make_harness(session_id="session-a", user_id=1, adapter=adapter)
    two = make_harness(session_id="session-b", user_id=2, adapter=adapter)
    await _activate(one, user_id=1)
    await _activate(two, user_id=2)

    task_a = asyncio.create_task(one.service.send_text(user_id=1, text="a"))
    task_b = asyncio.create_task(two.service.send_text(user_id=2, text="b"))
    await _wait_until(lambda: len(adapter.inject_calls) == 2)
    assert adapter.max_active_injections == 2
    adapter.inject_release.set()
    assert await task_a is SendOutcome.SENT
    assert await task_b is SendOutcome.SENT


async def test_busy_queue_drains_after_state_publish(make_harness) -> None:
    harness = make_harness(phase=SessionPhase.PROCESSING, drain_wait=0.2)
    await _activate(harness)
    assert await harness.service.send_text(user_id=42, text="later") is SendOutcome.QUEUED

    state = harness.session_store.get("session-1")
    assert state is not None
    state.phase = SessionPhase.IDLE
    harness.session_store.save(state)
    await _wait_until(lambda: harness.adapter.inject_calls == [("term-1", "later")])
    assert await harness.queue.peek_size("session-1") == 0


async def test_inflight_fifo_drains_one_item_per_stop(make_harness) -> None:
    harness = make_harness(drain_wait=0.2)
    await _activate(harness)
    assert await harness.service.send_text(user_id=42, text="one") is SendOutcome.SENT
    assert await harness.service.send_text(user_id=42, text="two") is SendOutcome.QUEUED
    assert await harness.service.send_text(user_id=42, text="three") is SendOutcome.QUEUED

    await harness.service.notify_hook_event(session_id="session-1", event_kind="Stop")
    await _wait_until(lambda: len(harness.adapter.inject_calls) == 2)
    assert harness.adapter.inject_calls[-1] == ("term-1", "two")
    assert await harness.queue.peek_size("session-1") == 1

    await harness.service.notify_hook_event(session_id="session-1", event_kind="Stop")
    await _wait_until(lambda: len(harness.adapter.inject_calls) == 3)
    assert [text for _terminal, text in harness.adapter.inject_calls] == ["one", "two", "three"]
    assert await harness.queue.peek_size("session-1") == 0


async def test_busy_wait_does_not_drop_queued_input(make_harness) -> None:
    harness = make_harness(phase=SessionPhase.PROCESSING, drain_wait=0.01)
    await _activate(harness)
    assert await harness.service.send_text(user_id=42, text="keep me") is SendOutcome.QUEUED
    await asyncio.sleep(0.05)  # several drain timeouts while still busy
    assert await harness.queue.peek_size("session-1") == 1
    assert harness.adapter.inject_calls == []

    state = harness.session_store.get("session-1")
    assert state is not None
    state.phase = SessionPhase.IDLE
    harness.session_store.save(state)
    await harness.service.notify_hook_event(session_id="session-1", event_kind="turn_completed")
    await _wait_until(lambda: harness.adapter.inject_calls == [("term-1", "keep me")])


async def test_drain_revalidates_after_terminal_await_and_restores_fifo(make_harness) -> None:
    probe = FakeLocalProcessProbe(valid=False)
    # send enqueue precheck succeeds; drain precheck succeeds; final check
    # observes shell takeover and must restore the dequeued entry.
    probe.validation_results.extend([True, True, False])
    harness = make_harness(
        phase=SessionPhase.PROCESSING,
        probe=probe,
        drain_wait=0.2,
    )
    await _activate(harness)
    assert await harness.service.send_text(user_id=42, text="keep queued") is SendOutcome.QUEUED

    state = harness.session_store.get("session-1")
    assert state is not None
    state.phase = SessionPhase.IDLE
    harness.session_store.save(state)
    await _wait_until(lambda: len(probe.validation_calls) >= 3)
    assert harness.adapter.inject_calls == []
    assert await harness.queue.peek_size("session-1") == 1

    probe.valid = True
    await harness.service.notify_hook_event(session_id="session-1", event_kind="Stop")
    await _wait_until(lambda: harness.adapter.inject_calls == [("term-1", "keep queued")])


async def test_drain_rechecks_binding_generation_after_terminal_await(make_harness) -> None:
    adapter = FakeGhosttyTerminalAdapter()
    harness = make_harness(
        phase=SessionPhase.PROCESSING,
        adapter=adapter,
        drain_wait=0.2,
    )
    await _activate(harness)
    assert await harness.service.send_text(user_id=42, text="old generation") is SendOutcome.QUEUED

    adapter.validate_entered = asyncio.Event()
    adapter.validate_release = asyncio.Event()
    state = harness.session_store.get("session-1")
    assert state is not None
    state.phase = SessionPhase.IDLE
    harness.session_store.save(state)
    await asyncio.wait_for(adapter.validate_entered.wait(), timeout=1)

    replacement = ExternalBinding(
        session_id="session-1",
        user_id=42,
        cwd="/project",
        bound_at=utc_now(),
        jsonl_path=None,
        binding_id="replacement-generation",
        pid=2222,
        tty="/dev/ttys009",
    )
    harness.binding_store.save_binding(replacement)
    adapter.validate_release.set()

    async def _wait_queue_cleared() -> None:
        while await harness.queue.peek_size("session-1") != 0:
            await asyncio.sleep(0.005)

    await asyncio.wait_for(_wait_queue_cleared(), timeout=1)
    assert adapter.inject_calls == []


async def test_leave_and_invalidate_clear_queue_and_mode(make_harness) -> None:
    harness = make_harness(phase=SessionPhase.PROCESSING)
    await _activate(harness)
    assert await harness.service.send_text(user_id=42, text="queued") is SendOutcome.QUEUED
    assert await harness.service.leave(user_id=42)
    assert await harness.mode_store.get_target(42) is None
    assert await harness.queue.peek_size("session-1") == 0

    await _activate(harness)
    await harness.queue.enqueue("session-1", text="queued2", binding_id=harness.binding.binding_id)
    await harness.service.invalidate_binding("session-1", reason="test")
    assert await harness.mode_store.get_target(42) is None
    assert await harness.queue.peek_size("session-1") == 0


async def test_rebind_aba_clears_old_mode_queue_and_pair_tokens(make_harness) -> None:
    harness = make_harness(phase=SessionPhase.PROCESSING)
    await _activate(harness)
    await harness.queue.enqueue("session-1", text="old", binding_id=harness.binding.binding_id)
    token = await harness.service.register_pair_token(
        user_id=42,
        session_id="session-1",
        expected_binding_id=harness.binding.binding_id,
        terminal_id="term-1",
    )
    assert token is not None

    await harness.service.rebind_aba("session-1", "new-generation")
    assert await harness.mode_store.get_target(42) is None
    assert await harness.queue.peek_size("session-1") == 0
    assert await harness.service.consume_pair_token(token=token, user_id=42) is PairOutcome.TOKEN_INVALID
