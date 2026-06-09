from __future__ import annotations

import json
import logging

import pytest

from app.infra.logging import configure_logging


@pytest.fixture
def restore_root_logger():
    root = logging.getLogger()
    old_handlers = list(root.handlers)
    old_level = root.level
    old_disabled = root.disabled
    yield
    root.handlers[:] = old_handlers
    root.setLevel(old_level)
    root.disabled = old_disabled


def _logged_extra(output: str) -> dict:
    line = output.strip().splitlines()[-1]
    marker = " extra="
    assert marker in line
    return json.loads(line.split(marker, 1)[1])


def test_configure_logging_emits_safe_extra_fields_to_stdout(capsys, monkeypatch, restore_root_logger) -> None:
    monkeypatch.setenv("LOG_LEVEL", "INFO")
    configure_logging()

    logger = logging.getLogger("tests.logging.extra")
    logger.info("audit event", extra={"session_id": "s1", "user_id": 42})

    output = capsys.readouterr().out
    assert "tests.logging.extra" in output
    assert "audit event" in output
    logged_extra = _logged_extra(output)
    assert logged_extra["session_id"] == "s1"
    assert logged_extra["user_id"] == 42


def test_configure_logging_keeps_plain_records_without_extra_suffix(capsys, monkeypatch, restore_root_logger) -> None:
    monkeypatch.setenv("LOG_LEVEL", "INFO")
    configure_logging()

    logger = logging.getLogger("tests.logging.plain")
    logger.info("plain event")

    output = capsys.readouterr().out
    assert "tests.logging.plain" in output
    assert "plain event" in output
    assert " extra=" not in output
    assert "extra={}" not in output


def test_configure_logging_redacts_sensitive_and_summarizes_tool_input(capsys, monkeypatch, restore_root_logger) -> None:
    monkeypatch.setenv("LOG_LEVEL", "INFO")
    configure_logging()
    secret = "SECRET_TOKEN_DO_NOT_LOG"

    logger = logging.getLogger("tests.logging.redaction")
    logger.info(
        "sensitive event",
        extra={
            "session_id": "s1",
            "token": secret,
            "tool_input": {"command": secret, "file_path": "/tmp/example"},
        },
    )

    output = capsys.readouterr().out
    assert secret not in output
    logged_extra = _logged_extra(output)
    assert logged_extra["session_id"] == "s1"
    assert logged_extra["token"] == "[REDACTED]"
    assert logged_extra["tool_input"]["key_count"] == 2
    assert "keys" not in logged_extra["tool_input"]


def test_configure_logging_summarizes_tool_input_without_raw_key_names(capsys, monkeypatch, restore_root_logger) -> None:
    monkeypatch.setenv("LOG_LEVEL", "INFO")
    configure_logging()
    secret = "SECRET_TOKEN_DO_NOT_LOG"

    logger = logging.getLogger("tests.logging.redaction_keys")
    logger.info("sensitive key event", extra={"tool_input": {secret: "value"}})

    output = capsys.readouterr().out
    assert secret not in output
    logged_extra = _logged_extra(output)
    assert logged_extra["tool_input"]["key_count"] == 1
    assert "keys" not in logged_extra["tool_input"]
