from __future__ import annotations

import logging
from dataclasses import dataclass

from aiogram import F
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.bot.handlers.user_utils import extract_user_id
from app.bot.middleware.callback_validator import CallbackValidatorMiddleware
from app.bot.presenters.structured_reply_presenter import UserQuestionOutput, build_user_question_prompt
from app.domain.user_question_models import UserQuestionPrompt
from app.services.task_service import TaskService

logger = logging.getLogger(__name__)
_QUESTION_CALLBACK_PREFIX = "ask"
_QUESTION_CALLBACK_ACTION_TOGGLE = "toggle"
_QUESTION_CALLBACK_ACTION_SUBMIT = "submit"


@dataclass(frozen=True)
class ParsedUserQuestionCallback:
    action: str
    tool_use_id: str
    question_index: int
    option_index: int | None = None


def build_user_question_callback_data(*, tool_use_id: str, question_index: int, option_index: int) -> str:
    return f"{_QUESTION_CALLBACK_PREFIX}:{tool_use_id}:{question_index}:{option_index}"


def build_multi_select_toggle_callback_data(*, tool_use_id: str, question_index: int, option_index: int) -> str:
    return f"{_QUESTION_CALLBACK_PREFIX}:{_QUESTION_CALLBACK_ACTION_TOGGLE}:{tool_use_id}:{question_index}:{option_index}"


def build_multi_select_submit_callback_data(*, tool_use_id: str, question_index: int) -> str:
    return f"{_QUESTION_CALLBACK_PREFIX}:{_QUESTION_CALLBACK_ACTION_SUBMIT}:{tool_use_id}:{question_index}"


def parse_user_question_callback_data(data: str | None) -> ParsedUserQuestionCallback | None:
    if not data:
        return None
    parts = data.split(":")
    if not parts or parts[0] != _QUESTION_CALLBACK_PREFIX:
        return None
    if len(parts) == 5 and parts[1] == _QUESTION_CALLBACK_ACTION_TOGGLE:
        _, _, tool_use_id, question_index_text, option_index_text = parts
        try:
            return ParsedUserQuestionCallback(
                action=_QUESTION_CALLBACK_ACTION_TOGGLE,
                tool_use_id=tool_use_id,
                question_index=int(question_index_text),
                option_index=int(option_index_text),
            )
        except ValueError:
            return None
    if len(parts) == 4 and parts[1] == _QUESTION_CALLBACK_ACTION_SUBMIT:
        _, _, tool_use_id, question_index_text = parts
        try:
            return ParsedUserQuestionCallback(
                action=_QUESTION_CALLBACK_ACTION_SUBMIT,
                tool_use_id=tool_use_id,
                question_index=int(question_index_text),
            )
        except ValueError:
            return None
    if len(parts) == 4:
        _, tool_use_id, question_index_text, option_index_text = parts
        try:
            return ParsedUserQuestionCallback(
                action="select",
                tool_use_id=tool_use_id,
                question_index=int(question_index_text),
                option_index=int(option_index_text),
            )
        except ValueError:
            return None
    return None


def _truncate_button_text(text: str, *, limit: int = 28) -> str:
    normalized = " ".join(text.split())
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[: limit - 3].rstrip()}..."


def build_user_question_keyboard(
    question: UserQuestionPrompt | UserQuestionOutput,
    *,
    selected_option_indexes: frozenset[int] | None = None,
) -> InlineKeyboardMarkup | None:
    prompt = question.question if isinstance(question, UserQuestionOutput) else question
    if not prompt.options:
        return None
    selected = selected_option_indexes or frozenset()
    rows = []
    for index, option in enumerate(prompt.options):
        label = option.label
        callback_data = build_user_question_callback_data(
            tool_use_id=prompt.tool_use_id,
            question_index=prompt.question_index,
            option_index=index,
        )
        if prompt.multi_select:
            label = f"{'☑' if index in selected else '☐'} {label}"
            callback_data = build_multi_select_toggle_callback_data(
                tool_use_id=prompt.tool_use_id,
                question_index=prompt.question_index,
                option_index=index,
            )
        rows.append(
            [
                InlineKeyboardButton(
                    text=_truncate_button_text(label),
                    callback_data=callback_data,
                )
            ]
        )
    if prompt.multi_select:
        rows.append(
            [
                InlineKeyboardButton(
                    text="提交选择",
                    callback_data=build_multi_select_submit_callback_data(
                        tool_use_id=prompt.tool_use_id,
                        question_index=prompt.question_index,
                    ),
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _acknowledge_and_send_next_prompt(
    *, message: Message, task_service: TaskService, user_id: int, next_prompt: UserQuestionPrompt | None
) -> None:
    if next_prompt is None:
        return
    await message.answer(
        build_user_question_prompt(next_prompt),
        reply_markup=build_user_question_keyboard(next_prompt),
    )
    await task_service.acknowledge_structured_user_question(user_id, question_key=next_prompt.key)


async def maybe_handle_pending_user_question_text(
    *,
    message: Message,
    task_service: TaskService,
) -> bool:
    user_id = extract_user_id(message)
    prompts = await task_service.get_pending_user_questions(user_id)
    if not prompts:
        return False

    text = (message.text or "").strip()
    ok, response_text, next_prompt = await task_service.answer_pending_user_question_text(user_id=user_id, text=text)
    if ok:
        await message.answer(response_text)
        await _acknowledge_and_send_next_prompt(message=message, task_service=task_service, user_id=user_id, next_prompt=next_prompt)
    else:
        await message.answer(f"回复失败: {response_text}")
    return True


def register_user_question_handlers(router, *, task_service: TaskService):
    router.callback_query.middleware(CallbackValidatorMiddleware(prefix=_QUESTION_CALLBACK_PREFIX))

    @router.callback_query(F.data.startswith(f"{_QUESTION_CALLBACK_PREFIX}:"))
    async def callback_user_question(callback: CallbackQuery) -> None:
        user_id = extract_user_id(callback)
        parsed = parse_user_question_callback_data(callback.data)
        if parsed is None:
            await callback.answer("无效的选择操作", show_alert=True)
            return

        if parsed.action == _QUESTION_CALLBACK_ACTION_TOGGLE:
            ok, text, prompt, selected_option_indexes = await task_service.toggle_pending_user_question_multi_select_option(
                user_id=user_id,
                tool_use_id=parsed.tool_use_id,
                question_index=parsed.question_index,
                option_index=parsed.option_index if parsed.option_index is not None else -1,
            )
            if callback.message is not None and ok and prompt is not None:
                try:
                    await callback.message.edit_reply_markup(  # type: ignore[union-attr]
                        reply_markup=build_user_question_keyboard(
                            prompt,
                            selected_option_indexes=selected_option_indexes,
                        )
                    )
                except Exception:
                    logger.exception(
                        "failed to refresh multi-select inline keyboard",
                        extra={
                            "user_id": user_id,
                            "tool_use_id": parsed.tool_use_id,
                            "question_index": parsed.question_index,
                        },
                    )
            await callback.answer(text, show_alert=not ok)
            return

        if parsed.action == _QUESTION_CALLBACK_ACTION_SUBMIT:
            ok, text, next_prompt = await task_service.submit_pending_user_question_multi_select(
                user_id=user_id,
                tool_use_id=parsed.tool_use_id,
                question_index=parsed.question_index,
            )
            if callback.message is not None and ok:
                try:
                    await callback.message.edit_reply_markup(reply_markup=None)  # type: ignore[union-attr]
                except Exception:
                    logger.exception(
                        "failed to clear multi-select inline keyboard",
                        extra={
                            "user_id": user_id,
                            "tool_use_id": parsed.tool_use_id,
                            "question_index": parsed.question_index,
                        },
                    )
                await callback.message.answer(text)
                await _acknowledge_and_send_next_prompt(
                    message=callback.message,  # type: ignore[arg-type]
                    task_service=task_service,
                    user_id=user_id,
                    next_prompt=next_prompt,
                )
            elif callback.message is not None and not ok:
                await callback.message.answer(f"选择失败: {text}")
            await callback.answer(text, show_alert=not ok)
            return

        ok, text, next_prompt = await task_service.answer_pending_user_question_option(
            user_id=user_id,
            tool_use_id=parsed.tool_use_id,
            question_index=parsed.question_index,
            option_index=parsed.option_index if parsed.option_index is not None else -1,
        )
        if callback.message is not None:
            if ok:
                try:
                    await callback.message.edit_reply_markup(reply_markup=None)  # type: ignore[union-attr]
                except Exception:
                    logger.exception(
                        "failed to clear user question inline keyboard",
                        extra={
                            "user_id": user_id,
                            "tool_use_id": parsed.tool_use_id,
                            "question_index": parsed.question_index,
                        },
                    )
                await callback.message.answer(text)
                await _acknowledge_and_send_next_prompt(
                    message=callback.message,  # type: ignore[arg-type]
                    task_service=task_service,
                    user_id=user_id,
                    next_prompt=next_prompt,
                )
            else:
                await callback.message.answer(f"选择失败: {text}")
        await callback.answer(text, show_alert=not ok)
