from __future__ import annotations

import json

import pytest

from app.adapters.claude.paths import ClaudePaths
from app.services.claude_jsonl_parser import ClaudeJSONLParser


def _write_jsonl(path, lines: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for line in lines:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")


def test_claude_jsonl_parser_rejects_path_component_traversal(tmp_path) -> None:
    paths = ClaudePaths.resolve(str(tmp_path / ".claude"))
    parser = ClaudeJSONLParser(paths)

    with pytest.raises(ValueError):
        parser.session_file_path(session_id="../evil", cwd="/tmp/project")

    with pytest.raises(ValueError):
        parser.subagent_file_path(session_id="session-1", agent_id="../evil", cwd="/tmp/project")


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
                    "content": [{"type": "tool_result", "tool_use_id": "tool-1", "content": "done", "is_error": False}],
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


def test_claude_jsonl_parser_ignores_truncated_tail_until_line_is_complete(tmp_path) -> None:
    paths = ClaudePaths.resolve(str(tmp_path / ".claude"))
    parser = ClaudeJSONLParser(paths)
    session_file = parser.session_file_path(session_id="session-1", cwd="/tmp/project")

    _write_jsonl(
        session_file,
        [
            {
                "type": "assistant",
                "timestamp": "2026-04-16T10:00:00Z",
                "message": {"id": "a1", "content": [{"type": "text", "text": "第一行"}]},
            }
        ],
    )
    with session_file.open("ab") as fh:
        fh.write(b'{"type":"assistant","message":')

    snapshot = parser.parse_incremental(session_id="session-1", cwd="/tmp/project")

    assert [turn.text for turn in snapshot.turns] == ["\n第一行\n"]
    assert snapshot.last_offset < session_file.stat().st_size

    with session_file.open("ab") as fh:
        fh.write('{"id":"a2","content":[{"type":"text","text":"第二行"}]}}\n'.encode())

    snapshot = parser.parse_incremental(session_id="session-1", cwd="/tmp/project")

    assert [turn.text for turn in snapshot.turns] == ["\n第一行\n", "\n第二行\n"]
    assert snapshot.last_offset == session_file.stat().st_size


def test_claude_jsonl_parser_skips_invalid_complete_line(tmp_path) -> None:
    paths = ClaudePaths.resolve(str(tmp_path / ".claude"))
    parser = ClaudeJSONLParser(paths)
    session_file = parser.session_file_path(session_id="session-1", cwd="/tmp/project")

    _write_jsonl(
        session_file,
        [
            {
                "type": "assistant",
                "timestamp": "2026-04-16T10:00:00Z",
                "message": {"id": "a1", "content": [{"type": "text", "text": "第一行"}]},
            }
        ],
    )
    with session_file.open("ab") as fh:
        fh.write(b'{"type": invalid}\n')

    snapshot = parser.parse_incremental(session_id="session-1", cwd="/tmp/project")

    assert [turn.text for turn in snapshot.turns] == ["\n第一行\n"]
    # B1 fix: parser now skips malformed lines instead of stopping.
    # last_offset advances past the bad line to the end of the file.
    assert snapshot.last_offset == session_file.stat().st_size


def test_claude_jsonl_parser_reports_reset_when_file_is_truncated(tmp_path) -> None:
    paths = ClaudePaths.resolve(str(tmp_path / ".claude"))
    parser = ClaudeJSONLParser(paths)
    session_file = parser.session_file_path(session_id="session-1", cwd="/tmp/project")

    _write_jsonl(
        session_file,
        [
            {
                "type": "assistant",
                "timestamp": "2026-04-16T10:00:00Z",
                "message": {"id": "a1", "content": [{"type": "text", "text": "第一行"}]},
            },
            {
                "type": "assistant",
                "timestamp": "2026-04-16T10:00:01Z",
                "message": {"id": "a-extra", "content": [{"type": "text", "text": "第二行"}]},
            },
        ],
    )
    parser.parse_incremental(session_id="session-1", cwd="/tmp/project")

    session_file.write_text(
        json.dumps(
            {
                "type": "assistant",
                "timestamp": "2026-04-16T10:00:02Z",
                "message": {"id": "a2", "content": [{"type": "text", "text": "短"}]},
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    snapshot = parser.parse_incremental(session_id="session-1", cwd="/tmp/project")

    assert snapshot.reset_detected is True
    assert [turn.text for turn in snapshot.turns] == ["\n短\n"]


def test_claude_jsonl_parser_detects_interrupt_from_user_message_and_tool_result(tmp_path) -> None:
    paths = ClaudePaths.resolve(str(tmp_path / ".claude"))
    parser = ClaudeJSONLParser(paths)
    session_file = parser.session_file_path(session_id="session-1", cwd="/tmp/project")

    _write_jsonl(
        session_file,
        [
            {
                "type": "assistant",
                "timestamp": "2026-04-16T10:00:00Z",
                "message": {
                    "id": "a1",
                    "content": [
                        {"type": "tool_use", "id": "tool-1", "name": "Bash", "input": {"command": "sleep 10"}},
                    ],
                },
            },
            {
                "type": "assistant",
                "timestamp": "2026-04-16T10:00:01Z",
                "toolUseResult": {"stderr": "Interrupted by user"},
                "message": {
                    "id": "a2",
                    "content": [{"type": "tool_result", "tool_use_id": "tool-1", "content": "Interrupted by user", "is_error": True}],
                },
            },
            {
                "type": "user",
                "timestamp": "2026-04-16T10:00:02Z",
                "message": {"id": "u1", "content": "[Request interrupted by user]"},
            },
        ],
    )

    snapshot = parser.parse_incremental(session_id="session-1", cwd="/tmp/project")

    assert snapshot.interrupt_detected is True
    assert snapshot.tool_calls["tool-1"].status.value == "interrupted"


def test_claude_jsonl_parser_populates_subagent_tools_from_agent_file(tmp_path) -> None:
    paths = ClaudePaths.resolve(str(tmp_path / ".claude"))
    parser = ClaudeJSONLParser(paths)
    cwd = "/tmp/project"
    session_file = parser.session_file_path(session_id="session-1", cwd=cwd)
    agent_file = parser.subagent_file_path(session_id="session-1", agent_id="agent-1", cwd=cwd)

    _write_jsonl(
        agent_file,
        [
            {
                "type": "assistant",
                "timestamp": "2026-04-16T10:00:01Z",
                "message": {
                    "id": "a-tool-1",
                    "content": [{"type": "tool_use", "id": "sub-tool-1", "name": "Read", "input": {"file_path": "/tmp/a.py"}}],
                },
            },
            {
                "type": "assistant",
                "timestamp": "2026-04-16T10:00:02Z",
                "toolUseResult": {"stdout": "done"},
                "message": {
                    "id": "a-tool-2",
                    "content": [{"type": "tool_result", "tool_use_id": "sub-tool-1", "content": "done", "is_error": False}],
                },
            },
        ],
    )
    _write_jsonl(
        session_file,
        [
            {
                "type": "assistant",
                "timestamp": "2026-04-16T10:00:03Z",
                "toolUseResult": {"agentId": "agent-1", "status": "completed", "content": "subagent done"},
                "message": {
                    "id": "a-main",
                    "content": [
                        {"type": "tool_use", "id": "task-tool-1", "name": "Task", "input": {"description": "do thing"}},
                        {"type": "tool_result", "tool_use_id": "task-tool-1", "content": "subagent done", "is_error": False},
                    ],
                },
            },
        ],
    )

    snapshot = parser.parse_incremental(session_id="session-1", cwd=cwd)

    tool = snapshot.tool_calls["task-tool-1"]
    assert tool.structured_result == {"agentId": "agent-1", "status": "completed", "content": "subagent done"}
    assert len(tool.subagent_tools) == 1
    assert tool.subagent_tools[0].tool_use_id == "sub-tool-1"
    assert tool.subagent_tools[0].name == "Read"
    assert tool.subagent_tools[0].status.value == "success"
