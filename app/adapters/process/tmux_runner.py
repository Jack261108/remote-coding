from __future__ import annotations

import asyncio
import shlex
import uuid
from dataclasses import dataclass
from pathlib import Path

from app.adapters.process.claude_stop_hook import ClaudeStopArtifacts, build_task_artifacts
from app.domain.models import CLIEvent, EventType

_INTERACTIVE_SYSTEM_PROMPT = "你是 Telegram CLI 网关的后端。直接输出回复正文。"


@dataclass
class _TmuxTaskMeta:
    session_name: str
    log_file: Path
    exit_file: Path
    task_id: str
    command_file: Path | None = None
    persistent_terminal: bool = False
    cancel_requested: bool = False
    interactive: bool = False
    provider: str | None = None
    settings_file: Path | None = None
    response_file: Path | None = None
    stop_exit_sent: bool = False


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
        provider: str | None = None,
    ):
        if not argv:
            yield CLIEvent(type=EventType.FAILED, task_id=task_id, error="命令参数为空")
            return

        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            yield CLIEvent(type=EventType.FAILED, task_id=task_id, error=f"tmux 数据目录创建失败: {exc}")
            return

        session_id = terminal_key or task_id
        session_name = self._build_session_name(session_id)
        log_file = self._data_dir / f"{task_id}.log"
        exit_file = self._data_dir / f"{task_id}.exit"
        command_file = self._data_dir / f"{task_id}.cmd.sh"
        persistent_terminal = terminal_key is not None

        self._safe_unlink(log_file)
        self._safe_unlink(exit_file)
        self._safe_unlink(command_file)

        artifacts: ClaudeStopArtifacts | None = None
        run_argv = list(argv)

        try:
            artifacts = self._build_claude_artifacts(task_id=task_id, provider=provider, interactive=interactive)
            if artifacts is not None and not interactive:
                run_argv = self._inject_claude_settings(run_argv, artifacts)

            if interactive:
                if not persistent_terminal:
                    yield CLIEvent(type=EventType.FAILED, task_id=task_id, error="交互式模式仅支持持久终端")
                    return
                if len(argv) != 1:
                    yield CLIEvent(type=EventType.FAILED, task_id=task_id, error="交互式模式参数错误")
                    return

                command = self._wrap_interactive_prompt(prompt=argv[0])
            else:
                command = self._build_shell_command(
                    argv=run_argv,
                    workdir=workdir,
                    log_file=log_file,
                    exit_file=exit_file,
                    command_file=command_file,
                    hide_launcher_line=persistent_terminal,
                )
        except Exception as exc:
            self._cleanup_artifacts(artifacts)
            yield CLIEvent(type=EventType.FAILED, task_id=task_id, error=f"任务脚本创建失败: {exc}")
            return

        meta = _TmuxTaskMeta(
            session_name=session_name,
            log_file=log_file,
            exit_file=exit_file,
            task_id=task_id,
            command_file=command_file,
            persistent_terminal=persistent_terminal,
            interactive=interactive,
            provider=provider,
            settings_file=artifacts.settings_file if artifacts is not None else None,
            response_file=artifacts.response_file if artifacts is not None else None,
        )

        session_lock = self._get_session_lock(session_name) if persistent_terminal else None
        if session_lock is not None:
            async with session_lock:
                async for event in self._run_task(meta=meta, timeout_sec=timeout_sec, env=env, workdir=workdir, command=command):
                    yield event
            return

        async for event in self._run_task(meta=meta, timeout_sec=timeout_sec, env=env, workdir=workdir, command=command):
            yield event

    async def _run_task(
        self,
        *,
        meta: _TmuxTaskMeta,
        timeout_sec: int,
        env: dict[str, str] | None,
        workdir: str,
        command: str,
    ):
        try:
            if meta.persistent_terminal:
                if meta.interactive:
                    ready, err = await self._ensure_claude_interactive_session(
                        session_name=meta.session_name,
                        workdir=workdir,
                        env=env,
                    )
                else:
                    ready, err = await self._ensure_persistent_session(meta.session_name, workdir=workdir, env=env)

                if not ready:
                    yield CLIEvent(type=EventType.FAILED, task_id=meta.task_id, error=err)
                    return

                if meta.interactive:
                    # 每个任务都重绑 pipe-pane 到当前 task log，避免沿用旧文件导致本次任务读不到输出。
                    try:
                        await self._run_tmux("pipe-pane", "-t", meta.session_name)
                    except Exception:
                        pass

                    pipe_cmd = f"cat >> {shlex.quote(str(meta.log_file))}"
                    try:
                        code, _, err_text = await self._run_tmux("pipe-pane", "-t", meta.session_name, pipe_cmd)
                    except Exception as exc:
                        yield CLIEvent(
                            type=EventType.FAILED,
                            task_id=meta.task_id,
                            error=f"tmux 管道设置异常: {exc}",
                        )
                        return

                    if code != 0:
                        err = err_text.strip() or "unknown error"
                        yield CLIEvent(
                            type=EventType.FAILED,
                            task_id=meta.task_id,
                            error=(
                                f"tmux 管道设置失败: {err}\n"
                                f"tmux_session: {meta.session_name}\n"
                                "hint: 请发送 /claude 重建会话后重试"
                            ),
                        )
                        return

                    respawn_command = self._build_interactive_claude_command(workdir=workdir, meta=meta)
                    respawned, respawn_err = await self._respawn_and_send_command(
                        session_name=meta.session_name,
                        command=respawn_command,
                        workdir=workdir,
                    )
                    if not respawned:
                        yield CLIEvent(type=EventType.FAILED, task_id=meta.task_id, error=respawn_err)
                        return

                sent, send_err = await self._send_command(
                    meta.session_name,
                    command,
                    workdir=workdir,
                    env=env,
                    interactive=meta.interactive,
                )
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

            yield CLIEvent(type=EventType.STARTED, task_id=meta.task_id, content=f"tmux_session={meta.session_name}")

            async for event in self._watch_task(meta=meta, timeout_sec=timeout_sec):
                yield event
        finally:
            async with self._lock:
                self._tasks.pop(meta.task_id, None)
            if meta.command_file is not None:
                self._safe_unlink(meta.command_file)
            self._cleanup_artifacts(meta)

    async def _watch_task(self, *, meta: _TmuxTaskMeta, timeout_sec: int):
        position = 0
        partial = ""
        timed_out = False
        exit_code: int | None = None
        final_reply: str | None = None

        started_at = asyncio.get_running_loop().time()
        last_partial_emit = started_at

        while True:
            now = asyncio.get_running_loop().time()
            text, position = self._read_new_text(meta.log_file, position)
            if text:
                partial, events = self._split_to_events(task_id=meta.task_id, text=partial + text)
                if not meta.interactive:
                    for event in events:
                        yield event

            if meta.interactive and not meta.stop_exit_sent and meta.response_file is not None:
                reply = self._read_response_once(meta.response_file)
                if reply is not None:
                    final_reply = reply
                    meta.stop_exit_sent = True
                    await self._send_command(
                        meta.session_name,
                        "/exit",
                        workdir=str(meta.log_file.parent),
                        env=None,
                        interactive=True,
                        allow_rebuild=False,
                    )

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

        text, position = self._read_new_text(meta.log_file, position)
        if text:
            partial, events = self._split_to_events(task_id=meta.task_id, text=partial + text)
            if not meta.interactive:
                for event in events:
                    yield event

        if partial and not meta.interactive:
            yield CLIEvent(type=EventType.STDOUT, task_id=meta.task_id, content=partial)

        canceled = await self._is_cancel_requested(meta.task_id)

        should_read_response_file = meta.response_file is not None and final_reply is None and (
            not meta.interactive or (not timed_out and not canceled)
        )
        if should_read_response_file:
            final_reply = await self._read_response_with_retry(meta.response_file)

        if final_reply is not None:
            yield CLIEvent(type=EventType.STDOUT, task_id=meta.task_id, content=final_reply)

        if timed_out:
            yield CLIEvent(type=EventType.TIMEOUT, task_id=meta.task_id, error=f"任务超时({timeout_sec}s)")
        elif canceled:
            yield CLIEvent(type=EventType.CANCELED, task_id=meta.task_id, error="任务已取消")
        elif exit_code == 0:
            yield CLIEvent(type=EventType.EXITED, task_id=meta.task_id, exit_code=0)
        else:
            yield CLIEvent(
                type=EventType.FAILED,
                task_id=meta.task_id,
                exit_code=exit_code,
                error=f"进程退出码: {exit_code}",
            )

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

    async def ensure_claude_interactive_session(
        self,
        *,
        terminal_key: str,
        workdir: str,
        env: dict[str, str] | None = None,
    ) -> tuple[bool, str]:
        session_name = self._build_session_name(terminal_key)
        session_lock = self._get_session_lock(session_name)
        async with session_lock:
            return await self._ensure_claude_interactive_session(session_name=session_name, workdir=workdir, env=env)

    async def reveal_terminal(self, terminal_key: str) -> tuple[bool, str]:
        session_name = self._build_session_name(terminal_key)
        exists = await self._session_exists(session_name)
        if not exists:
            return (
                False,
                f"tmux 会话不存在: {session_name}\n"
                "hint: 请先发送 /claude 创建会话后再打开桌面终端",
            )

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

    async def _start_ephemeral_session(
        self,
        session_name: str,
        *,
        workdir: str,
        env: dict[str, str] | None,
        command: str,
    ) -> tuple[bool, str]:
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

    async def _ensure_persistent_session(
        self,
        session_name: str,
        *,
        workdir: str,
        env: dict[str, str] | None,
    ) -> tuple[bool, str]:
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
            return (
                False,
                f"启动失败: 找不到 tmux 可执行文件 ({self._tmux_bin})\n"
                "hint: 请先安装 tmux，或在 .env 中设置 TMUX_BIN 为正确路径",
            )
        except Exception as exc:
            return False, f"tmux 启动异常: {exc}"

        if code == 0:
            return True, ""

        exists = await self._session_exists(session_name)
        if exists:
            return True, ""

        # 兼容 stale session：先尝试清理旧会话再创建一次
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
        return (
            False,
            f"tmux 会话创建失败: {err}\n"
            f"tmux_session: {session_name}\n"
            "hint: 可先发送 /claude 触发会话重建；若仍失败请检查 tmux 是否可用（tmux ls）",
        )

    async def _ensure_claude_interactive_session(
        self,
        *,
        session_name: str,
        workdir: str,
        env: dict[str, str] | None,
    ) -> tuple[bool, str]:
        return await self._ensure_persistent_session(session_name, workdir=workdir, env=env)

    async def _send_command(
        self,
        session_name: str,
        command: str,
        *,
        workdir: str,
        env: dict[str, str] | None,
        interactive: bool = False,
        allow_rebuild: bool = True,
    ) -> tuple[bool, str]:
        try:
            # 先退出 copy-mode，避免 send/paste 无效
            await self._run_tmux("send-keys", "-t", session_name, "-X", "cancel")

            # 清空当前命令行，避免与用户手工输入粘连（例如: mkdir imagesbash -lc ...）
            code, _, err_text = await self._run_tmux("send-keys", "-t", session_name, "C-u")
            if code != 0:
                rebuilt, rebuild_err = ((False, "已禁用自动重建") if not allow_rebuild else await self._force_rebuild_session(session_name, workdir=workdir, env=env))
                if not rebuilt:
                    return False, self._format_send_failure(
                        base="tmux 清空输入失败",
                        raw_err=err_text,
                        session_name=session_name,
                        rebuilt=False,
                        rebuild_err=rebuild_err,
                    )
                code, _, err_text = await self._run_tmux("send-keys", "-t", session_name, "C-u")
                if code != 0:
                    return False, self._format_send_failure(
                        base="tmux 清空输入失败",
                        raw_err=err_text,
                        session_name=session_name,
                        rebuilt=True,
                    )

            buffer_name = f"tgcli-{uuid.uuid4().hex}"
            code, _, err_text = await self._run_tmux(
                "load-buffer",
                "-b",
                buffer_name,
                "-",
                input_data=command.encode("utf-8"),
            )
            if code != 0:
                return False, self._format_send_failure(
                    base="tmux 加载缓冲区失败",
                    raw_err=err_text,
                    session_name=session_name,
                    rebuilt=False,
                )

            try:
                code, _, err_text = await self._run_tmux("paste-buffer", "-p", "-t", session_name, "-b", buffer_name)
                if code != 0:
                    rebuilt, rebuild_err = ((False, "已禁用自动重建") if not allow_rebuild else await self._force_rebuild_session(session_name, workdir=workdir, env=env))
                    if not rebuilt:
                        return False, self._format_send_failure(
                            base="tmux 粘贴命令失败",
                            raw_err=err_text,
                            session_name=session_name,
                            rebuilt=False,
                            rebuild_err=rebuild_err,
                        )
                    code, _, err_text = await self._run_tmux("paste-buffer", "-p", "-t", session_name, "-b", buffer_name)
                    if code != 0:
                        return False, self._format_send_failure(
                            base="tmux 粘贴命令失败",
                            raw_err=err_text,
                            session_name=session_name,
                            rebuilt=True,
                        )

                if self._enter_delay_sec > 0:
                    await asyncio.sleep(self._enter_delay_sec)

                enter_key = "C-m" if interactive else "Enter"
                code, _, err_text = await self._run_tmux("send-keys", "-t", session_name, enter_key)
                if code != 0:
                    rebuilt, rebuild_err = ((False, "已禁用自动重建") if not allow_rebuild else await self._force_rebuild_session(session_name, workdir=workdir, env=env))
                    if not rebuilt:
                        return False, self._format_send_failure(
                            base="tmux 执行命令失败",
                            raw_err=err_text,
                            session_name=session_name,
                            rebuilt=False,
                            rebuild_err=rebuild_err,
                        )
                    code, _, err_text = await self._run_tmux("send-keys", "-t", session_name, enter_key)
                    if code != 0:
                        return False, self._format_send_failure(
                            base="tmux 执行命令失败",
                            raw_err=err_text,
                            session_name=session_name,
                            rebuilt=True,
                        )

                # 非交互模式执行后清空输入行，尽量不在提示符保留 launcher 文本。
                # 交互式 Claude 在发送 Enter 后再发 C-u 会破坏其 TUI 输入/输出状态。
                if not interactive:
                    await self._run_tmux("send-keys", "-t", session_name, "C-u")
                return True, ""
            finally:
                await self._run_tmux("delete-buffer", "-b", buffer_name)
        except FileNotFoundError:
            return False, f"启动失败: 找不到 tmux 可执行文件 ({self._tmux_bin})"
        except Exception as exc:
            return False, f"tmux 命令发送异常: {exc}"

    async def _force_rebuild_session(
        self,
        session_name: str,
        *,
        workdir: str,
        env: dict[str, str] | None,
    ) -> tuple[bool, str]:
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
        return (
            False,
            f"tmux respawn 失败: {err}\n"
            f"tmux_session: {session_name}\n"
            "hint: 请发送 /claude 重新建立会话后再试",
        )

    def _format_send_failure(
        self,
        *,
        base: str,
        raw_err: str,
        session_name: str,
        rebuilt: bool,
        rebuild_err: str | None = None,
    ) -> str:
        err = raw_err.strip() or "unknown error"
        hint = (
            "自动重建已执行但仍失败，请发送 /claude 重新建立会话后再试"
            if rebuilt
            else "检测到会话可能失效，已尝试自动重建；如仍失败请发送 /claude 重建后重试"
        )
        rebuilt_text = "是" if rebuilt else "否"
        lines = [
            f"{base}: {err}",
            f"tmux_session: {session_name}",
            f"auto_rebuilt: {rebuilt_text}",
        ]
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
        return (
            process.returncode or 0,
            stdout.decode(errors="replace"),
            stderr.decode(errors="replace"),
        )

    def _build_session_name(self, terminal_key: str) -> str:
        sanitized = "".join(ch for ch in terminal_key if ch.isalnum() or ch in {"-", "_"})
        if not sanitized:
            sanitized = "terminal"
        return f"tgcli_{sanitized}"[:64]

    def _build_shell_command(
        self,
        *,
        argv: list[str],
        workdir: str,
        log_file: Path,
        exit_file: Path,
        command_file: Path,
        hide_launcher_line: bool,
    ) -> str:
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

        # 在持久会话里使用 respawn-pane 执行该命令。
        # 这样命令不会经由 send-keys 输入到提示符，避免出现“bash /tmp/xxx.cmd.sh”回显。
        script_target = shlex.quote(str(command_file))
        return f"bash {script_target}; exec \"${{SHELL:-bash}}\" -l"

    def _build_interactive_claude_command(self, *, workdir: str, meta: _TmuxTaskMeta) -> str:
        if meta.settings_file is None:
            raise ValueError("interactive claude settings_file 不能为空")
        workdir_target = shlex.quote(str(Path(workdir).resolve()))
        claude_bin = shlex.quote(self._claude_cli_bin)
        settings_file = shlex.quote(str(meta.settings_file))
        exit_file = shlex.quote(str(meta.exit_file))
        system_prompt = shlex.quote(_INTERACTIVE_SYSTEM_PROMPT)
        return (
            f"cd {workdir_target} && {claude_bin} --settings {settings_file} "
            f"--append-system-prompt {system_prompt}; "
            f"code=$?; printf '%s' \"$code\" > {exit_file}; "
            f"exec \"${{SHELL:-bash}}\" -l"
        )

    def _wrap_interactive_prompt(self, *, prompt: str) -> str:
        safe_prompt = prompt.replace("\r", "").strip()
        if not safe_prompt:
            raise ValueError("prompt 不能为空")
        return safe_prompt

    async def _session_current_command(self, session_name: str) -> str:
        _ = session_name
        return ""

    def _read_new_text(self, file_path: Path, offset: int) -> tuple[str, int]:
        if not file_path.exists():
            return "", offset

        with file_path.open("rb") as fp:
            fp.seek(offset)
            data = fp.read()
            new_offset = fp.tell()

        return data.decode(errors="replace"), new_offset

    def _split_to_events(self, *, task_id: str, text: str) -> tuple[str, list[CLIEvent]]:
        lines = text.splitlines(keepends=True)
        partial = ""
        if lines and not lines[-1].endswith("\n"):
            partial = lines.pop()

        events = [CLIEvent(type=EventType.STDOUT, task_id=task_id, content=line) for line in lines if line]
        return partial, events

    def _inject_claude_settings(self, argv: list[str], artifacts: ClaudeStopArtifacts) -> list[str]:
        if not argv:
            return argv
        return [argv[0], "--settings", str(artifacts.settings_file), *argv[1:]]

    def _build_claude_artifacts(
        self,
        *,
        task_id: str,
        provider: str | None,
        interactive: bool,
    ) -> ClaudeStopArtifacts | None:
        _ = interactive
        if provider != "claude_code":
            return None
        return build_task_artifacts(task_id=task_id, data_dir=self._data_dir)

    def _read_response_once(self, response_file: Path) -> str | None:
        try:
            content = response_file.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError:
            return None
        if content.strip():
            return content
        return None

    async def _read_response_with_retry(self, response_file: Path) -> str | None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 0.6
        while True:
            content = self._read_response_once(response_file)
            if content is not None:
                return content

            if loop.time() >= deadline:
                return None
            await asyncio.sleep(0.05)

    def _cleanup_artifacts(self, meta: _TmuxTaskMeta | ClaudeStopArtifacts | None) -> None:
        if meta is None:
            return
        if isinstance(meta, ClaudeStopArtifacts):
            paths = (meta.settings_file, meta.response_file)
        else:
            paths = (meta.settings_file, meta.response_file)
        for path in paths:
            if path is None:
                continue
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    def _read_exit_code(self, exit_file: Path) -> int | None:
        try:
            return int(exit_file.read_text(encoding="utf-8").strip())
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
