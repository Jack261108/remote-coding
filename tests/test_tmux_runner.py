import asyncio
import json
from pathlib import Path

import pytest

from app.adapters.process.tmux_runner import _TmuxTaskMeta, TmuxRunner
from app.domain.models import CLIEvent, EventType
from app.domain.session_models import ConversationTurn, SessionPhase


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
async def test_interactive_run_rebinds_pipe_to_session_transcript(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runner = TmuxRunner(data_dir=str(tmp_path), poll_interval_sec=0.01, partial_flush_sec=0.01)
    seen_pipe_calls: list[tuple[str, ...]] = []

    async def fake_ensure(*, session_name: str, workdir: str, env=None):
        return True, ""

    async def fake_send(*args, **kwargs):
        return True, ""

    async def fake_watch(*, meta, timeout_sec: int):
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
    assert "cat >>" in seen_pipe_calls[1][3]
    assert "sessions/tgcli_user_1/transcript.raw.log" in seen_pipe_calls[1][3]


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


@pytest.mark.asyncio
async def test_process_interactive_chunk_persists_snapshot_without_stdout(tmp_path: Path) -> None:
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
    text = "TGCLI_BEGIN\n冒泡排序说明\nTGCLI_DONE\n"
    meta.log_file.write_text(text, encoding="utf-8")

    events = [
        event
        async for event in runner._process_interactive_chunk(
            meta=meta,
            text=text,
            flush_partial=False,
            offset=len(text.encode("utf-8")),
        )
    ]

    assert events == []

    state = runner._session_store.get(meta.session_name)
    assert state is not None
    assert state.phase == SessionPhase.PROCESSING
    assert state.turns == []
    assert state.checkpoint.last_offset == len(text.encode("utf-8"))

    raw_text = meta.log_file.read_text(encoding="utf-8")
    assert "冒泡排序说明" in raw_text

    lines = runner._file_store.events_path(meta.session_name).read_text(encoding="utf-8").splitlines()
    records = [json.loads(line) for line in lines]
    assert [item["kind"] for item in records] == ["raw"]
    assert records[0]["text"] == text


@pytest.mark.asyncio
async def test_process_interactive_chunk_uses_checkpoint_offset(tmp_path: Path) -> None:
    runner = TmuxRunner(data_dir=str(tmp_path), poll_interval_sec=0.01, partial_flush_sec=0.01)
    session_name = "tgcli_user_1"
    log_file = runner._file_store.raw_transcript_path(session_name)
    old_text = "TGCLI_BEGIN\n旧回复\nTGCLI_DONE\n"
    new_text = "TGCLI_BEGIN\n新回复\nTGCLI_DONE\n"
    log_file.write_text(old_text + new_text, encoding="utf-8")

    state = runner._session_store.get_or_create(
        session_id=session_name,
        provider="claude_code",
        workdir=str(tmp_path),
        terminal_id="user_1",
    )
    state.checkpoint.last_offset = len(old_text.encode("utf-8"))
    runner._session_store.save_checkpoint(session_name, state.checkpoint)

    meta = _TmuxTaskMeta(
        session_name=session_name,
        log_file=log_file,
        exit_file=tmp_path / "x.exit",
        task_id="t2",
        workdir=str(tmp_path),
        terminal_id="user_1",
        persistent_terminal=True,
        interactive=True,
    )

    events = [
        event
        async for event in runner._process_interactive_chunk(
            meta=meta,
            text=new_text,
            flush_partial=False,
            offset=len((old_text + new_text).encode("utf-8")),
        )
    ]

    assert events == []
    state = runner._session_store.get(session_name)
    assert state is not None
    assert state.checkpoint.last_offset == len((old_text + new_text).encode("utf-8"))


@pytest.mark.asyncio
async def test_interactive_timeout_keeps_session_alive(tmp_path: Path) -> None:
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
    state = runner._session_store.get(session_name)
    assert state is not None
    assert state.phase == SessionPhase.PROCESSING


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
    bound.phase = SessionPhase.WAITING_FOR_INPUT
    runner._session_store._persist(bound)

    events = await task

    assert meta.claude_session_id == claude_session
    assert [event.type for event in events] == [EventType.EXITED]


@pytest.mark.asyncio
async def test_watch_task_uses_bound_claude_session_for_second_turn(tmp_path: Path) -> None:
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
    runner._session_store._persist(state)
    await asyncio.sleep(0.03)
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
