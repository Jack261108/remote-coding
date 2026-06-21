from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, Any

from pydantic import Field, field_validator, model_validator
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

    tg_bot_token: str = Field(..., alias="TG_BOT_TOKEN", min_length=1)
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

    # Tmux runner timing
    tmux_poll_interval_sec: float = Field(0.2, alias="TMUX_POLL_INTERVAL_SEC")
    tmux_enter_delay_sec: float = Field(0.2, alias="TMUX_ENTER_DELAY_SEC")
    tmux_partial_flush_sec: float = Field(0.5, alias="TMUX_PARTIAL_FLUSH_SEC")
    tmux_completion_grace_sec: float = Field(0.1, alias="TMUX_COMPLETION_GRACE_SEC")

    # UI timing
    structured_reply_pump_interval_sec: float = Field(0.05, alias="STRUCTURED_REPLY_PUMP_INTERVAL_SEC")
    spinner_initial_delay_sec: float = Field(3.0, alias="SPINNER_INITIAL_DELAY_SEC")
    spinner_interval_sec: float = Field(1.0, alias="SPINNER_INTERVAL_SEC")
    codex_cli_bin: str = Field("codex", alias="CODEX_CLI_BIN")
    gemini_cli_bin: str = Field("gemini", alias="GEMINI_CLI_BIN")

    allowed_workdirs: Annotated[list[str], NoDecode] = Field(default_factory=lambda: [str(Path.cwd())], alias="ALLOWED_WORKDIRS")
    admin_password: str | None = Field(None, alias="ADMIN_PASSWORD")

    rate_limit_max_requests: int = Field(6, alias="RATE_LIMIT_MAX_REQUESTS")
    rate_limit_window_sec: int = Field(20, alias="RATE_LIMIT_WINDOW_SEC")
    rate_limit_bucket_ttl_sec: int | None = Field(None, alias="RATE_LIMIT_BUCKET_TTL_SEC")
    rate_limit_bucket_cleanup_interval_sec: int = Field(60, alias="RATE_LIMIT_BUCKET_CLEANUP_INTERVAL_SEC")
    rate_limit_bucket_cleanup_batch_size: int = Field(50, alias="RATE_LIMIT_BUCKET_CLEANUP_BATCH_SIZE")

    # Task store settings
    task_store_ttl_hours: int = Field(168, alias="TASK_STORE_TTL_HOURS")
    task_store_max_records: int = Field(1000, alias="TASK_STORE_MAX_RECORDS")

    # Lock settings
    permission_lock_ttl_sec: int | None = Field(None, alias="PERMISSION_LOCK_TTL_SEC")
    session_lock_ttl_sec: int = Field(3600, alias="SESSION_LOCK_TTL_SEC")
    lock_cleanup_interval_sec: int = Field(60, alias="LOCK_CLEANUP_INTERVAL_SEC")
    lock_cleanup_batch_size: int = Field(50, alias="LOCK_CLEANUP_BATCH_SIZE")

    chunk_size: int = Field(3800, alias="CHUNK_SIZE")
    chunk_flush_interval_sec: float = Field(1.0, alias="CHUNK_FLUSH_INTERVAL_SEC")

    task_output_char_limit: int = Field(120_000, alias="TASK_OUTPUT_CHAR_LIMIT")

    session_health_check_interval_sec: float = Field(30.0, alias="SESSION_HEALTH_CHECK_INTERVAL_SEC")
    external_binding_idle_ttl_hours: int = Field(24, alias="EXTERNAL_BINDING_IDLE_TTL_HOURS", ge=1)
    external_binding_pid_liveness_enabled: bool = Field(True, alias="EXTERNAL_BINDING_PID_LIVENESS_ENABLED")

    # File upload settings
    upload_max_file_size_mb: int = Field(20, alias="UPLOAD_MAX_FILE_SIZE_MB")
    upload_queue_max_files_per_user: int = Field(5, alias="UPLOAD_QUEUE_MAX_FILES_PER_USER")
    upload_queue_max_bytes_per_user: int | None = Field(None, alias="UPLOAD_QUEUE_MAX_BYTES_PER_USER")
    upload_queue_ttl_sec: int = Field(3600, alias="UPLOAD_QUEUE_TTL_SEC")
    upload_queue_cleanup_interval_sec: int = Field(60, alias="UPLOAD_QUEUE_CLEANUP_INTERVAL_SEC")
    allowed_file_extensions: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: list(DEFAULT_ALLOWED_EXTENSIONS),
        alias="ALLOWED_FILE_EXTENSIONS",
    )
    upload_expiry_hours: int = Field(24, alias="UPLOAD_EXPIRY_HOURS")
    upload_cleanup_interval_min: int = Field(60, alias="UPLOAD_CLEANUP_INTERVAL_MIN")

    # External session settings
    external_session_stale_timeout_sec: float = Field(600.0, alias="EXTERNAL_SESSION_STALE_TIMEOUT_SEC")
    tombstone_ttl_sec: int = Field(3600, alias="TOMBSTONE_TTL_SEC")
    push_notification_retry_count: int = Field(1, alias="PUSH_NOTIFICATION_RETRY_COUNT")

    # Session cleanup settings
    session_cleanup_interval_sec: int = Field(3600, alias="SESSION_CLEANUP_INTERVAL_SEC")  # 1 hour
    session_cleanup_max_age_hours: int = Field(24, alias="SESSION_CLEANUP_MAX_AGE_HOURS")  # 24 hours

    # Auto file send settings
    auto_file_send_enabled: bool = Field(True, alias="AUTO_FILE_SEND_ENABLED")
    auto_file_send_extensions: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [
            ".png",
            ".jpg",
            ".jpeg",
            ".gif",
            ".webp",
            ".svg",
            ".pdf",
            ".docx",
            ".xlsx",
            ".csv",
            ".html",
            ".zip",
            ".tar.gz",
        ],
        alias="AUTO_FILE_SEND_EXTENSIONS",
    )

    # Export settings
    auto_export_threshold_chars: int = Field(4096, alias="AUTO_EXPORT_THRESHOLD_CHARS")
    zip_max_size_mb: int = Field(50, alias="ZIP_MAX_SIZE_MB")

    # Risk evaluation settings
    risk_eval_enabled: bool = Field(True, alias="RISK_EVAL_ENABLED")
    risk_eval_dangerous_commands: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [
            "rm -rf",
            "rm -r",
            "rm -f",
            "sudo rm",
            "dd ",
            "dd if=",
            "mkfs",
            "unlink",
            "shred",
            "git reset --hard",
            "git clean -fd",
            "git push --force",
            "git push -f",
            "DROP TABLE",
            "DELETE FROM",
            "TRUNCATE",
            "chmod 777",
            "chown root",
        ],
        alias="RISK_EVAL_DANGEROUS_COMMANDS",
    )
    risk_eval_dangerous_paths: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [
            ".env",
            ".ssh",
            "id_rsa",
            "id_ed25519",
            "token",
            "credentials",
            "private_key",
            "secrets",
            ".pem",
            ".key",
        ],
        alias="RISK_EVAL_DANGEROUS_PATHS",
    )
    risk_eval_protected_paths: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["/etc", "/var", "/usr", "/root"],
        alias="RISK_EVAL_PROTECTED_PATHS",
    )
    risk_eval_auto_approve_max_risk: str = Field("低", alias="RISK_EVAL_AUTO_APPROVE_MAX_RISK")

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

    @field_validator("tg_proxy_url", "claude_config_dir", "admin_password", mode="before")
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

    @field_validator("allowed_file_extensions", "auto_file_send_extensions", mode="before")
    @classmethod
    def parse_file_extensions(cls, value: Any) -> list[str]:
        if isinstance(value, list):
            return [ext.strip().lower() for ext in value if str(ext).strip()]
        if isinstance(value, str):
            return [ext.strip().lower() for ext in value.split(",") if ext.strip()]
        raise ValueError("ALLOWED_FILE_EXTENSIONS 格式错误，需为逗号分隔扩展名")

    @field_validator(
        "risk_eval_dangerous_commands",
        "risk_eval_dangerous_paths",
        "risk_eval_protected_paths",
        mode="before",
    )
    @classmethod
    def parse_risk_eval_lists(cls, value: Any) -> list[str]:
        if isinstance(value, list):
            return [item.strip() for item in value if str(item).strip()]
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        raise ValueError("风险评估配置格式错误，需为逗号分隔的字符串列表")

    @field_validator("risk_eval_auto_approve_max_risk")
    @classmethod
    def validate_risk_eval_auto_approve_max_risk(cls, value: str) -> str:
        # 取值必须与 RiskLevel 枚举一致（app/services/risk_evaluator.py），
        # 否则 RiskLevel(value) 在 bootstrap 阶段抛出不可读的 ValueError 导致启动崩溃。
        valid = {"低", "中", "高", "极高"}
        if value not in valid:
            raise ValueError(f"RISK_EVAL_AUTO_APPROVE_MAX_RISK 必须是 {'/'.join(valid)} 之一，当前为 {value!r}")
        return value

    @field_validator("claude_tmux_mode", "claude_install_hooks", "auto_file_send_enabled", mode="before")
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
        "rate_limit_bucket_cleanup_interval_sec",
        "rate_limit_bucket_cleanup_batch_size",
        "task_store_ttl_hours",
        "task_store_max_records",
        "session_lock_ttl_sec",
        "lock_cleanup_interval_sec",
        "lock_cleanup_batch_size",
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
        "upload_queue_ttl_sec",
        "upload_queue_cleanup_interval_sec",
        "upload_expiry_hours",
        "upload_cleanup_interval_min",
        "auto_export_threshold_chars",
        "zip_max_size_mb",
        "push_notification_retry_count",
        "tombstone_ttl_sec",
    )
    @classmethod
    def validate_positive_int(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("配置值必须大于 0")
        return value

    @field_validator("upload_queue_max_files_per_user")
    @classmethod
    def validate_upload_queue_max_files_per_user(cls, value: int) -> int:
        if value < 0:
            raise ValueError("UPLOAD_QUEUE_MAX_FILES_PER_USER 必须大于等于 0")
        return value

    @field_validator(
        "tmux_poll_interval_sec",
        "tmux_enter_delay_sec",
        "tmux_partial_flush_sec",
        "tmux_completion_grace_sec",
        "structured_reply_pump_interval_sec",
        "spinner_initial_delay_sec",
        "spinner_interval_sec",
        "session_health_check_interval_sec",
        "external_session_stale_timeout_sec",
    )
    @classmethod
    def validate_positive_float(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("配置值必须大于 0")
        return value

    @field_validator("rate_limit_bucket_ttl_sec", "permission_lock_ttl_sec", "upload_queue_max_bytes_per_user", mode="before")
    @classmethod
    def validate_optional_positive_int(cls, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        if int(value) <= 0:
            raise ValueError("配置值必须大于 0")
        return value

    @model_validator(mode="after")
    def validate_rate_limit_bucket_ttl(self) -> Settings:
        if self.rate_limit_bucket_ttl_sec is not None and self.rate_limit_bucket_ttl_sec < self.rate_limit_window_sec:
            raise ValueError("RATE_LIMIT_BUCKET_TTL_SEC 必须大于等于 RATE_LIMIT_WINDOW_SEC")
        return self

    @property
    def allow_all_users(self) -> bool:
        return len(self.tg_allowed_user_ids) == 0

    @property
    def allowed_user_id_set(self) -> set[int]:
        return set(self.tg_allowed_user_ids)

    @property
    def default_workdir(self) -> str:
        return self.allowed_workdirs[0]

    @property
    def effective_rate_limit_bucket_ttl_sec(self) -> int:
        return self.rate_limit_bucket_ttl_sec if self.rate_limit_bucket_ttl_sec is not None else self.rate_limit_window_sec

    @property
    def effective_permission_lock_ttl_sec(self) -> int:
        return self.permission_lock_ttl_sec if self.permission_lock_ttl_sec is not None else self.claude_hook_pending_permission_ttl_sec

    @property
    def effective_upload_queue_max_bytes_per_user(self) -> int:
        if self.upload_queue_max_bytes_per_user is not None:
            return self.upload_queue_max_bytes_per_user
        return self.upload_queue_max_files_per_user * self.upload_max_file_size_mb * 1024 * 1024
