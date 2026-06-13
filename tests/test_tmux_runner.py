import asyncio
import logging
from datetime import timedelta
from pathlib import Path

import pytest

from app.adapters.process.tmux_runner import TmuxRunner, _TmuxTaskMeta
from app.domain.models import CLIEvent, EventType, utc_now
from app.domain.session_models import ConversationTurn, PendingPermission, SessionPhase, ToolCallRecord, ToolStatus


async def _collect_events(stream):
    return [event async for event in stream]


def test_build_shell_command_writes_script_file(tmp_path: Path) -> None:
    runner = TmuxRunner(data_dir=str(tmp_path))
    command_file = tmp_path / "x.cmd.sh"
    cmd = runner._build_shell_command(
        argv=["claude", "-p", "hello"],
        workdir="/tmp",
        log_file=tmp_path / "x.log",
        exit_file=tmp_path / "x.exit",
        command_file=command_file,
        hide_launcher_line=False,
    )
    assert cmd.startswith("bash ")
    script = command_file.read_text(encoding="utf-8")
    assert "tee -a" in script
    assert "PIPESTATUS[0]" in script


def test_build_shell_command_hides_launcher_in_persistent_mode(tmp_path: Path) -> None:
    runner = TmuxRunner(data_dir=str(tmp_path))
    command_file = tmp_path / "hidden.cmd.sh"
    cmd = runner._build_shell_command(
        argv=["claude", "-p", "hello"],
        workdir="/tmp",
        log_file=tmp_path / "x.log",
        exit_file=tmp_path / "x.exit",
        command_file=command_file,
        hide_launcher_line=True,
    )

    assert cmd.startswith("bash ")
    assert 'exec "${SHELL:-bash}" -l' in cmd


def test_tmux_runner_enter_delay_default_is_positive() -> None:
    runner = TmuxRunner()
    assert runner._enter_delay_sec > 0


def test_format_send_failure_contains_actionable_hint() -> None:
    runner = TmuxRunner()
    msg = runner._format_send_failure(
        base="tmux 粘贴命令失败",
        raw_err="pane not found",
        session_name="tgcli_user_1",
        rebuilt=False,
        rebuild_err="旧会话关闭失败",
    )

    assert "tmux 粘贴命令失败: pane not found" in msg
    assert "tmux_session: tgcli_user_1" in msg
    assert "auto_rebuilt: 否" in msg
    assert "rebuild_error: 旧会话关闭失败" in msg
    assert "hint:" in msg


def test_format_send_failure_after_rebuild_has_specific_hint() -> None:
    runner = TmuxRunner()
    msg = runner._format_send_failure(
        base="tmux 执行命令失败",
        raw_err="target pane missing",
        session_name="tgcli_user_2",
        rebuilt=True,
    )

    assert "auto_rebuilt: 是" in msg
    assert "自动重建已执行但仍失败" in msg


@pytest.mark.asyncio
async def test_ensure_persistent_session_rejects_missing_workdir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runner = TmuxRunner()
    calls: list[tuple[str, ...]] = []

    async def fake_run_tmux(*args: str, input_data: bytes | None = None):
        calls.append(args)
        return 0, "", ""

    monkeypatch.setattr(runner, "_run_tmux", fake_run_tmux)

    ok, err = await runner._ensure_persistent_session(
        "tgcli_user_1",
        workdir=str(tmp_path / "不存在的目录"),
        env=None,
    )

    assert ok is False
    assert err.startswith("workdir 不存在或不是目录:")
    assert calls == []


@pytest.mark.asyncio
async def test_respawn_and_send_command_success(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = TmuxRunner()
    calls: list[tuple[str, ...]] = []

    async def fake_run_tmux(*args: str, input_data: bytes | None = None):
        calls.append(args)
        return 0, "", ""

    monkeypatch.setattr(runner, "_run_tmux", fake_run_tmux)
    ok, err = await runner._respawn_and_send_command(
        session_name="tgcli_user_1",
        command="bash '/tmp/test.cmd.sh'; exec \"${SHELL:-bash}\" -l",
        workdir="/tmp",
    )

    assert ok is True
    assert err == ""
    assert calls[0][0:3] == ("respawn-pane", "-k", "-t")
    assert calls[0][3] == "tgcli_user_1"


@pytest.mark.asyncio
async def test_respawn_and_send_command_failure_has_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = TmuxRunner()

    async def fake_run_tmux(*args: str, input_data: bytes | None = None):
        return 1, "", "target not found"

    monkeypatch.setattr(runner, "_run_tmux", fake_run_tmux)
    ok, err = await runner._respawn_and_send_command(
        session_name="tgcli_user_1",
        command="bash '/tmp/test.cmd.sh'; exec \"${SHELL:-bash}\" -l",
        workdir="/tmp",
    )

    assert ok is False
    assert "tmux respawn 失败" in err
    assert "tmux_session: tgcli_user_1" in err
    assert "hint:" in err


@pytest.mark.asyncio
async def test_ensure_claude_interactive_session_respawns_when_not_claude(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = TmuxRunner(claude_cli_bin="claude")

    async def fake_ensure_persistent_session(session_name: str, *, workdir: str, env=None):
        return True, ""

    async def fake_session_current_command(session_name: str) -> str:
        return "bash"

    async def fake_respawn_and_send_command(*, session_name: str, command: str, workdir: str):
        assert session_name == "tgcli_user_1"
        assert workdir == "/tmp"
        assert "exec claude --append-system-prompt" in command
        return True, ""

    monkeypatch.setattr(runner, "_ensure_persistent_session", fake_ensure_persistent_session)
    monkeypatch.setattr(runner, "_session_current_command", fake_session_current_command)
    monkeypatch.setattr(runner, "_respawn_and_send_command", fake_respawn_and_send_command)

    ok, err = await runner.ensure_claude_interactive_session(terminal_key="user_1", workdir="/tmp")
    assert ok is True
    assert err == ""


@pytest.mark.asyncio
async def test_send_command_uses_ctrl_m_in_interactive(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = TmuxRunner()
    seen_enter_key: list[str] = []

    async def fake_run_tmux(*args: str, input_data: bytes | None = None):
        if args and args[0] == "send-keys" and len(args) >= 4 and args[1] == "-t" and args[2] == "tgcli_user_1":
            key = args[3]
            if key in {"Enter", "C-m"}:
                seen_enter_key.append(key)
        return 0, "", ""

    monkeypatch.setattr(runner, "_run_tmux", fake_run_tmux)

    ok, err = await runner._send_command(
        "tgcli_user_1",
        "hello",
        workdir="/tmp",
        env=None,
        interactive=True,
    )

    assert ok is True
    assert err == ""
    assert seen_enter_key == ["C-m"]


@pytest.mark.asyncio
async def test_select_user_question_option_sends_expected_tmux_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = TmuxRunner()
    key_calls: list[tuple[str, ...]] = []

    async def fake_session_exists(session_name: str) -> bool:
        return session_name == "tgcli_user_1"

    async def fake_session_current_command(session_name: str) -> str:
        assert session_name == "tgcli_user_1"
        return "claude"

    async def fake_run_tmux(*args: str, input_data: bytes | None = None):
        if args and args[0] == "capture-pane":
            return 0, "› 1. 今天\n  2. 明天\n  3. 后天\nEnter to select · Tab/Arrow keys to navigate · Esc to cancel\n", ""
        if args and args[0] == "send-keys":
            key_calls.append(args)
        return 0, "", ""

    monkeypatch.setattr(runner, "session_exists", fake_session_exists)
    monkeypatch.setattr(runner, "_session_current_command", fake_session_current_command)
    monkeypatch.setattr(runner, "_run_tmux", fake_run_tmux)

    ok, err = await runner.select_user_question_option(
        terminal_key="user_1",
        workdir="/tmp",
        option_index=2,
        submit_after=True,
    )

    assert ok is True
    assert err == ""
    assert key_calls == [
        ("send-keys", "-t", "tgcli_user_1", "Down", "Down"),
        ("send-keys", "-t", "tgcli_user_1", "C-m"),
        ("send-keys", "-t", "tgcli_user_1", "C-m"),
    ]


@pytest.mark.asyncio
async def test_answer_user_question_with_text_uses_type_something_path(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = TmuxRunner()
    key_calls: list[tuple[str, ...]] = []
    buffer_inputs: list[bytes] = []

    async def fake_session_exists(session_name: str) -> bool:
        return session_name == "tgcli_user_1"

    async def fake_session_current_command(session_name: str) -> str:
        assert session_name == "tgcli_user_1"
        return "claude"

    async def fake_run_tmux(*args: str, input_data: bytes | None = None):
        if args and args[0] == "capture-pane":
            return (
                0,
                "› 1. 今天\n  2. 明天\n  3. 后天\n  4. Type something.\nEnter to select · Tab/Arrow keys to navigate · Esc to cancel\n",
                "",
            )
        if args and args[0] == "load-buffer" and input_data is not None:
            buffer_inputs.append(input_data)
        if args and args[0] == "send-keys":
            key_calls.append(args)
        return 0, "", ""

    monkeypatch.setattr(runner, "session_exists", fake_session_exists)
    monkeypatch.setattr(runner, "_session_current_command", fake_session_current_command)
    monkeypatch.setattr(runner, "_run_tmux", fake_run_tmux)

    ok, err = await runner.answer_user_question_with_text(
        terminal_key="user_1",
        workdir="/tmp",
        option_count=3,
        text="2026-05-01",
        submit_after=False,
    )

    assert ok is True
    assert err == ""
    assert key_calls == [
        ("send-keys", "-t", "tgcli_user_1", "Down", "Down", "Down"),
        ("send-keys", "-t", "tgcli_user_1", "C-m"),
        ("send-keys", "-t", "tgcli_user_1", "C-m"),
    ]
    assert buffer_inputs == [b"2026-05-01"]


@pytest.mark.asyncio
async def test_select_user_question_option_returns_text_fallback_when_not_tui(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = TmuxRunner()

    async def fake_session_exists(session_name: str) -> bool:
        return session_name == "tgcli_user_1"

    async def fake_session_current_command(session_name: str) -> str:
        return "claude"

    async def fake_run_tmux(*args: str, input_data: bytes | None = None):
        if args and args[0] == "capture-pane":
            return 0, "请直接按选项回复我，格式示例：2 / 3 / 3\n", ""
        return 0, "", ""

    monkeypatch.setattr(runner, "session_exists", fake_session_exists)
    monkeypatch.setattr(runner, "_session_current_command", fake_session_current_command)
    monkeypatch.setattr(runner, "_run_tmux", fake_run_tmux)

    ok, err = await runner.select_user_question_option(
        terminal_key="user_1",
        workdir="/tmp",
        option_index=1,
        submit_after=False,
    )

    assert ok is False
    assert err == "当前问题不是 Claude 选择框界面，将回退为文本回答"


def test_selected_user_question_option_index_uses_latest_tui_block() -> None:
    runner = TmuxRunner()
    pane_text = (
        "旧问题\n"
        "  1. 今天\n"
        "  2. 明天\n"
        "› 3. 后天\n"
        "Enter to select · Tab/Arrow keys to navigate · Esc to cancel\n"
        "当前问题\n"
        "› 1. 郑州站\n"
        "  2. 郑州东\n"
        "Enter to select · Tab/Arrow keys to navigate · Esc to cancel\n"
    )

    assert runner._selected_user_question_option_index(pane_text) == 0


@pytest.mark.asyncio
async def test_interactive_run_rebinds_pipe_to_session_transcript(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runner = TmuxRunner(data_dir=str(tmp_path), poll_interval_sec=0.01, partial_flush_sec=0.01)
    seen_pipe_calls: list[tuple[str, ...]] = []

    async def fake_ensure(*, session_name: str, workdir: str, env=None):
        return True, ""

    async def fake_send(*args, **kwargs):
        return True, ""

    async def fake_watch(*, meta, timeout_sec: int, fifo_reader=None):
        yield CLIEvent(type=EventType.EXITED, task_id=meta.task_id, exit_code=0)

    async def fake_run_tmux(*args: str, input_data: bytes | None = None):
        if args and args[0] == "pipe-pane":
            seen_pipe_calls.append(args)
        return 0, "", ""

    monkeypatch.setattr(runner, "_ensure_claude_interactive_session", fake_ensure)
    monkeypatch.setattr(runner, "_send_command", fake_send)
    monkeypatch.setattr(runner, "_watch_task", fake_watch)
    monkeypatch.setattr(runner, "_run_tmux", fake_run_tmux)

    events = [
        e
        async for e in runner.run(
            task_id="t-pipe",
            argv=["hello"],
            workdir=str(tmp_path),
            timeout_sec=10,
            terminal_key="user_1",
            interactive=True,
        )
    ]

    assert [e.type for e in events] == [EventType.STARTED, EventType.EXITED]
    assert len(seen_pipe_calls) == 2
    assert seen_pipe_calls[0] == ("pipe-pane", "-t", "tgcli_user_1")
    assert seen_pipe_calls[1][0:3] == ("pipe-pane", "-t", "tgcli_user_1")
    assert "cat >" in seen_pipe_calls[1][3]
    assert ".fifo" in seen_pipe_calls[1][3]


@pytest.mark.asyncio
async def test_interactive_run_rebuilds_and_retries_pipe_when_tmux_server_is_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runner = TmuxRunner(data_dir=str(tmp_path), poll_interval_sec=0.01, partial_flush_sec=0.01)
    ensure_calls: list[str] = []
    pipe_bind_calls: list[tuple[str, ...]] = []

    async def fake_ensure(*, session_name: str, workdir: str, env=None):
        ensure_calls.append(session_name)
        return True, ""

    async def fake_send(*args, **kwargs):
        return True, ""

    async def fake_watch(*, meta, timeout_sec: int, fifo_reader=None):
        yield CLIEvent(type=EventType.EXITED, task_id=meta.task_id, exit_code=0)

    async def fake_run_tmux(*args: str, input_data: bytes | None = None):
        if args and args[0] == "pipe-pane" and len(args) >= 4:
            pipe_bind_calls.append(args)
            if len(pipe_bind_calls) == 1:
                return 1, "", "no server running on /private/tmp/tmux-501/default"
        return 0, "", ""

    monkeypatch.setattr(runner, "_ensure_claude_interactive_session", fake_ensure)
    monkeypatch.setattr(runner, "_send_command", fake_send)
    monkeypatch.setattr(runner, "_watch_task", fake_watch)
    monkeypatch.setattr(runner, "_run_tmux", fake_run_tmux)

    events = [
        e
        async for e in runner.run(
            task_id="t-pipe-retry",
            argv=["hello"],
            workdir=str(tmp_path),
            timeout_sec=10,
            terminal_key="user_1",
            interactive=True,
        )
    ]

    assert [e.type for e in events] == [EventType.STARTED, EventType.EXITED]
    assert ensure_calls == ["tgcli_user_1", "tgcli_user_1"]
    assert len(pipe_bind_calls) == 2


@pytest.mark.asyncio
async def test_watch_task_flushes_partial_content(tmp_path: Path) -> None:
    runner = TmuxRunner(data_dir=str(tmp_path), poll_interval_sec=0.01, partial_flush_sec=0.01)
    task_id = "t1"
    log_file = tmp_path / f"{task_id}.log"
    exit_file = tmp_path / f"{task_id}.exit"

    meta = _TmuxTaskMeta(
        session_name="tgcli_user_1",
        log_file=log_file,
        exit_file=exit_file,
        task_id=task_id,
        workdir=str(tmp_path),
        persistent_terminal=False,
    )

    async def writer() -> None:
        await asyncio.sleep(0.02)
        log_file.write_text("partial", encoding="utf-8")
        await asyncio.sleep(0.03)
        exit_file.write_text("0", encoding="utf-8")

    write_task = asyncio.create_task(writer())
    events = [event async for event in runner._watch_task(meta=meta, timeout_sec=1)]
    await write_task

    stdout_events = [e for e in events if e.type == EventType.STDOUT]
    assert stdout_events
    assert any((e.content or "") for e in stdout_events)
    assert events[-1].type == EventType.EXITED


def test_read_new_text_recovers_after_log_truncation(tmp_path: Path) -> None:
    runner = TmuxRunner(data_dir=str(tmp_path))
    log_file = tmp_path / "truncated.log"
    log_file.write_text("new-output", encoding="utf-8")

    text, position = runner._read_new_text(log_file, position=100)

    assert text == "new-output"
    assert position == len(b"new-output")


@pytest.mark.asyncio
async def test_process_interactive_chunk_does_not_overwrite_structured_checkpoint(tmp_path: Path) -> None:
    runner = TmuxRunner(data_dir=str(tmp_path), poll_interval_sec=0.01)
    meta = _TmuxTaskMeta(
        session_name="tgcli_user_1",
        log_file=runner._file_store.raw_transcript_path("tgcli_user_1"),
        exit_file=tmp_path / "x.exit",
        task_id="t1",
        workdir=str(tmp_path),
        terminal_id="user_1",
        persistent_terminal=True,
        interactive=True,
    )
    runner._session_store.get_or_create(
        session_id=meta.session_name,
        provider="claude_code",
        workdir=meta.workdir,
        terminal_id=meta.terminal_id,
    )
    state = runner._session_store.get(meta.session_name)
    assert state is not None
    state.checkpoint.last_offset = 7
    runner._session_store.save_checkpoint(meta.session_name, state.checkpoint)
    text = "TGCLI_BEGIN\n冒泡排序说明\nTGCLI_DONE\n"
    meta.log_file.write_text(text, encoding="utf-8")

    runner._process_interactive_chunk(
        meta=meta,
        offset=len(text.encode("utf-8")),
    )

    state = runner._session_store.get(meta.session_name)
    assert state is not None
    assert state.phase == SessionPhase.PROCESSING
    assert state.turns == []
    assert state.checkpoint.last_offset == 7

    raw_text = meta.log_file.read_text(encoding="utf-8")
    assert "冒泡排序说明" in raw_text
    session_files = {path.name for path in runner._file_store.session_dir(meta.session_name).iterdir()}
    assert "transcript.raw.log" in session_files
    assert "parser.cursor.json" in session_files
    assert "transcript.events.jsonl" not in session_files


@pytest.mark.asyncio
async def test_interactive_watch_starts_from_current_transcript_end(tmp_path: Path) -> None:
    runner = TmuxRunner(data_dir=str(tmp_path), poll_interval_sec=0.01, partial_flush_sec=0.01)
    session_name = "tgcli_user_1"
    claude_session = "claude-session-1"
    log_file = runner._file_store.raw_transcript_path(session_name)
    old_text = "TGCLI_BEGIN\n旧回复\nTGCLI_DONE\n"
    new_text = "TGCLI_BEGIN\n新回复\nTGCLI_DONE\n"
    log_file.write_text(old_text, encoding="utf-8")

    runner._session_store.get_or_create(
        session_id=session_name,
        provider="claude_code",
        workdir=str(tmp_path),
        terminal_id="user_1",
    )
    bound = runner._session_store.get_or_create(
        session_id=claude_session,
        provider="claude_code",
        workdir=str(tmp_path),
        terminal_id="user_1",
    )
    bound.phase = SessionPhase.PROCESSING
    runner._session_store._persist(bound)

    meta = _TmuxTaskMeta(
        session_name=session_name,
        log_file=log_file,
        exit_file=tmp_path / "x.exit",
        task_id="t2",
        workdir=str(tmp_path),
        terminal_id="user_1",
        claude_session_id=claude_session,
        persistent_terminal=True,
        interactive=True,
    )

    async def append_and_complete() -> None:
        await asyncio.sleep(0.03)
        log_file.write_text(old_text + new_text, encoding="utf-8")
        bound = runner._session_store.get(claude_session)
        assert bound is not None
        bound.phase = SessionPhase.WAITING_FOR_INPUT
        bound.turns.append(
            ConversationTurn(
                turn_id="turn-new",
                role="assistant",
                text="\n新回复\n",
                is_complete=True,
                source="jsonl",
            )
        )
        bound.checkpoint.last_offset = 1
        runner._session_store._persist(bound)

    writer = asyncio.create_task(append_and_complete())
    events = [event async for event in runner._watch_task(meta=meta, timeout_sec=1)]
    await writer

    assert [event.type for event in events] == [EventType.EXITED]
    state = runner._session_store.get(claude_session)
    assert state is not None
    assert state.checkpoint.last_offset == 1


@pytest.mark.asyncio
async def test_interactive_timeout_keeps_session_alive(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="app.adapters.process.tmux_runner")
    runner = TmuxRunner(data_dir=str(tmp_path), poll_interval_sec=0.01, partial_flush_sec=0.01)
    session_name = "tgcli_user_1"
    state = runner._session_store.get_or_create(session_id=session_name, workdir=str(tmp_path), terminal_id="user_1")
    state.phase = SessionPhase.PROCESSING
    runner._session_store._persist(state)

    meta = _TmuxTaskMeta(
        session_name=session_name,
        log_file=runner._file_store.raw_transcript_path(session_name),
        exit_file=tmp_path / "x.exit",
        task_id="t3",
        workdir=str(tmp_path),
        terminal_id="user_1",
        claude_session_id=session_name,
        persistent_terminal=True,
        interactive=True,
    )

    events = [event async for event in runner._watch_task(meta=meta, timeout_sec=0)]

    assert events[-1].type == EventType.TIMEOUT
    assert any(record.message == "tmux task timeout" for record in caplog.records)
    assert any(record.message == "tmux task finished" and record.result == "timeout" for record in caplog.records)
    state = runner._session_store.get(session_name)
    assert state is not None
    assert state.phase == SessionPhase.PROCESSING


@pytest.mark.asyncio
async def test_interactive_timeout_resets_after_structured_progress(tmp_path: Path) -> None:
    runner = TmuxRunner(data_dir=str(tmp_path), poll_interval_sec=0.01, partial_flush_sec=0.01)
    terminal_session = "tgcli_user_1"
    claude_session = "claude-session-1"
    runner._session_store.get_or_create(session_id=terminal_session, workdir=str(tmp_path), terminal_id="user_1")
    state = runner._session_store.get_or_create(session_id=claude_session, workdir=str(tmp_path), terminal_id="user_1")
    state.phase = SessionPhase.PROCESSING
    runner._session_store._persist(state)

    meta = _TmuxTaskMeta(
        session_name=terminal_session,
        log_file=runner._file_store.raw_transcript_path(terminal_session),
        exit_file=tmp_path / "x-progress.exit",
        task_id="t3-progress",
        workdir=str(tmp_path),
        terminal_id="user_1",
        claude_session_id=claude_session,
        persistent_terminal=True,
        interactive=True,
    )

    async def update_progress_and_complete() -> None:
        await asyncio.sleep(0.05)
        state = runner._session_store.get(claude_session)
        assert state is not None
        state.checkpoint.last_offset = 1
        runner._session_store._persist(state)
        await asyncio.sleep(0.07)
        state.turns.append(
            ConversationTurn(
                turn_id="turn-progress",
                role="assistant",
                text="完成",
                is_complete=True,
                source="jsonl",
            )
        )
        state.phase = SessionPhase.WAITING_FOR_INPUT
        state.checkpoint.last_offset = 2
        runner._session_store._persist(state)

    writer = asyncio.create_task(update_progress_and_complete())
    events = [event async for event in runner._watch_task(meta=meta, timeout_sec=0.1)]
    await writer

    assert [event.type for event in events] == [EventType.EXITED]


@pytest.mark.asyncio
async def test_interactive_completion_does_not_end_terminal_session_state(tmp_path: Path) -> None:
    runner = TmuxRunner(data_dir=str(tmp_path), poll_interval_sec=0.01, partial_flush_sec=0.01)
    terminal_session = "tgcli_user_1"
    claude_session = "claude-session-1"
    runner._session_store.get_or_create(session_id=terminal_session, workdir=str(tmp_path), terminal_id="user_1")
    state = runner._session_store.get_or_create(session_id=claude_session, workdir=str(tmp_path), terminal_id="user_1")
    state.phase = SessionPhase.WAITING_FOR_INPUT
    runner._session_store._persist(state)

    meta = _TmuxTaskMeta(
        session_name=terminal_session,
        log_file=runner._file_store.raw_transcript_path(terminal_session),
        exit_file=tmp_path / "x.exit",
        task_id="t3b",
        workdir=str(tmp_path),
        terminal_id="user_1",
        claude_session_id=claude_session,
        persistent_terminal=True,
        interactive=True,
    )

    task = asyncio.create_task(_collect_events(runner._watch_task(meta=meta, timeout_sec=1)))
    await asyncio.sleep(0.03)
    state = runner._session_store.get(claude_session)
    assert state is not None
    assert state.phase == SessionPhase.PROCESSING
    state.checkpoint.last_offset = 1
    state.phase = SessionPhase.WAITING_FOR_INPUT
    runner._session_store._persist(state)

    events = await task

    assert [event.type for event in events] == [EventType.EXITED]
    terminal_state = runner._session_store.get(terminal_session)
    assert terminal_state is not None
    assert terminal_state.phase != SessionPhase.ENDED
    state = runner._session_store.get(claude_session)
    assert state is not None
    assert state.phase == SessionPhase.WAITING_FOR_INPUT


@pytest.mark.asyncio
async def test_watch_task_follows_late_bound_claude_session_on_first_turn(tmp_path: Path) -> None:
    runner = TmuxRunner(data_dir=str(tmp_path), poll_interval_sec=0.01, partial_flush_sec=0.01)
    terminal_session = "tgcli_user_1"
    claude_session = "claude-session-1"

    runner._session_store.get_or_create(session_id=terminal_session, workdir=str(tmp_path), terminal_id="user_1")

    meta = _TmuxTaskMeta(
        session_name=terminal_session,
        log_file=runner._file_store.raw_transcript_path(terminal_session),
        exit_file=tmp_path / "x1.exit",
        task_id="t4",
        workdir=str(tmp_path),
        terminal_id="user_1",
        persistent_terminal=True,
        interactive=True,
    )

    task = asyncio.create_task(_collect_events(runner._watch_task(meta=meta, timeout_sec=1)))
    await asyncio.sleep(0.03)
    bound = runner._session_store.get_or_create(session_id=claude_session, workdir=str(tmp_path), terminal_id="user_1")
    bound.phase = SessionPhase.PROCESSING
    runner._session_store._persist(bound)
    await asyncio.sleep(0.03)
    bound.turns.append(
        ConversationTurn(
            turn_id="turn-first",
            role="assistant",
            text="\n首轮回复\n",
            is_complete=True,
            source="jsonl",
        )
    )
    bound.checkpoint.last_offset = 1
    bound.phase = SessionPhase.WAITING_FOR_INPUT
    runner._session_store._persist(bound)

    events = await task

    assert meta.claude_session_id == claude_session
    assert [event.type for event in events] == [EventType.EXITED]


@pytest.mark.asyncio
async def test_watch_task_follows_late_bound_uuid_claude_session_on_first_turn(tmp_path: Path) -> None:
    runner = TmuxRunner(data_dir=str(tmp_path), poll_interval_sec=0.01, partial_flush_sec=0.01)
    terminal_session = "tgcli_user_1"
    claude_session = "2185ae1c-14e5-4423-8f0d-1b76fcd893d6"

    runner._session_store.get_or_create(session_id=terminal_session, workdir=str(tmp_path), terminal_id="user_1")

    meta = _TmuxTaskMeta(
        session_name=terminal_session,
        log_file=runner._file_store.raw_transcript_path(terminal_session),
        exit_file=tmp_path / "x1-uuid.exit",
        task_id="t4-uuid",
        workdir=str(tmp_path),
        terminal_id="user_1",
        persistent_terminal=True,
        interactive=True,
    )

    task = asyncio.create_task(_collect_events(runner._watch_task(meta=meta, timeout_sec=1)))
    await asyncio.sleep(0.03)
    bound = runner._session_store.get_or_create(session_id=claude_session, workdir=str(tmp_path), terminal_id="user_1")
    bound.phase = SessionPhase.PROCESSING
    runner._session_store._persist(bound)
    await asyncio.sleep(0.03)
    bound.turns.append(
        ConversationTurn(
            turn_id="turn-first-uuid",
            role="assistant",
            text="\n首轮 UUID 回复\n",
            is_complete=True,
            source="jsonl",
        )
    )
    bound.checkpoint.last_offset = 1
    bound.phase = SessionPhase.WAITING_FOR_INPUT
    runner._session_store._persist(bound)

    events = await task

    assert meta.claude_session_id == claude_session
    assert [event.type for event in events] == [EventType.EXITED]


def test_capture_interactive_baseline_does_not_mark_captured_without_real_claude_session(tmp_path: Path) -> None:
    runner = TmuxRunner(data_dir=str(tmp_path), poll_interval_sec=0.01, partial_flush_sec=0.01)
    terminal_session = "tgcli_user_1"
    runner._session_store.get_or_create(session_id=terminal_session, workdir=str(tmp_path), terminal_id="user_1")
    meta = _TmuxTaskMeta(
        session_name=terminal_session,
        log_file=runner._file_store.raw_transcript_path(terminal_session),
        exit_file=tmp_path / "x-baseline.exit",
        task_id="t-baseline",
        workdir=str(tmp_path),
        terminal_id="user_1",
        persistent_terminal=True,
        interactive=True,
    )

    runner._capture_interactive_baseline(meta=meta)

    assert meta.baseline_captured is False
    assert meta.baseline_offset == 0
    assert meta.baseline_completed_turn_id is None


@pytest.mark.asyncio
async def test_watch_task_late_bound_existing_history_does_not_exit_until_new_turn(tmp_path: Path) -> None:
    runner = TmuxRunner(data_dir=str(tmp_path), poll_interval_sec=0.01, partial_flush_sec=0.01)
    terminal_session = "tgcli_user_1"
    claude_session = "claude-session-1"

    runner._session_store.get_or_create(session_id=terminal_session, workdir=str(tmp_path), terminal_id="user_1")
    meta = _TmuxTaskMeta(
        session_name=terminal_session,
        log_file=runner._file_store.raw_transcript_path(terminal_session),
        exit_file=tmp_path / "x-late-history.exit",
        task_id="t-late-history",
        workdir=str(tmp_path),
        terminal_id="user_1",
        persistent_terminal=True,
        interactive=True,
    )

    old_turn_started_at = utc_now() - timedelta(minutes=1)
    task = asyncio.create_task(_collect_events(runner._watch_task(meta=meta, timeout_sec=1)))
    await asyncio.sleep(0.03)
    state = runner._session_store.get_or_create(session_id=claude_session, workdir=str(tmp_path), terminal_id="user_1")
    state.phase = SessionPhase.WAITING_FOR_INPUT
    state.turns.append(
        ConversationTurn(
            turn_id="turn-old",
            role="assistant",
            text="\n旧回复\n",
            is_complete=True,
            source="jsonl",
            started_at=old_turn_started_at,
        )
    )
    state.checkpoint.last_offset = 10
    runner._session_store._persist(state)
    await asyncio.sleep(0.05)

    assert task.done() is False

    state.turns.append(
        ConversationTurn(
            turn_id="turn-new",
            role="assistant",
            text="\n新回复\n",
            is_complete=True,
            source="jsonl",
        )
    )
    state.checkpoint.last_offset = 20
    state.phase = SessionPhase.WAITING_FOR_INPUT
    runner._session_store._persist(state)

    events = await task

    assert meta.claude_session_id == claude_session
    assert [event.type for event in events] == [EventType.EXITED]


@pytest.mark.asyncio
async def test_watch_task_late_bound_completed_first_turn_exits_without_waiting_for_another_turn(tmp_path: Path) -> None:
    runner = TmuxRunner(data_dir=str(tmp_path), poll_interval_sec=0.01, partial_flush_sec=0.01)
    terminal_session = "tgcli_user_1"
    claude_session = "claude-session-1"

    runner._session_store.get_or_create(session_id=terminal_session, workdir=str(tmp_path), terminal_id="user_1")
    meta = _TmuxTaskMeta(
        session_name=terminal_session,
        log_file=runner._file_store.raw_transcript_path(terminal_session),
        exit_file=tmp_path / "x-late-completed.exit",
        task_id="t-late-completed",
        workdir=str(tmp_path),
        terminal_id="user_1",
        persistent_terminal=True,
        interactive=True,
    )

    task = asyncio.create_task(_collect_events(runner._watch_task(meta=meta, timeout_sec=0.2)))
    await asyncio.sleep(0.03)
    state = runner._session_store.get_or_create(session_id=claude_session, workdir=str(tmp_path), terminal_id="user_1")
    state.turns.append(
        ConversationTurn(
            turn_id="turn-first",
            role="assistant",
            text="\n首轮回复\n",
            is_complete=True,
            source="jsonl",
        )
    )
    state.checkpoint.last_offset = 10
    state.phase = SessionPhase.WAITING_FOR_INPUT
    runner._session_store._persist(state)

    events = await task

    assert meta.claude_session_id == claude_session
    assert [event.type for event in events] == [EventType.EXITED]


@pytest.mark.asyncio
async def test_watch_task_waits_for_stable_completion_candidate_before_exit(tmp_path: Path) -> None:
    runner = TmuxRunner(
        data_dir=str(tmp_path),
        poll_interval_sec=0.01,
        partial_flush_sec=0.01,
        interactive_completion_grace_sec=0.5,
    )
    terminal_session = "tgcli_user_1"
    claude_session = "claude-session-1"

    runner._session_store.get_or_create(session_id=terminal_session, workdir=str(tmp_path), terminal_id="user_1")
    meta = _TmuxTaskMeta(
        session_name=terminal_session,
        log_file=runner._file_store.raw_transcript_path(terminal_session),
        exit_file=tmp_path / "x-stable-completion.exit",
        task_id="t-stable-completion",
        workdir=str(tmp_path),
        terminal_id="user_1",
        persistent_terminal=True,
        interactive=True,
    )

    task = asyncio.create_task(_collect_events(runner._watch_task(meta=meta, timeout_sec=2)))
    await asyncio.sleep(0.03)
    state = runner._session_store.get_or_create(session_id=claude_session, workdir=str(tmp_path), terminal_id="user_1")
    progress_turn_started_at = utc_now()
    state.turns.append(
        ConversationTurn(
            turn_id="turn-progress",
            role="assistant",
            text="\n我先查一下。\n",
            is_complete=True,
            source="jsonl",
            started_at=progress_turn_started_at,
            ended_at=progress_turn_started_at,
        )
    )
    state.checkpoint.last_offset = 10
    state.phase = SessionPhase.WAITING_FOR_INPUT
    runner._session_store._persist(state)
    await asyncio.sleep(0.05)

    assert task.done() is False

    state.tool_calls["tool-after-progress"] = ToolCallRecord(
        tool_use_id="tool-after-progress",
        name="Read",
        status=ToolStatus.SUCCESS,
        started_at=progress_turn_started_at + timedelta(milliseconds=1),
        completed_at=progress_turn_started_at + timedelta(milliseconds=2),
    )
    state.checkpoint.last_offset = 20
    runner._session_store._persist(state)
    await asyncio.sleep(0.08)

    assert task.done() is False

    final_turn_started_at = utc_now()
    state.turns.append(
        ConversationTurn(
            turn_id="turn-final",
            role="assistant",
            text="\n最终结果。\n",
            is_complete=True,
            source="jsonl",
            started_at=final_turn_started_at,
            ended_at=final_turn_started_at,
        )
    )
    state.checkpoint.last_offset = 30
    state.phase = SessionPhase.WAITING_FOR_INPUT
    runner._session_store._persist(state)

    events = await task

    assert meta.claude_session_id == claude_session
    assert [event.type for event in events] == [EventType.EXITED]


@pytest.mark.asyncio
async def test_watch_task_does_not_finish_on_progress_turn_followed_by_tool(tmp_path: Path) -> None:
    runner = TmuxRunner(data_dir=str(tmp_path), poll_interval_sec=0.01, partial_flush_sec=0.01)
    terminal_session = "tgcli_user_1"
    claude_session = "claude-session-1"
    command_started_at = utc_now() - timedelta(seconds=1)

    runner._session_store.get_or_create(session_id=terminal_session, workdir=str(tmp_path), terminal_id="user_1")
    meta = _TmuxTaskMeta(
        session_name=terminal_session,
        log_file=runner._file_store.raw_transcript_path(terminal_session),
        exit_file=tmp_path / "x-progress-tool.exit",
        task_id="t-progress-tool",
        workdir=str(tmp_path),
        terminal_id="user_1",
        persistent_terminal=True,
        interactive=True,
        command_started_at=command_started_at,
    )

    task = asyncio.create_task(_collect_events(runner._watch_task(meta=meta, timeout_sec=1)))
    await asyncio.sleep(0.03)
    state = runner._session_store.get_or_create(session_id=claude_session, workdir=str(tmp_path), terminal_id="user_1")
    progress_turn_started_at = command_started_at + timedelta(milliseconds=100)
    state.turns.append(
        ConversationTurn(
            turn_id="turn-progress",
            role="assistant",
            text="\n我先查一下。\n",
            is_complete=True,
            source="jsonl",
            started_at=progress_turn_started_at,
            ended_at=progress_turn_started_at,
        )
    )
    state.tool_calls["tool-after-progress"] = ToolCallRecord(
        tool_use_id="tool-after-progress",
        name="Read",
        status=ToolStatus.SUCCESS,
        started_at=progress_turn_started_at,
        completed_at=progress_turn_started_at + timedelta(milliseconds=100),
    )
    state.checkpoint.last_offset = 10
    state.phase = SessionPhase.WAITING_FOR_INPUT
    runner._session_store._persist(state)
    await asyncio.sleep(0.05)

    assert task.done() is False

    final_turn_started_at = progress_turn_started_at + timedelta(seconds=1)
    state.turns.append(
        ConversationTurn(
            turn_id="turn-final",
            role="assistant",
            text="\n最终结果。\n",
            is_complete=True,
            source="jsonl",
            started_at=final_turn_started_at,
            ended_at=final_turn_started_at,
        )
    )
    state.checkpoint.last_offset = 20
    state.phase = SessionPhase.WAITING_FOR_INPUT
    runner._session_store._persist(state)

    events = await task

    assert meta.claude_session_id == claude_session
    assert [event.type for event in events] == [EventType.EXITED]


@pytest.mark.asyncio
async def test_watch_task_does_not_finish_while_permission_is_pending(tmp_path: Path) -> None:
    runner = TmuxRunner(data_dir=str(tmp_path), poll_interval_sec=0.01, partial_flush_sec=0.01, interactive_completion_grace_sec=0)
    terminal_session = "tgcli_user_1"
    claude_session = "claude-session-1"
    command_started_at = utc_now() - timedelta(seconds=1)

    runner._session_store.get_or_create(session_id=terminal_session, workdir=str(tmp_path), terminal_id="user_1")
    meta = _TmuxTaskMeta(
        session_name=terminal_session,
        log_file=runner._file_store.raw_transcript_path(terminal_session),
        exit_file=tmp_path / "x-pending-permission.exit",
        task_id="t-pending-permission",
        workdir=str(tmp_path),
        terminal_id="user_1",
        persistent_terminal=True,
        interactive=True,
        command_started_at=command_started_at,
    )

    task = asyncio.create_task(_collect_events(runner._watch_task(meta=meta, timeout_sec=1)))
    await asyncio.sleep(0.03)
    state = runner._session_store.get_or_create(session_id=claude_session, workdir=str(tmp_path), terminal_id="user_1")
    progress_turn_started_at = command_started_at + timedelta(milliseconds=100)
    state.turns.append(
        ConversationTurn(
            turn_id="turn-progress",
            role="assistant",
            text="\n需要执行命令。\n",
            is_complete=True,
            source="jsonl",
            started_at=progress_turn_started_at,
            ended_at=progress_turn_started_at,
        )
    )
    state.tool_calls["tool-pending"] = ToolCallRecord(
        tool_use_id="tool-pending",
        name="Bash",
        status=ToolStatus.WAITING_FOR_APPROVAL,
        started_at=progress_turn_started_at + timedelta(milliseconds=1),
    )
    state.pending_permission = PendingPermission(tool_use_id="tool-pending", tool_name="Bash", tool_input={"command": "pwd"})
    state.checkpoint.last_offset = 10
    state.phase = SessionPhase.WAITING_FOR_APPROVAL
    runner._session_store._persist(state)
    await asyncio.sleep(0.05)

    assert task.done() is False

    state.pending_permission = None
    state.tool_calls["tool-pending"].status = ToolStatus.SUCCESS
    state.tool_calls["tool-pending"].completed_at = progress_turn_started_at + timedelta(milliseconds=200)
    final_turn_started_at = progress_turn_started_at + timedelta(seconds=1)
    state.turns.append(
        ConversationTurn(
            turn_id="turn-final",
            role="assistant",
            text="\n命令执行完了。\n",
            is_complete=True,
            source="jsonl",
            started_at=final_turn_started_at,
            ended_at=final_turn_started_at,
        )
    )
    state.checkpoint.last_offset = 20
    state.phase = SessionPhase.WAITING_FOR_INPUT
    runner._session_store._persist(state)

    events = await task

    assert meta.claude_session_id == claude_session
    assert [event.type for event in events] == [EventType.EXITED]


@pytest.mark.asyncio
async def test_watch_task_ignores_delayed_old_completed_turn_after_captured_baseline(tmp_path: Path) -> None:
    runner = TmuxRunner(data_dir=str(tmp_path), poll_interval_sec=0.01, partial_flush_sec=0.01)
    terminal_session = "tgcli_user_1"
    claude_session = "claude-session-1"
    command_started_at = utc_now()

    runner._session_store.get_or_create(session_id=terminal_session, workdir=str(tmp_path), terminal_id="user_1")
    state = runner._session_store.get_or_create(session_id=claude_session, workdir=str(tmp_path), terminal_id="user_1")
    state.phase = SessionPhase.PROCESSING
    runner._session_store._persist(state)

    meta = _TmuxTaskMeta(
        session_name=terminal_session,
        log_file=runner._file_store.raw_transcript_path(terminal_session),
        exit_file=tmp_path / "x-delayed-old-turn.exit",
        task_id="t-delayed-old-turn",
        workdir=str(tmp_path),
        terminal_id="user_1",
        claude_session_id=claude_session,
        persistent_terminal=True,
        interactive=True,
        baseline_captured=True,
        baseline_offset=0,
        baseline_completed_turn_id=None,
        command_started_at=command_started_at,
    )

    task = asyncio.create_task(_collect_events(runner._watch_task(meta=meta, timeout_sec=1)))
    await asyncio.sleep(0.03)
    state.turns.append(
        ConversationTurn(
            turn_id="turn-delayed-old",
            role="assistant",
            text="\n上一轮延迟回复\n",
            is_complete=True,
            source="jsonl",
            started_at=command_started_at - timedelta(seconds=1),
        )
    )
    state.checkpoint.last_offset = 10
    state.phase = SessionPhase.WAITING_FOR_INPUT
    runner._session_store._persist(state)
    await asyncio.sleep(0.05)

    assert task.done() is False

    state.turns.append(
        ConversationTurn(
            turn_id="turn-current",
            role="assistant",
            text="\n本轮回复\n",
            is_complete=True,
            source="jsonl",
            started_at=command_started_at + timedelta(seconds=1),
        )
    )
    state.checkpoint.last_offset = 20
    state.phase = SessionPhase.WAITING_FOR_INPUT
    runner._session_store._persist(state)

    events = await task

    assert [event.type for event in events] == [EventType.EXITED]


@pytest.mark.asyncio
async def test_watch_task_bound_completed_turn_started_after_command_before_watch_exits(tmp_path: Path) -> None:
    runner = TmuxRunner(data_dir=str(tmp_path), poll_interval_sec=0.01, partial_flush_sec=0.01)
    terminal_session = "tgcli_user_1"
    claude_session = "claude-session-1"
    command_started_at = utc_now() - timedelta(seconds=1)

    runner._session_store.get_or_create(session_id=terminal_session, workdir=str(tmp_path), terminal_id="user_1")
    state = runner._session_store.get_or_create(session_id=claude_session, workdir=str(tmp_path), terminal_id="user_1")
    state.turns.append(
        ConversationTurn(
            turn_id="turn-first",
            role="assistant",
            text="\n首轮回复\n",
            is_complete=True,
            source="jsonl",
            started_at=command_started_at + timedelta(milliseconds=100),
        )
    )
    state.checkpoint.last_offset = 10
    state.phase = SessionPhase.WAITING_FOR_INPUT
    runner._session_store._persist(state)

    meta = _TmuxTaskMeta(
        session_name=terminal_session,
        log_file=runner._file_store.raw_transcript_path(terminal_session),
        exit_file=tmp_path / "x-bound-prewatch-completed.exit",
        task_id="t-bound-prewatch-completed",
        workdir=str(tmp_path),
        terminal_id="user_1",
        claude_session_id=claude_session,
        persistent_terminal=True,
        interactive=True,
        command_started_at=command_started_at,
    )

    events = await _collect_events(runner._watch_task(meta=meta, timeout_sec=0.2))

    assert [event.type for event in events] == [EventType.EXITED]


@pytest.mark.asyncio
async def test_watch_task_late_bound_completed_turn_started_after_command_before_watch_exits(tmp_path: Path) -> None:
    runner = TmuxRunner(data_dir=str(tmp_path), poll_interval_sec=0.01, partial_flush_sec=0.01)
    terminal_session = "tgcli_user_1"
    claude_session = "claude-session-1"
    command_started_at = utc_now() - timedelta(seconds=1)

    runner._session_store.get_or_create(session_id=terminal_session, workdir=str(tmp_path), terminal_id="user_1")
    meta = _TmuxTaskMeta(
        session_name=terminal_session,
        log_file=runner._file_store.raw_transcript_path(terminal_session),
        exit_file=tmp_path / "x-late-prewatch-completed.exit",
        task_id="t-late-prewatch-completed",
        workdir=str(tmp_path),
        terminal_id="user_1",
        persistent_terminal=True,
        interactive=True,
    )
    meta.command_started_at = command_started_at

    task = asyncio.create_task(_collect_events(runner._watch_task(meta=meta, timeout_sec=0.2)))
    await asyncio.sleep(0.03)
    state = runner._session_store.get_or_create(session_id=claude_session, workdir=str(tmp_path), terminal_id="user_1")
    state.turns.append(
        ConversationTurn(
            turn_id="turn-first",
            role="assistant",
            text="\n首轮回复\n",
            is_complete=True,
            source="jsonl",
            started_at=command_started_at + timedelta(milliseconds=100),
        )
    )
    state.checkpoint.last_offset = 10
    state.phase = SessionPhase.WAITING_FOR_INPUT
    runner._session_store._persist(state)

    events = await task

    assert meta.claude_session_id == claude_session
    assert [event.type for event in events] == [EventType.EXITED]


@pytest.mark.asyncio
async def test_watch_task_switches_from_stale_explicit_session_to_newer_bound_session(tmp_path: Path) -> None:
    runner = TmuxRunner(data_dir=str(tmp_path), poll_interval_sec=0.01, partial_flush_sec=0.01)
    terminal_session = "tgcli_user_1"
    old_session = "2185ae1c-14e5-4423-8f0d-1b76fcd893d6"
    new_session = "f5bc22fa-0e77-42f6-a2d3-e422037296f6"

    runner._session_store.get_or_create(session_id=terminal_session, workdir=str(tmp_path), terminal_id="user_1")
    old_state = runner._session_store.get_or_create(session_id=old_session, workdir=str(tmp_path), terminal_id="user_1")
    old_state.phase = SessionPhase.WAITING_FOR_INPUT
    runner._session_store._persist(old_state)

    meta = _TmuxTaskMeta(
        session_name=terminal_session,
        log_file=runner._file_store.raw_transcript_path(terminal_session),
        exit_file=tmp_path / "x1-switch.exit",
        task_id="t4-switch",
        workdir=str(tmp_path),
        terminal_id="user_1",
        claude_session_id=old_session,
        persistent_terminal=True,
        interactive=True,
    )

    task = asyncio.create_task(_collect_events(runner._watch_task(meta=meta, timeout_sec=1)))
    await asyncio.sleep(0.03)
    new_state = runner._session_store.get_or_create(session_id=new_session, workdir=str(tmp_path), terminal_id="user_1")
    new_state.turns.append(
        ConversationTurn(
            turn_id="turn-switch",
            role="assistant",
            text="\n切换后的新回复\n",
            is_complete=True,
            source="jsonl",
        )
    )
    new_state.checkpoint.last_offset = 1
    new_state.phase = SessionPhase.WAITING_FOR_INPUT
    runner._session_store._persist(new_state)

    events = await task

    assert meta.claude_session_id == new_session
    assert [event.type for event in events] == [EventType.EXITED]


@pytest.mark.asyncio
async def test_watch_task_uses_bound_claude_session_for_second_turn(tmp_path: Path) -> None:
    runner = TmuxRunner(data_dir=str(tmp_path), poll_interval_sec=0.01, partial_flush_sec=0.01)
    terminal_session = "tgcli_user_1"
    claude_session = "claude-session-1"

    runner._session_store.get_or_create(session_id=terminal_session, workdir=str(tmp_path), terminal_id="user_1")
    state = runner._session_store.get_or_create(session_id=claude_session, workdir=str(tmp_path), terminal_id="user_1")
    state.phase = SessionPhase.WAITING_FOR_INPUT
    state.turns.append(
        ConversationTurn(
            turn_id="turn-old",
            role="assistant",
            text="\n旧回复\n",
            is_complete=True,
            source="jsonl",
        )
    )
    state.checkpoint.last_offset = 10
    runner._session_store._persist(state)

    meta = _TmuxTaskMeta(
        session_name=terminal_session,
        log_file=runner._file_store.raw_transcript_path(terminal_session),
        exit_file=tmp_path / "x2.exit",
        task_id="t5",
        workdir=str(tmp_path),
        terminal_id="user_1",
        claude_session_id=claude_session,
        persistent_terminal=True,
        interactive=True,
    )

    task = asyncio.create_task(_collect_events(runner._watch_task(meta=meta, timeout_sec=1)))
    await asyncio.sleep(0.03)
    state = runner._session_store.get(claude_session)
    assert state is not None
    assert state.phase == SessionPhase.PROCESSING
    await asyncio.sleep(0.03)
    state.turns.append(
        ConversationTurn(
            turn_id="turn-new",
            role="assistant",
            text="\n第二轮回复\n",
            is_complete=True,
            source="jsonl",
        )
    )
    state.checkpoint.last_offset = 20
    state.phase = SessionPhase.WAITING_FOR_INPUT
    runner._session_store._persist(state)

    events = await task

    assert [event.type for event in events] == [EventType.EXITED]


@pytest.mark.asyncio
async def test_watch_task_exits_when_fast_reply_arrives_before_watch_baseline(tmp_path: Path) -> None:
    runner = TmuxRunner(data_dir=str(tmp_path), poll_interval_sec=0.01, partial_flush_sec=0.01)
    terminal_session = "tgcli_user_1"
    claude_session = "claude-session-1"

    runner._session_store.get_or_create(session_id=terminal_session, workdir=str(tmp_path), terminal_id="user_1")
    state = runner._session_store.get_or_create(session_id=claude_session, workdir=str(tmp_path), terminal_id="user_1")
    state.phase = SessionPhase.WAITING_FOR_INPUT
    state.checkpoint.last_offset = 1
    state.turns.append(
        ConversationTurn(
            turn_id="turn-fast",
            role="assistant",
            text="\n很快返回的新回复\n",
            is_complete=True,
            source="jsonl",
        )
    )
    runner._session_store._persist(state)

    meta = _TmuxTaskMeta(
        session_name=terminal_session,
        log_file=runner._file_store.raw_transcript_path(terminal_session),
        exit_file=tmp_path / "x-fast.exit",
        task_id="t-fast",
        workdir=str(tmp_path),
        terminal_id="user_1",
        claude_session_id=claude_session,
        persistent_terminal=True,
        interactive=True,
        baseline_captured=True,
        baseline_offset=0,
        baseline_completed_turn_id=None,
    )

    events = await _collect_events(runner._watch_task(meta=meta, timeout_sec=1))

    assert [event.type for event in events] == [EventType.EXITED]


@pytest.mark.asyncio
async def test_watch_task_does_not_exit_on_stale_waiting_phase_without_new_progress(tmp_path: Path) -> None:
    runner = TmuxRunner(data_dir=str(tmp_path), poll_interval_sec=0.01, partial_flush_sec=0.01)
    terminal_session = "tgcli_user_1"
    claude_session = "claude-session-1"

    runner._session_store.get_or_create(session_id=terminal_session, workdir=str(tmp_path), terminal_id="user_1")
    state = runner._session_store.get_or_create(session_id=claude_session, workdir=str(tmp_path), terminal_id="user_1")
    state.phase = SessionPhase.WAITING_FOR_INPUT
    state.turns.append(
        ConversationTurn(
            turn_id="turn-old",
            role="assistant",
            text="\n旧回复\n",
            is_complete=True,
            source="jsonl",
        )
    )
    state.checkpoint.last_offset = 10
    runner._session_store._persist(state)

    meta = _TmuxTaskMeta(
        session_name=terminal_session,
        log_file=runner._file_store.raw_transcript_path(terminal_session),
        exit_file=tmp_path / "x2-stale.exit",
        task_id="t5-stale",
        workdir=str(tmp_path),
        terminal_id="user_1",
        claude_session_id=claude_session,
        persistent_terminal=True,
        interactive=True,
    )

    task = asyncio.create_task(_collect_events(runner._watch_task(meta=meta, timeout_sec=1)))
    await asyncio.sleep(0.03)
    state = runner._session_store.get(claude_session)
    assert state is not None
    assert state.phase == SessionPhase.PROCESSING

    state.phase = SessionPhase.WAITING_FOR_INPUT
    runner._session_store._persist(state)
    await asyncio.sleep(0.05)
    assert task.done() is False

    state.turns.append(
        ConversationTurn(
            turn_id="turn-new",
            role="assistant",
            text="\n真正的新回复\n",
            is_complete=True,
            source="jsonl",
        )
    )
    state.checkpoint.last_offset = 20
    state.phase = SessionPhase.WAITING_FOR_INPUT
    runner._session_store._persist(state)

    events = await task

    assert [event.type for event in events] == [EventType.EXITED]


@pytest.mark.asyncio
async def test_watch_task_exits_when_new_completed_turn_arrives_without_waiting_hook(tmp_path: Path) -> None:
    runner = TmuxRunner(data_dir=str(tmp_path), poll_interval_sec=0.01, partial_flush_sec=0.01)
    terminal_session = "tgcli_user_1"
    claude_session = "claude-session-1"

    runner._session_store.get_or_create(session_id=terminal_session, workdir=str(tmp_path), terminal_id="user_1")
    state = runner._session_store.get_or_create(session_id=claude_session, workdir=str(tmp_path), terminal_id="user_1")
    state.phase = SessionPhase.WAITING_FOR_INPUT
    state.turns = []
    runner._session_store._persist(state)

    meta = _TmuxTaskMeta(
        session_name=terminal_session,
        log_file=runner._file_store.raw_transcript_path(terminal_session),
        exit_file=tmp_path / "x3.exit",
        task_id="t6",
        workdir=str(tmp_path),
        terminal_id="user_1",
        claude_session_id=claude_session,
        persistent_terminal=True,
        interactive=True,
    )

    task = asyncio.create_task(_collect_events(runner._watch_task(meta=meta, timeout_sec=1)))
    await asyncio.sleep(0.03)
    state = runner._session_store.get(claude_session)
    assert state is not None
    assert state.phase == SessionPhase.PROCESSING
    state.turns.append(
        ConversationTurn(
            turn_id="turn-new",
            role="assistant",
            text="\n补同步后的新回复\n",
            is_complete=True,
            source="jsonl",
        )
    )
    runner._session_store._persist(state)

    events = await task

    assert [event.type for event in events] == [EventType.EXITED]


@pytest.mark.asyncio
async def test_cancel_returns_false_for_unknown_task() -> None:
    runner = TmuxRunner()
    canceled = await runner.cancel("not-found")
    assert canceled is False


@pytest.mark.asyncio
async def test_run_task_terminates_ephemeral_session_when_watch_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runner = TmuxRunner(data_dir=str(tmp_path), cancel_grace_sec=0)
    terminated: list[str] = []

    async def fake_start(*args, **kwargs):
        return True, ""

    async def fake_watch(*, meta, timeout_sec: int, fifo_reader=None):
        raise RuntimeError("watch failed")
        yield

    async def fake_terminate(session_name: str) -> bool:
        terminated.append(session_name)
        return True

    monkeypatch.setattr(runner, "_start_ephemeral_session", fake_start)
    monkeypatch.setattr(runner, "_watch_task", fake_watch)
    monkeypatch.setattr(runner, "_terminate_session", fake_terminate)

    meta = _TmuxTaskMeta(
        session_name="tgcli_task_1",
        log_file=tmp_path / "task.log",
        exit_file=tmp_path / "task.exit",
        task_id="task-1",
        workdir=str(tmp_path),
        command_file=tmp_path / "task.cmd.sh",
        persistent_terminal=False,
    )

    with pytest.raises(RuntimeError, match="watch failed"):
        await _collect_events(runner._run_task(meta=meta, timeout_sec=1, env=None, workdir=str(tmp_path), command="echo hi"))

    assert terminated == ["tgcli_task_1"]


@pytest.mark.asyncio
async def test_run_task_terminates_ephemeral_session_when_generator_is_closed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runner = TmuxRunner(data_dir=str(tmp_path), cancel_grace_sec=0)
    terminated: list[str] = []

    async def fake_start(*args, **kwargs):
        return True, ""

    async def fake_watch(*, meta, timeout_sec: int, fifo_reader=None):
        await asyncio.sleep(10)
        yield CLIEvent(type=EventType.EXITED, task_id=meta.task_id, exit_code=0)

    async def fake_terminate(session_name: str) -> bool:
        terminated.append(session_name)
        return True

    monkeypatch.setattr(runner, "_start_ephemeral_session", fake_start)
    monkeypatch.setattr(runner, "_watch_task", fake_watch)
    monkeypatch.setattr(runner, "_terminate_session", fake_terminate)

    meta = _TmuxTaskMeta(
        session_name="tgcli_task_close",
        log_file=tmp_path / "close.log",
        exit_file=tmp_path / "close.exit",
        task_id="task-close",
        workdir=str(tmp_path),
        command_file=tmp_path / "close.cmd.sh",
        persistent_terminal=False,
    )
    stream = runner._run_task(meta=meta, timeout_sec=1, env=None, workdir=str(tmp_path), command="echo hi")

    first_event = await anext(stream)
    await stream.aclose()

    assert first_event.type == EventType.STARTED
    assert terminated == ["tgcli_task_close"]


@pytest.mark.asyncio
async def test_run_task_does_not_terminate_persistent_session_when_watch_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runner = TmuxRunner(data_dir=str(tmp_path), cancel_grace_sec=0)
    terminated: list[str] = []

    async def fake_ensure(*args, **kwargs):
        return True, ""

    async def fake_send(*args, **kwargs):
        return True, ""

    async def fake_watch(*, meta, timeout_sec: int, fifo_reader=None):
        raise RuntimeError("watch failed")
        yield

    async def fake_terminate(session_name: str) -> bool:
        terminated.append(session_name)
        return True

    monkeypatch.setattr(runner, "_ensure_persistent_session", fake_ensure)
    monkeypatch.setattr(runner, "_send_command", fake_send)
    monkeypatch.setattr(runner, "_watch_task", fake_watch)
    monkeypatch.setattr(runner, "_terminate_session", fake_terminate)

    meta = _TmuxTaskMeta(
        session_name="tgcli_user_1",
        log_file=tmp_path / "persistent.log",
        exit_file=tmp_path / "persistent.exit",
        task_id="task-persistent",
        workdir=str(tmp_path),
        command_file=tmp_path / "persistent.cmd.sh",
        persistent_terminal=True,
    )

    with pytest.raises(RuntimeError, match="watch failed"):
        await _collect_events(runner._run_task(meta=meta, timeout_sec=1, env=None, workdir=str(tmp_path), command="echo hi"))

    assert terminated == []


@pytest.mark.asyncio
async def test_reveal_terminal_reports_session_check_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = TmuxRunner(cancel_grace_sec=0)

    async def fake_run_tmux(*args: str, input_data: bytes | None = None):
        if args and args[0] == "has-session":
            return 1, "", "permission denied"
        return 0, "", ""

    monkeypatch.setattr(runner, "_run_tmux", fake_run_tmux)

    opened, text = await runner.reveal_terminal("user_1")

    assert opened is False
    assert text == "终端状态检查失败: permission denied"


@pytest.mark.asyncio
async def test_close_terminal_treats_missing_tmux_socket_as_missing_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = TmuxRunner(cancel_grace_sec=0)
    calls: list[tuple[str, ...]] = []

    async def fake_run_tmux(*args: str, input_data: bytes | None = None):
        calls.append(args)
        if args and args[0] == "has-session":
            return 1, "", "error connecting to /private/tmp/tmux-501/default (No such file or directory)"
        return 0, "", ""

    monkeypatch.setattr(runner, "_run_tmux", fake_run_tmux)

    closed, text = await runner.close_terminal("user_1")

    assert closed is False
    assert text == "终端不存在"
    assert not any(args and args[0] == "kill-session" for args in calls)


@pytest.mark.asyncio
async def test_close_terminal_reports_session_check_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = TmuxRunner(cancel_grace_sec=0)
    calls: list[tuple[str, ...]] = []

    async def fake_run_tmux(*args: str, input_data: bytes | None = None):
        calls.append(args)
        if args and args[0] == "has-session":
            return 1, "", "permission denied"
        return 0, "", ""

    monkeypatch.setattr(runner, "_run_tmux", fake_run_tmux)

    closed, text = await runner.close_terminal("user_1")

    assert closed is False
    assert text == "终端状态检查失败: permission denied"
    assert not any(args and args[0] == "kill-session" for args in calls)


@pytest.mark.asyncio
async def test_close_terminal_reports_tmux_binary_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = TmuxRunner(cancel_grace_sec=0, tmux_bin="missing-tmux")

    async def fake_run_tmux(*args: str, input_data: bytes | None = None):
        raise FileNotFoundError("missing-tmux")

    monkeypatch.setattr(runner, "_run_tmux", fake_run_tmux)

    closed, text = await runner.close_terminal("user_1")

    assert closed is False
    assert text == "终端状态检查失败: 找不到 tmux 可执行文件 (missing-tmux)"


@pytest.mark.asyncio
async def test_terminate_session_succeeds_when_session_is_already_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = TmuxRunner(cancel_grace_sec=0)
    calls: list[tuple[str, ...]] = []

    async def fake_run_tmux(*args: str, input_data: bytes | None = None):
        calls.append(args)
        if args and args[0] == "has-session":
            return 1, "", "can't find session"
        return 1, "", "can't find session"

    monkeypatch.setattr(runner, "_run_tmux", fake_run_tmux)

    assert await runner._terminate_session("missing-session") is True
    assert ("kill-session", "-t", "missing-session") not in calls


@pytest.mark.asyncio
async def test_terminate_session_returns_false_when_kill_fails_and_session_remains(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = TmuxRunner(cancel_grace_sec=0)
    calls: list[tuple[str, ...]] = []

    async def fake_run_tmux(*args: str, input_data: bytes | None = None):
        calls.append(args)
        if args and args[0] == "has-session":
            return 0, "", ""
        if args and args[0] == "kill-session":
            return 1, "", "kill failed"
        return 0, "", ""

    monkeypatch.setattr(runner, "_run_tmux", fake_run_tmux)

    assert await runner._terminate_session("stuck-session") is False
    assert ("kill-session", "-t", "stuck-session") in calls


@pytest.mark.asyncio
async def test_cancel_returns_false_when_ephemeral_terminate_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="app.adapters.process.tmux_runner")
    runner = TmuxRunner(data_dir=str(tmp_path))
    meta = _TmuxTaskMeta(
        session_name="tgcli_task_cancel",
        log_file=tmp_path / "cancel.log",
        exit_file=tmp_path / "cancel.exit",
        task_id="task-cancel",
        workdir=str(tmp_path),
        persistent_terminal=False,
    )
    runner._tasks[meta.task_id] = meta

    async def fake_terminate(session_name: str) -> bool:
        assert session_name == "tgcli_task_cancel"
        return False

    monkeypatch.setattr(runner, "_terminate_session", fake_terminate)

    assert await runner.cancel("task-cancel") is False
    assert meta.cancel_requested is True
    assert any(record.message == "tmux task cancel requested" for record in caplog.records)


@pytest.mark.asyncio
async def test_persistent_session_locks_are_ref_counted_and_cleaned(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    current = {"now": 0.0}

    def clock() -> float:
        return current["now"]

    def advance(seconds: float) -> None:
        current["now"] += seconds

    runner = TmuxRunner(
        data_dir=str(tmp_path),
        session_lock_ttl_sec=1,
        lock_cleanup_interval_sec=1,
        lock_cleanup_batch_size=10,
        lock_clock=clock,
    )

    async def fake_run_task(*, meta, timeout_sec: int, env, workdir: str, command: str):
        yield CLIEvent(type=EventType.STARTED, task_id=meta.task_id)
        yield CLIEvent(type=EventType.EXITED, task_id=meta.task_id, exit_code=0)

    monkeypatch.setattr(runner, "_run_task", fake_run_task)

    for idx in range(3):
        events = await _collect_events(
            runner.run(
                task_id=f"task-{idx}",
                argv=["echo", "ok"],
                workdir=str(tmp_path),
                timeout_sec=10,
                terminal_key=f"user-{idx}",
            )
        )
        assert events[-1].type == EventType.EXITED
        advance(2.0)

    assert len(runner._session_locks) <= 1
