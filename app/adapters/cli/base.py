from __future__ import annotations

from abc import ABC
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

from app.domain.models import CLIEvent, ExecutionTask
from app.domain.protocols import AdapterCapabilities


class BaseCLIAdapter(ABC):
    provider: str
    _cli_run_args: list[str] = []  # 子类覆盖，如 ["exec"] 或 ["-p"]

    def __init__(self, cli_bin: str, runner: Any) -> None:
        self._cli_bin = cli_bin
        self._runner = runner

    @classmethod
    def class_capabilities(cls) -> AdapterCapabilities:
        """该 provider 类别的静态能力上限（与运行环境无关）。

        子类按需覆盖——claude_code 返回满能力，codex/gemini 默认空。
        运行时动态位（如 tmux 后端可用性）由 registry 在注册时合并覆盖。
        """
        return AdapterCapabilities()

    @classmethod
    def aliases(cls) -> list[str]:
        """provider 自声明的别名（不含 provider 本身），registry 归一化时纳入。"""
        return []

    def build_file_args(self, file_paths: list[Path]) -> list[str]:
        """构造携带文件上下文的 provider 专属 CLI 标志位。

        默认返回空列表（文件在 prompt 文本中引用）；具备 --file 语义的
        provider 子类覆盖此方法。
        """
        _ = file_paths
        return []

    async def run(
        self,
        task: ExecutionTask,
        *,
        terminal_key: str | None = None,
        interactive: bool = False,
        claude_session_id: str | None = None,
    ) -> AsyncGenerator[CLIEvent, None]:
        argv = [self._cli_bin, *self._cli_run_args, task.prompt]
        async for event in self._runner.run(
            task_id=task.task_id,
            argv=argv,
            workdir=task.workdir,
            timeout_sec=task.timeout_sec,
            terminal_key=terminal_key,
            interactive=interactive,
        ):
            yield event

    async def cancel(self, task_id: str) -> bool:
        return await self._runner.cancel(task_id)
