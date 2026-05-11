from __future__ import annotations

import asyncio
import uuid

from app.domain.user_question_models import USER_QUESTION_TUI_FALLBACK_ERROR


class TmuxSessionMixin:
    _tmux_bin: str
    _cancel_grace_sec: float
    _enter_delay_sec: float

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

    async def _ensure_live_claude_session_for_user_question(self, *, session_name: str) -> tuple[bool, str]:
        exists = await self._session_exists(session_name)
        if not exists:
            return False, f"tmux 会话不存在: {session_name}\nhint: 请先发送 /claude 重建 Claude 会话后再试"
        current_cmd = await self._session_current_command(session_name)
        if "claude" not in current_cmd.lower():
            return False, "当前 Claude 会话不在可回答问题的界面\nhint: 请先发送 /claude 重建会话后再试"
        return True, ""

    async def _move_user_question_cursor_to_option(self, *, session_name: str, target_index: int) -> tuple[bool, str]:
        pane_text = await self._capture_pane_text(session_name)
        if not self._looks_like_user_question_tui(pane_text):
            return False, USER_QUESTION_TUI_FALLBACK_ERROR

        current_index = self._selected_user_question_option_index(pane_text)
        if current_index is None:
            return False, USER_QUESTION_TUI_FALLBACK_ERROR

        delta = target_index - current_index
        if delta == 0:
            return True, ""
        direction = "Down" if delta > 0 else "Up"
        return await self._send_keys(session_name, *([direction] * abs(delta)))

    async def _send_keys(self, session_name: str, *keys: str) -> tuple[bool, str]:
        if not keys:
            return True, ""
        try:
            code, _, err_text = await self._run_tmux("send-keys", "-t", session_name, *keys)
        except FileNotFoundError:
            return False, f"启动失败: 找不到 tmux 可执行文件 ({self._tmux_bin})"
        except Exception as exc:
            return False, f"tmux 按键发送异常: {exc}"
        if code == 0:
            return True, ""
        err = err_text.strip() or "unknown error"
        return False, f"tmux 按键发送失败: {err}\ntmux_session: {session_name}"

    async def _paste_text(self, session_name: str, text: str) -> tuple[bool, str]:
        buffer_name = f"tgcli-answer-{uuid.uuid4().hex}"
        try:
            code, _, err_text = await self._run_tmux("load-buffer", "-b", buffer_name, "-", input_data=text.encode("utf-8"))
            if code != 0:
                err = err_text.strip() or "unknown error"
                return False, f"tmux 加载回答内容失败: {err}\ntmux_session: {session_name}"
            code, _, err_text = await self._run_tmux("paste-buffer", "-p", "-t", session_name, "-b", buffer_name)
            if code == 0:
                return True, ""
            err = err_text.strip() or "unknown error"
            return False, f"tmux 粘贴回答内容失败: {err}\ntmux_session: {session_name}"
        except FileNotFoundError:
            return False, f"启动失败: 找不到 tmux 可执行文件 ({self._tmux_bin})"
        except Exception as exc:
            return False, f"tmux 粘贴回答内容异常: {exc}"
        finally:
            try:
                await self._run_tmux("delete-buffer", "-b", buffer_name)
            except Exception:
                pass

    async def _capture_pane_text(self, session_name: str, *, start_line: int = -200) -> str:
        try:
            code, stdout, _ = await self._run_tmux("capture-pane", "-p", "-S", str(start_line), "-t", session_name)
        except Exception:
            return ""
        if code != 0:
            return ""
        return stdout

    def _looks_like_user_question_tui(self, pane_text: str) -> bool:
        normalized = pane_text or ""
        return (
            "Enter to select" in normalized
            or "Tab/Arrow keys to navigate" in normalized
            or ("Submit" in normalized and ("☐" in normalized or "☑" in normalized or "☒" in normalized))
        )

    def _selected_user_question_option_index(self, pane_text: str) -> int | None:
        for line in reversed(pane_text.splitlines()):
            stripped = line.strip()
            if not stripped.startswith(("›", "❯", ">")):
                continue
            candidate = stripped[1:].strip()
            digits = []
            for ch in candidate:
                if ch.isdigit():
                    digits.append(ch)
                    continue
                break
            if not digits:
                continue
            remainder = candidate[len(digits):].lstrip()
            if not remainder.startswith((".", ")", "）")):
                continue
            try:
                return max(0, int("".join(digits)) - 1)
            except ValueError:
                return None
        return None

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
