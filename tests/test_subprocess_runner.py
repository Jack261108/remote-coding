import asyncio
from pathlib import Path

import pytest

from app.adapters.process.claude_stop_hook import ClaudeStopArtifacts
from app.adapters.process.subprocess_runner import SubprocessRunner
from app.domain.models import EventType


class FakeStream:
    def __init__(self, lines: list[bytes] | None = None, *, block: bool = False) -> None:
        self._lines = list(lines or [])
        self._block = block

    async def readline(self) -> bytes:
        if self._lines:
            return self._lines.pop(0)
        if self._block:
            await asyncio.Future()
        return b""


class FakeProcess:
    def __init__(
        self,
        *,
        stdout_lines: list[bytes] | None = None,
        stderr_lines: list[bytes] | None = None,
        exit_code: int = 0,
        wait_delay: float = 0.0,
        never_exit: bool = False,
    ) -> None:
        self.stdout = FakeStream(stdout_lines)
        self.stderr = FakeStream(stderr_lines)
        self.returncode: int | None = None
        self._exit_code = exit_code
        self._wait_delay = wait_delay
        self._never_exit = never_exit
        self.terminated = False
        self.killed = False
        self._wait_event = asyncio.Event()

    async def wait(self) -> int:
        if self._never_exit and self.returncode is None:
            await self._wait_event.wait()
        if self._wait_delay:
            await asyncio.sleep(self._wait_delay)
        if self.returncode is None:
            self.returncode = self._exit_code
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        if self._never_exit:
            self.returncode = -15
            self._wait_event.set()

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self._wait_event.set()



@pytest.mark.asyncio
async def test_runner_injects_stop_reply_for_claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings_file = tmp_path / "claude-settings.json"
    response_file = tmp_path / "claude-response.txt"
    response_file.write_text("Claude stop reply", encoding="utf-8")
    artifacts = ClaudeStopArtifacts(settings_file=settings_file, response_file=response_file)
    captured: dict[str, object] = {}

    def fake_build_task_artifacts(*, task_id: str, data_dir: Path) -> ClaudeStopArtifacts:
        captured["task_id"] = task_id
        captured["data_dir"] = data_dir
        settings_file.write_text("{}", encoding="utf-8")
        return artifacts

    async def fake_create_subprocess_exec(*argv, **kwargs):
        captured["argv"] = list(argv)
        captured["cwd"] = kwargs["cwd"]
        return FakeProcess(stdout_lines=[b"stream output\n"])

    monkeypatch.setattr("app.adapters.process.subprocess_runner.build_task_artifacts", fake_build_task_artifacts)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    runner = SubprocessRunner(data_dir=str(tmp_path))
    events = await _collect_events(
        runner.run(
            task_id="claude-task",
            argv=["claude", "-p", "hello"],
            workdir="/tmp",
            timeout_sec=1,
            provider="claude_code",
            interactive=False,
        )
    )

    assert captured["task_id"] == "claude-task"
    assert captured["data_dir"] == tmp_path
    assert captured["argv"] == ["claude", "--settings", str(settings_file), "-p", "hello"]
    assert [event.type for event in events] == [EventType.STARTED, EventType.STDOUT, EventType.STDOUT, EventType.EXITED]
    assert events[1].content == "stream output\n"
    assert events[2].content == "Claude stop reply"
    assert not settings_file.exists()
    assert not response_file.exists()


@pytest.mark.asyncio
async def test_runner_skips_duplicate_stop_reply_when_stdout_already_contains_same_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings_file = tmp_path / "claude-settings-dedup.json"
    response_file = tmp_path / "claude-response-dedup.txt"
    response_file.write_text("Claude stop reply", encoding="utf-8")
    artifacts = ClaudeStopArtifacts(settings_file=settings_file, response_file=response_file)

    def fake_build_task_artifacts(*, task_id: str, data_dir: Path) -> ClaudeStopArtifacts:
        settings_file.write_text("{}", encoding="utf-8")
        return artifacts

    async def fake_create_subprocess_exec(*argv, **kwargs):
        return FakeProcess(stdout_lines=[b"Claude stop reply\n"])

    monkeypatch.setattr("app.adapters.process.subprocess_runner.build_task_artifacts", fake_build_task_artifacts)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    runner = SubprocessRunner(data_dir=str(tmp_path))
    events = await _collect_events(
        runner.run(
            task_id="claude-dedup",
            argv=["claude", "-p", "hello"],
            workdir="/tmp",
            timeout_sec=1,
            provider="claude_code",
            interactive=False,
        )
    )

    assert [event.type for event in events] == [EventType.STARTED, EventType.STDOUT, EventType.EXITED]
    assert events[1].content == "Claude stop reply\n"
    assert not settings_file.exists()
    assert not response_file.exists()


@pytest.mark.asyncio
async def test_runner_cleans_temp_files_when_process_start_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings_file = tmp_path / "claude-settings.json"
    response_file = tmp_path / "claude-response.txt"
    artifacts = ClaudeStopArtifacts(settings_file=settings_file, response_file=response_file)

    def fake_build_task_artifacts(*, task_id: str, data_dir: Path) -> ClaudeStopArtifacts:
        settings_file.write_text("{}", encoding="utf-8")
        response_file.write_text("pending", encoding="utf-8")
        return artifacts

    async def fake_create_subprocess_exec(*argv, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("app.adapters.process.subprocess_runner.build_task_artifacts", fake_build_task_artifacts)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    runner = SubprocessRunner(data_dir=str(tmp_path))
    events = await _collect_events(
        runner.run(
            task_id="start-fail",
            argv=["claude", "-p", "hello"],
            workdir="/tmp",
            timeout_sec=1,
            provider="claude_code",
            interactive=False,
        )
    )

    assert [event.type for event in events] == [EventType.FAILED]
    assert not settings_file.exists()
    assert not response_file.exists()


@pytest.mark.asyncio
async def test_runner_cleans_temp_files_on_non_zero_exit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings_file = tmp_path / "claude-settings.json"
    response_file = tmp_path / "claude-response.txt"
    artifacts = ClaudeStopArtifacts(settings_file=settings_file, response_file=response_file)

    def fake_build_task_artifacts(*, task_id: str, data_dir: Path) -> ClaudeStopArtifacts:
        settings_file.write_text("{}", encoding="utf-8")
        response_file.write_text("Claude failure reply", encoding="utf-8")
        return artifacts

    async def fake_create_subprocess_exec(*argv, **kwargs):
        return FakeProcess(exit_code=7)

    monkeypatch.setattr("app.adapters.process.subprocess_runner.build_task_artifacts", fake_build_task_artifacts)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    runner = SubprocessRunner(data_dir=str(tmp_path))
    events = await _collect_events(
        runner.run(
            task_id="non-zero",
            argv=["claude", "-p", "hello"],
            workdir="/tmp",
            timeout_sec=1,
            provider="claude_code",
            interactive=False,
        )
    )

    assert [event.type for event in events] == [EventType.STARTED, EventType.STDOUT, EventType.FAILED]
    assert events[1].content == "Claude failure reply"
    assert not settings_file.exists()
    assert not response_file.exists()


@pytest.mark.asyncio
async def test_runner_timeout_cleans_temp_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings_file = tmp_path / "claude-settings.json"
    response_file = tmp_path / "claude-response.txt"
    artifacts = ClaudeStopArtifacts(settings_file=settings_file, response_file=response_file)

    def fake_build_task_artifacts(*, task_id: str, data_dir: Path) -> ClaudeStopArtifacts:
        settings_file.write_text("{}", encoding="utf-8")
        response_file.write_text("Claude timeout reply", encoding="utf-8")
        return artifacts

    async def fake_create_subprocess_exec(*argv, **kwargs):
        return FakeProcess(wait_delay=0.2, never_exit=True)

    monkeypatch.setattr("app.adapters.process.subprocess_runner.build_task_artifacts", fake_build_task_artifacts)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    runner = SubprocessRunner(kill_grace_sec=0.01, data_dir=str(tmp_path))
    events = await _collect_events(
        runner.run(
            task_id="timeout",
            argv=["claude", "-p", "hello"],
            workdir="/tmp",
            timeout_sec=0.01,
            provider="claude_code",
            interactive=False,
        )
    )

    assert events[0].type == EventType.STARTED
    assert events[-2].type == EventType.STDOUT
    assert events[-2].content == "Claude timeout reply"
    assert events[-1].type == EventType.TIMEOUT
    assert not settings_file.exists()
    assert not response_file.exists()


@pytest.mark.asyncio
async def test_runner_cancel_cleans_temp_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings_file = tmp_path / "claude-settings.json"
    response_file = tmp_path / "claude-response.txt"
    artifacts = ClaudeStopArtifacts(settings_file=settings_file, response_file=response_file)

    def fake_build_task_artifacts(*, task_id: str, data_dir: Path) -> ClaudeStopArtifacts:
        settings_file.write_text("{}", encoding="utf-8")
        response_file.write_text("Claude cancel reply", encoding="utf-8")
        return artifacts

    async def fake_create_subprocess_exec(*argv, **kwargs):
        return FakeProcess(wait_delay=0.2, never_exit=True)

    monkeypatch.setattr("app.adapters.process.subprocess_runner.build_task_artifacts", fake_build_task_artifacts)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    runner = SubprocessRunner(kill_grace_sec=0.01, data_dir=str(tmp_path))
    task = asyncio.create_task(
        _collect_events(
            runner.run(
                task_id="cancel",
                argv=["claude", "-p", "hello"],
                workdir="/tmp",
                timeout_sec=10,
                provider="claude_code",
                interactive=False,
            )
        )
    )

    await asyncio.sleep(0.01)
    canceled = await runner.cancel("cancel")
    assert canceled is True

    events = await task
    assert events[0].type == EventType.STARTED
    assert events[-2].type == EventType.STDOUT
    assert events[-2].content == "Claude cancel reply"
    assert events[-1].type == EventType.CANCELED
    assert not settings_file.exists()
    assert not response_file.exists()


@pytest.mark.asyncio
async def test_runner_timeout() -> None:
    runner = SubprocessRunner(kill_grace_sec=0.2)

    events = []
    async for event in runner.run(
        task_id="t1",
        argv=["python3", "-c", "import time; time.sleep(2)"],
        workdir="/tmp",
        timeout_sec=1,
    ):
        events.append(event)

    assert events[0].type == EventType.STARTED
    assert events[-1].type == EventType.TIMEOUT


@pytest.mark.asyncio
async def test_runner_cancel() -> None:
    runner = SubprocessRunner(kill_grace_sec=0.2)

    task = asyncio.create_task(
        _collect_events(
            runner.run(
                task_id="t2",
                argv=["python3", "-c", "import time; time.sleep(5)"],
                workdir="/tmp",
                timeout_sec=10,
            )
        )
    )

    await asyncio.sleep(0.3)
    canceled = await runner.cancel("t2")
    assert canceled is True

    events = await task
    assert events[0].type == EventType.STARTED
    assert events[-1].type == EventType.CANCELED


@pytest.mark.asyncio
async def test_runner_returns_failed_event_when_build_task_artifacts_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_build_task_artifacts(*, task_id: str, data_dir: Path) -> ClaudeStopArtifacts:
        raise RuntimeError("artifact boom")

    monkeypatch.setattr("app.adapters.process.subprocess_runner.build_task_artifacts", fake_build_task_artifacts)

    runner = SubprocessRunner(data_dir=str(tmp_path))
    events = await _collect_events(
        runner.run(
            task_id="artifact-build-fail",
            argv=["claude", "-p", "hello"],
            workdir="/tmp",
            timeout_sec=1,
            provider="claude_code",
            interactive=False,
        )
    )

    assert [event.type for event in events] == [EventType.FAILED]
    assert "artifact boom" in (events[0].error or "")


@pytest.mark.asyncio
async def test_runner_reads_response_file_after_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings_file = tmp_path / "claude-settings.json"
    response_file = tmp_path / "claude-response.txt"
    artifacts = ClaudeStopArtifacts(settings_file=settings_file, response_file=response_file)
    read_attempts = 0

    def fake_build_task_artifacts(*, task_id: str, data_dir: Path) -> ClaudeStopArtifacts:
        settings_file.write_text("{}", encoding="utf-8")
        return artifacts

    async def fake_create_subprocess_exec(*argv, **kwargs):
        return FakeProcess()

    original_read_text = Path.read_text

    def fake_read_text(self: Path, *args, **kwargs) -> str:
        nonlocal read_attempts
        if self != response_file:
            return original_read_text(self, *args, **kwargs)
        read_attempts += 1
        if read_attempts == 1:
            raise FileNotFoundError
        if read_attempts == 2:
            return "   "
        return "Claude retried stop reply"

    monkeypatch.setattr("app.adapters.process.subprocess_runner.build_task_artifacts", fake_build_task_artifacts)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(Path, "read_text", fake_read_text)

    runner = SubprocessRunner(data_dir=str(tmp_path))
    events = await _collect_events(
        runner.run(
            task_id="response-retry",
            argv=["claude", "-p", "hello"],
            workdir="/tmp",
            timeout_sec=1,
            provider="claude_code",
            interactive=False,
        )
    )

    assert [event.type for event in events] == [EventType.STARTED, EventType.STDOUT, EventType.EXITED]
    assert events[1].content == "Claude retried stop reply"
    assert read_attempts == 3
    assert not settings_file.exists()
    assert not response_file.exists()


@pytest.mark.asyncio
async def test_runner_skips_claude_artifacts_for_non_claude_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def fail_build_task_artifacts(*, task_id: str, data_dir: Path) -> ClaudeStopArtifacts:
        raise AssertionError("build_task_artifacts should not be called for non-claude providers")

    async def fake_create_subprocess_exec(*argv, **kwargs):
        captured["argv"] = list(argv)
        captured["cwd"] = kwargs["cwd"]
        return FakeProcess(stdout_lines=[b"plain output\n"])

    monkeypatch.setattr("app.adapters.process.subprocess_runner.build_task_artifacts", fail_build_task_artifacts)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    runner = SubprocessRunner(data_dir=str(tmp_path))
    events = await _collect_events(
        runner.run(
            task_id="non-claude",
            argv=["codex", "exec", "hello"],
            workdir="/tmp",
            timeout_sec=1,
            provider="codex",
            interactive=False,
        )
    )

    assert captured["argv"] == ["codex", "exec", "hello"]
    assert "--settings" not in captured["argv"]
    assert [event.type for event in events] == [EventType.STARTED, EventType.STDOUT, EventType.EXITED]
    assert events[1].content == "plain output\n"


@pytest.mark.asyncio
async def test_runner_skips_claude_artifacts_for_interactive_claude(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def fail_build_task_artifacts(*, task_id: str, data_dir: Path) -> ClaudeStopArtifacts:
        raise AssertionError("build_task_artifacts should not be called for interactive claude runs")

    async def fake_create_subprocess_exec(*argv, **kwargs):
        captured["argv"] = list(argv)
        captured["cwd"] = kwargs["cwd"]
        return FakeProcess(stdout_lines=[b"interactive output\n"])

    monkeypatch.setattr("app.adapters.process.subprocess_runner.build_task_artifacts", fail_build_task_artifacts)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    runner = SubprocessRunner(data_dir=str(tmp_path))
    events = await _collect_events(
        runner.run(
            task_id="interactive-claude",
            argv=["claude", "chat"],
            workdir="/tmp",
            timeout_sec=1,
            provider="claude_code",
            interactive=True,
        )
    )

    assert captured["argv"] == ["claude", "chat"]
    assert "--settings" not in captured["argv"]
    assert [event.type for event in events] == [EventType.STARTED, EventType.STDOUT, EventType.EXITED]
    assert events[1].content == "interactive output\n"


async def _collect_events(stream):
    result = []
    async for event in stream:
        result.append(event)
    return result
