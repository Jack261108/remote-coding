from __future__ import annotations

import json
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class ClaudeStopArtifacts:
    settings_file: Path
    response_file: Path


def build_task_artifacts(*, task_id: str, data_dir: Path, base_settings_path: Path | None = None) -> ClaudeStopArtifacts:
    settings = _load_base_settings(base_settings_path)
    hooks = _normalize_hooks(settings)
    stop_hooks = _normalize_stop_hooks(hooks)

    response_file = data_dir / f"{task_id}-stop-response.txt"
    settings_file = data_dir / f"{task_id}-claude-settings.json"

    bridge_hook = {
        "hooks": [
            {
                "type": "command",
                "command": build_stop_hook_command(response_file=response_file),
            }
        ]
    }
    hooks["Stop"] = [bridge_hook, *stop_hooks]

    data_dir.mkdir(parents=True, exist_ok=True)
    settings_file.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    return ClaudeStopArtifacts(settings_file=settings_file, response_file=response_file)


def build_stop_hook_command(*, response_file: Path) -> str:
    python_bin = shlex.quote(sys.executable)
    module_path = shlex.quote(str(Path(__file__).resolve()))
    response_path = shlex.quote(str(response_file))
    return f"{python_bin} {module_path} write-stop-response {response_path}"


def write_stop_message(*, response_file: Path, payload: dict[str, Any]) -> None:
    if payload.get("stop_hook_active") is True:
        return

    message = payload.get("last_assistant_message")
    if not isinstance(message, str) or not message.strip():
        return

    response_file.parent.mkdir(parents=True, exist_ok=True)
    response_file.write_text(message, encoding="utf-8")


def main(argv: list[str] | None = None, *, stdin_text: str | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2 or args[0] != "write-stop-response":
        print("usage: claude_stop_hook.py write-stop-response <response-file>", file=sys.stderr)
        return 1

    response_file = Path(args[1])
    raw_input = sys.stdin.read() if stdin_text is None else stdin_text

    try:
        payload = json.loads(raw_input)
    except json.JSONDecodeError as exc:
        print(f"Invalid JSON input: {exc}", file=sys.stderr)
        return 2

    if not isinstance(payload, dict):
        print("Invalid JSON input: payload must be an object", file=sys.stderr)
        return 2

    write_stop_message(response_file=response_file, payload=payload)
    return 0


def _load_base_settings(base_settings_path: Path | None) -> dict[str, Any]:
    settings_path = base_settings_path or Path.home() / ".claude" / "settings.json"
    if not settings_path.exists():
        return {}

    try:
        content = json.loads(settings_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"基础 Claude settings JSON 非法: {settings_path}") from exc

    if not isinstance(content, dict):
        raise ValueError("基础 Claude settings 必须是 JSON 对象")
    return content


def _normalize_hooks(settings: dict[str, Any]) -> dict[str, Any]:
    hooks = settings.get("hooks")
    if hooks is None:
        hooks = {}
        settings["hooks"] = hooks
        return hooks
    if not isinstance(hooks, dict):
        raise ValueError("基础 Claude settings 中 hooks 必须是对象")
    return hooks


def _normalize_stop_hooks(hooks: dict[str, Any]) -> list[dict[str, Any]]:
    stop_hooks = hooks.get("Stop")
    if stop_hooks is None:
        return []
    if not isinstance(stop_hooks, list):
        raise ValueError("基础 Claude settings 中 hooks.Stop 必须是数组")

    normalized: list[dict[str, Any]] = []
    for entry in stop_hooks:
        if not isinstance(entry, dict):
            raise ValueError("基础 Claude settings 中 hooks.Stop 条目必须是对象")

        entry_hooks = entry.get("hooks")
        if not isinstance(entry_hooks, list):
            raise ValueError("基础 Claude settings 中 hooks.Stop 条目必须包含 hooks 数组")
        for hook in entry_hooks:
            if not isinstance(hook, dict):
                raise ValueError("基础 Claude settings 中 hooks.Stop.hooks 条目必须是对象")

        normalized.append(dict(entry))
    return normalized


if __name__ == "__main__":
    raise SystemExit(main())
