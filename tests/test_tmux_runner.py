from pathlib import Path

import pytest

from app.adapters.process.tmux_runner import _TmuxTaskMeta, TmuxRunner
from app.domain.models import CLIEvent, EventType


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
    assert "exec \"${SHELL:-bash}\" -l" in cmd


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
async def test_interactive_run_rebinds_pipe_to_current_log(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
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
    assert "t-pipe.log" in seen_pipe_calls[1][3]


@pytest.mark.asyncio
async def test_watch_task_flushes_partial_content(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runner = TmuxRunner(data_dir=str(tmp_path), poll_interval_sec=0.01, partial_flush_sec=0.01)
    task_id = "t1"
    log_file = tmp_path / f"{task_id}.log"
    exit_file = tmp_path / f"{task_id}.exit"

    m = _TmuxTaskMeta(
        session_name="tgcli_user_1",
        log_file=log_file,
        exit_file=exit_file,
        task_id=task_id,
        persistent_terminal=False,
    )

    async def writer():
        await asyncio.sleep(0.02)
        log_file.write_text("partial", encoding="utf-8")
        await asyncio.sleep(0.03)
        exit_file.write_text("0", encoding="utf-8")

    import asyncio

    write_task = asyncio.create_task(writer())
    events = [event async for event in runner._watch_task(meta=m, timeout_sec=1)]
    await write_task

    stdout_events = [e for e in events if e.type.value == "STDOUT"]
    assert stdout_events
    assert any((e.content or "") for e in stdout_events)
    assert events[-1].type.value == "EXITED"


def test_process_interactive_text_extracts_reply_in_single_chunk() -> None:
    runner = TmuxRunner()
    m = _TmuxTaskMeta(
        session_name="tgcli_user_1",
        log_file=Path("/tmp/x.log"),
        exit_file=Path("/tmp/x.exit"),
        task_id="t1",
        persistent_terminal=True,
        interactive=True,
        begin_marker="__TGCLI_BEGIN__",
        done_marker="__TGCLI_DONE__",
    )

    event, done = runner._process_interactive_partial(
        meta=m,
        text="noise\n__TGCLI_BEGIN__ t1\n你好，Jack\n__TGCLI_DONE__ t1\n",
    )

    assert done is True
    assert event is not None
    assert event.content in {"\n你好，Jack\n", " t1\n你好，Jack\n"}


def test_process_interactive_text_extracts_reply_across_chunks() -> None:
    runner = TmuxRunner()
    m = _TmuxTaskMeta(
        session_name="tgcli_user_1",
        log_file=Path("/tmp/x.log"),
        exit_file=Path("/tmp/x.exit"),
        task_id="t2",
        persistent_terminal=True,
        interactive=True,
        begin_marker="__TGCLI_BEGIN__",
        done_marker="__TGCLI_DONE__",
    )

    event1, done1 = runner._process_interactive_partial(meta=m, text="__TGCLI_BEGIN__\n你")
    assert event1 is None
    assert done1 is False

    event2, done2 = runner._process_interactive_partial(meta=m, text="好\n__TGCLI_DONE__")
    assert done2 is True
    assert event2 is not None
    assert event2.content == "\n你好\n"


def test_process_interactive_text_strips_ansi_before_marker_match() -> None:
    runner = TmuxRunner()
    m = _TmuxTaskMeta(
        session_name="tgcli_user_1",
        log_file=Path("/tmp/x.log"),
        exit_file=Path("/tmp/x.exit"),
        task_id="t3",
        persistent_terminal=True,
        interactive=True,
        begin_marker="__TGCLI_BEGIN__",
        done_marker="__TGCLI_DONE__",
    )

    ansi_wrapped = "\x1b[0m__TGCLI_BEGIN__\x1b[0m\nreply\n\x1b[0m__TGCLI_DONE__\x1b[0m\n"
    event, done = runner._process_interactive_partial(meta=m, text=ansi_wrapped)

    assert done is True
    assert event is not None
    assert event.content == "\nreply\n"


def test_process_interactive_text_handles_control_chars_between_marker_tokens() -> None:
    runner = TmuxRunner()
    m = _TmuxTaskMeta(
        session_name="tgcli_user_1",
        log_file=Path("/tmp/x.log"),
        exit_file=Path("/tmp/x.exit"),
        task_id="t4",
        persistent_terminal=True,
        interactive=True,
        begin_marker="__TGCLI_BEGIN__",
        done_marker="__TGCLI_DONE__",
    )

    text = "\x1b[?2026l\x1b[?2026h\r\x1b[7A\x1b[38;5;231m⏺\x1b[1C\x1b[39m__TGCLI_BEGIN__\x1bt4\n"
    event1, done1 = runner._process_interactive_partial(meta=m, text=text)
    assert event1 is None
    assert done1 is False

    event2, done2 = runner._process_interactive_partial(meta=m, text="你好\n__TGCLI_DONE__\x1bt4\n")
    assert done2 is True
    assert event2 is not None
    assert event2.content in {"\n你好\n", "t4\n你好\n", " t4\n你好\n"}


def test_process_interactive_text_filters_tmux_ui_noise() -> None:
    runner = TmuxRunner()
    m = _TmuxTaskMeta(
        session_name="tgcli_user_1",
        log_file=Path("/tmp/x.log"),
        exit_file=Path("/tmp/x.exit"),
        task_id="t5",
        persistent_terminal=True,
        interactive=True,
        begin_marker="__TGCLI_BEGIN__",
        done_marker="__TGCLI_DONE__",
    )

    text = (
        "__TGCLI_BEGIN__\n"
        "冒泡排序说明\n"
        "────────────────────────\n"
        "❯\n"
        "esc to interrupt Update available! Run: brew upgrade claude-code\n"
        "__TGCLI_DONE__\n"
    )
    event, done = runner._process_interactive_partial(meta=m, text=text)

    assert done is True
    assert event is not None
    assert event.content == "\n冒泡排序说明\n"


def test_process_interactive_text_filters_marker_artifacts() -> None:
    runner = TmuxRunner()
    m = _TmuxTaskMeta(
        session_name="tgcli_user_1",
        log_file=Path("/tmp/x.log"),
        exit_file=Path("/tmp/x.exit"),
        task_id="t6",
        persistent_terminal=True,
        interactive=True,
        begin_marker="__TGCLI_BEGIN__",
        done_marker="__TGCLI_DONE__",
    )

    text = "__TGCLI_BEGIN__\nTGCLI_BEGIN\n你好\nTGCLI_DONE\n__TGCLI_DONE__\n"
    event, done = runner._process_interactive_partial(meta=m, text=text)

    assert done is True
    assert event is not None
    assert event.content == "\n你好\n"


def test_extract_assistant_reply_chunk_finds_body_between_assistant_and_prompt() -> None:
    runner = TmuxRunner()
    chunk, consumed, completed = runner._extract_assistant_reply_chunk(
        "header\n⏺ 你好，Jack！\n❯ \nfooter\n"
    )

    assert chunk == "你好，Jack！"
    assert consumed > 0
    assert completed is True


def test_extract_assistant_reply_chunk_without_boundary_waits_for_more() -> None:
    runner = TmuxRunner()
    chunk, consumed, completed = runner._extract_assistant_reply_chunk("⏺ 你好，Jack！")

    assert chunk == ""
    assert consumed == 0
    assert completed is False


def test_extract_assistant_reply_chunk_uses_next_assistant_start_as_boundary() -> None:
    runner = TmuxRunner()
    text = "⏺ 你好，Jack！\n⏺ 你好，Jack！\n"
    chunk, consumed, completed = runner._extract_assistant_reply_chunk(text)

    assert chunk.strip() == "你好，Jack！"
    assert consumed > 0
    assert completed is True


def test_process_interactive_text_emits_clean_chunk_without_done_marker() -> None:
    runner = TmuxRunner()
    m = _TmuxTaskMeta(
        session_name="tgcli_user_1",
        log_file=Path("/tmp/x.log"),
        exit_file=Path("/tmp/x.exit"),
        task_id="t7",
        persistent_terminal=True,
        interactive=True,
        begin_marker="TGCLI_BEGIN",
        done_marker="TGCLI_DONE",
        in_reply_block=True,
        prompt_text="你好啊",
    )

    event, done = runner._process_interactive_partial(meta=m, text="⏺ 你好，Jack！\n❯ ")

    assert done is True
    assert event is not None
    assert event.content == "\n你好，Jack！\n"


def test_process_interactive_text_skips_only_noise_without_done_marker() -> None:
    runner = TmuxRunner()
    m = _TmuxTaskMeta(
        session_name="tgcli_user_1",
        log_file=Path("/tmp/x.log"),
        exit_file=Path("/tmp/x.exit"),
        task_id="t8",
        persistent_terminal=True,
        interactive=True,
        begin_marker="TGCLI_BEGIN",
        done_marker="TGCLI_DONE",
        in_reply_block=True,
    )

    event, done = runner._process_interactive_partial(meta=m, text="✢ Forging…\n⎿  Tip: Use /config\n")

    assert done is False
    assert event is None


def test_process_interactive_text_does_not_emit_short_fragment_in_marker_mode() -> None:
    runner = TmuxRunner()
    m = _TmuxTaskMeta(
        session_name="tgcli_user_1",
        log_file=Path("/tmp/x.log"),
        exit_file=Path("/tmp/x.exit"),
        task_id="t9",
        persistent_terminal=True,
        interactive=True,
        begin_marker="__TGCLI_BEGIN__",
        done_marker="__TGCLI_DONE__",
    )

    event1, done1 = runner._process_interactive_partial(meta=m, text="__TGCLI_BEGIN__\n你")
    assert done1 is False
    assert event1 is None

    event2, done2 = runner._process_interactive_partial(meta=m, text="好\n__TGCLI_DONE__")
    assert done2 is True
    assert event2 is not None
    assert event2.content == "\n你好\n"


@pytest.mark.asyncio
async def test_cancel_returns_false_for_unknown_task() -> None:
    runner = TmuxRunner()
    canceled = await runner.cancel("not-found")
    assert canceled is False
