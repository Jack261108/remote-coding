from __future__ import annotations

import logging
import os
import sys

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
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
        force=True,
    )
