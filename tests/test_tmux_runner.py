import asyncio
import json
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


def test_build_shell_command_preserves_injected_claude_settings(tmp_path: Path) -> None:
    runner = TmuxRunner(data_dir=str(tmp_path))
    command_file = tmp_path / "claude.cmd.sh"
    settings_file = tmp_path / "task-claude-settings.json"
    cmd = runner._build_shell_command(
        argv=["claude", "--settings", str(settings_file), "-p", "hello"],
        workdir="/tmp",
        log_file=tmp_path / "x.log",
        exit_file=tmp_path / "x.exit",
        command_file=command_file,
        hide_launcher_line=False,
    )

    assert cmd.startswith("bash ")
    script = command_file.read_text(encoding="utf-8")
    assert f"--settings {settings_file}" in script


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
async def test_ensure_claude_interactive_session_only_ensures_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = TmuxRunner(claude_cli_bin="claude")
    called: dict[str, object] = {}

    async def fake_ensure_persistent_session(session_name: str, *, workdir: str, env=None):
        called["session_name"] = session_name
        called["workdir"] = workdir
        return True, ""

    async def fail_session_current_command(session_name: str) -> str:
        raise AssertionError("should not inspect pane command")

    async def fail_respawn_and_send_command(*, session_name: str, command: str, workdir: str):
        raise AssertionError("should not respawn claude in ensure")

    monkeypatch.setattr(runner, "_ensure_persistent_session", fake_ensure_persistent_session)
    monkeypatch.setattr(runner, "_session_current_command", fail_session_current_command)
    monkeypatch.setattr(runner, "_respawn_and_send_command", fail_respawn_and_send_command)

    ok, err = await runner.ensure_claude_interactive_session(terminal_key="user_1", workdir="/tmp")
    assert ok is True
    assert err == ""
    assert called == {"session_name": "tgcli_user_1", "workdir": "/tmp"}


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
async def test_send_command_interactive_does_not_clear_input_after_enter(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = TmuxRunner()
    clear_calls = 0

    async def fake_run_tmux(*args: str, input_data: bytes | None = None):
        nonlocal clear_calls
        if args[:4] == ("send-keys", "-t", "tgcli_user_1", "C-u"):
            clear_calls += 1
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
    assert clear_calls == 1


@pytest.mark.asyncio
async def test_send_command_does_not_rebuild_when_allow_rebuild_is_false(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = TmuxRunner()

    async def fake_run_tmux(*args: str, input_data: bytes | None = None):
        if args[:4] == ("send-keys", "-t", "tgcli_user_1", "C-u"):
            return 1, "", "pane not found"
        return 0, "", ""

    async def fail_force_rebuild_session(session_name: str, *, workdir: str, env):
        raise AssertionError("allow_rebuild=False should not rebuild session")

    monkeypatch.setattr(runner, "_run_tmux", fake_run_tmux)
    monkeypatch.setattr(runner, "_force_rebuild_session", fail_force_rebuild_session)

    ok, err = await runner._send_command(
        "tgcli_user_1",
        "/exit",
        workdir="/tmp",
        env=None,
        interactive=True,
        allow_rebuild=False,
    )

    assert ok is False
    assert "tmux 清空输入失败" in err
    assert "rebuild_error: 已禁用自动重建" in err


@pytest.mark.asyncio
async def test_interactive_run_respawns_claude_with_settings_and_system_prompt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runner = TmuxRunner(data_dir=str(tmp_path), poll_interval_sec=0.01, partial_flush_sec=0.01)
    captured: dict[str, object] = {}

    async def fake_ensure(*, session_name: str, workdir: str, env=None):
        captured["ensured"] = (session_name, workdir)
        return True, ""

    async def fake_respawn(*, session_name: str, command: str, workdir: str):
        captured["respawn_session"] = session_name
        captured["respawn_workdir"] = workdir
        captured["respawn_command"] = command
        return True, ""

    async def fake_send(session_name: str, command: str, *, workdir: str, env, interactive: bool = False):
        captured.setdefault("sent_commands", []).append((session_name, command, workdir, interactive))
        return True, ""

    async def fake_watch(*, meta, timeout_sec: int):
        captured["settings_file"] = meta.settings_file
        captured["response_file"] = meta.response_file
        yield CLIEvent(type=EventType.EXITED, task_id=meta.task_id, exit_code=0)

    monkeypatch.setattr(runner, "_ensure_claude_interactive_session", fake_ensure)
    monkeypatch.setattr(runner, "_respawn_and_send_command", fake_respawn)
    monkeypatch.setattr(runner, "_send_command", fake_send)
    monkeypatch.setattr(runner, "_watch_task", fake_watch)

    events = [
        e
        async for e in runner.run(
            task_id="t-interactive",
            argv=["hello tmux"],
            workdir=str(tmp_path),
            timeout_sec=10,
            terminal_key="user_1",
            interactive=True,
            provider="claude_code",
        )
    ]

    assert [e.type for e in events] == [EventType.STARTED, EventType.EXITED]
    assert captured["ensured"] == ("tgcli_user_1", str(tmp_path))
    assert captured["respawn_session"] == "tgcli_user_1"
    assert captured["respawn_workdir"] == str(tmp_path)
    assert "--settings" in str(captured["respawn_command"])
    assert "--append-system-prompt" in str(captured["respawn_command"])
    assert "printf '%s' \"$code\" >" in str(captured["respawn_command"])
    assert str(tmp_path / "t-interactive.exit") in str(captured["respawn_command"])
    assert "exec \"${SHELL:-bash}\" -l" in str(captured["respawn_command"])
    assert "hello tmux" not in str(captured["respawn_command"])
    assert captured["sent_commands"] == [("tgcli_user_1", "hello tmux", str(tmp_path), True)]
    assert Path(captured["settings_file"]).exists() is False
    assert Path(captured["response_file"]).exists() is False


@pytest.mark.asyncio
async def test_watch_task_uses_response_file_and_sends_exit_after_stop_reply(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runner = TmuxRunner(data_dir=str(tmp_path), poll_interval_sec=0.01, partial_flush_sec=0.01)
    task_id = "t-stop"
    log_file = tmp_path / f"{task_id}.log"
    exit_file = tmp_path / f"{task_id}.exit"
    response_file = tmp_path / f"{task_id}-stop-response.txt"

    meta = _TmuxTaskMeta(
        session_name="tgcli_user_1",
        log_file=log_file,
        exit_file=exit_file,
        task_id=task_id,
        persistent_terminal=True,
        interactive=True,
        provider="claude_code",
        response_file=response_file,
    )

    sent_commands: list[str] = []

    async def fake_send_command(session_name: str, command: str, *, workdir: str, env, interactive: bool = False, allow_rebuild: bool = True):
        sent_commands.append(command)
        assert session_name == "tgcli_user_1"
        assert interactive is True
        return True, ""

    monkeypatch.setattr(runner, "_send_command", fake_send_command)

    async def writer() -> None:
        await asyncio.sleep(0.02)
        log_file.write_text("noise from pane\n", encoding="utf-8")
        response_file.write_text("final answer", encoding="utf-8")
        await asyncio.sleep(0.05)
        exit_file.write_text("0", encoding="utf-8")

    write_task = asyncio.create_task(writer())
    events = [event async for event in runner._watch_task(meta=meta, timeout_sec=1)]
    await write_task

    assert sent_commands == ["/exit"]
    assert [event.type for event in events] == [EventType.STDOUT, EventType.EXITED]
    assert events[0].content == "final answer"


@pytest.mark.asyncio
async def test_watch_task_interactive_ignores_log_body_for_final_reply(tmp_path: Path) -> None:
    runner = TmuxRunner(data_dir=str(tmp_path), poll_interval_sec=0.01, partial_flush_sec=0.01)
    task_id = "t-response-only"
    log_file = tmp_path / f"{task_id}.log"
    exit_file = tmp_path / f"{task_id}.exit"
    response_file = tmp_path / f"{task_id}-stop-response.txt"

    meta = _TmuxTaskMeta(
        session_name="tgcli_user_1",
        log_file=log_file,
        exit_file=exit_file,
        task_id=task_id,
        persistent_terminal=True,
        interactive=True,
        provider="claude_code",
        response_file=response_file,
    )

    async def writer() -> None:
        await asyncio.sleep(0.02)
        log_file.write_text("assistant pane text that should not be forwarded\n", encoding="utf-8")
        response_file.write_text("response file answer", encoding="utf-8")
        await asyncio.sleep(0.05)
        exit_file.write_text("0", encoding="utf-8")

    write_task = asyncio.create_task(writer())
    events = [event async for event in runner._watch_task(meta=meta, timeout_sec=1)]
    await write_task

    assert [event.type for event in events] == [EventType.STDOUT, EventType.EXITED]
    assert events[0].content == "response file answer"


@pytest.mark.asyncio
async def test_watch_task_interactive_reads_final_reply_even_if_exit_file_appears_first(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runner = TmuxRunner(data_dir=str(tmp_path), poll_interval_sec=0.01, partial_flush_sec=0.01)
    task_id = "t-exit-before-reply"
    log_file = tmp_path / f"{task_id}.log"
    exit_file = tmp_path / f"{task_id}.exit"
    response_file = tmp_path / f"{task_id}-stop-response.txt"

    meta = _TmuxTaskMeta(
        session_name="tgcli_user_1",
        log_file=log_file,
        exit_file=exit_file,
        task_id=task_id,
        persistent_terminal=True,
        interactive=True,
        provider="claude_code",
        response_file=response_file,
    )

    reads = {"count": 0}

    def fake_read_response_once(path: Path) -> str | None:
        assert path == response_file
        exit_file.write_text("0", encoding="utf-8")
        return None

    async def fake_read_response_with_retry(path: Path) -> str | None:
        assert path == response_file
        reads["count"] += 1
        return "late final answer"

    monkeypatch.setattr(runner, "_read_response_once", fake_read_response_once)
    monkeypatch.setattr(runner, "_read_response_with_retry", fake_read_response_with_retry)

    events = [event async for event in runner._watch_task(meta=meta, timeout_sec=1)]

    assert reads["count"] == 1
    assert [event.type for event in events] == [EventType.STDOUT, EventType.EXITED]
    assert events[0].content == "late final answer"
    assert events[1].exit_code == 0


@pytest.mark.asyncio
async def test_watch_task_interactive_exit_send_failure_waits_for_real_exit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runner = TmuxRunner(data_dir=str(tmp_path), poll_interval_sec=0.01, partial_flush_sec=0.01)
    task_id = "t-exit-send-fail"
    log_file = tmp_path / f"{task_id}.log"
    exit_file = tmp_path / f"{task_id}.exit"
    response_file = tmp_path / f"{task_id}-stop-response.txt"

    meta = _TmuxTaskMeta(
        session_name="tgcli_user_1",
        log_file=log_file,
        exit_file=exit_file,
        task_id=task_id,
        persistent_terminal=True,
        interactive=True,
        provider="claude_code",
        response_file=response_file,
    )

    send_attempts: list[str] = []

    def fake_read_response_once(path: Path) -> str | None:
        assert path == response_file
        return "final answer before exit"

    async def fake_send_command(session_name: str, command: str, *, workdir: str, env, interactive: bool = False, allow_rebuild: bool = True):
        send_attempts.append(command)
        assert session_name == "tgcli_user_1"
        assert interactive is True
        exit_file.write_text("0", encoding="utf-8")
        return False, "tmux 执行命令失败"

    monkeypatch.setattr(runner, "_read_response_once", fake_read_response_once)
    monkeypatch.setattr(runner, "_send_command", fake_send_command)

    events = [event async for event in runner._watch_task(meta=meta, timeout_sec=1)]

    assert send_attempts == ["/exit"]
    assert [event.type for event in events] == [EventType.STDOUT, EventType.EXITED]
    assert events[0].content == "final answer before exit"
    assert events[1].exit_code == 0


@pytest.mark.asyncio
async def test_watch_task_interactive_exit_send_failure_waits_for_timeout_and_keeps_response_reply(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runner = TmuxRunner(data_dir=str(tmp_path), poll_interval_sec=0.01, partial_flush_sec=0.01)
    task_id = "t-exit-send-fail-timeout"
    log_file = tmp_path / f"{task_id}.log"
    exit_file = tmp_path / f"{task_id}.exit"
    response_file = tmp_path / f"{task_id}-stop-response.txt"
    log_file.write_text("pane body should be ignored\n", encoding="utf-8")

    meta = _TmuxTaskMeta(
        session_name="tgcli_user_1",
        log_file=log_file,
        exit_file=exit_file,
        task_id=task_id,
        persistent_terminal=True,
        interactive=True,
        provider="claude_code",
        response_file=response_file,
    )

    send_calls: list[tuple[str, bool]] = []

    def fake_read_response_once(path: Path) -> str | None:
        assert path == response_file
        return "reply from response file"

    async def fake_send_command(
        session_name: str,
        command: str,
        *,
        workdir: str,
        env,
        interactive: bool = False,
        allow_rebuild: bool = True,
    ):
        send_calls.append((command, allow_rebuild))
        assert session_name == "tgcli_user_1"
        assert interactive is True
        return False, "tmux 执行命令失败"

    async def fake_interrupt_session(session_name: str) -> bool:
        assert session_name == "tgcli_user_1"
        return True

    monkeypatch.setattr(runner, "_read_response_once", fake_read_response_once)
    monkeypatch.setattr(runner, "_send_command", fake_send_command)
    monkeypatch.setattr(runner, "_interrupt_session", fake_interrupt_session)

    events = [event async for event in runner._watch_task(meta=meta, timeout_sec=0)]

    assert send_calls == [("/exit", False)]
    assert [event.type for event in events] == [EventType.STDOUT, EventType.TIMEOUT]
    assert events[0].content == "reply from response file"


@pytest.mark.asyncio
async def test_watch_task_interactive_timeout_does_not_call_blocking_response_retry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runner = TmuxRunner(data_dir=str(tmp_path), poll_interval_sec=0.01, partial_flush_sec=0.01)
    task_id = "t-timeout-no-retry"
    meta = _TmuxTaskMeta(
        session_name="tgcli_user_1",
        log_file=tmp_path / f"{task_id}.log",
        exit_file=tmp_path / f"{task_id}.exit",
        task_id=task_id,
        persistent_terminal=True,
        interactive=True,
        provider="claude_code",
        response_file=tmp_path / f"{task_id}-stop-response.txt",
    )

    async def fail_read_response_with_retry(path: Path) -> str | None:
        raise AssertionError("timeout path should not call blocking retry reader")

    async def fake_interrupt(session_name: str) -> bool:
        assert session_name == "tgcli_user_1"
        return True

    monkeypatch.setattr(runner, "_read_response_with_retry", fail_read_response_with_retry)
    monkeypatch.setattr(runner, "_interrupt_session", fake_interrupt)

    events = [event async for event in runner._watch_task(meta=meta, timeout_sec=0)]

    assert [event.type for event in events] == [EventType.TIMEOUT]


@pytest.mark.asyncio
async def test_watch_task_timeout_cleans_up_after_interrupt(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runner = TmuxRunner(data_dir=str(tmp_path), poll_interval_sec=0.01, partial_flush_sec=0.01)
    task_id = "t-timeout-watch"
    settings_file = tmp_path / f"{task_id}-claude-settings.json"
    response_file = tmp_path / f"{task_id}-stop-response.txt"
    settings_file.write_text("{}", encoding="utf-8")
    response_file.write_text("", encoding="utf-8")

    meta = _TmuxTaskMeta(
        session_name="tgcli_user_1",
        log_file=tmp_path / f"{task_id}.log",
        exit_file=tmp_path / f"{task_id}.exit",
        task_id=task_id,
        persistent_terminal=True,
        interactive=True,
        provider="claude_code",
        settings_file=settings_file,
        response_file=response_file,
    )

    async def fake_ensure(*, session_name: str, workdir: str, env=None):
        return True, ""

    async def fake_respawn(*, session_name: str, command: str, workdir: str):
        return True, ""

    async def fake_send(session_name: str, command: str, *, workdir: str, env, interactive: bool = False):
        return True, ""

    async def fake_interrupt(session_name: str) -> bool:
        return True

    monkeypatch.setattr(runner, "_ensure_claude_interactive_session", fake_ensure)
    monkeypatch.setattr(runner, "_respawn_and_send_command", fake_respawn)
    monkeypatch.setattr(runner, "_send_command", fake_send)
    monkeypatch.setattr(runner, "_interrupt_session", fake_interrupt)

    events = [
        event
        async for event in runner.run(
            task_id=task_id,
            argv=["hello"],
            workdir=str(tmp_path),
            timeout_sec=0,
            terminal_key="user_1",
            interactive=True,
            provider="claude_code",
        )
    ]

    assert [event.type for event in events] == [EventType.STARTED, EventType.TIMEOUT]
    assert settings_file.exists() is False
    assert response_file.exists() is False


@pytest.mark.asyncio
async def test_watch_task_cancel_cleans_up_after_interrupt(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runner = TmuxRunner(data_dir=str(tmp_path), poll_interval_sec=0.01, partial_flush_sec=0.01)
    task_id = "t-cancel-watch"
    settings_file = tmp_path / f"{task_id}-claude-settings.json"
    response_file = tmp_path / f"{task_id}-stop-response.txt"
    settings_file.write_text("{}", encoding="utf-8")
    response_file.write_text("", encoding="utf-8")

    meta = _TmuxTaskMeta(
        session_name="tgcli_user_1",
        log_file=tmp_path / f"{task_id}.log",
        exit_file=tmp_path / f"{task_id}.exit",
        task_id=task_id,
        persistent_terminal=True,
        interactive=True,
        provider="claude_code",
        settings_file=settings_file,
        response_file=response_file,
        cancel_requested=True,
    )

    async def fake_ensure(*, session_name: str, workdir: str, env=None):
        return True, ""

    async def fake_respawn(*, session_name: str, command: str, workdir: str):
        return True, ""

    async def fake_send(session_name: str, command: str, *, workdir: str, env, interactive: bool = False):
        return True, ""

    async def fake_interrupt(session_name: str) -> bool:
        return True

    async def fake_watch_task(*, meta, timeout_sec: int):
        yield CLIEvent(type=EventType.CANCELED, task_id=meta.task_id, error="任务已取消")

    monkeypatch.setattr(runner, "_ensure_claude_interactive_session", fake_ensure)
    monkeypatch.setattr(runner, "_respawn_and_send_command", fake_respawn)
    monkeypatch.setattr(runner, "_send_command", fake_send)
    monkeypatch.setattr(runner, "_interrupt_session", fake_interrupt)
    monkeypatch.setattr(runner, "_watch_task", fake_watch_task)

    events = [
        event
        async for event in runner.run(
            task_id=task_id,
            argv=["hello"],
            workdir=str(tmp_path),
            timeout_sec=5,
            terminal_key="user_1",
            interactive=True,
            provider="claude_code",
        )
    ]

    assert [event.type for event in events] == [EventType.STARTED, EventType.CANCELED]
    assert settings_file.exists() is False
    assert response_file.exists() is False


def test_run_builds_temp_settings_with_stop_hook(tmp_path: Path) -> None:
    runner = TmuxRunner(data_dir=str(tmp_path))
    task_id = "t-settings"
    settings_file = tmp_path / f"{task_id}-claude-settings.json"
    response_file = tmp_path / f"{task_id}-stop-response.txt"

    artifacts = runner._build_claude_artifacts(task_id=task_id, provider="claude_code", interactive=False)

    assert artifacts is not None
    assert artifacts.settings_file == settings_file
    assert artifacts.response_file == response_file
    config = json.loads(settings_file.read_text(encoding="utf-8"))
    stop_entries = config["hooks"]["Stop"]
    assert stop_entries
    bridge_command = stop_entries[0]["hooks"][0]["command"]
    assert str(response_file) in bridge_command
    assert "write-stop-response" in bridge_command


@pytest.mark.asyncio
async def test_watch_task_non_interactive_emits_response_file_reply_for_claude(tmp_path: Path) -> None:
    runner = TmuxRunner(data_dir=str(tmp_path), poll_interval_sec=0.01, partial_flush_sec=0.01)
    task_id = "t-non-interactive-response"
    log_file = tmp_path / f"{task_id}.log"
    exit_file = tmp_path / f"{task_id}.exit"
    response_file = tmp_path / f"{task_id}-stop-response.txt"

    meta = _TmuxTaskMeta(
        session_name="tgcli_task",
        log_file=log_file,
        exit_file=exit_file,
        task_id=task_id,
        provider="claude_code",
        response_file=response_file,
    )

    async def writer() -> None:
        await asyncio.sleep(0.02)
        log_file.write_text("stream output\n", encoding="utf-8")
        response_file.write_text("reply from response file", encoding="utf-8")
        await asyncio.sleep(0.05)
        exit_file.write_text("0", encoding="utf-8")

    write_task = asyncio.create_task(writer())
    events = [event async for event in runner._watch_task(meta=meta, timeout_sec=1)]
    await write_task

    assert [event.type for event in events] == [EventType.STDOUT, EventType.STDOUT, EventType.EXITED]
    assert events[0].content == "stream output\n"
    assert events[1].content == "reply from response file"
    assert events[2].exit_code == 0


@pytest.mark.asyncio
async def test_run_cleans_claude_artifacts_when_startup_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runner = TmuxRunner(data_dir=str(tmp_path))

    async def fake_start_ephemeral_session(session_name: str, *, workdir: str, env, command: str):
        return False, "tmux 启动失败"

    monkeypatch.setattr(runner, "_start_ephemeral_session", fake_start_ephemeral_session)

    events = [
        event
        async for event in runner.run(
            task_id="t-start-fail",
            argv=["claude", "-p", "hello"],
            workdir=str(tmp_path),
            timeout_sec=5,
            provider="claude_code",
        )
    ]

    settings_files = list(tmp_path.glob("t-start-fail-claude-settings.json"))
    response_files = list(tmp_path.glob("t-start-fail-stop-response.txt"))
    assert [event.type for event in events] == [EventType.FAILED]
    assert settings_files == []
    assert response_files == []


@pytest.mark.asyncio
async def test_interactive_run_cleans_claude_artifacts_when_respawn_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runner = TmuxRunner(data_dir=str(tmp_path))

    async def fake_ensure(*, session_name: str, workdir: str, env=None):
        return True, ""

    async def fake_respawn(*, session_name: str, command: str, workdir: str):
        return False, "tmux respawn 失败"

    monkeypatch.setattr(runner, "_ensure_claude_interactive_session", fake_ensure)
    monkeypatch.setattr(runner, "_respawn_and_send_command", fake_respawn)

    events = [
        event
        async for event in runner.run(
            task_id="t-interactive-respawn-fail",
            argv=["hello"],
            workdir=str(tmp_path),
            timeout_sec=5,
            terminal_key="user_1",
            interactive=True,
            provider="claude_code",
        )
    ]

    assert [event.type for event in events] == [EventType.FAILED]
    assert list(tmp_path.glob("t-interactive-respawn-fail-claude-settings.json")) == []
    assert list(tmp_path.glob("t-interactive-respawn-fail-stop-response.txt")) == []


@pytest.mark.asyncio
async def test_run_cleans_claude_artifacts_after_non_zero_exit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runner = TmuxRunner(data_dir=str(tmp_path))
    seen: dict[str, Path | str] = {}

    async def fake_start_ephemeral_session(session_name: str, *, workdir: str, env, command: str):
        return True, ""

    async def fake_watch_task(*, meta, timeout_sec: int):
        seen["settings_file"] = meta.settings_file
        seen["response_file"] = meta.response_file
        yield CLIEvent(type=EventType.FAILED, task_id=meta.task_id, exit_code=2, error="进程退出码: 2")

    monkeypatch.setattr(runner, "_start_ephemeral_session", fake_start_ephemeral_session)
    monkeypatch.setattr(runner, "_watch_task", fake_watch_task)

    events = [
        event
        async for event in runner.run(
            task_id="t-failed",
            argv=["claude", "-p", "hello"],
            workdir=str(tmp_path),
            timeout_sec=5,
            provider="claude_code",
        )
    ]

    assert [event.type for event in events] == [EventType.STARTED, EventType.FAILED]
    assert seen["settings_file"] is not None
    assert seen["response_file"] is not None
    assert Path(seen["settings_file"]).exists() is False
    assert Path(seen["response_file"]).exists() is False


@pytest.mark.asyncio
async def test_run_cleans_claude_artifacts_after_timeout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runner = TmuxRunner(data_dir=str(tmp_path))
    seen: dict[str, Path] = {}

    async def fake_start_ephemeral_session(session_name: str, *, workdir: str, env, command: str):
        return True, ""

    async def fake_watch_task(*, meta, timeout_sec: int):
        seen["settings_file"] = meta.settings_file
        seen["response_file"] = meta.response_file
        yield CLIEvent(type=EventType.TIMEOUT, task_id=meta.task_id, error="任务超时(5s)")

    monkeypatch.setattr(runner, "_start_ephemeral_session", fake_start_ephemeral_session)
    monkeypatch.setattr(runner, "_watch_task", fake_watch_task)

    events = [
        event
        async for event in runner.run(
            task_id="t-timeout",
            argv=["claude", "-p", "hello"],
            workdir=str(tmp_path),
            timeout_sec=5,
            provider="claude_code",
        )
    ]

    assert [event.type for event in events] == [EventType.STARTED, EventType.TIMEOUT]
    assert Path(seen["settings_file"]).exists() is False
    assert Path(seen["response_file"]).exists() is False


@pytest.mark.asyncio
async def test_run_cleans_claude_artifacts_after_cancel(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runner = TmuxRunner(data_dir=str(tmp_path))
    seen: dict[str, Path] = {}

    async def fake_start_ephemeral_session(session_name: str, *, workdir: str, env, command: str):
        return True, ""

    async def fake_watch_task(*, meta, timeout_sec: int):
        seen["settings_file"] = meta.settings_file
        seen["response_file"] = meta.response_file
        yield CLIEvent(type=EventType.CANCELED, task_id=meta.task_id, error="任务已取消")

    monkeypatch.setattr(runner, "_start_ephemeral_session", fake_start_ephemeral_session)
    monkeypatch.setattr(runner, "_watch_task", fake_watch_task)

    events = [
        event
        async for event in runner.run(
            task_id="t-cancel",
            argv=["claude", "-p", "hello"],
            workdir=str(tmp_path),
            timeout_sec=5,
            provider="claude_code",
        )
    ]

    assert [event.type for event in events] == [EventType.STARTED, EventType.CANCELED]
    assert Path(seen["settings_file"]).exists() is False
    assert Path(seen["response_file"]).exists() is False


@pytest.mark.asyncio
async def test_non_claude_provider_skips_artifacts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runner = TmuxRunner(data_dir=str(tmp_path))
    captured: dict[str, object] = {}

    async def fake_start_ephemeral_session(session_name: str, *, workdir: str, env, command: str):
        captured["command"] = command
        return True, ""

    async def fake_watch_task(*, meta, timeout_sec: int):
        captured["settings_file"] = meta.settings_file
        captured["response_file"] = meta.response_file
        yield CLIEvent(type=EventType.EXITED, task_id=meta.task_id, exit_code=0)

    monkeypatch.setattr(runner, "_start_ephemeral_session", fake_start_ephemeral_session)
    monkeypatch.setattr(runner, "_watch_task", fake_watch_task)

    events = [
        event
        async for event in runner.run(
            task_id="t-non-claude",
            argv=["python", "-c", "print('ok')"],
            workdir=str(tmp_path),
            timeout_sec=5,
            provider="python",
        )
    ]

    assert [event.type for event in events] == [EventType.STARTED, EventType.EXITED]
    assert captured["settings_file"] is None
    assert captured["response_file"] is None
    assert "--settings" not in str(captured["command"])
    assert list(tmp_path.glob("t-non-claude-claude-settings.json")) == []
    assert list(tmp_path.glob("t-non-claude-stop-response.txt")) == []


@pytest.mark.asyncio
async def test_cancel_returns_false_for_unknown_task() -> None:
    runner = TmuxRunner()
    canceled = await runner.cancel("not-found")
    assert canceled is False
