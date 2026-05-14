from __future__ import annotations

import asyncio
import shlex
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app.adapters.process.tmux_commands import TmuxCommandMixin
from app.adapters.process.tmux_log import TmuxLogMixin
from app.adapters.process.tmux_session import TmuxSessionMixin
from app.adapters.storage.file_session_store import FileSessionStore
from app.domain.models import CLIEvent, EventType, utc_now
from app.domain.session_models import ConversationTurn, SessionEvent, SessionEventType, SessionPhase, SessionState, ToolStatus
from app.services.session_store import SessionStore, is_claude_session_id

CCB_BEGIN_PREFIX = "TGCLI_BEGIN"
CCB_DONE_PREFIX = "TGCLI_DONE"
_RECOVERABLE_TMUX_SESSION_ERRORS = (
    "no server running",
    "can't find session",
    "can't find pane",
    "no such session",
    "session not found",
)


def _is_recoverable_tmux_session_error(error_text: str) -> bool:
    normalized = error_text.lower()
    return any(marker in normalized for marker in _RECOVERABLE_TMUX_SESSION_ERRORS)


@dataclass
class _TmuxTaskMeta:
    session_name: str
    log_file: Path
    exit_file: Path
    task_id: str
    workdir: str
    terminal_id: str | None = None
    claude_session_id: str | None = None
    command_file: Path | None = None
    persistent_terminal: bool = False
    cancel_requested: bool = False
    interactive: bool = False
    prompt_text: str = ""
    baseline_captured: bool = False
    baseline_offset: int = 0
    baseline_completed_turn_id: str | None = None
    command_started_at: datetime | None = None


class TmuxRunner(TmuxSessionMixin, TmuxCommandMixin, TmuxLogMixin):
    def __init__(
        self,
        *,
        tmux_bin: str = "tmux",
        data_dir: str = "/tmp/tg-cli-gateway",
        poll_interval_sec: float = 0.2,
        cancel_grace_sec: float = 1.0,
        enter_delay_sec: float = 0.2,
        partial_flush_sec: float = 0.5,
        interactive_completion_grace_sec: float = 0.1,
        claude_cli_bin: str = "claude",
        file_store: FileSessionStore | None = None,
        session_store: SessionStore | None = None,
    ) -> None:
        self._tmux_bin = tmux_bin
        self._data_dir = Path(data_dir)
        self._poll_interval_sec = poll_interval_sec
        self._cancel_grace_sec = cancel_grace_sec
        self._enter_delay_sec = max(0.0, enter_delay_sec)
        self._partial_flush_sec = max(0.0, partial_flush_sec)
        self._interactive_completion_grace_sec = max(0.0, interactive_completion_grace_sec)
        self._claude_cli_bin = claude_cli_bin
        self._tasks: dict[str, _TmuxTaskMeta] = {}
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._lock = asyncio.Lock()
        self._file_store = file_store or FileSessionStore(str(self._data_dir))
        self._session_store = session_store or SessionStore(self._file_store)

    async def run(
        self,
        *,
        task_id: str,
        argv: list[str],
        workdir: str,
        timeout_sec: int,
        env: dict[str, str] | None = None,
        terminal_key: str | None = None,
        interactive: bool = False,
        claude_session_id: str | None = None,
    ):
        if not argv:
            yield CLIEvent(type=EventType.FAILED, task_id=task_id, error="命令参数为空")
            return

        self._data_dir.mkdir(parents=True, exist_ok=True)

        session_id = terminal_key or task_id
        session_name = self._build_session_name(session_id)
        log_file = self._file_store.raw_transcript_path(session_name) if interactive else self._data_dir / f"{task_id}.log"
        exit_file = self._data_dir / f"{task_id}.exit"
        command_file = self._data_dir / f"{task_id}.cmd.sh"
        persistent_terminal = terminal_key is not None

        self._safe_unlink(exit_file)
        self._safe_unlink(command_file)
        if not interactive:
            self._safe_unlink(log_file)

        try:
            if interactive:
                if not persistent_terminal:
                    yield CLIEvent(type=EventType.FAILED, task_id=task_id, error="交互式模式仅支持持久终端")
                    return
                if len(argv) != 1:
                    yield CLIEvent(type=EventType.FAILED, task_id=task_id, error="交互式模式参数错误")
                    return
                prompt = argv[0]
                command = self._wrap_interactive_prompt(prompt=prompt)
            else:
                command = self._build_shell_command(
                    argv=argv,
                    workdir=workdir,
                    log_file=log_file,
                    exit_file=exit_file,
                    command_file=command_file,
                    hide_launcher_line=persistent_terminal,
                )
        except Exception as exc:
            yield CLIEvent(type=EventType.FAILED, task_id=task_id, error=f"任务脚本创建失败: {exc}")
            return

        meta = _TmuxTaskMeta(
            session_name=session_name,
            log_file=log_file,
            exit_file=exit_file,
            task_id=task_id,
            workdir=workdir,
            terminal_id=session_id,
            claude_session_id=claude_session_id,
            command_file=command_file,
            persistent_terminal=persistent_terminal,
            interactive=interactive,
            prompt_text=(argv[0].strip() if interactive and argv else ""),
        )

        self._session_store.get_or_create(
            session_id=session_name,
            provider="claude_code",
            workdir=workdir,
            terminal_id=session_id,
        )

        session_lock = self._get_session_lock(session_name) if persistent_terminal else None
        if session_lock is not None:
            async with session_lock:
                async for event in self._run_task(meta=meta, timeout_sec=timeout_sec, env=env, workdir=workdir, command=command):
                    yield event
            return

        async for event in self._run_task(meta=meta, timeout_sec=timeout_sec, env=env, workdir=workdir, command=command):
            yield event

    async def _run_task(self, *, meta: _TmuxTaskMeta, timeout_sec: int, env: dict[str, str] | None, workdir: str, command: str):
        if meta.persistent_terminal:
            if meta.interactive:
                ready, err = await self._ensure_claude_interactive_session(session_name=meta.session_name, workdir=workdir, env=env)
            else:
                ready, err = await self._ensure_persistent_session(meta.session_name, workdir=workdir, env=env)
            if not ready:
                yield CLIEvent(type=EventType.FAILED, task_id=meta.task_id, error=err)
                return
            if meta.interactive:
                pipe_ready, pipe_err = await self._bind_interactive_pipe(meta=meta, workdir=workdir, env=env)
                if not pipe_ready:
                    yield CLIEvent(type=EventType.FAILED, task_id=meta.task_id, error=pipe_err)
                    return
                self._capture_interactive_baseline(meta=meta)
            meta.command_started_at = utc_now()
            sent, send_err = await self._send_command(meta.session_name, command, workdir=workdir, env=env, interactive=meta.interactive)
            if not sent:
                yield CLIEvent(type=EventType.FAILED, task_id=meta.task_id, error=send_err)
                return
        else:
            started, err = await self._start_ephemeral_session(meta.session_name, workdir=workdir, env=env, command=command)
            if not started:
                yield CLIEvent(type=EventType.FAILED, task_id=meta.task_id, error=err)
                return

        async with self._lock:
            self._tasks[meta.task_id] = meta

        if meta.interactive:
            state = self._session_store.mark_interactive_turn_processing(
                terminal_id=meta.terminal_id,
                workdir=meta.workdir,
                claude_session_id=meta.claude_session_id,
                fallback_session_id=meta.session_name,
            )
            if state is not None and is_claude_session_id(state.session_id):
                meta.claude_session_id = state.session_id
        else:
            self._session_store.process(SessionEvent(session_id=meta.session_name, type=SessionEventType.SESSION_STARTED))
        yield CLIEvent(type=EventType.STARTED, task_id=meta.task_id, content=f"tmux_session={meta.session_name}")

        try:
            async for event in self._watch_task(meta=meta, timeout_sec=timeout_sec):
                yield event
        finally:
            async with self._lock:
                self._tasks.pop(meta.task_id, None)
            if meta.command_file is not None:
                self._safe_unlink(meta.command_file)

    async def _bind_interactive_pipe(self, *, meta: _TmuxTaskMeta, workdir: str, env: dict[str, str] | None) -> tuple[bool, str]:
        await self._clear_interactive_pipe(meta.session_name)
        ok, err = await self._set_interactive_pipe(meta)
        if ok:
            return True, ""
        if not _is_recoverable_tmux_session_error(err):
            return False, f"tmux 管道设置失败: {err}"

        rebuilt, rebuild_err = await self._ensure_claude_interactive_session(
            session_name=meta.session_name,
            workdir=workdir,
            env=env,
        )
        if not rebuilt:
            return False, "\n".join(
                [
                    f"tmux 管道设置失败: {err}",
                    f"tmux_session: {meta.session_name}",
                    "auto_rebuilt: 否",
                    f"rebuild_error: {rebuild_err}",
                    "hint: tmux 会话丢失且自动重建失败，请发送 /claude 重新建立会话后再试",
                ]
            )

        await self._clear_interactive_pipe(meta.session_name)
        ok, retry_err = await self._set_interactive_pipe(meta)
        if ok:
            return True, ""
        return False, "\n".join(
            [
                f"tmux 管道设置失败: {retry_err}",
                f"tmux_session: {meta.session_name}",
                "auto_rebuilt: 是",
                "hint: 自动重建已执行但仍失败，请发送 /claude 重新建立会话后再试",
            ]
        )

    async def _clear_interactive_pipe(self, session_name: str) -> None:
        try:
            await self._run_tmux("pipe-pane", "-t", session_name)
        except Exception:
            pass

    async def _set_interactive_pipe(self, meta: _TmuxTaskMeta) -> tuple[bool, str]:
        pipe_cmd = f"cat >> {shlex.quote(str(meta.log_file))}"
        try:
            code, _, err_text = await self._run_tmux("pipe-pane", "-t", meta.session_name, pipe_cmd)
        except FileNotFoundError:
            return False, f"找不到 tmux 可执行文件 ({self._tmux_bin})"
        except Exception as exc:
            return False, f"tmux pipe-pane 异常: {exc}"
        if code == 0:
            return True, ""
        return False, err_text.strip() or "unknown error"

    async def _watch_task(self, *, meta: _TmuxTaskMeta, timeout_sec: int):
        watch_started_at = utc_now()
        completion_started_after = meta.command_started_at or watch_started_at
        position = 0
        latest_completed_turn_id_before_run: str | None = meta.baseline_completed_turn_id
        saw_interactive_progress = False
        structured_offset_before_run = meta.baseline_offset
        partial = ""
        timed_out = False
        exit_code: int | None = None
        started_at = asyncio.get_running_loop().time()
        last_partial_emit = started_at
        completion_candidate_key: tuple[object, ...] | None = None
        completion_candidate_seen_at: float | None = None

        def completion_turn_ready(state: SessionState, turn: ConversationTurn, *, observed_at: float) -> bool:
            nonlocal completion_candidate_key, completion_candidate_seen_at
            candidate_key = self._interactive_completion_candidate_key(state, turn)
            if candidate_key is None:
                completion_candidate_key = None
                completion_candidate_seen_at = None
                return False
            if candidate_key != completion_candidate_key:
                completion_candidate_key = candidate_key
                completion_candidate_seen_at = observed_at
                return self._interactive_completion_grace_sec <= 0
            return completion_candidate_seen_at is not None and (observed_at - completion_candidate_seen_at) >= self._interactive_completion_grace_sec

        if meta.interactive:
            state = self._session_store.mark_interactive_turn_processing(
                terminal_id=meta.terminal_id,
                workdir=meta.workdir,
                claude_session_id=meta.claude_session_id,
                fallback_session_id=meta.session_name,
            )
            if state is not None:
                if is_claude_session_id(state.session_id):
                    meta.claude_session_id = state.session_id
                    if not meta.baseline_captured:
                        latest_completed_turn = self._latest_completed_assistant_turn(state)
                        current_completed_turn = latest_completed_turn is not None and latest_completed_turn.started_at >= completion_started_after
                        if current_completed_turn and latest_completed_turn is not None:
                            if completion_turn_ready(state, latest_completed_turn, observed_at=started_at):
                                exit_code = 0
                        else:
                            self._record_interactive_baseline(meta=meta, state=state)
                            structured_offset_before_run = meta.baseline_offset
                            latest_completed_turn_id_before_run = meta.baseline_completed_turn_id
                elif not meta.baseline_captured:
                    structured_offset_before_run = state.checkpoint.last_offset
            position = self._interactive_log_position(meta.log_file)
            if latest_completed_turn_id_before_run is None and not meta.baseline_captured:
                latest_completed_turn_id_before_run = self._session_store.latest_completed_assistant_turn_id(
                    terminal_id=meta.terminal_id,
                    workdir=meta.workdir,
                    claude_session_id=meta.claude_session_id,
                    fallback_session_id=meta.session_name,
                )

        while exit_code is None:
            now = asyncio.get_running_loop().time()
            text, new_position = self._read_new_text(meta.log_file, position)
            if text:
                position = new_position
                if meta.interactive:
                    self._process_interactive_chunk(meta=meta, offset=position)
                else:
                    partial, events = self._split_to_events(task_id=meta.task_id, text=partial + text)
                    for event in events:
                        yield event

            if partial and not meta.interactive and self._partial_flush_sec > 0 and (now - last_partial_emit) >= self._partial_flush_sec:
                yield CLIEvent(type=EventType.STDOUT, task_id=meta.task_id, content=partial)
                partial = ""
                last_partial_emit = now

            if meta.interactive:
                resolved_session_id = self._session_store.resolve_interactive_session_id(
                    terminal_id=meta.terminal_id,
                    claude_session_id=meta.claude_session_id,
                    fallback_session_id=meta.session_name,
                    require_claude_session=True,
                )
                if resolved_session_id is not None:
                    meta.claude_session_id = resolved_session_id
                active_state = self._session_store.get_interactive_state(
                    terminal_id=meta.terminal_id,
                    workdir=meta.workdir,
                    claude_session_id=meta.claude_session_id,
                    fallback_session_id=meta.session_name,
                    require_claude_session=True,
                )
                latest_completed_turn = self._latest_completed_assistant_turn(active_state) if active_state is not None else None
                latest_completed_turn_is_current = latest_completed_turn is not None and (
                    (meta.command_started_at is not None and latest_completed_turn.started_at >= completion_started_after)
                    or (meta.command_started_at is None and meta.baseline_captured and latest_completed_turn.turn_id != latest_completed_turn_id_before_run)
                    or (meta.command_started_at is None and not meta.baseline_captured and latest_completed_turn.started_at >= watch_started_at)
                )
                if active_state is not None and is_claude_session_id(active_state.session_id) and not meta.baseline_captured:
                    meta.claude_session_id = active_state.session_id
                    if latest_completed_turn_is_current and latest_completed_turn is not None:
                        if completion_turn_ready(active_state, latest_completed_turn, observed_at=now):
                            exit_code = 0
                            break
                    else:
                        self._record_interactive_baseline(meta=meta, state=active_state)
                        structured_offset_before_run = meta.baseline_offset
                        latest_completed_turn_id_before_run = meta.baseline_completed_turn_id
                if active_state is not None and active_state.checkpoint.last_offset > structured_offset_before_run:
                    if latest_completed_turn is not None and not latest_completed_turn_is_current:
                        structured_offset_before_run = active_state.checkpoint.last_offset
                        latest_completed_turn_id_before_run = latest_completed_turn.turn_id
                        meta.baseline_offset = structured_offset_before_run
                        meta.baseline_completed_turn_id = latest_completed_turn_id_before_run
                    else:
                        saw_interactive_progress = True
                completion_phase = self._session_store.interactive_completion_phase(
                    terminal_id=meta.terminal_id,
                    workdir=meta.workdir,
                    claude_session_id=meta.claude_session_id,
                    fallback_session_id=meta.session_name,
                )
                completion_ready = latest_completed_turn is None or (
                    latest_completed_turn_is_current
                    and active_state is not None
                    and completion_turn_ready(active_state, latest_completed_turn, observed_at=now)
                )
                if completion_phase is not None and saw_interactive_progress and completion_ready:
                    exit_code = 0
                    break
                if latest_completed_turn is not None and latest_completed_turn_is_current:
                    if active_state is not None and completion_turn_ready(active_state, latest_completed_turn, observed_at=now):
                        exit_code = 0
                        break
                elif latest_completed_turn is not None and latest_completed_turn.turn_id != latest_completed_turn_id_before_run:
                    latest_completed_turn_id_before_run = latest_completed_turn.turn_id
                    meta.baseline_completed_turn_id = latest_completed_turn_id_before_run

            if meta.exit_file.exists():
                exit_code = self._read_exit_code(meta.exit_file)
                break

            if (now - started_at) >= timeout_sec:
                timed_out = True
                if meta.persistent_terminal:
                    await self._interrupt_session(meta.session_name)
                else:
                    await self._terminate_session(meta.session_name)
                break

            if await self._is_cancel_requested(meta.task_id):
                if meta.persistent_terminal:
                    await self._interrupt_session(meta.session_name)
                else:
                    await self._terminate_session(meta.session_name)
                break

            await asyncio.sleep(self._poll_interval_sec)

        text, new_position = self._read_new_text(meta.log_file, position)
        if text:
            position = new_position
            if meta.interactive:
                self._process_interactive_chunk(meta=meta, offset=position)
            else:
                partial, events = self._split_to_events(task_id=meta.task_id, text=partial + text)
                for event in events:
                    yield event

        if partial and not meta.interactive:
            yield CLIEvent(type=EventType.STDOUT, task_id=meta.task_id, content=partial)

        canceled = await self._is_cancel_requested(meta.task_id)
        if not meta.interactive and self._session_store.get(meta.session_name) is not None:
            self._session_store.process(SessionEvent(session_id=meta.session_name, type=SessionEventType.SESSION_ENDED))
        if timed_out:
            yield CLIEvent(type=EventType.TIMEOUT, task_id=meta.task_id, error=f"任务超时({timeout_sec}s)")
        elif canceled:
            yield CLIEvent(type=EventType.CANCELED, task_id=meta.task_id, error="任务已取消")
        elif exit_code == 0:
            yield CLIEvent(type=EventType.EXITED, task_id=meta.task_id, exit_code=0)
        else:
            yield CLIEvent(type=EventType.FAILED, task_id=meta.task_id, exit_code=exit_code, error=f"进程退出码: {exit_code}")

    def _process_interactive_chunk(self, *, meta: _TmuxTaskMeta, offset: int) -> None:
        state = self._session_store.mark_interactive_turn_processing(
            terminal_id=meta.terminal_id,
            workdir=meta.workdir,
            claude_session_id=meta.claude_session_id,
            fallback_session_id=meta.session_name,
        )
        if state is None:
            return
        session_id = state.session_id
        if is_claude_session_id(session_id):
            meta.claude_session_id = session_id
        self._session_store._persist(state)

    async def cancel(self, task_id: str) -> bool:
        async with self._lock:
            meta = self._tasks.get(task_id)
            if meta is None:
                return False
            meta.cancel_requested = True
            session_name = meta.session_name
            persistent_terminal = meta.persistent_terminal
        if persistent_terminal:
            return await self._interrupt_session(session_name)
        return await self._terminate_session(session_name)

    def get_session_state(self, terminal_key: str) -> SessionState | None:
        state = self._session_store.get(terminal_key)
        if state is not None:
            return state
        session_name = self._build_session_name(terminal_key)
        return self._session_store.get(session_name)

    async def close_terminal(self, terminal_key: str) -> bool:
        session_name = self._build_session_name(terminal_key)
        exists = await self._session_exists(session_name)
        if not exists:
            return False
        return await self._terminate_session(session_name)

    async def ensure_terminal(self, *, terminal_key: str, workdir: str, env: dict[str, str] | None = None) -> tuple[bool, str]:
        session_name = self._build_session_name(terminal_key)
        session_lock = self._get_session_lock(session_name)
        async with session_lock:
            return await self._ensure_persistent_session(session_name, workdir=workdir, env=env)

    async def ensure_claude_interactive_session(self, *, terminal_key: str, workdir: str, env: dict[str, str] | None = None) -> tuple[bool, str]:
        session_name = self._build_session_name(terminal_key)
        session_lock = self._get_session_lock(session_name)
        async with session_lock:
            return await self._ensure_claude_interactive_session(session_name=session_name, workdir=workdir, env=env)

    async def send_interactive_input(self, *, terminal_key: str, workdir: str, text: str) -> tuple[bool, str]:
        session_name = self._build_session_name(terminal_key)
        prompt = self._wrap_interactive_prompt(prompt=text)
        ready, err = await self._ensure_claude_interactive_session(session_name=session_name, workdir=workdir, env=None)
        if not ready:
            return False, err
        return await self._send_command(session_name, prompt, workdir=workdir, env=None, interactive=True)

    async def select_user_question_option(
        self,
        *,
        terminal_key: str,
        workdir: str,
        option_index: int,
        submit_after: bool = False,
    ) -> tuple[bool, str]:
        session_name = self._build_session_name(terminal_key)
        ready, err = await self._ensure_live_claude_session_for_user_question(session_name=session_name)
        if not ready:
            return False, err
        ok, err = await self._move_user_question_cursor_to_option(session_name=session_name, target_index=option_index)
        if not ok:
            return False, err
        ok, err = await self._send_keys(session_name, "C-m")
        if not ok:
            return False, err
        if submit_after:
            await asyncio.sleep(self._enter_delay_sec)
            ok, err = await self._send_keys(session_name, "C-m")
            if not ok:
                return False, err
        return True, ""

    async def answer_user_question_with_text(
        self,
        *,
        terminal_key: str,
        workdir: str,
        option_count: int,
        text: str,
        submit_after: bool = False,
    ) -> tuple[bool, str]:
        session_name = self._build_session_name(terminal_key)
        ready, err = await self._ensure_live_claude_session_for_user_question(session_name=session_name)
        if not ready:
            return False, err
        ok, err = await self._move_user_question_cursor_to_option(session_name=session_name, target_index=option_count)
        if not ok:
            return False, err
        ok, err = await self._send_keys(session_name, "C-m")
        if not ok:
            return False, err
        await asyncio.sleep(self._enter_delay_sec)
        ok, err = await self._paste_text(session_name, text)
        if not ok:
            return False, err
        ok, err = await self._send_keys(session_name, "C-m")
        if not ok:
            return False, err
        if submit_after:
            await asyncio.sleep(self._enter_delay_sec)
            ok, err = await self._send_keys(session_name, "C-m")
            if not ok:
                return False, err
        return True, ""

    async def advance_user_question_after_multi_select(
        self,
        *,
        terminal_key: str,
        workdir: str,
        final_question: bool,
    ) -> tuple[bool, str]:
        session_name = self._build_session_name(terminal_key)
        ready, err = await self._ensure_live_claude_session_for_user_question(session_name=session_name)
        if not ready:
            return False, err
        ok, err = await self._send_keys(session_name, "Right")
        if not ok:
            return False, err
        if final_question:
            await asyncio.sleep(self._enter_delay_sec)
            ok, err = await self._send_keys(session_name, "C-m")
            if not ok:
                return False, err
        return True, ""

    async def reveal_terminal(self, terminal_key: str) -> tuple[bool, str]:
        session_name = self._build_session_name(terminal_key)
        exists = await self._session_exists(session_name)
        if not exists:
            return False, f"tmux 会话不存在: {session_name}\nhint: 请先发送 /claude 创建会话后再打开桌面终端"
        try:
            process = await asyncio.create_subprocess_exec(
                "osascript",
                "-e",
                'tell application "Terminal"',
                "-e",
                "activate",
                "-e",
                f'do script "tmux attach -t {session_name}"',
                "-e",
                "end tell",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await process.communicate()
        except FileNotFoundError:
            return False, "找不到 osascript（仅支持 macOS 桌面）\nhint: 可手动执行 `tmux attach -t <tmux_session>`"
        except Exception as exc:
            return False, f"打开桌面终端失败: {exc}"
        if process.returncode != 0:
            err = stderr.decode(errors="replace").strip() or stdout.decode(errors="replace").strip() or "unknown error"
            return False, f"打开桌面终端失败: {err}\nhint: 可手动执行 `tmux attach -t {session_name}`"
        return True, f"已在桌面打开 Terminal 并附着到 {session_name}"

    async def _is_cancel_requested(self, task_id: str) -> bool:
        async with self._lock:
            meta = self._tasks.get(task_id)
            return bool(meta and meta.cancel_requested)

    def _get_session_lock(self, session_name: str) -> asyncio.Lock:
        lock = self._session_locks.get(session_name)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[session_name] = lock
        return lock

    def _capture_interactive_baseline(self, *, meta: _TmuxTaskMeta) -> None:
        resolved_session_id = self._session_store.resolve_interactive_session_id(
            terminal_id=meta.terminal_id,
            claude_session_id=meta.claude_session_id,
            fallback_session_id=meta.session_name,
            require_claude_session=True,
        )
        if resolved_session_id is not None:
            meta.claude_session_id = resolved_session_id
        state = self._session_store.get_interactive_state(
            terminal_id=meta.terminal_id,
            workdir=meta.workdir,
            claude_session_id=meta.claude_session_id,
            fallback_session_id=meta.session_name,
            require_claude_session=True,
        )
        if state is None or not is_claude_session_id(state.session_id):
            meta.baseline_captured = False
            meta.baseline_offset = 0
            meta.baseline_completed_turn_id = None
            return
        meta.claude_session_id = state.session_id
        self._record_interactive_baseline(meta=meta, state=state)

    def _record_interactive_baseline(self, *, meta: _TmuxTaskMeta, state: SessionState) -> None:
        meta.baseline_captured = True
        meta.baseline_offset = state.checkpoint.last_offset
        latest_completed_turn = self._latest_completed_assistant_turn(state)
        meta.baseline_completed_turn_id = latest_completed_turn.turn_id if latest_completed_turn is not None else None

    def _latest_completed_assistant_turn(self, state: SessionState) -> ConversationTurn | None:
        return next(
            (turn for turn in reversed(state.turns) if turn.role == "assistant" and turn.is_complete),
            None,
        )

    def _interactive_completion_candidate_key(self, state: SessionState, turn: ConversationTurn) -> tuple[object, ...] | None:
        if state.phase not in {SessionPhase.PROCESSING, SessionPhase.WAITING_FOR_INPUT, SessionPhase.ENDED}:
            return None
        if state.pending_permission is not None:
            return None
        if self._has_active_tool_call(state):
            return None
        if self._has_tool_call_started_after_turn(state, turn):
            return None
        return (
            state.session_id,
            state.revision,
            state.phase.value,
            state.checkpoint.last_offset,
            turn.turn_id,
            tuple(
                sorted(
                    (
                        tool.tool_use_id,
                        tool.status.value,
                        tool.started_at.isoformat(),
                        tool.completed_at.isoformat() if tool.completed_at else "",
                        tuple(
                            sorted(
                                (
                                    subtool.tool_use_id,
                                    subtool.status.value,
                                    subtool.started_at.isoformat(),
                                    subtool.completed_at.isoformat() if subtool.completed_at else "",
                                )
                                for subtool in tool.subagent_tools
                            )
                        ),
                    )
                    for tool in state.tool_calls.values()
                )
            ),
        )

    def _has_active_tool_call(self, state: SessionState) -> bool:
        active_statuses = {ToolStatus.RUNNING, ToolStatus.WAITING_FOR_APPROVAL}
        return any(
            tool.status in active_statuses or any(subtool.status in active_statuses for subtool in tool.subagent_tools)
            for tool in state.tool_calls.values()
        )

    def _has_tool_call_started_after_turn(self, state: SessionState, turn: ConversationTurn) -> bool:
        turn_completed_at = turn.ended_at or turn.started_at
        for tool in state.tool_calls.values():
            if tool.started_at >= turn_completed_at:
                return True
            if any(subtool.started_at >= turn_completed_at for subtool in tool.subagent_tools):
                return True
        return False
