from __future__ import annotations

import asyncio
import shlex
import uuid
from dataclasses import dataclass
from pathlib import Path

from app.adapters.storage.file_session_store import FileSessionStore
from app.domain.models import CLIEvent, EventType
from app.domain.session_models import SessionEvent, SessionEventType, SessionPhase
from app.services.session_store import SessionStore

CCB_BEGIN_PREFIX = "TGCLI_BEGIN"
CCB_DONE_PREFIX = "TGCLI_DONE"
_INTERACTIVE_SYSTEM_PROMPT = (
    "你是 Telegram CLI 网关的后端。直接输出回复正文，不要输出 TGCLI_BEGIN/TGCLI_DONE 等标签。"
)


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


class TmuxRunner:
    def __init__(
        self,
        *,
        tmux_bin: str = "tmux",
        data_dir: str = "/tmp/tg-cli-gateway",
        poll_interval_sec: float = 0.2,
        cancel_grace_sec: float = 1.0,
        enter_delay_sec: float = 0.2,
        partial_flush_sec: float = 0.5,
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
                try:
                    await self._run_tmux("pipe-pane", "-t", meta.session_name)
                except Exception:
                    pass
                pipe_cmd = f"cat >> {shlex.quote(str(meta.log_file))}"
                code, _, err_text = await self._run_tmux("pipe-pane", "-t", meta.session_name, pipe_cmd)
                if code != 0:
                    err = err_text.strip() or "unknown error"
                    yield CLIEvent(type=EventType.FAILED, task_id=meta.task_id, error=f"tmux 管道设置失败: {err}")
                    return
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
            if state is not None and state.session_id.startswith("claude-session-"):
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

    async def _watch_task(self, *, meta: _TmuxTaskMeta, timeout_sec: int):
        position = 0
        latest_completed_turn_id_before_run: str | None = None
        if meta.interactive:
            state = self._session_store.mark_interactive_turn_processing(
                terminal_id=meta.terminal_id,
                workdir=meta.workdir,
                claude_session_id=meta.claude_session_id,
                fallback_session_id=meta.session_name,
            )
            if state is not None:
                if state.session_id.startswith("claude-session-"):
                    meta.claude_session_id = state.session_id
                position = state.checkpoint.last_offset
            latest_completed_turn_id_before_run = self._session_store.latest_completed_assistant_turn_id(
                terminal_id=meta.terminal_id,
                workdir=meta.workdir,
                claude_session_id=meta.claude_session_id,
                fallback_session_id=meta.session_name,
            )
        partial = ""
        timed_out = False
        exit_code: int | None = None
        started_at = asyncio.get_running_loop().time()
        last_partial_emit = started_at

        while True:
            now = asyncio.get_running_loop().time()
            text, new_position = self._read_new_text(meta.log_file, position)
            if text:
                position = new_position
                if meta.interactive:
                    async for out_event in self._process_interactive_chunk(meta=meta, text=text, flush_partial=False, offset=position):
                        yield out_event
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
                completion_phase = self._session_store.interactive_completion_phase(
                    terminal_id=meta.terminal_id,
                    workdir=meta.workdir,
                    claude_session_id=meta.claude_session_id,
                    fallback_session_id=meta.session_name,
                )
                if completion_phase is not None:
                    exit_code = 0
                    break
                latest_completed_turn_id = self._session_store.latest_completed_assistant_turn_id(
                    terminal_id=meta.terminal_id,
                    workdir=meta.workdir,
                    claude_session_id=meta.claude_session_id,
                    fallback_session_id=meta.session_name,
                )
                if latest_completed_turn_id and latest_completed_turn_id != latest_completed_turn_id_before_run:
                    exit_code = 0
                    break

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
                async for out_event in self._process_interactive_chunk(meta=meta, text=text, flush_partial=True, offset=position):
                    yield out_event
            else:
                partial, events = self._split_to_events(task_id=meta.task_id, text=partial + text)
                for event in events:
                    yield event

        if partial and not meta.interactive:
            yield CLIEvent(type=EventType.STDOUT, task_id=meta.task_id, content=partial)

        canceled = await self._is_cancel_requested(meta.task_id)
        should_end_session = (not meta.interactive) or exit_code is not None
        if should_end_session and self._session_store.get(meta.session_name) is not None:
            self._session_store.process(SessionEvent(session_id=meta.session_name, type=SessionEventType.SESSION_ENDED))
        if timed_out:
            yield CLIEvent(type=EventType.TIMEOUT, task_id=meta.task_id, error=f"任务超时({timeout_sec}s)")
        elif canceled:
            yield CLIEvent(type=EventType.CANCELED, task_id=meta.task_id, error="任务已取消")
        elif exit_code == 0:
            yield CLIEvent(type=EventType.EXITED, task_id=meta.task_id, exit_code=0)
        else:
            yield CLIEvent(type=EventType.FAILED, task_id=meta.task_id, exit_code=exit_code, error=f"进程退出码: {exit_code}")

    async def _process_interactive_chunk(self, *, meta: _TmuxTaskMeta, text: str, flush_partial: bool, offset: int):
        state = self._session_store.mark_interactive_turn_processing(
            terminal_id=meta.terminal_id,
            workdir=meta.workdir,
            claude_session_id=meta.claude_session_id,
            fallback_session_id=meta.session_name,
        )
        if state is None:
            if False:
                yield CLIEvent(type=EventType.STDOUT, task_id=meta.task_id, content="")
            return
        session_id = state.session_id
        if session_id.startswith("claude-session-"):
            meta.claude_session_id = session_id
        state.checkpoint.last_offset = offset
        self._session_store.save_checkpoint(session_id, state.checkpoint)
        self._session_store._persist(state)
        if False:
            yield CLIEvent(type=EventType.STDOUT, task_id=meta.task_id, content="")

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

    async def _start_ephemeral_session(self, session_name: str, *, workdir: str, env: dict[str, str] | None, command: str) -> tuple[bool, str]:
        args = ["new-session", "-d", "-s", session_name, "-c", workdir]
        if env:
            for key, value in env.items():
                args.extend(["-e", f"{key}={value}"])
        args.append(command)
        try:
            code, _, err_text = await self._run_tmux(*args)
        except FileNotFoundError:
            return False, f"启动失败: 找不到 tmux 可执行文件 ({self._tmux_bin})"
        except Exception as exc:
            return False, f"tmux 启动异常: {exc}"
        if code == 0:
            return True, ""
        err = err_text.strip() or "unknown error"
        return False, f"tmux 启动失败: {err}"

    async def _ensure_persistent_session(self, session_name: str, *, workdir: str, env: dict[str, str] | None) -> tuple[bool, str]:
        exists = await self._session_exists(session_name)
        if exists:
            return True, ""
        args = ["new-session", "-d", "-s", session_name, "-c", workdir]
        if env:
            for key, value in env.items():
                args.extend(["-e", f"{key}={value}"])
        try:
            code, _, err_text = await self._run_tmux(*args)
        except FileNotFoundError:
            return False, f"启动失败: 找不到 tmux 可执行文件 ({self._tmux_bin})\nhint: 请先安装 tmux，或在 .env 中设置 TMUX_BIN 为正确路径"
        except Exception as exc:
            return False, f"tmux 启动异常: {exc}"
        if code == 0:
            return True, ""
        exists = await self._session_exists(session_name)
        if exists:
            return True, ""
        await self._run_tmux("kill-session", "-t", session_name)
        try:
            retry_code, _, retry_err_text = await self._run_tmux(*args)
        except Exception:
            retry_code = 1
            retry_err_text = ""
        if retry_code == 0:
            return True, ""
        exists = await self._session_exists(session_name)
        if exists:
            return True, ""
        err = (retry_err_text or err_text).strip() or "unknown error"
        return False, f"tmux 会话创建失败: {err}\ntmux_session: {session_name}\nhint: 可先发送 /claude 触发会话重建；若仍失败请检查 tmux 是否可用（tmux ls）"

    async def _ensure_claude_interactive_session(self, *, session_name: str, workdir: str, env: dict[str, str] | None) -> tuple[bool, str]:
        ready, err = await self._ensure_persistent_session(session_name, workdir=workdir, env=env)
        if not ready:
            return False, err
        current_cmd = await self._session_current_command(session_name)
        if "claude" in current_cmd:
            return True, ""
        command = self._build_interactive_claude_command(workdir=workdir)
        respawned, respawn_err = await self._respawn_and_send_command(session_name=session_name, command=command, workdir=workdir)
        if not respawned:
            return False, respawn_err
        return True, ""

    async def _send_command(self, session_name: str, command: str, *, workdir: str, env: dict[str, str] | None, interactive: bool = False) -> tuple[bool, str]:
        try:
            await self._run_tmux("send-keys", "-t", session_name, "-X", "cancel")
            code, _, err_text = await self._run_tmux("send-keys", "-t", session_name, "C-u")
            if code != 0:
                rebuilt, rebuild_err = await self._force_rebuild_session(session_name, workdir=workdir, env=env)
                if not rebuilt:
                    return False, self._format_send_failure(base="tmux 清空输入失败", raw_err=err_text, session_name=session_name, rebuilt=False, rebuild_err=rebuild_err)
                code, _, err_text = await self._run_tmux("send-keys", "-t", session_name, "C-u")
                if code != 0:
                    return False, self._format_send_failure(base="tmux 清空输入失败", raw_err=err_text, session_name=session_name, rebuilt=True)
            buffer_name = f"tgcli-{uuid.uuid4().hex}"
            code, _, err_text = await self._run_tmux("load-buffer", "-b", buffer_name, "-", input_data=command.encode("utf-8"))
            if code != 0:
                return False, self._format_send_failure(base="tmux 加载缓冲区失败", raw_err=err_text, session_name=session_name, rebuilt=False)
            try:
                code, _, err_text = await self._run_tmux("paste-buffer", "-p", "-t", session_name, "-b", buffer_name)
                if code != 0:
                    rebuilt, rebuild_err = await self._force_rebuild_session(session_name, workdir=workdir, env=env)
                    if not rebuilt:
                        return False, self._format_send_failure(base="tmux 粘贴命令失败", raw_err=err_text, session_name=session_name, rebuilt=False, rebuild_err=rebuild_err)
                    code, _, err_text = await self._run_tmux("paste-buffer", "-p", "-t", session_name, "-b", buffer_name)
                    if code != 0:
                        return False, self._format_send_failure(base="tmux 粘贴命令失败", raw_err=err_text, session_name=session_name, rebuilt=True)
                if self._enter_delay_sec > 0:
                    await asyncio.sleep(self._enter_delay_sec)
                enter_key = "C-m" if interactive else "Enter"
                code, _, err_text = await self._run_tmux("send-keys", "-t", session_name, enter_key)
                if code != 0:
                    rebuilt, rebuild_err = await self._force_rebuild_session(session_name, workdir=workdir, env=env)
                    if not rebuilt:
                        return False, self._format_send_failure(base="tmux 执行命令失败", raw_err=err_text, session_name=session_name, rebuilt=False, rebuild_err=rebuild_err)
                    code, _, err_text = await self._run_tmux("send-keys", "-t", session_name, enter_key)
                    if code != 0:
                        return False, self._format_send_failure(base="tmux 执行命令失败", raw_err=err_text, session_name=session_name, rebuilt=True)
                await self._run_tmux("send-keys", "-t", session_name, "C-u")
                return True, ""
            finally:
                await self._run_tmux("delete-buffer", "-b", buffer_name)
        except FileNotFoundError:
            return False, f"启动失败: 找不到 tmux 可执行文件 ({self._tmux_bin})"
        except Exception as exc:
            return False, f"tmux 命令发送异常: {exc}"

    async def _force_rebuild_session(self, session_name: str, *, workdir: str, env: dict[str, str] | None) -> tuple[bool, str]:
        terminated = await self._terminate_session(session_name)
        if not terminated:
            return False, "旧会话关闭失败"
        return await self._ensure_persistent_session(session_name, workdir=workdir, env=env)

    async def _respawn_and_send_command(self, *, session_name: str, command: str, workdir: str) -> tuple[bool, str]:
        args = ["respawn-pane", "-k", "-t", session_name, "-c", workdir, command]
        try:
            code, _, err_text = await self._run_tmux(*args)
        except FileNotFoundError:
            return False, f"tmux 不可用: {self._tmux_bin}"
        except Exception as exc:
            return False, f"tmux respawn 异常: {exc}"
        if code == 0:
            return True, ""
        err = err_text.strip() or "unknown error"
        return False, f"tmux respawn 失败: {err}\ntmux_session: {session_name}\nhint: 请发送 /claude 重新建立会话后再试"

    def _format_send_failure(self, *, base: str, raw_err: str, session_name: str, rebuilt: bool, rebuild_err: str | None = None) -> str:
        err = raw_err.strip() or "unknown error"
        hint = "自动重建已执行但仍失败，请发送 /claude 重新建立会话后再试" if rebuilt else "检测到会话可能失效，已尝试自动重建；如仍失败请发送 /claude 重建后重试"
        rebuilt_text = "是" if rebuilt else "否"
        lines = [f"{base}: {err}", f"tmux_session: {session_name}", f"auto_rebuilt: {rebuilt_text}"]
        if rebuild_err:
            lines.append(f"rebuild_error: {rebuild_err}")
        lines.append(f"hint: {hint}")
        return "\n".join(lines)

    async def _interrupt_session(self, session_name: str) -> bool:
        try:
            code, _, _ = await self._run_tmux("send-keys", "-t", session_name, "C-c")
            return code == 0
        except Exception:
            return False

    async def _terminate_session(self, session_name: str) -> bool:
        try:
            await self._run_tmux("send-keys", "-t", session_name, "C-c")
            await asyncio.sleep(self._cancel_grace_sec)
            exists = await self._session_exists(session_name)
            if exists:
                await self._run_tmux("kill-session", "-t", session_name)
        except Exception:
            return False
        return True

    async def _session_exists(self, session_name: str) -> bool:
        try:
            code, _, _ = await self._run_tmux("has-session", "-t", session_name)
            return code == 0
        except Exception:
            return False

    async def _session_current_command(self, session_name: str) -> str:
        try:
            code, stdout, _ = await self._run_tmux("display-message", "-p", "-t", session_name, "#{pane_current_command}")
            if code != 0:
                return ""
            return stdout.strip()
        except Exception:
            return ""

    async def _is_cancel_requested(self, task_id: str) -> bool:
        async with self._lock:
            meta = self._tasks.get(task_id)
            return bool(meta and meta.cancel_requested)

    async def _run_tmux(self, *args: str, input_data: bytes | None = None) -> tuple[int, str, str]:
        process = await asyncio.create_subprocess_exec(
            self._tmux_bin,
            *args,
            stdin=asyncio.subprocess.PIPE if input_data is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate(input_data)
        return process.returncode or 0, stdout.decode(errors="replace"), stderr.decode(errors="replace")

    def _build_session_name(self, terminal_key: str) -> str:
        sanitized = "".join(ch for ch in terminal_key if ch.isalnum() or ch in {"-", "_"})
        if not sanitized:
            sanitized = "terminal"
        return f"tgcli_{sanitized}"[:64]

    def _build_shell_command(self, *, argv: list[str], workdir: str, log_file: Path, exit_file: Path, command_file: Path, hide_launcher_line: bool) -> str:
        cli_command = shlex.join(argv)
        workdir_target = shlex.quote(str(Path(workdir).resolve()))
        log_target = shlex.quote(str(log_file))
        exit_target = shlex.quote(str(exit_file))
        script = (
            "#!/usr/bin/env bash\n"
            "set -o pipefail\n"
            f"cd {workdir_target}\n"
            f"{cli_command} 2>&1 | tee -a {log_target}\n"
            f"code=${{PIPESTATUS[0]}}\n"
            f"printf '%s' \"$code\" > {exit_target}\n"
        )
        command_file.write_text(script, encoding="utf-8")
        if not hide_launcher_line:
            return f"bash {shlex.quote(str(command_file))}"
        script_target = shlex.quote(str(command_file))
        return f"bash {script_target}; exec \"${{SHELL:-bash}}\" -l"

    def _build_interactive_claude_command(self, *, workdir: str) -> str:
        workdir_target = shlex.quote(str(Path(workdir).resolve()))
        claude_bin = shlex.quote(self._claude_cli_bin)
        system_prompt = shlex.quote(_INTERACTIVE_SYSTEM_PROMPT)
        return f"cd {workdir_target} && exec {claude_bin} --append-system-prompt {system_prompt}"

    def _wrap_interactive_prompt(self, *, prompt: str) -> str:
        safe_prompt = prompt.replace("\r", "").strip()
        if not safe_prompt:
            raise ValueError("prompt 不能为空")
        return safe_prompt

    def _read_new_text(self, path: Path, position: int) -> tuple[str, int]:
        if not path.exists():
            return "", position
        with path.open("rb") as fh:
            fh.seek(position)
            data = fh.read()
            return data.decode("utf-8", errors="replace"), fh.tell()

    def _split_to_events(self, *, task_id: str, text: str) -> tuple[str, list[CLIEvent]]:
        events: list[CLIEvent] = []
        parts = text.splitlines(keepends=True)
        partial = ""
        for chunk in parts:
            if chunk.endswith("\n"):
                events.append(CLIEvent(type=EventType.STDOUT, task_id=task_id, content=chunk))
            else:
                partial += chunk
        return partial, events

    def _read_exit_code(self, path: Path) -> int | None:
        try:
            return int(path.read_text(encoding="utf-8").strip())
        except Exception:
            return None

    def _safe_unlink(self, path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except Exception:
            return

    def _get_session_lock(self, session_name: str) -> asyncio.Lock:
        lock = self._session_locks.get(session_name)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[session_name] = lock
        return lock
