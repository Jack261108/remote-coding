from __future__ import annotations

from app.adapters.cli.base import BaseCLIAdapter


class CodexCLIAdapter(BaseCLIAdapter):
    provider = "codex"
    _cli_run_args = ["exec"]

    @classmethod
    def aliases(cls) -> list[str]:
        return ["codex_cli", "codex-cli"]

    @classmethod
    def cli_bin_setting(cls) -> str:
        return "codex_cli_bin"
