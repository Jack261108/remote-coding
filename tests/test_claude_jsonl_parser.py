from __future__ import annotations

import json

from app.adapters.claude.paths import ClaudePaths
from app.services.claude_jsonl_parser import ClaudeJSONLParser


def _write_jsonl(path, lines: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for line in lines:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")


def test_claude_jsonl_parser_reads_turns_and_tool_results(tmp_path) -> None:
    paths = ClaudePaths.resolve(str(tmp_path / ".claude"))
    parser = ClaudeJSONLParser(paths)
    session_file = parser.session_file_path(session_id="session-1", cwd="/tmp/project")

    _write_jsonl(
        session_file,
        [
            {
                "type": "user",
                "timestamp": "2026-04-16T10:00:00Z",
                "message": {"id": "u1", "content": "你好"},
            },
            {
                "type": "assistant",
                "timestamp": "2026-04-16T10:00:01Z",
                "message": {
                    "id": "a1",
                    "content": [
                        {"type": "text", "text": "我来看看"},
                        {"type": "tool_use", "id": "tool-1", "name": "Bash", "input": {"command": "pwd"}},
                    ],
                },
            },
            {
                "type": "assistant",
                "timestamp": "2026-04-16T10:00:02Z",
                "toolUseResult": {"stdout": "/tmp/project"},
                "message": {
                    "id": "a2",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "tool-1", "content": "done", "is_error": False}
                    ],
                },
            },
            {
                "type": "summary",
                "summary": "排查当前目录",
            },
        ],
    )

    snapshot = parser.parse_incremental(session_id="session-1", cwd="/tmp/project")

    assert [turn.role for turn in snapshot.turns] == ["user", "assistant"]
    assert snapshot.turns[0].text == "\n你好\n"
    assert snapshot.turns[1].text == "\n我来看看\n"
    assert snapshot.tool_calls["tool-1"].status.value == "success"
    assert snapshot.tool_calls["tool-1"].result == "done"
    assert snapshot.tool_calls["tool-1"].structured_result == {"stdout": "/tmp/project"}
    assert snapshot.tool_calls["tool-1"].completed_at is not None
    assert snapshot.summary == "排查当前目录"
    assert snapshot.last_reply == "我来看看"
    assert snapshot.last_reply_role == "assistant"


def test_claude_jsonl_parser_detects_clear_and_resets_state(tmp_path) -> None:
    paths = ClaudePaths.resolve(str(tmp_path / ".claude"))
    parser = ClaudeJSONLParser(paths)
    session_file = parser.session_file_path(session_id="session-1", cwd="/tmp/project")

    _write_jsonl(
        session_file,
        [
            {
                "type": "assistant",
                "timestamp": "2026-04-16T10:00:00Z",
                "message": {"id": "a1", "content": [{"type": "text", "text": "旧回复"}]},
            }
        ],
    )
    parser.parse_incremental(session_id="session-1", cwd="/tmp/project")

    _write_jsonl(
        session_file,
        [
            {
                "type": "user",
                "timestamp": "2026-04-16T10:00:01Z",
                "message": {"id": "u-clear", "content": "<command-name>/clear</command-name>"},
            },
            {
                "type": "assistant",
                "timestamp": "2026-04-16T10:00:02Z",
                "message": {"id": "a2", "content": [{"type": "text", "text": "新回复"}]},
            },
        ],
    )

    snapshot = parser.parse_incremental(session_id="session-1", cwd="/tmp/project")

    assert snapshot.clear_detected is True
    assert len(snapshot.turns) == 1
    assert snapshot.turns[0].text == "\n新回复\n"
    assert snapshot.last_reply == "新回复"
