from __future__ import annotations

import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.config.settings import Settings


@dataclass(frozen=True)
class PreflightResult:
    """tmux 启动预检查结果。

    ok=False 时 error 给出指明缺失 tmux 的友好文本。
    """

    ok: bool
    error: str | None


def tmux_preflight(
    tmux_mode: bool,
    tmux_bin: str,
    resolver: Callable[[str], str | None] = shutil.which,
) -> PreflightResult:
    """纯函数：根据 tmux 模式与可执行解析结果判定是否允许启动。

    注入 ``resolver``（默认 ``shutil.which``）以便测试。

    - ``tmux_mode`` 为假 → ok（不查 tmux，Req4 AC3）。
    - ``tmux_mode`` 为真且 ``resolver(tmux_bin)`` 非 None → ok（Req4 AC4）。
    - ``tmux_mode`` 为真且 ``resolver(tmux_bin)`` 为 None → ok=False 且 error 提及 tmux（Req4 AC5）。
    """
    if not tmux_mode:
        return PreflightResult(ok=True, error=None)

    if resolver(tmux_bin) is not None:
        return PreflightResult(ok=True, error=None)

    error = (
        f"CLAUDE_TMUX_MODE=true 但在 PATH 中找不到 tmux 可执行程序（TMUX_BIN={tmux_bin!r}）。"
        "请安装 tmux（例如 brew install tmux）或将 CLAUDE_TMUX_MODE 设为 false。"
    )
    return PreflightResult(ok=False, error=error)


def check_tmux_preflight(
    settings: Settings,
    resolver: Callable[[str], str | None] = shutil.which,
) -> None:
    """启动期 tmux 预检查。

    调用 :func:`tmux_preflight`；当 ok=False 时将错误写入 ``sys.stderr`` 并以非 0 退出码结束。

    实现说明：直接写 ``sys.stderr`` 并 ``sys.exit(1)``，与现有启动流程保持一致。
    """
    result = tmux_preflight(settings.claude_tmux_mode, settings.tmux_bin, resolver=resolver)
    if not result.ok:
        print(result.error, file=sys.stderr)
        sys.exit(1)
