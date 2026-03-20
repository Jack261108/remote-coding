from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.adapters.process.claude_stop_hook import (
    ClaudeStopArtifacts,
    build_stop_hook_command,
    build_task_artifacts,
    main,
    write_stop_message,
)


def test_preserves_existing_stop_events_and_prepends_bridge_event(tmp_path: Path) -> None:
    base_settings = tmp_path / "base-settings.json"
    base_settings.write_text(
        json.dumps(
            {
                "model": "sonnet",
                "hooks": {
                    "Stop": [
                        {
                            "matcher": "first-event",
                            "hooks": [
                                {"type": "command", "command": "first-stop"},
                                {"type": "command", "command": "first-stop-2"},
                            ],
                        },
                        {
                            "matcher": "second-event",
                            "hooks": [
                                {"type": "command", "command": "second-stop"},
                            ],
                        },
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    artifacts = build_task_artifacts(
        task_id="task-123",
        data_dir=tmp_path,
        base_settings_path=base_settings,
    )

    assert isinstance(artifacts, ClaudeStopArtifacts)
    assert artifacts.settings_file.exists()
    assert artifacts.response_file.exists() is False

    generated_settings = json.loads(artifacts.settings_file.read_text(encoding="utf-8"))
    stop_events = generated_settings["hooks"]["Stop"]

    assert stop_events[0] == {
        "hooks": [
            {
                "type": "command",
                "command": build_stop_hook_command(response_file=artifacts.response_file),
            }
        ]
    }
    assert stop_events[1:] == [
        {
            "matcher": "first-event",
            "hooks": [
                {"type": "command", "command": "first-stop"},
                {"type": "command", "command": "first-stop-2"},
            ],
        },
        {
            "matcher": "second-event",
            "hooks": [
                {"type": "command", "command": "second-stop"},
            ],
        },
    ]
    assert isinstance(stop_events, list)
    assert isinstance(stop_events[0]["hooks"], list)
    assert generated_settings["model"] == "sonnet"


def test_invalid_base_settings_json(tmp_path: Path) -> None:
    base_settings = tmp_path / "base-settings.json"
    base_settings.write_text("{invalid", encoding="utf-8")

    with pytest.raises(ValueError, match="settings"):
        build_task_artifacts(
            task_id="task-123",
            data_dir=tmp_path,
            base_settings_path=base_settings,
        )


def test_hooks_is_not_dict(tmp_path: Path) -> None:
    base_settings = tmp_path / "base-settings.json"
    base_settings.write_text(json.dumps({"hooks": []}), encoding="utf-8")

    with pytest.raises(ValueError, match="hooks"):
        build_task_artifacts(
            task_id="task-123",
            data_dir=tmp_path,
            base_settings_path=base_settings,
        )


def test_invalid_stop_hook_shape(tmp_path: Path) -> None:
    base_settings = tmp_path / "base-settings.json"
    base_settings.write_text(json.dumps({"hooks": {"Stop": {}}}), encoding="utf-8")

    with pytest.raises(ValueError, match="Stop"):
        build_task_artifacts(
            task_id="task-123",
            data_dir=tmp_path,
            base_settings_path=base_settings,
        )


@pytest.mark.parametrize(
    "stop_entries",
    [
        [{"matcher": "missing-hooks"}],
        [{"matcher": "bad-hooks", "hooks": {}}],
        [{"matcher": "bad-hooks-item", "hooks": ["bad-entry"]}],
    ],
)
def test_invalid_stop_entry_shape(tmp_path: Path, stop_entries: list[object]) -> None:
    base_settings = tmp_path / "base-settings.json"
    base_settings.write_text(
        json.dumps({"hooks": {"Stop": stop_entries}}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Stop"):
        build_task_artifacts(
            task_id="task-123",
            data_dir=tmp_path,
            base_settings_path=base_settings,
        )


def test_stop_hook_cli_ignores_reentrant_or_empty_payload(tmp_path: Path) -> None:
    response_file = tmp_path / "response.txt"

    exit_code = main(
        ["write-stop-response", str(response_file)],
        stdin_text=json.dumps(
            {
                "stop_hook_active": True,
                "last_assistant_message": "ignored",
            }
        ),
    )
    assert exit_code == 0
    assert response_file.exists() is False

    exit_code = main(
        ["write-stop-response", str(response_file)],
        stdin_text=json.dumps(
            {
                "stop_hook_active": False,
                "last_assistant_message": "   ",
            }
        ),
    )
    assert exit_code == 0
    assert response_file.exists() is False


def test_stop_hook_cli_persists_last_assistant_message(tmp_path: Path) -> None:
    response_file = tmp_path / "response.txt"

    exit_code = main(
        ["write-stop-response", str(response_file)],
        stdin_text=json.dumps(
            {
                "stop_hook_active": False,
                "last_assistant_message": "hello from claude",
            }
        ),
    )

    assert exit_code == 0
    assert response_file.read_text(encoding="utf-8") == "hello from claude"


def test_stop_hook_cli_returns_error_on_invalid_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    response_file = tmp_path / "response.txt"

    exit_code = main(
        ["write-stop-response", str(response_file)],
        stdin_text="{invalid",
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert response_file.exists() is False
    assert "JSON" in captured.err


def test_build_task_artifacts_defaults_to_empty_settings_when_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    monkeypatch.setenv("HOME", str(home_dir))

    artifacts = build_task_artifacts(task_id="task-456", data_dir=tmp_path)
    generated_settings = json.loads(artifacts.settings_file.read_text(encoding="utf-8"))

    assert generated_settings["hooks"]["Stop"] == [
        {
            "hooks": [
                {
                    "type": "command",
                    "command": build_stop_hook_command(response_file=artifacts.response_file),
                }
            ]
        }
    ]


def test_write_stop_message_writes_payload_message(tmp_path: Path) -> None:
    response_file = tmp_path / "response.txt"

    write_stop_message(
        response_file=response_file,
        payload={
            "stop_hook_active": False,
            "last_assistant_message": "persist me",
        },
    )

    assert response_file.read_text(encoding="utf-8") == "persist me"
