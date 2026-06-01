"""Property-based tests for environment variable precedence over Env_File.

Feature: homebrew-packaging, Property 4: 进程环境变量优先于 Env_File

**Validates: Requirements 5.2**
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import hypothesis.strategies as st
from hypothesis import given, settings

from app.config.loader import load_settings


_token_st = st.text(min_size=1, max_size=50, alphabet="abcdefghijklmnopqrstuvwxyz0123456789")
_ids_st = st.integers(min_value=1, max_value=999999).map(str)


@settings(max_examples=100)
@given(
    env_token=_token_st,
    dotenv_token=_token_st,
    env_id=_ids_st,
    dotenv_id=_ids_st,
)
def test_process_env_overrides_dotenv_for_shared_keys(
    env_token: str,
    dotenv_token: str,
    env_id: str,
    dotenv_id: str,
) -> None:
    """Process env vars take precedence over .env file for overlapping keys."""
    # Build .env with required vars set to dotenv values
    dotenv_lines = [
        f"TG_BOT_TOKEN={dotenv_token}",
        f"TG_ALLOWED_USER_IDS={dotenv_id}",
    ]

    with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
        f.write("\n".join(dotenv_lines) + "\n")
        env_file_path = f.name

    old_token = os.environ.get("TG_BOT_TOKEN")
    old_ids = os.environ.get("TG_ALLOWED_USER_IDS")
    try:
        # Set process env vars to different values
        os.environ["TG_BOT_TOKEN"] = env_token
        os.environ["TG_ALLOWED_USER_IDS"] = env_id

        settings = load_settings(env_file_path)
        # Process env vars must win over .env file values
        assert settings.tg_bot_token == env_token
        assert settings.tg_allowed_user_ids == [int(env_id)]
    finally:
        if old_token is None:
            os.environ.pop("TG_BOT_TOKEN", None)
        else:
            os.environ["TG_BOT_TOKEN"] = old_token
        if old_ids is None:
            os.environ.pop("TG_ALLOWED_USER_IDS", None)
        else:
            os.environ["TG_ALLOWED_USER_IDS"] = old_ids
        Path(env_file_path).unlink(missing_ok=True)
