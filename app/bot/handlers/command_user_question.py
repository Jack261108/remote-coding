from __future__ import annotations

import logging

from aiogram import F
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.bot.presenters.structured_reply_presenter import UserQuestionOutput, build_user_question_prompt
from app.domain.user_question_models import UserQuestionPrompt
from app.services.task_service import TaskService

logger = logging.getLogger(__name__)
_QUESTION_CALLBACK_PREFIX = "ask"


def build_user_question_callback_data(*, tool_use_id: str, question_index: int, option_index: int) -> str:
    return f"{_QUESTION_CALLBACK_PREFIX}:{tool_use_id}:{question_index}:{option_index}"


def parse_user_question_callback_data(data: str | None) -> tuple[str, int, int] | None:
    if not data:
        return None
    prefix, sep, rest = data.partition(":")
    if prefix != _QUESTION_CALLBACK_PREFIX or not sep:
        return None
    tool_use_id, sep, indexes = rest.partition(":")
    if not sep or not tool_use_id:
        return None
    question_index_text, sep, option_index_text = indexes.partition(":")
    if not sep:
        return None
    try:
        return tool_use_id, int(question_index_text), int(option_index_text)
    except ValueError:
        return None


def _truncate_button_text(text: str, *, limit: int = 28) -> str:
    normalized = " ".join(text.split())
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[: limit - 3].rstrip()}..."


def build_user_question_keyboard(question: UserQuestionPrompt | UserQuestionOutput) -> InlineKeyboardMarkup | None:
    prompt = question.question if isinstance(question, UserQuestionOutput) else question
    if not prompt.options:
        return None
    rows = [
        [
            InlineKeyboardButton(
                text=_truncate_button_text(option.label),
                callback_data=build_user_question_callback_data(
                    tool_use_id=prompt.tool_use_id,
                    question_index=prompt.question_index,
                    option_index=index,
                ),
            )
        ]
        for index, option in enumerate(prompt.options)
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def maybe_handle_pending_user_question_text(
    *,
    message: Message,
    task_service: TaskService,
) -> bool:
    user_id = message.from_user.id if message.from_user else 0
    prompts = await task_service.get_pending_user_questions(user_id)
    if not prompts:
        return False

    text = (message.text or "").strip()
    ok, response_text, next_prompt = await task_service.answer_pending_user_question_text(user_id=user_id, text=text)
    if ok:
        await message.answer(response_text)
        if next_prompt is not None:
            await message.answer(
                build_user_question_prompt(next_prompt),
                reply_markup=build_user_question_keyboard(next_prompt),
            )
    else:
        await message.answer(f"回复失败: {response_text}")
    return True


def register_user_question_handlers(router, *, task_service: TaskService):
    @router.callback_query(F.data.startswith(f"{_QUESTION_CALLBACK_PREFIX}:"))
    async def callback_user_question(callback: CallbackQuery) -> None:
        user_id = callback.from_user.id if callback.from_user else 0
        parsed = parse_user_question_callback_data(callback.data)
        if parsed is None:
            await callback.answer("无效的选择操作", show_alert=True)
            return

        tool_use_id, question_index, option_index = parsed
        ok, text, next_prompt = await task_service.answer_pending_user_question_option(
            user_id=user_id,
            tool_use_id=tool_use_id,
            question_index=question_index,
            option_index=option_index,
        )
        if callback.message is not None:
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except Exception:
                logger.exception(
                    "failed to clear user question inline keyboard",
                    extra={
                        "user_id": user_id,
                        "tool_use_id": tool_use_id,
                        "question_index": question_index,
                    },
                )
            if ok:
                await callback.message.answer(text)
                if next_prompt is not None:
                    await callback.message.answer(
                        build_user_question_prompt(next_prompt),
                        reply_markup=build_user_question_keyboard(next_prompt),
                    )
            else:
                await callback.message.answer(f"选择失败: {text}")
        await callback.answer(text, show_alert=not ok)
