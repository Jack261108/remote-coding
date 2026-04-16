from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


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
        "claude_jsonl_sync_debounce_ms",
        "claude_periodic_recheck_ms",
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
