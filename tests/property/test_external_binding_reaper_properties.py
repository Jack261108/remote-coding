"""Property-based tests for the ExternalBindingReaper removal log.

Spec: external-binding-pid-liveness, Task 7.2

Property 9: Removal log structure and reason label
  For any binding removed through the reaper, the emitted INFO log contains the
  required context fields, `pid` is ALWAYS present as an explicit attribute
  (rendered as `None` on the unknown-pid path rather than omitted), and
  `reason` is logged verbatim as supplied by the caller.

**Validates: Requirements 8.1, 8.2, 8.3**
"""

from __future__ import annotations

import logging
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.domain.external_session_models import ExternalBinding
from app.domain.models import utc_now
from app.services.external_binding_reaper import ExternalBindingReaper
from app.services.external_binding_store import ExternalBindingStore

_REAPER_LOGGER = "app.services.external_binding_reaper"

_REQUIRED_EXTRA_KEYS = (
    "session_id",
    "user_id",
    "cwd",
    "bound_at",
    "last_activity_at",
    "idle_hours",
    "pid",
    "reason",
)


# Feature: external-binding-pid-liveness, Property 9: Removal log structure and reason label
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    pid=st.one_of(st.none(), st.integers(min_value=1)),
    reason=st.sampled_from(["pid_dead", "idle_ttl_expired"]),
)
@pytest.mark.asyncio
async def test_property_9_removal_log_structure_and_reason_label(
    caplog: pytest.LogCaptureFixture,
    pid: int | None,
    reason: str,
) -> None:
    """A reaper removal emits ONE INFO 'external binding removed' record whose
    `extra` carries every required context field, with `pid` always present as
    an explicit attribute (None on the unknown-pid path, never missing) and the
    supplied `reason` recorded verbatim.

    `caplog` accumulates records across Hypothesis examples, so we clear it at
    the start of every example body to avoid cross-example contamination.

    **Validates: Requirements 8.1, 8.2, 8.3**
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        store = ExternalBindingStore(data_dir=Path(tmp_dir))

        now = utc_now()
        session_id = "pbt-reaper-session"
        store.save_binding(
            ExternalBinding(
                session_id=session_id,
                user_id=4242,
                cwd="/home/user/project",
                bound_at=now - timedelta(hours=3),
                jsonl_path=None,
                pid=pid,
                last_activity_at_init=now - timedelta(hours=1),
            )
        )

        auto_approve_service = AsyncMock()
        auto_approve_service.clear_session = AsyncMock(return_value=None)
        hook_socket_server = AsyncMock()
        hook_socket_server.cancel_pending_permissions = AsyncMock(return_value=None)

        reaper = ExternalBindingReaper(
            binding_store=store,
            auto_approve_service=auto_approve_service,
            hook_socket_server=hook_socket_server,
        )

        with caplog.at_level(logging.INFO, logger=_REAPER_LOGGER):
            caplog.clear()
            removed = await reaper.remove_with_cleanup(session_id, reason=reason)

        assert removed is True

        records = [r for r in caplog.records if r.name == _REAPER_LOGGER and r.getMessage() == "external binding removed"]
        assert len(records) == 1, f"expected exactly one removal log, got {len(records)}"
        record = records[0]
        assert record.levelno == logging.INFO

        # All required context fields are present on the record.
        for key in _REQUIRED_EXTRA_KEYS:
            assert hasattr(record, key), f"removal log is missing required field {key!r}"

        # `pid` is ALWAYS an explicit attribute, even when unknown (None), never
        # omitted, and equals the binding's pid (None on the unknown-pid path).
        assert hasattr(record, "pid")
        assert record.pid == pid

        # `reason` is logged verbatim as supplied by the caller.
        assert record.reason == reason
