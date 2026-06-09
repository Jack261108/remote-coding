from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class LoggingSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    log_level: str = Field("INFO", alias="LOG_LEVEL")


_STANDARD_LOG_RECORD_KEYS = set(
    logging.LogRecord(
        name="",
        level=0,
        pathname="",
        lineno=0,
        msg="",
        args=(),
        exc_info=None,
    ).__dict__
) | {"asctime", "message"}
_SENSITIVE_KEY_PARTS = (
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "credential",
    "new_string",
    "old_string",
    "password",
    "private_key",
    "secret",
    "stderr",
    "stdout",
    "token",
)
_SUMMARY_ONLY_KEYS = {"command", "content", "tool_input", "tool_output"}
_MAX_STRING_CHARS = 300
_MAX_COLLECTION_ITEMS = 20


def _json_size(value: Any) -> int | None:
    try:
        return len(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))
    except (TypeError, ValueError):
        return None


def _summarize_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {"key_count": len(value)}
    approx_bytes = _json_size(dict(value))
    if approx_bytes is not None:
        summary["approx_bytes"] = approx_bytes
    return summary


def _summarize_value(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return _summarize_mapping(value)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return {"item_count": len(value), "approx_bytes": _json_size(list(value))}
    if isinstance(value, (bytes, bytearray)):
        return {"byte_count": len(value)}
    text = str(value)
    return {"char_count": len(text)}


def _is_sensitive_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def _safe_extra_value(key: str, value: Any) -> Any:
    if key in _SUMMARY_ONLY_KEYS:
        return _summarize_value(value)
    if _is_sensitive_key(key):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {str(child_key): _safe_extra_value(str(child_key), child_value) for child_key, child_value in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_safe_extra_value("item", item) for item in list(value)[:_MAX_COLLECTION_ITEMS]]
    if isinstance(value, (bytes, bytearray)):
        return {"byte_count": len(value)}
    if isinstance(value, str) and len(value) > _MAX_STRING_CHARS:
        return f"{value[:_MAX_STRING_CHARS]}…[truncated {len(value) - _MAX_STRING_CHARS} chars]"
    return value


class ExtraFieldsFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        extras = {key: _safe_extra_value(key, value) for key, value in record.__dict__.items() if key not in _STANDARD_LOG_RECORD_KEYS}
        if not extras:
            return rendered
        serialized = json.dumps(extras, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
        return f"{rendered} extra={serialized}"


def configure_logging() -> None:
    # 优先使用环境变量，如果没有则使用 pydantic-settings 读取 .env 文件
    log_level = os.getenv("LOG_LEVEL")
    if log_level is None:
        try:
            settings = LoggingSettings()  # type: ignore[call-arg]
            log_level = settings.log_level
        except Exception:
            log_level = "INFO"
    log_level = log_level.upper()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(ExtraFieldsFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        handlers=[handler],
        force=True,
    )
