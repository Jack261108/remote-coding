"""Property-based test for the ``/list`` liveness visibility partition.

Feature: external-binding-pid-liveness, Task 10.2

Covers the ``/list`` correctness property from the design's "Correctness
Properties" section and "Components §8":

  - Property 8: /list liveness visibility partition

Target under test:
``app.bot.handlers.command_list.register_list_handler`` and the inner
``command_list`` message handler it registers. When
``liveness_enabled and reaper is not None`` the handler partitions a user's
bound bindings into:

  - EXCLUDED + reaped: ``Pid_Known`` (``pid is not None and pid > 0``) AND the
    liveness probe reports the process dead (Req 9.1). Each excluded binding is
    removed through ``reaper.remove_with_cleanup(session_id, reason="pid_dead")``
    (Req 9.2).
  - KEPT (rendered): bindings whose process is alive (Req 9.4) or whose pid is
    unknown (``Pid_Known`` false — Req 9.3, which falls through to existing
    idle-TTL behavior).

When ``liveness_enabled`` is false the handler renders bindings exactly as
before and performs no pid-based exclusion (Req 10.3); the probe is never
consulted to exclude anything.

INVOCATION PATTERN: this mirrors the established handler-unit-test pattern in
this repo (see ``tests/test_session_handlers.py``): register the handler on a
real ``aiogram.Router`` and retrieve the inner coroutine via
``router.message.handlers[-1].callback``. The test is a SYNC Hypothesis test
that drives the async handler via ``asyncio.run(...)`` (the repo runs pytest
with ``asyncio_mode = "auto"``; an explicit ``asyncio.run`` here keeps the test
free of function-scoped-fixture health-check concerns and builds fresh mocks per
example).

PATCH PATH (critical): the handler binds the probe at import time via
``from app.services.process_liveness import process_is_alive``, so it calls the
module-local name. The patch therefore targets
``app.bot.handlers.command_list.process_is_alive`` (the consumer's binding),
NOT ``app.services.process_liveness.process_is_alive``.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram import Router
from hypothesis import given, settings
from hypothesis import strategies as st

from app.bot.handlers.command_list import register_list_handler
from app.domain.external_session_models import ExternalBinding
from app.domain.models import utc_now
from app.services.external_binding_store import ExternalBindingStore

_USER_ID = 777
_PROBE_PATH = "app.bot.handlers.command_list.process_is_alive"
_CALLBACK_PREFIX = "sess:select:"


def _rendered_session_id_prefixes(message: "_DummyMessage") -> set[str]:
    """Extract the bound-session callback prefixes from the rendered keyboard.

    Each bound binding renders an inline button with
    ``callback_data = f"sess:select:{session_id[:16]}"``. ``external_discovery``
    is ``None`` in this test, so there are no unbound-session buttons to confuse
    the parse — every ``sess:select:`` button corresponds to a rendered (kept)
    binding.
    """
    if not message.reply_markups:
        return set()
    keyboard = message.reply_markups[-1]
    if keyboard is None:
        return set()
    rendered: set[str] = set()
    for row in keyboard.inline_keyboard:
        for button in row:
            data = button.callback_data or ""
            if data.startswith(_CALLBACK_PREFIX):
                rendered.add(data[len(_CALLBACK_PREFIX) :])
    return rendered


class _DummyMessage:
    """Minimal aiogram ``Message`` stand-in capturing ``answer`` calls."""

    def __init__(self, user_id: int) -> None:
        self.from_user = SimpleNamespace(id=user_id)
        self.answers: list[str] = []
        self.reply_markups: list[object] = []

    async def answer(self, text: str, reply_markup=None, parse_mode=None) -> None:
        self.answers.append(text)
        self.reply_markups.append(reply_markup)


# Feature: external-binding-pid-liveness, Property 8: /list liveness visibility partition
@settings(max_examples=100, deadline=None)
@given(
    tags=st.lists(st.sampled_from(["unknown", "alive", "dead"]), min_size=0, max_size=8),
    liveness_enabled=st.booleans(),
)
def test_property_8_list_liveness_visibility_partition(
    tags: list[str],
    liveness_enabled: bool,
) -> None:
    """For any set of a user's bindings with mixed pid states:

      - liveness_enabled=True  -> the render excludes EXACTLY the
        ``Pid_Known``-and-dead bindings (each reaped with reason='pid_dead') and
        includes EVERY alive-or-unknown binding.
      - liveness_enabled=False -> NO binding is excluded on the basis of pid and
        the probe is never consulted.

    **Validates: Requirements 9.1, 9.3, 9.4, 10.3**
    """
    # Build one binding per generated tag. session_ids are short + unique so the
    # ``session_id[:16]`` callback slice is the full id (no collisions). Each
    # alive/dead binding gets a unique positive pid; unknown bindings have
    # pid=None (Pid_Known = False).
    alive_by_pid: dict[int, bool] = {}
    visible_expected: set[str] = set()  # alive + unknown -> rendered
    dead_expected: set[str] = set()  # Pid_Known + dead -> excluded + reaped

    with tempfile.TemporaryDirectory() as tmp_dir:
        store = ExternalBindingStore(data_dir=Path(tmp_dir))
        now = utc_now()

        for i, tag in enumerate(tags):
            session_id = f"s{i}"
            if tag == "unknown":
                pid: int | None = None
                visible_expected.add(session_id)
            else:
                pid = 1000 + i
                if tag == "alive":
                    alive_by_pid[pid] = True
                    visible_expected.add(session_id)
                else:  # dead
                    alive_by_pid[pid] = False
                    dead_expected.add(session_id)
            store.save_binding(
                ExternalBinding(
                    session_id=session_id,
                    user_id=_USER_ID,
                    cwd=f"/home/user/project-{i}",
                    bound_at=now,
                    jsonl_path=None,
                    pid=pid,
                    last_activity_at_init=now,
                )
            )

        # registry_service: no active tmux sessions (focus the test on bindings).
        registry_service = AsyncMock()
        registry_service.list_active_sessions = AsyncMock(return_value=[])

        # external_binder stub exposing only the attribute the handler reads:
        # ``external_binder._binding_store.get_bindings_for_user(user_id)``.
        external_binder = SimpleNamespace(_binding_store=store)

        # Reaper is an AsyncMock so it records reap calls without mutating the
        # store; the handler partitions BEFORE reaping, so rendering is unaffected
        # by the mock not actually removing bindings.
        reaper = AsyncMock()
        reaper.remove_with_cleanup = AsyncMock(return_value=True)

        router = Router()
        register_list_handler(
            router,
            registry_service=registry_service,
            external_discovery=None,
            external_binder=external_binder,
            liveness_enabled=liveness_enabled,
            reaper=reaper,
        )
        handler = router.message.handlers[-1].callback

        message = _DummyMessage(_USER_ID)

        def fake_probe(pid: int) -> bool:
            return alive_by_pid.get(pid, True)

        with patch(_PROBE_PATH, side_effect=fake_probe) as probe_mock:
            asyncio.run(handler(message))

        rendered = _rendered_session_id_prefixes(message)
        all_ids = visible_expected | dead_expected

        if liveness_enabled:
            # Rendered set is exactly the alive + unknown bindings; the dead ones
            # are excluded (Req 9.1, 9.3, 9.4).
            assert rendered == {sid[:16] for sid in visible_expected}, (
                f"liveness on: expected rendered={visible_expected!r}, got prefixes={rendered!r} (dead_expected={dead_expected!r})"
            )

            # Each Pid_Known-and-dead binding is reaped exactly once with
            # reason='pid_dead' (Req 9.2); no alive/unknown binding is reaped.
            reaped_ids = {c.args[0] for c in reaper.remove_with_cleanup.await_args_list}
            reaped_reasons = {c.kwargs.get("reason") for c in reaper.remove_with_cleanup.await_args_list}
            assert reaped_ids == dead_expected, f"expected reaped={dead_expected!r}, got {reaped_ids!r}"
            assert reaper.remove_with_cleanup.await_count == len(dead_expected)
            if dead_expected:
                assert reaped_reasons == {"pid_dead"}
            assert reaped_ids.isdisjoint(visible_expected), "no alive/unknown binding may be reaped"
        else:
            # Liveness disabled: every binding is rendered, none excluded on pid
            # (Req 10.3), and the probe is never consulted to exclude.
            assert rendered == {sid[:16] for sid in all_ids}, f"liveness off: expected all rendered={all_ids!r}, got {rendered!r}"
            reaper.remove_with_cleanup.assert_not_awaited()
            probe_mock.assert_not_called()
