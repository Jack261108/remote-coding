from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeGuard

from aiogram import F
from aiogram.types import CallbackQuery, InaccessibleMessage, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.bot.handlers.callback_utils import parse_callback_prefix, safe_edit_keyboard
from app.bot.handlers.user_utils import extract_user_id
from app.bot.presenters.structured_reply_presenter import UserQuestionOutput, build_user_question_prompt
from app.domain.user_question_models import UserQuestionPrompt
from app.infra.user_question_callbacks import (
    build_user_question_callback_data as build_opaque_callback_data,
)
from app.infra.user_question_callbacks import (
    parse_user_question_callback_token,
)
from app.services.task_service import TaskService
from app.services.user_question_callback_registry import (
    QuestionCallbackTokens,
    UserQuestionCallbackAction,
    UserQuestionCallbackRegistry,
    UserQuestionCallbackResolved,
    UserQuestionCallbackSnapshot,
    UserQuestionCallbackUnauthorized,
)

_QUESTION_CALLBACK_PREFIX = "ask"
_QUESTION_CALLBACK_ACTION_TOGGLE = "toggle"
_QUESTION_CALLBACK_ACTION_SUBMIT = "submit"


@dataclass(frozen=True)
class ParsedUserQuestionCallback:
    action: str
    tool_use_id: str
    question_index: int
    option_index: int | None = None


def _is_accessible_message(message: object) -> TypeGuard[Message]:
    if message is None or isinstance(message, InaccessibleMessage):
        return False
    return callable(getattr(message, "answer", None)) and callable(getattr(message, "edit_reply_markup", None))


def build_legacy_select_callback_data(*, tool_use_id: str, question_index: int, option_index: int) -> str:
    return f"{_QUESTION_CALLBACK_PREFIX}:{tool_use_id}:{question_index}:{option_index}"


def build_multi_select_toggle_callback_data(*, tool_use_id: str, question_index: int, option_index: int) -> str:
    return f"{_QUESTION_CALLBACK_PREFIX}:{_QUESTION_CALLBACK_ACTION_TOGGLE}:{tool_use_id}:{question_index}:{option_index}"


def build_multi_select_submit_callback_data(*, tool_use_id: str, question_index: int) -> str:
    return f"{_QUESTION_CALLBACK_PREFIX}:{_QUESTION_CALLBACK_ACTION_SUBMIT}:{tool_use_id}:{question_index}"


def parse_user_question_callback_data(data: str | tuple[str, ...] | None) -> ParsedUserQuestionCallback | None:
    if not data:
        return None
    raw_data = ":".join(data) if isinstance(data, tuple) else data
    toggle_parts = parse_callback_prefix(raw_data, 5, _QUESTION_CALLBACK_PREFIX)
    if toggle_parts is not None and toggle_parts[1] == _QUESTION_CALLBACK_ACTION_TOGGLE:
        _, _, tool_use_id, question_index_text, option_index_text = toggle_parts
        try:
            return ParsedUserQuestionCallback(
                action=_QUESTION_CALLBACK_ACTION_TOGGLE,
                tool_use_id=tool_use_id,
                question_index=int(question_index_text),
                option_index=int(option_index_text),
            )
        except ValueError:
            return None
    submit_parts = parse_callback_prefix(raw_data, 4, _QUESTION_CALLBACK_PREFIX)
    if submit_parts is not None and submit_parts[1] == _QUESTION_CALLBACK_ACTION_SUBMIT:
        _, _, tool_use_id, question_index_text = submit_parts
        try:
            return ParsedUserQuestionCallback(
                action=_QUESTION_CALLBACK_ACTION_SUBMIT,
                tool_use_id=tool_use_id,
                question_index=int(question_index_text),
            )
        except ValueError:
            return None
    select_parts = parse_callback_prefix(raw_data, 4, _QUESTION_CALLBACK_PREFIX)
    if select_parts is not None:
        _, tool_use_id, question_index_text, option_index_text = select_parts
        # 验证 tool_use_id 格式（至少应为非空字符串）
        if not tool_use_id:
            return None
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
    tokens: QuestionCallbackTokens | None = None,
) -> InlineKeyboardMarkup | None:
    prompt = question.question if isinstance(question, UserQuestionOutput) else question
    if not prompt.options:
        return None
    selected = selected_option_indexes or frozenset()
    rows = []
    for index, option in enumerate(prompt.options):
        label = option.label
        if prompt.multi_select:
            label = f"{'☑' if index in selected else '☐'} {label}"
            callback_data = _build_button_callback_data(
                token=tokens.toggle_tokens[index] if tokens and index < len(tokens.toggle_tokens) else None,
                legacy_builder=build_multi_select_toggle_callback_data,
                prompt=prompt,
                option_index=index,
            )
        else:
            callback_data = _build_button_callback_data(
                token=tokens.select_tokens[index] if tokens and index < len(tokens.select_tokens) else None,
                legacy_builder=build_legacy_select_callback_data,
                prompt=prompt,
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
        submit_token = tokens.submit_token if tokens else None
        if submit_token is not None:
            submit_data = build_opaque_callback_data(prefix=_QUESTION_CALLBACK_PREFIX, token=submit_token)
        else:
            submit_data = build_multi_select_submit_callback_data(
                tool_use_id=prompt.tool_use_id,
                question_index=prompt.question_index,
            )
        rows.append(
            [
                InlineKeyboardButton(
                    text="提交选择",
                    callback_data=submit_data,
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _build_button_callback_data(
    *,
    token: str | None,
    legacy_builder: Callable[..., str],
    prompt: UserQuestionPrompt,
    option_index: int,
) -> str:
    if token is not None:
        return build_opaque_callback_data(prefix=_QUESTION_CALLBACK_PREFIX, token=token)
    return legacy_builder(
        tool_use_id=prompt.tool_use_id,
        question_index=prompt.question_index,
        option_index=option_index,
    )


async def _acknowledge_and_send_next_prompt(
    *,
    message: Message,
    task_service: TaskService,
    user_id: int,
    next_prompt: UserQuestionPrompt | None,
) -> None:
    if next_prompt is None:
        return
    tokens = await task_service.register_question_callback_tokens(user_id=user_id, prompt=next_prompt)
    await message.answer(
        build_user_question_prompt(next_prompt),
        reply_markup=build_user_question_keyboard(next_prompt, tokens=tokens),
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
        await _acknowledge_and_send_next_prompt(
            message=message,
            task_service=task_service,
            user_id=user_id,
            next_prompt=next_prompt,
        )
    else:
        await message.answer(f"回复失败: {response_text}")
    return True


def register_user_question_handlers(
    router,
    *,
    task_service: TaskService,
    callback_registry: UserQuestionCallbackRegistry | None,
):
    @router.callback_query(F.data.startswith(f"{_QUESTION_CALLBACK_PREFIX}:"))
    async def callback_user_question(callback: CallbackQuery, callback_parts: tuple[str, ...]) -> None:
        user_id = extract_user_id(callback)

        # Prefer opaque-token dispatch; fall back to legacy inline identity for
        # any buttons issued before tokenisation (degenerate managed cards) or
        # when no registry is wired in (legacy test wiring).
        token = parse_user_question_callback_token(callback_parts, prefix=_QUESTION_CALLBACK_PREFIX)
        if token is not None and callback_registry is not None:
            resolved = await callback_registry.resolve(token, user_id=user_id)
            if isinstance(resolved, UserQuestionCallbackResolved):
                snapshot = resolved.snapshot
                await _dispatch_resolved_callback(
                    callback=callback,
                    callback_message=callback.message if _is_accessible_message(callback.message) else None,
                    user_id=user_id,
                    task_service=task_service,
                    snapshot=snapshot,
                )
                return
            if isinstance(resolved, UserQuestionCallbackUnauthorized):
                await callback.answer("无权操作该问题", show_alert=True)
                return
            # NotFound: fall through to legacy parse (token may belong to a
            # pre-tokenisation card still carrying inline identity).

        parsed = parse_user_question_callback_data(callback_parts)
        if parsed is None:
            await callback.answer("无效的选择操作", show_alert=True)
            return

        callback_message = callback.message if _is_accessible_message(callback.message) else None

        if parsed.action == _QUESTION_CALLBACK_ACTION_TOGGLE:
            ok, text, prompt, selected_option_indexes = await task_service.toggle_pending_user_question_multi_select_option(
                user_id=user_id,
                tool_use_id=parsed.tool_use_id,
                question_index=parsed.question_index,
                option_index=parsed.option_index if parsed.option_index is not None else -1,
            )
            if callback_message is not None and ok and prompt is not None:
                tokens = await task_service.register_question_callback_tokens(user_id=user_id, prompt=prompt)
                await safe_edit_keyboard(
                    callback_message,
                    build_user_question_keyboard(prompt, selected_option_indexes=selected_option_indexes, tokens=tokens),
                    "refresh multi-select inline keyboard",
                )
            await callback.answer(text, show_alert=not ok)
            return

        if parsed.action == _QUESTION_CALLBACK_ACTION_SUBMIT:
            ok, text, next_prompt = await task_service.submit_pending_user_question_multi_select(
                user_id=user_id,
                tool_use_id=parsed.tool_use_id,
                question_index=parsed.question_index,
            )
            if callback_message is not None and ok:
                await safe_edit_keyboard(callback_message, None, "clear multi-select inline keyboard")
                await callback_message.answer(text)
                await _acknowledge_and_send_next_prompt(
                    message=callback_message,
                    task_service=task_service,
                    user_id=user_id,
                    next_prompt=next_prompt,
                )
            elif callback_message is not None and not ok:
                await callback_message.answer(f"选择失败: {text}")
            await callback.answer(text, show_alert=not ok)
            return

        ok, text, next_prompt = await task_service.answer_pending_user_question_option(
            user_id=user_id,
            tool_use_id=parsed.tool_use_id,
            question_index=parsed.question_index,
            option_index=parsed.option_index if parsed.option_index is not None else -1,
        )
        if callback_message is not None:
            if ok:
                await safe_edit_keyboard(callback_message, None, "clear user question inline keyboard")
                await callback_message.answer(text)
                await _acknowledge_and_send_next_prompt(
                    message=callback_message,
                    task_service=task_service,
                    user_id=user_id,
                    next_prompt=next_prompt,
                )
            else:
                await callback_message.answer(f"选择失败: {text}")
        await callback.answer(text, show_alert=not ok)


async def _dispatch_resolved_callback(
    *,
    callback: CallbackQuery,
    callback_message: Message | None,
    user_id: int,
    task_service: TaskService,
    snapshot: UserQuestionCallbackSnapshot,
) -> None:
    action = snapshot.action
    if action == UserQuestionCallbackAction.TOGGLE:
        ok, text, prompt, selected_option_indexes = await task_service.toggle_pending_user_question_multi_select_option(
            user_id=user_id,
            tool_use_id=snapshot.tool_use_id,
            question_index=snapshot.question_index,
            option_index=snapshot.option_index if snapshot.option_index is not None else -1,
        )
        if callback_message is not None and ok and prompt is not None:
            tokens = await task_service.register_question_callback_tokens(user_id=user_id, prompt=prompt)
            await safe_edit_keyboard(
                callback_message,
                build_user_question_keyboard(prompt, selected_option_indexes=selected_option_indexes, tokens=tokens),
                "refresh multi-select inline keyboard",
            )
        await callback.answer(text, show_alert=not ok)
        return

    if action == UserQuestionCallbackAction.SUBMIT:
        ok, text, next_prompt = await task_service.submit_pending_user_question_multi_select(
            user_id=user_id,
            tool_use_id=snapshot.tool_use_id,
            question_index=snapshot.question_index,
        )
        if callback_message is not None and ok:
            await safe_edit_keyboard(callback_message, None, "clear multi-select inline keyboard")
            await callback_message.answer(text)
            await _acknowledge_and_send_next_prompt(
                message=callback_message,
                task_service=task_service,
                user_id=user_id,
                next_prompt=next_prompt,
            )
        elif callback_message is not None and not ok:
            await callback_message.answer(f"选择失败: {text}")
        await callback.answer(text, show_alert=not ok)
        return

    ok, text, next_prompt = await task_service.answer_pending_user_question_option(
        user_id=user_id,
        tool_use_id=snapshot.tool_use_id,
        question_index=snapshot.question_index,
        option_index=snapshot.option_index if snapshot.option_index is not None else -1,
    )
    if callback_message is not None:
        if ok:
            await safe_edit_keyboard(callback_message, None, "clear user question inline keyboard")
            await callback_message.answer(text)
            await _acknowledge_and_send_next_prompt(
                message=callback_message,
                task_service=task_service,
                user_id=user_id,
                next_prompt=next_prompt,
            )
        else:
            await callback_message.answer(f"选择失败: {text}")
    await callback.answer(text, show_alert=not ok)
