import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.bot.middleware.auth import AuthMiddleware
from app.bot.middleware.rate_limit import RateLimitMiddleware
from app.config.settings import Settings


class DummyCallbackQuery:
    def __init__(self, user_id: int | None = 1) -> None:
        self.from_user = SimpleNamespace(id=user_id) if user_id is not None else None
        self.answers: list[str] = []

    async def answer(self, text: str, show_alert: bool = False) -> None:
        self.answers.append(text)


class SlowAnswerCallbackQuery(DummyCallbackQuery):
    def __init__(self, user_id: int | None = 1) -> None:
        super().__init__(user_id)
        self.answer_started = asyncio.Event()
        self.release_answer = asyncio.Event()

    async def answer(self, text: str, show_alert: bool = False) -> None:
        self.answer_started.set()
        await self.release_answer.wait()
        await super().answer(text, show_alert)


async def _passing_handler(event, data):
    data["called"] = True
    return "ok"


def test_settings_allow_all_users_star() -> None:
    settings = Settings.model_validate(
        {
            "TG_BOT_TOKEN": "token",
            "TG_ALLOWED_USER_IDS": "*",
            "DEFAULT_PROVIDER": "claude_code",
            "DEFAULT_TIMEOUT_SEC": 10,
            "MAX_CONCURRENT_TASKS": 1,
            "CLAUDE_TMUX_MODE": False,
            "CLAUDE_CLI_BIN": "claude",
            "CODEX_CLI_BIN": "codex",
            "GEMINI_CLI_BIN": "gemini",
            "ALLOWED_WORKDIRS": "/tmp",
        }
    )

    assert settings.allow_all_users is True
    assert settings.allowed_user_id_set == set()
    assert settings.effective_unbound_permission_notify_user_id_set == set()


def test_settings_parses_unbound_permission_notify_user_ids() -> None:
    settings = Settings.model_validate({**_BASE_PAYLOAD, "UNBOUND_PERMISSION_NOTIFY_USER_IDS": "1"})

    assert settings.unbound_permission_notify_user_ids == [1]
    assert settings.unbound_permission_notify_user_id_set == {1}
    assert settings.effective_unbound_permission_notify_user_id_set == {1}


def test_settings_rejects_unbound_permission_notify_star() -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({**_BASE_PAYLOAD, "UNBOUND_PERMISSION_NOTIFY_USER_IDS": "*"})


def test_settings_rejects_unbound_permission_notify_user_outside_whitelist() -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({**_BASE_PAYLOAD, "UNBOUND_PERMISSION_NOTIFY_USER_IDS": "2"})


def test_auth_middleware_allow_all_flag() -> None:
    middleware = AuthMiddleware(set(), allow_all_users=True)
    assert middleware is not None


def test_settings_parse_claude_hook_fields() -> None:
    settings = Settings.model_validate(
        {
            "TG_BOT_TOKEN": "token",
            "TG_ALLOWED_USER_IDS": "1",
            "DEFAULT_PROVIDER": "claude_code",
            "DEFAULT_TIMEOUT_SEC": 10,
            "MAX_CONCURRENT_TASKS": 1,
            "CLAUDE_TMUX_MODE": False,
            "CLAUDE_CLI_BIN": "claude",
            "CLAUDE_CONFIG_DIR": " ~/.config/claude ",
            "CLAUDE_HOOK_SOCKET_PATH": "/tmp/remote-coding.sock",
            "CLAUDE_INSTALL_HOOKS": "true",
            "CLAUDE_HOOK_MAX_MESSAGE_BYTES": 2048,
            "CLAUDE_HOOK_PENDING_PERMISSION_TTL_SEC": 30,
            "CLAUDE_HOOK_MAX_PENDING_PERMISSIONS": 4,
            "CLAUDE_JSONL_SYNC_DEBOUNCE_MS": 250,
            "CLAUDE_PERIODIC_RECHECK_MS": 750,
            "CODEX_CLI_BIN": "codex",
            "GEMINI_CLI_BIN": "gemini",
            "ALLOWED_WORKDIRS": "/tmp",
        }
    )

    assert settings.claude_config_dir == "~/.config/claude"
    assert settings.claude_hook_socket_path == "/tmp/remote-coding.sock"
    assert settings.claude_install_hooks is True
    assert settings.claude_hook_max_message_bytes == 2048
    assert settings.claude_hook_pending_permission_ttl_sec == 30
    assert settings.claude_hook_max_pending_permissions == 4
    assert settings.claude_jsonl_sync_debounce_ms == 250
    assert settings.claude_periodic_recheck_ms == 750


def test_settings_rejects_non_positive_claude_hook_limits() -> None:
    base_payload = {
        "TG_BOT_TOKEN": "token",
        "TG_ALLOWED_USER_IDS": "1",
        "DEFAULT_PROVIDER": "claude_code",
        "DEFAULT_TIMEOUT_SEC": 10,
        "MAX_CONCURRENT_TASKS": 1,
        "CLAUDE_TMUX_MODE": False,
        "CLAUDE_CLI_BIN": "claude",
        "CODEX_CLI_BIN": "codex",
        "GEMINI_CLI_BIN": "gemini",
        "ALLOWED_WORKDIRS": "/tmp",
    }

    for field in (
        "CLAUDE_HOOK_MAX_MESSAGE_BYTES",
        "CLAUDE_HOOK_PENDING_PERMISSION_TTL_SEC",
        "CLAUDE_HOOK_MAX_PENDING_PERMISSIONS",
    ):
        with pytest.raises(ValidationError):
            Settings.model_validate({**base_payload, field: 0})


def test_env_example_matches_supported_claude_settings() -> None:
    content = (Path(__file__).resolve().parents[1] / "deploy" / "env" / ".env.example").read_text(encoding="utf-8")

    assert "BRIDGE_WS_" not in content
    assert "CLAUDE_CONFIG_DIR=" in content
    assert "CLAUDE_HOOK_SOCKET_PATH=/tmp/remote-coding-claude.sock" in content
    assert "CLAUDE_INSTALL_HOOKS=true" in content
    assert "CLAUDE_HOOK_MAX_MESSAGE_BYTES=1048576" in content
    assert "CLAUDE_HOOK_PENDING_PERMISSION_TTL_SEC=600" in content
    assert "CLAUDE_HOOK_MAX_PENDING_PERMISSIONS=64" in content
    assert "CLAUDE_JSONL_SYNC_DEBOUNCE_MS=100" in content
    assert "CLAUDE_PERIODIC_RECHECK_MS=500" in content


_BASE_PAYLOAD = {
    "TG_BOT_TOKEN": "token",
    "TG_ALLOWED_USER_IDS": "1",
    "DEFAULT_PROVIDER": "claude_code",
    "DEFAULT_TIMEOUT_SEC": 10,
    "MAX_CONCURRENT_TASKS": 1,
    "CLAUDE_TMUX_MODE": False,
    "CLAUDE_CLI_BIN": "claude",
    "CODEX_CLI_BIN": "codex",
    "GEMINI_CLI_BIN": "gemini",
    "ALLOWED_WORKDIRS": "/tmp",
}


def test_settings_new_fields_defaults() -> None:
    settings = Settings.model_validate(_BASE_PAYLOAD)
    assert settings.task_store_ttl_hours == 168
    assert settings.task_store_max_records == 1000
    assert settings.rate_limit_bucket_ttl_sec is None
    assert settings.rate_limit_bucket_cleanup_interval_sec == 60
    assert settings.rate_limit_bucket_cleanup_batch_size == 50
    assert settings.permission_lock_ttl_sec is None
    assert settings.session_lock_ttl_sec == 3600
    assert settings.lock_cleanup_interval_sec == 60
    assert settings.lock_cleanup_batch_size == 50
    assert settings.upload_queue_max_files_per_user == 5
    assert settings.upload_queue_max_bytes_per_user is None
    assert settings.effective_upload_queue_max_bytes_per_user == 5 * 20 * 1024 * 1024
    assert settings.unbound_permission_notify_user_ids == []
    assert settings.effective_unbound_permission_notify_user_id_set == {1}


def test_settings_derived_defaults() -> None:
    settings = Settings.model_validate(_BASE_PAYLOAD)
    assert settings.effective_rate_limit_bucket_ttl_sec == settings.rate_limit_window_sec
    assert settings.effective_permission_lock_ttl_sec == settings.claude_hook_pending_permission_ttl_sec


def test_settings_treats_blank_optional_cleanup_fields_as_none() -> None:
    settings = Settings.model_validate({**_BASE_PAYLOAD, "RATE_LIMIT_BUCKET_TTL_SEC": "", "PERMISSION_LOCK_TTL_SEC": ""})

    assert settings.rate_limit_bucket_ttl_sec is None
    assert settings.permission_lock_ttl_sec is None


def test_settings_explicit_override_new_fields() -> None:
    payload = {
        **_BASE_PAYLOAD,
        "TASK_STORE_TTL_HOURS": 72,
        "TASK_STORE_MAX_RECORDS": 500,
        "RATE_LIMIT_BUCKET_TTL_SEC": 30,
        "RATE_LIMIT_BUCKET_CLEANUP_INTERVAL_SEC": 120,
        "RATE_LIMIT_BUCKET_CLEANUP_BATCH_SIZE": 10,
        "PERMISSION_LOCK_TTL_SEC": 120,
        "SESSION_LOCK_TTL_SEC": 7200,
        "LOCK_CLEANUP_INTERVAL_SEC": 30,
        "LOCK_CLEANUP_BATCH_SIZE": 25,
        "UPLOAD_QUEUE_MAX_FILES_PER_USER": 2,
        "UPLOAD_QUEUE_MAX_BYTES_PER_USER": 1234,
    }
    settings = Settings.model_validate(payload)
    assert settings.task_store_ttl_hours == 72
    assert settings.task_store_max_records == 500
    assert settings.rate_limit_bucket_ttl_sec == 30
    assert settings.rate_limit_bucket_cleanup_interval_sec == 120
    assert settings.rate_limit_bucket_cleanup_batch_size == 10
    assert settings.permission_lock_ttl_sec == 120
    assert settings.session_lock_ttl_sec == 7200
    assert settings.lock_cleanup_interval_sec == 30
    assert settings.lock_cleanup_batch_size == 25
    assert settings.upload_queue_max_files_per_user == 2
    assert settings.upload_queue_max_bytes_per_user == 1234
    assert settings.effective_upload_queue_max_bytes_per_user == 1234
    assert settings.effective_rate_limit_bucket_ttl_sec == 30
    assert settings.effective_permission_lock_ttl_sec == 120


def test_settings_allows_upload_queue_disabled_with_zero_files() -> None:
    settings = Settings.model_validate({**_BASE_PAYLOAD, "UPLOAD_QUEUE_MAX_FILES_PER_USER": 0})
    assert settings.upload_queue_max_files_per_user == 0
    assert settings.effective_upload_queue_max_bytes_per_user == 0


def test_settings_rejects_invalid_upload_queue_values() -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({**_BASE_PAYLOAD, "UPLOAD_QUEUE_MAX_FILES_PER_USER": -1})
    with pytest.raises(ValidationError):
        Settings.model_validate({**_BASE_PAYLOAD, "UPLOAD_QUEUE_MAX_BYTES_PER_USER": 0})


def test_settings_rejects_bucket_ttl_below_rate_limit_window() -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({**_BASE_PAYLOAD, "RATE_LIMIT_WINDOW_SEC": 20, "RATE_LIMIT_BUCKET_TTL_SEC": 10})


def test_settings_rejects_non_positive_new_fields() -> None:
    for field in (
        "TASK_STORE_TTL_HOURS",
        "TASK_STORE_MAX_RECORDS",
        "RATE_LIMIT_BUCKET_CLEANUP_INTERVAL_SEC",
        "RATE_LIMIT_BUCKET_CLEANUP_BATCH_SIZE",
        "SESSION_LOCK_TTL_SEC",
        "LOCK_CLEANUP_INTERVAL_SEC",
        "LOCK_CLEANUP_BATCH_SIZE",
    ):
        with pytest.raises(ValidationError):
            Settings.model_validate({**_BASE_PAYLOAD, field: 0})

    for field in ("RATE_LIMIT_BUCKET_TTL_SEC", "PERMISSION_LOCK_TTL_SEC"):
        with pytest.raises(ValidationError):
            Settings.model_validate({**_BASE_PAYLOAD, field: 0})
        with pytest.raises(ValidationError):
            Settings.model_validate({**_BASE_PAYLOAD, field: -1})


def test_env_example_contains_new_entries() -> None:
    content = (Path(__file__).resolve().parents[1] / "deploy" / "env" / ".env.example").read_text(encoding="utf-8")
    assert "RATE_LIMIT_BUCKET_TTL_SEC=" in content
    assert "RATE_LIMIT_BUCKET_CLEANUP_INTERVAL_SEC=60" in content
    assert "RATE_LIMIT_BUCKET_CLEANUP_BATCH_SIZE=50" in content
    assert "TASK_STORE_TTL_HOURS=168" in content
    assert "TASK_STORE_MAX_RECORDS=1000" in content
    assert "PERMISSION_LOCK_TTL_SEC=" in content
    assert "SESSION_LOCK_TTL_SEC=3600" in content
    assert "LOCK_CLEANUP_INTERVAL_SEC=60" in content
    assert "LOCK_CLEANUP_BATCH_SIZE=50" in content
    assert "UPLOAD_MAX_FILE_SIZE_MB=20" in content
    assert "UPLOAD_QUEUE_MAX_FILES_PER_USER=5" in content
    assert "UPLOAD_QUEUE_MAX_BYTES_PER_USER=" in content
    assert "UNBOUND_PERMISSION_NOTIFY_USER_IDS=" in content


@pytest.mark.asyncio
async def test_auth_middleware_rejects_callback_query_user() -> None:
    middleware = AuthMiddleware({1})
    callback = DummyCallbackQuery(user_id=2)
    data = {}

    result = await middleware(_passing_handler, callback, data)

    assert result is None
    assert data == {}
    assert callback.answers == ["未授权用户，拒绝访问。"]


@pytest.mark.asyncio
async def test_auth_middleware_allows_callback_query_user() -> None:
    middleware = AuthMiddleware({1})
    callback = DummyCallbackQuery(user_id=1)
    data = {}

    result = await middleware(_passing_handler, callback, data)

    assert result == "ok"
    assert data == {"called": True}
    assert callback.answers == []


@pytest.mark.asyncio
async def test_rate_limit_middleware_limits_callback_query_user() -> None:
    middleware = RateLimitMiddleware(limit=1, window_sec=20)
    callback = DummyCallbackQuery(user_id=1)

    first = await middleware(_passing_handler, callback, {})
    second = await middleware(_passing_handler, callback, {})

    assert first == "ok"
    assert second is None
    assert callback.answers == ["请求过于频繁，请稍后再试。"]


@pytest.mark.asyncio
async def test_rate_limit_slow_answer_does_not_block_other_users() -> None:
    middleware = RateLimitMiddleware(limit=1, window_sec=20)
    limited_user = SlowAnswerCallbackQuery(user_id=1)
    await middleware(_passing_handler, limited_user, {})

    limited_task = asyncio.create_task(middleware(_passing_handler, limited_user, {}))
    await limited_user.answer_started.wait()

    other_user = DummyCallbackQuery(user_id=2)
    other_task = asyncio.create_task(middleware(_passing_handler, other_user, {}))
    try:
        assert await asyncio.wait_for(other_task, timeout=0.1) == "ok"
    finally:
        limited_user.release_answer.set()
        await limited_task


@pytest.mark.asyncio
async def test_rate_limit_window_cleanup_allows_after_window() -> None:
    """After the rate-limit window expires, the user can pass again."""
    middleware = RateLimitMiddleware(limit=1, window_sec=5)
    callback = DummyCallbackQuery(user_id=1)

    first = await middleware(_passing_handler, callback, {})
    assert first == "ok"

    # Second request within window is blocked.
    second = await middleware(_passing_handler, callback, {})
    assert second is None

    # Advance time past the window by manipulating the bucket timestamps.
    bucket = middleware._buckets[1]
    bucket[0] -= 6.0  # make the timestamp 6 seconds ago

    third = await middleware(_passing_handler, callback, {})
    assert third == "ok"


def test_rate_limit_rejects_bucket_ttl_below_window() -> None:
    with pytest.raises(ValueError):
        RateLimitMiddleware(limit=2, window_sec=20, bucket_ttl_sec=10)


@pytest.mark.asyncio
async def test_rate_limit_bucket_ttl_defaults_to_window_sec() -> None:
    middleware = RateLimitMiddleware(limit=2, window_sec=1, cleanup_interval_sec=60, cleanup_batch_size=50)
    await middleware(_passing_handler, DummyCallbackQuery(user_id=1), {})

    middleware._buckets[1][-1] -= 2.0
    middleware._last_cleanup_ts = 0.0

    await middleware(_passing_handler, DummyCallbackQuery(user_id=2), {})

    assert 1 not in middleware._buckets


@pytest.mark.asyncio
async def test_global_cleanup_respects_interval_and_batch() -> None:
    """Global cleanup runs only after the interval and processes at most batch_size items."""
    middleware = RateLimitMiddleware(
        limit=1,
        window_sec=1,
        bucket_ttl_sec=1,
        cleanup_interval_sec=100,
        cleanup_batch_size=2,
    )

    # Create 4 buckets by sending requests from 4 users.
    for uid in range(1, 5):
        cb = DummyCallbackQuery(user_id=uid)
        await middleware(_passing_handler, cb, {})

    assert len(middleware._buckets) == 4
    assert len(middleware._cleanup_queue) == 4

    # Make all buckets stale (timestamps far in the past).
    for uid in range(1, 5):
        middleware._buckets[uid][-1] -= 200.0

    # Reset _last_cleanup_ts relative to the current loop time so cleanup triggers
    # even on fresh CI event loops whose monotonic time is still below the interval.
    middleware._last_cleanup_ts = asyncio.get_running_loop().time() - middleware._cleanup_interval_sec

    # Trigger a request from a fresh user to force global cleanup.
    # cleanup_batch_size=2, so at most 2 stale buckets are removed.
    cb_fresh = DummyCallbackQuery(user_id=99)
    await middleware(_passing_handler, cb_fresh, {})

    # At least 2 stale buckets should have been cleaned (batch_size=2).
    # user 99 also got a new bucket. Original 4 minus at least 2 cleaned = at most 2 + user 99.
    assert len(middleware._buckets) <= 3  # 4 originals - 2 cleaned + 1 new


@pytest.mark.asyncio
async def test_cleanup_queue_re_enqueue_after_bucket_recreation() -> None:
    """After a bucket is deleted, re-creating it puts it back in the cleanup queue."""
    middleware = RateLimitMiddleware(
        limit=2,
        window_sec=1,
        bucket_ttl_sec=1,
        cleanup_interval_sec=60,
        cleanup_batch_size=100,
    )

    cb = DummyCallbackQuery(user_id=42)
    await middleware(_passing_handler, cb, {})
    assert 42 in middleware._cleanup_queued

    # Make the bucket stale and reset _last_cleanup_ts to force cleanup on next request.
    middleware._buckets[42][-1] -= 200.0
    middleware._last_cleanup_ts = 0.0

    cb2 = DummyCallbackQuery(user_id=999)
    await middleware(_passing_handler, cb2, {})

    # Bucket 42 should have been cleaned away (stale for 200s > TTL 1s).
    assert 42 not in middleware._buckets
    assert 42 not in middleware._cleanup_queued

    # Re-create the bucket for user 42.
    await middleware(_passing_handler, cb, {})
    assert 42 in middleware._buckets
    assert 42 in middleware._cleanup_queued


def _container_payload(tmp_path: Path, **overrides: object) -> dict[str, object]:
    """Build a Settings payload valid for AppContainer construction."""
    base: dict[str, object] = {
        "TG_BOT_TOKEN": "123456:TESTTOKEN",
        "TG_ALLOWED_USER_IDS": "1",
        "DEFAULT_PROVIDER": "claude_code",
        "DEFAULT_TIMEOUT_SEC": 10,
        "MAX_CONCURRENT_TASKS": 1,
        "CLAUDE_TMUX_MODE": False,
        "CLAUDE_CLI_BIN": "claude",
        "CODEX_CLI_BIN": "codex",
        "GEMINI_CLI_BIN": "gemini",
        "TMUX_DATA_DIR": str(tmp_path),
        "CLAUDE_CONFIG_DIR": str(tmp_path / ".claude"),
        "CLAUDE_HOOK_SOCKET_PATH": str(tmp_path / "hook.sock"),
        "ALLOWED_WORKDIRS": str(tmp_path),
    }
    base.update(overrides)
    return base


def test_container_wiring_passes_settings_to_task_store(tmp_path: Path) -> None:
    """AppContainer passes TASK_STORE_TTL_HOURS and TASK_STORE_MAX_RECORDS to task_store."""
    from app.bootstrap import AppContainer

    settings = Settings.model_validate(_container_payload(tmp_path, TASK_STORE_TTL_HOURS=72, TASK_STORE_MAX_RECORDS=500))
    container = AppContainer(settings)

    assert container.task_store._max_records == 500
    assert container.task_store._ttl.total_seconds() == 72 * 3600
    assert container.upload_queue._max_files_per_user == settings.upload_queue_max_files_per_user
    assert container.upload_queue._max_bytes_per_user == settings.effective_upload_queue_max_bytes_per_user

    # wire() should succeed and register RateLimitMiddleware on dispatcher
    container.wire()

    # Verify RateLimitMiddleware is registered on dispatcher message middleware
    outer = container.dispatcher.message.middleware
    registered = [m for m in outer._middlewares if isinstance(m, RateLimitMiddleware)]
    assert len(registered) == 1
    rl = registered[0]
    assert rl._cleanup_interval_sec == settings.rate_limit_bucket_cleanup_interval_sec
    assert rl._cleanup_batch_size == settings.rate_limit_bucket_cleanup_batch_size
    assert rl._bucket_ttl_sec == settings.effective_rate_limit_bucket_ttl_sec


def test_container_wiring_task_store_defaults(tmp_path: Path) -> None:
    """AppContainer uses default settings when TASK_STORE_* fields are not overridden."""
    from app.bootstrap import AppContainer

    settings = Settings.model_validate(_container_payload(tmp_path))
    container = AppContainer(settings)

    assert container.task_store._max_records == 1000
    assert container.task_store._ttl.total_seconds() == 168 * 3600
