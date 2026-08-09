from __future__ import annotations

import dataclasses
from typing import Any

from app.adapters.cli.base import BaseCLIAdapter
from app.adapters.cli.claude_code import ClaudeCodeAdapter
from app.adapters.cli.codex_cli import CodexCLIAdapter
from app.adapters.cli.gemini_cli import GeminiCLIAdapter
from app.adapters.process.subprocess_runner import SubprocessRunner
from app.adapters.process.tmux_runner import TmuxRunner
from app.config.settings import Settings
from app.domain.protocols import AdapterCapabilities


class CLIAdapterRegistry:
    """Provider adapter 注册表。

    新增 provider：编写 BaseCLIAdapter 子类（声明 provider/aliases/class_capabilities/
    cli_bin_setting/build_file_args），在 settings 配置对应 *_cli_bin，然后调
    register(<XxxCLIAdapter>) 即可接入，无需改本类内部字典。
    """

    def __init__(self, settings: Settings, runner: SubprocessRunner, tmux_runner: TmuxRunner | None = None) -> None:
        self._settings = settings
        self._runner = runner
        self._tmux_runner = tmux_runner
        self._claude_terminal_enabled = settings.claude_tmux_mode and tmux_runner is not None
        self._adapters: dict[str, BaseCLIAdapter] = {}
        self._capabilities: dict[str, AdapterCapabilities] = {}
        self._aliases: dict[str, str] = {}
        self._register_builtin()

    def _register_builtin(self) -> None:
        """注册内置 provider——保持与历史硬编码等价的能力与运行时配置。"""
        self.register(
            ClaudeCodeAdapter,
            persistent_terminal_active=self._claude_terminal_enabled,
        )
        self.register(CodexCLIAdapter)
        self.register(GeminiCLIAdapter)

    def register(
        self,
        adapter_cls: type[BaseCLIAdapter],
        *,
        persistent_terminal_active: bool | None = None,
    ) -> None:
        """注册一个 provider adapter。

        provider、aliases、静态能力由 adapter 类自描述；registry 仅按运行环境
        覆盖动态位 persistent_terminal_active（tmux 后端可用性）。cli_bin 取自
        settings.<cli_bin_setting()>，runner 选取对内置 claude_code 走 tmux_runner
        （启用时），其余走默认 subprocess runner。
        """
        provider = adapter_cls.provider
        bin_value = getattr(self._settings, adapter_cls.cli_bin_setting())
        runner: Any = (
            self._tmux_runner
            if provider == "claude_code" and self._claude_terminal_enabled and self._tmux_runner is not None
            else self._runner
        )
        self._adapters[provider] = adapter_cls(cli_bin=bin_value, runner=runner)

        caps = adapter_cls.class_capabilities()
        if persistent_terminal_active is not None:
            caps = dataclasses.replace(caps, persistent_terminal_active=persistent_terminal_active)
        self._capabilities[provider] = caps

        self._aliases[provider.lower()] = provider
        for alias in adapter_cls.aliases():
            self._aliases[alias.lower()] = provider

    @property
    def claude_terminal_enabled(self) -> bool:
        return self._claude_terminal_enabled

    def normalize_provider(self, provider: str) -> str:
        key = provider.strip().lower()
        normalized = self._aliases.get(key)
        if normalized is None:
            raise ValueError(f"不支持 provider: {provider}")
        return normalized

    def get(self, provider: str) -> BaseCLIAdapter:
        normalized = self.normalize_provider(provider)
        return self._adapters[normalized]

    def available_providers(self) -> list[str]:
        return sorted(self._adapters.keys())

    def capabilities(self, provider: str) -> AdapterCapabilities:
        normalized = self.normalize_provider(provider)
        return self._capabilities[normalized]
