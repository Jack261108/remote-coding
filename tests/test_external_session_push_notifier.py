"""Tests for ExternalSessionPushNotifier.

Covers: notify_phase_change, notify_session_end, notify_user_question,
notify_info, _send_with_retry, error branches.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.domain.session_models import SessionPhase
from app.domain.user_question_models import UserQuestionOption, UserQuestionPrompt
from app.services.external_session_push_notifier import ExternalSessionPushNotifier

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_notifier(
    *,
    retry_count: int = 1,
    send_side_effects: list | None = None,
) -> tuple[ExternalSessionPushNotifier, MagicMock]:
    sender = MagicMock()
    if send_side_effects:
        sender.send_message = AsyncMock(side_effect=send_side_effects)
    else:
        sender.send_message = AsyncMock(return_value=123)
    notifier = ExternalSessionPushNotifier(
        message_sender=sender,
        binding_store=MagicMock(),
        retry_count=retry_count,
    )
    return notifier, sender


# ---------------------------------------------------------------------------
# notify_phase_change
# ---------------------------------------------------------------------------


class TestNotifyPhaseChange:
    @pytest.mark.asyncio
    async def test_sends_phase_change_message(self):
        notifier, sender = _make_notifier()
        result = await notifier.notify_phase_change(
            user_id=42,
            session_id="sess-1",
            old_phase=SessionPhase.WAITING_FOR_INPUT,
            new_phase=SessionPhase.PROCESSING,
            cwd="/tmp/project",
        )
        assert result is True
        sender.send_message.assert_awaited_once()
        text = sender.send_message.call_args.kwargs["text"]
        assert "waiting_for_input" in text
        assert "processing" in text


# ---------------------------------------------------------------------------
# notify_session_end
# ---------------------------------------------------------------------------


class TestNotifySessionEnd:
    @pytest.mark.asyncio
    async def test_sends_session_end_message(self):
        notifier, sender = _make_notifier()
        result = await notifier.notify_session_end(
            user_id=42,
            session_id="sess-1",
            cwd="/tmp/project",
        )
        assert result is True
        text = sender.send_message.call_args.kwargs["text"]
        assert "会话已结束" in text


# ---------------------------------------------------------------------------
# notify_user_question
# ---------------------------------------------------------------------------


class TestNotifyUserQuestion:
    @pytest.mark.asyncio
    async def test_returns_false_for_empty_prompts(self):
        notifier, sender = _make_notifier()
        result = await notifier.notify_user_question(
            user_id=42,
            session_id="sess-1",
            prompts=(),
        )
        assert result is False
        sender.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_sends_info_only_when_not_interactive(self):
        notifier, sender = _make_notifier()
        prompts = (
            UserQuestionPrompt(
                tool_use_id="tuid-1",
                question_index=0,
                total_questions=1,
                question="Continue?",
            ),
        )
        result = await notifier.notify_user_question(
            user_id=42,
            session_id="sess-1",
            prompts=prompts,
            interactive=False,
        )
        assert result is True
        text = sender.send_message.call_args.kwargs["text"]
        assert "请在终端中选择" in text

    @pytest.mark.asyncio
    async def test_sends_interactive_with_options(self):
        notifier, sender = _make_notifier()
        prompts = (
            UserQuestionPrompt(
                tool_use_id="tuid-1",
                question_index=0,
                total_questions=1,
                question="Choose?",
                options=(
                    UserQuestionOption(label="Yes", description="confirm"),
                    UserQuestionOption(label="No"),
                ),
            ),
        )
        result = await notifier.notify_user_question(
            user_id=42,
            session_id="sess-1",
            prompts=prompts,
            interactive=True,
        )
        assert result is True
        text = sender.send_message.call_args.kwargs["text"]
        assert "点击按钮选择" in text
        keyboard = sender.send_message.call_args.kwargs["keyboard"]
        assert keyboard is not None

    @pytest.mark.asyncio
    async def test_sends_info_when_interactive_but_no_options(self):
        notifier, sender = _make_notifier()
        prompts = (
            UserQuestionPrompt(
                tool_use_id="tuid-1",
                question_index=0,
                total_questions=1,
                question="Type something:",
            ),
        )
        result = await notifier.notify_user_question(
            user_id=42,
            session_id="sess-1",
            prompts=prompts,
            interactive=True,
        )
        assert result is True
        text = sender.send_message.call_args.kwargs["text"]
        assert "请在终端中选择" in text


# ---------------------------------------------------------------------------
# notify_info
# ---------------------------------------------------------------------------


class TestNotifyInfo:
    @pytest.mark.asyncio
    async def test_sends_info_message(self):
        notifier, sender = _make_notifier()
        result = await notifier.notify_info(user_id=42, text="Hello!")
        assert result is True
        sender.send_message.assert_awaited_once_with(chat_id=42, text="Hello!", keyboard=None, parse_mode=None)


# ---------------------------------------------------------------------------
# _send_with_retry
# ---------------------------------------------------------------------------


class TestSendWithRetry:
    @pytest.mark.asyncio
    async def test_returns_message_id_on_success(self):
        notifier, sender = _make_notifier()
        result = await notifier._send_with_retry(chat_id=1, text="hi")
        assert result == 123

    @pytest.mark.asyncio
    async def test_retries_on_failure(self):
        notifier, sender = _make_notifier(
            retry_count=2,
            send_side_effects=[RuntimeError("fail"), RuntimeError("fail"), 456],
        )
        result = await notifier._send_with_retry(chat_id=1, text="hi")
        assert result == 456
        assert sender.send_message.await_count == 3

    @pytest.mark.asyncio
    async def test_returns_none_after_all_retries_fail(self):
        notifier, sender = _make_notifier(
            retry_count=1,
            send_side_effects=[RuntimeError("fail"), RuntimeError("fail")],
        )
        result = await notifier._send_with_retry(chat_id=1, text="hi")
        assert result is None
        assert sender.send_message.await_count == 2


# ---------------------------------------------------------------------------
# notify_permission_resolved_in_terminal
# ---------------------------------------------------------------------------


class TestNotifyPermissionResolved:
    @pytest.mark.asyncio
    async def test_sends_approved_notification(self):
        notifier, sender = _make_notifier()
        result = await notifier.notify_permission_resolved_in_terminal(
            user_id=42,
            session_id="sess-1",
            tool_name="Bash",
            tool_use_id="tuid-1",
            reason="terminal_approved",
        )
        assert result is True
        text = sender.send_message.call_args.kwargs["text"]
        assert "已批准" in text
        assert "Bash" in text

    @pytest.mark.asyncio
    async def test_sends_other_reason_notification(self):
        notifier, sender = _make_notifier()
        result = await notifier.notify_permission_resolved_in_terminal(
            user_id=42,
            session_id="sess-1",
            tool_name="Write",
            tool_use_id="tuid-2",
            reason="denied",
        )
        assert result is True
        text = sender.send_message.call_args.kwargs["text"]
        assert "denied" in text


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
