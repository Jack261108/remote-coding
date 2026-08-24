"""插件化 registry demo：证明通过 register() 注入新 provider 即可与内置 provider 并存可用。

新增一个 provider 不需改 CLIAdapterRegistry 内部字典——只需编写 BaseCLIAdapter
子类并在 settings.cli_bins 配置其可执行路径，再调 register()。本测试是该契约的
正向回归。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.adapters.cli.base import BaseCLIAdapter
from app.adapters.cli.registry import CLIAdapterRegistry
from app.adapters.process.subprocess_runner import SubprocessRunner
from app.config.settings import Settings


class FakeMarkdownAdapter(BaseCLIAdapter):
    """最小可注入的演示 provider：仅支持一次性任务，沿用基类 run/cancel。"""

    provider = "markdown"
    _cli_run_args = ["-p"]

    @classmethod
    def aliases(cls) -> list[str]:
        return ["md"]

    def build_file_args(self, file_paths: list[Path]) -> list[str]:
        # 演示 provider 不支持 --file 语义，文件在 prompt 文本中引用。
        _ = file_paths
        return []


def _build_settings() -> Settings:
    # 内置 provider 仍用老式 *_CLI_BIN env，验证 validator 收编后 cli_bins 同时含新 provider。
    return Settings.model_validate(
        {
            "TG_BOT_TOKEN": "token",
            "TG_ALLOWED_USER_IDS": "1",
            "DEFAULT_PROVIDER": "claude_code",
            "DEFAULT_TIMEOUT_SEC": 10,
            "MAX_CONCURRENT_TASKS": 1,
            "CLAUDE_CLI_BIN": "claude",
            "CODEX_CLI_BIN": "codex",
            "GEMINI_CLI_BIN": "gemini",
            "ALLOWED_WORKDIRS": "/tmp",
            "CLI_BINS": {"markdown": "mdcli"},
        }
    )


def test_register_new_provider_alongside_builtins() -> None:
    registry = CLIAdapterRegistry(settings=_build_settings(), runner=SubprocessRunner())
    # 内置 provider 已注册；现注入一个全新的 markdown provider。
    registry.register(FakeMarkdownAdapter)

    # 新 provider 与内置 provider 并存，available_providers 保持稳定排序。
    assert registry.available_providers() == ["claude_code", "codex", "gemini", "markdown"]

    # 别名归一化纳入新 provider 自声明的别名，且不影响内置 provider。
    assert registry.normalize_provider("markdown") == "markdown"
    assert registry.normalize_provider("md") == "markdown"
    assert registry.normalize_provider("claude") == "claude_code"

    # 能力取自 adapter 类的自描述（类未覆盖 class_capabilities → 全默认）。
    caps = registry.capabilities("markdown")
    assert caps.run_task is True
    assert caps.cancel_task is True
    assert caps.persistent_terminal is False
    assert caps.interactive_input is False

    # get() 返回的实例即为注入的 adapter 类型，cli_bin 来自 settings。
    adapter: Any = registry.get("markdown")
    assert isinstance(adapter, FakeMarkdownAdapter)
    assert adapter._cli_bin == "mdcli"

    # 内置 provider 仍可正常访问——注入未破坏既有注册。
    assert isinstance(registry.get("claude_code"), BaseCLIAdapter)
    assert isinstance(registry.get("gemini"), BaseCLIAdapter)
