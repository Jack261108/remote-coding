from __future__ import annotations

import asyncio

from hypothesis import given, settings
from hypothesis import strategies as st

from app.infra.user_question_callbacks import (
    build_user_question_callback_data,
    parse_user_question_callback_token,
)
from app.services.user_question_callback_registry import (
    UserQuestionCallbackAction,
    UserQuestionCallbackOrigin,
    UserQuestionCallbackRegistry,
    UserQuestionCallbackResolved,
)


@settings(max_examples=80, deadline=None)
@given(
    tool_use_id=st.text(min_size=1, max_size=512),
    session_id=st.text(min_size=1, max_size=256).filter(lambda value: bool(value.strip())),
    question_index=st.integers(min_value=0, max_value=1000),
    option_index=st.integers(min_value=0, max_value=1000),
    origin=st.sampled_from(list(UserQuestionCallbackOrigin)),
)
def test_opaque_callback_never_contains_identity_or_exceeds_limit(
    tool_use_id: str,
    session_id: str,
    question_index: int,
    option_index: int,
    origin: UserQuestionCallbackOrigin,
) -> None:
    async def scenario() -> None:
        registry = UserQuestionCallbackRegistry(
            ttl_sec=60,
            token_factory=lambda: "opaque_token_123",
        )
        token = await registry.register(
            owner_user_id=42,
            session_id=session_id,
            tool_use_id=tool_use_id,
            question_index=question_index,
            action=UserQuestionCallbackAction.SELECT,
            option_index=option_index,
            origin=origin,
        )
        prefix = "ext_uq" if origin is UserQuestionCallbackOrigin.EXTERNAL_TMUX else "ask"
        data = build_user_question_callback_data(prefix=prefix, token=token)
        assert len(data.encode("utf-8")) <= 64
        # callback_data 只允许是 '{prefix}:{token}'，绝不内嵌 identity 字段。
        # 用结构化解析验证：token 应能完整还原，identity 不应作为可解析片段出现。
        assert parse_user_question_callback_token(data, prefix=prefix) == token
        non_trivial_identity = next(
            (value for value in (tool_use_id, session_id) if len(value) > len(token)),
            None,
        )
        if non_trivial_identity is not None:
            assert non_trivial_identity not in data
        resolved = await registry.resolve(token, user_id=42)
        assert isinstance(resolved, UserQuestionCallbackResolved)
        assert resolved.snapshot.tool_use_id == tool_use_id
        assert resolved.snapshot.session_id == session_id

    asyncio.run(scenario())
