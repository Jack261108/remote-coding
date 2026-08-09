from __future__ import annotations

from app.adapters.cli.base import BaseCLIAdapter


class GeminiCLIAdapter(BaseCLIAdapter):
    provider = "gemini"
    _cli_run_args = ["-p"]

    @classmethod
    def aliases(cls) -> list[str]:
        return ["gemini_cli", "gemini-cli"]

    @classmethod
    def cli_bin_setting(cls) -> str:
        return "gemini_cli_bin"
