"""Property-based tests for ExternalSessionPushNotifier.

Feature: external-session-takeover
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.domain.session_models import SessionPhase
from app.services.external_session_push_notifier import ExternalSessionPushNotifier

# --- Property 15: Phase transitions trigger push notifications ---


class TestPhaseTransitionPushNotifications:
    """Property 15: Phase transitions trigger push notifications.

    **Validates: Requirements 6.1, 6.2**

    When a bound external session transitions phases, the bound user
    receives a push notification containing session context.
    """

    @pytest.mark.asyncio
    async def test_phase_change_sends_notification_to_bound_user(self):
        """Phase change triggers push notification with session context."""
        message_sender = MagicMock()
        message_sender.send_message = AsyncMock(return_value=123)
        binding_store = MagicMock()

        notifier = ExternalSessionPushNotifier(
            message_sender=message_sender,
            binding_store=binding_store,
        )

        user_id = 42
        session_id = "abc12345-session"
        cwd = "/home/user/project"

        result = await notifier.notify_phase_change(
            user_id=user_id,
            session_id=session_id,
            old_phase=SessionPhase.IDLE,
            new_phase=SessionPhase.PROCESSING,
            cwd=cwd,
        )

        assert result is True
        message_sender.send_message.assert_called_once()
        call_kwargs = message_sender.send_message.call_args.kwargs
        assert call_kwargs["chat_id"] == user_id
        # Message should contain session info and phase transition
        text = call_kwargs["text"]
        assert session_id[:8] in text
        assert SessionPhase.IDLE.value in text
        assert SessionPhase.PROCESSING.value in text
        assert cwd in text

    @pytest.mark.asyncio
    async def test_all_phase_transitions_trigger_notification(self):
        """Various phase transitions all produce push notifications."""
        transitions = [
            (SessionPhase.IDLE, SessionPhase.PROCESSING),
            (SessionPhase.PROCESSING, SessionPhase.WAITING_FOR_INPUT),
            (SessionPhase.WAITING_FOR_INPUT, SessionPhase.PROCESSING),
            (SessionPhase.PROCESSING, SessionPhase.WAITING_FOR_APPROVAL),
            (SessionPhase.PROCESSING, SessionPhase.ENDED),
            (SessionPhase.PROCESSING, SessionPhase.COMPACTING),
        ]

        for old_phase, new_phase in transitions:
            message_sender = MagicMock()
            message_sender.send_message = AsyncMock(return_value=123)
            binding_store = MagicMock()

            notifier = ExternalSessionPushNotifier(
                message_sender=message_sender,
                binding_store=binding_store,
            )

            result = await notifier.notify_phase_change(
                user_id=100,
                session_id="session-xyz",
                old_phase=old_phase,
                new_phase=new_phase,
                cwd="/tmp/work",
            )

            assert result is True, f"Failed for {old_phase} → {new_phase}"
            message_sender.send_message.assert_called_once()
            text = message_sender.send_message.call_args.kwargs["text"]
            assert old_phase.value in text
            assert new_phase.value in text

    @pytest.mark.asyncio
    async def test_notification_failure_returns_false(self):
        """When message_sender.send_message fails, notify_phase_change returns False."""
        message_sender = MagicMock()
        message_sender.send_message = AsyncMock(side_effect=Exception("Network error"))
        binding_store = MagicMock()

        notifier = ExternalSessionPushNotifier(
            message_sender=message_sender,
            binding_store=binding_store,
            retry_count=0,
        )

        result = await notifier.notify_phase_change(
            user_id=999,
            session_id="failing-session",
            old_phase=SessionPhase.IDLE,
            new_phase=SessionPhase.PROCESSING,
            cwd="/tmp/fail",
        )

        assert result is False

    @pytest.mark.asyncio
    async def test_notification_contains_session_context(self):
        """Push notification message includes session_id prefix and cwd."""
        message_sender = MagicMock()
        message_sender.send_message = AsyncMock()
        binding_store = MagicMock()

        notifier = ExternalSessionPushNotifier(
            message_sender=message_sender,
            binding_store=binding_store,
        )

        session_id = "longsession12345"
        cwd = "/home/dev/myproject"

        await notifier.notify_phase_change(
            user_id=7,
            session_id=session_id,
            old_phase=SessionPhase.PROCESSING,
            new_phase=SessionPhase.ENDED,
            cwd=cwd,
        )

        text = message_sender.send_message.call_args.kwargs["text"]
        # Should contain short session id (first 8 chars)
        assert session_id[:8] in text
        # Should contain cwd
        assert cwd in text
