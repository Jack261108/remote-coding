from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, Any

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


DEFAULT_ALLOWED_EXTENSIONS: list[str] = [
    ".txt",
    ".md",
    ".py",
    ".js",
    ".ts",
    ".java",
    ".go",
    ".rs",
    ".c",
    ".cpp",
    ".h",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".xml",
    ".html",
    ".css",
    ".sql",
    ".sh",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".pdf",
    ".csv",
    ".log",
]


def is_workdir_allowed(workdir: str, allowed_workdirs: Sequence[str]) -> bool:
    try:
        target = Path(workdir).resolve()
        for allowed in allowed_workdirs:
            allowed_path = Path(allowed).resolve()
            if target == allowed_path or allowed_path in target.parents:
                return True
    except (OSError, ValueError):
        return False
    return False


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    tg_bot_token: str = Field(..., alias="TG_BOT_TOKEN")
    tg_allowed_user_ids: Annotated[list[int], NoDecode] = Field(..., alias="TG_ALLOWED_USER_IDS")
    tg_proxy_url: str | None = Field(None, alias="TG_PROXY_URL")
    tg_request_timeout_sec: int = Field(30, alias="TG_REQUEST_TIMEOUT_SEC")
    tg_polling_retry_delay_sec: int = Field(5, alias="TG_POLLING_RETRY_DELAY_SEC")

    default_provider: str = Field("claude_code", alias="DEFAULT_PROVIDER")
    default_timeout_sec: int = Field(600, alias="DEFAULT_TIMEOUT_SEC")
    max_concurrent_tasks: int = Field(2, alias="MAX_CONCURRENT_TASKS")
    claude_tmux_mode: bool = Field(False, alias="CLAUDE_TMUX_MODE")
    tmux_bin: str = Field("tmux", alias="TMUX_BIN")
    tmux_data_dir: str = Field("/tmp/tg-cli-gateway", alias="TMUX_DATA_DIR")

    claude_cli_bin: str = Field("claude", alias="CLAUDE_CLI_BIN")
    claude_config_dir: str | None = Field(None, alias="CLAUDE_CONFIG_DIR")
    claude_hook_socket_path: str = Field("/tmp/remote-coding-claude.sock", alias="CLAUDE_HOOK_SOCKET_PATH")
    claude_install_hooks: bool = Field(True, alias="CLAUDE_INSTALL_HOOKS")
    claude_hook_max_message_bytes: int = Field(1_048_576, alias="CLAUDE_HOOK_MAX_MESSAGE_BYTES")
    claude_hook_pending_permission_ttl_sec: int = Field(600, alias="CLAUDE_HOOK_PENDING_PERMISSION_TTL_SEC")
    claude_hook_max_pending_permissions: int = Field(64, alias="CLAUDE_HOOK_MAX_PENDING_PERMISSIONS")
    claude_jsonl_sync_debounce_ms: int = Field(100, alias="CLAUDE_JSONL_SYNC_DEBOUNCE_MS")
    claude_periodic_recheck_ms: int = Field(500, alias="CLAUDE_PERIODIC_RECHECK_MS")
    codex_cli_bin: str = Field("codex", alias="CODEX_CLI_BIN")
    gemini_cli_bin: str = Field("gemini", alias="GEMINI_CLI_BIN")

    allowed_workdirs: Annotated[list[str], NoDecode] = Field(default_factory=lambda: [str(Path.cwd())], alias="ALLOWED_WORKDIRS")

    rate_limit_max_requests: int = Field(6, alias="RATE_LIMIT_MAX_REQUESTS")
    rate_limit_window_sec: int = Field(20, alias="RATE_LIMIT_WINDOW_SEC")

    chunk_size: int = Field(3800, alias="CHUNK_SIZE")
    chunk_flush_interval_sec: float = Field(1.0, alias="CHUNK_FLUSH_INTERVAL_SEC")

    task_output_char_limit: int = Field(120_000, alias="TASK_OUTPUT_CHAR_LIMIT")

    session_health_check_interval_sec: float = Field(30.0, alias="SESSION_HEALTH_CHECK_INTERVAL_SEC")

    # File upload settings
    upload_max_file_size_mb: int = Field(20, alias="UPLOAD_MAX_FILE_SIZE_MB")
    allowed_file_extensions: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: list(DEFAULT_ALLOWED_EXTENSIONS),
        alias="ALLOWED_FILE_EXTENSIONS",
    )
    upload_expiry_hours: int = Field(24, alias="UPLOAD_EXPIRY_HOURS")
    upload_cleanup_interval_min: int = Field(60, alias="UPLOAD_CLEANUP_INTERVAL_MIN")

    # External session settings
    external_session_stale_timeout_sec: float = Field(600.0, alias="EXTERNAL_SESSION_STALE_TIMEOUT_SEC")
    push_notification_retry_count: int = Field(1, alias="PUSH_NOTIFICATION_RETRY_COUNT")

    # Export settings
    auto_export_threshold_chars: int = Field(4096, alias="AUTO_EXPORT_THRESHOLD_CHARS")
    zip_max_size_mb: int = Field(50, alias="ZIP_MAX_SIZE_MB")

    @field_validator("tg_allowed_user_ids", mode="before")
    @classmethod
    def parse_user_ids(cls, value: Any) -> list[int]:
        if isinstance(value, list):
            if any(str(x).strip() == "*" for x in value):
                return []
            items = [int(x) for x in value]
        elif isinstance(value, str):
            text = value.strip()
            if text == "*":
                return []
            parts = [x.strip() for x in value.split(",") if x.strip()]
            items = [int(x) for x in parts]
        else:
            raise ValueError("TG_ALLOWED_USER_IDS 格式错误，需为逗号分隔数字或 *")

        if not items:
            raise ValueError("TG_ALLOWED_USER_IDS 不能为空（或使用 * 代表允许所有用户）")
        return items

    @field_validator("tg_proxy_url", "claude_config_dir", mode="before")
    @classmethod
    def parse_optional_text(cls, value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @field_validator("allowed_workdirs", mode="before")
    @classmethod
    def parse_workdirs(cls, value: Any) -> list[str]:
        if isinstance(value, list):
            dirs = [str(Path(x).resolve()) for x in value if str(x).strip()]
            if not dirs:
                raise ValueError("ALLOWED_WORKDIRS 不能为空")
            return dirs
        if isinstance(value, str):
            dirs = [str(Path(x.strip()).resolve()) for x in value.split(",") if x.strip()]
            if not dirs:
                raise ValueError("ALLOWED_WORKDIRS 不能为空")
            return dirs
        raise ValueError("ALLOWED_WORKDIRS 格式错误，需为逗号分隔路径")

    @field_validator("allowed_file_extensions", mode="before")
    @classmethod
    def parse_file_extensions(cls, value: Any) -> list[str]:
        if isinstance(value, list):
            return [ext.strip().lower() for ext in value if str(ext).strip()]
        if isinstance(value, str):
            return [ext.strip().lower() for ext in value.split(",") if ext.strip()]
        raise ValueError("ALLOWED_FILE_EXTENSIONS 格式错误，需为逗号分隔扩展名")

    @field_validator("claude_tmux_mode", "claude_install_hooks", mode="before")
    @classmethod
    def parse_bool_flag(cls, value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            text = value.strip().lower()
            if text in {"1", "true", "yes", "on"}:
                return True
            if text in {"0", "false", "no", "off", ""}:
                return False
        if isinstance(value, int):
            return value != 0
        raise ValueError("布尔配置格式错误，支持 true/false")

    @field_validator("default_timeout_sec")
    @classmethod
    def validate_timeout(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("DEFAULT_TIMEOUT_SEC 必须大于 0")
        return value

    @field_validator("max_concurrent_tasks")
    @classmethod
    def validate_concurrency(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("MAX_CONCURRENT_TASKS 必须大于 0")
        return value

    @field_validator(
        "rate_limit_max_requests",
        "rate_limit_window_sec",
        "chunk_size",
        "task_output_char_limit",
        "tg_request_timeout_sec",
        "tg_polling_retry_delay_sec",
        "claude_hook_max_message_bytes",
        "claude_hook_pending_permission_ttl_sec",
        "claude_hook_max_pending_permissions",
        "claude_jsonl_sync_debounce_ms",
        "claude_periodic_recheck_ms",
        "upload_max_file_size_mb",
        "upload_expiry_hours",
        "upload_cleanup_interval_min",
        "auto_export_threshold_chars",
        "zip_max_size_mb",
        "push_notification_retry_count",
    )
    @classmethod
    def validate_positive_int(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("配置值必须大于 0")
        return value

    @property
    def allow_all_users(self) -> bool:
        return len(self.tg_allowed_user_ids) == 0

    @property
    def allowed_user_id_set(self) -> set[int]:
        return set(self.tg_allowed_user_ids)

    @property
    def default_workdir(self) -> str:
        return self.allowed_workdirs[0]
