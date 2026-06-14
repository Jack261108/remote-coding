from __future__ import annotations

from app.adapters.cli.base import BaseCLIAdapter


class GeminiCLIAdapter(BaseCLIAdapter):
    provider = "gemini"
    _cli_run_args = ["-p"]
