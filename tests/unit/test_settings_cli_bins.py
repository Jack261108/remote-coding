"""cli_bins 合并契约测试。

固化三条优先级与环境来源，覆盖 Step 5 引入过的回归：把老式
CLAUDE_CLI_BIN/CODEX_CLI_BIN/GEMINI_CLI_BIN 字段删成 @property 后，pydantic
不再收集进程 env / .env 里的 legacy 变量。现恢复为 legacy 收集器字段并由
_absorb_legacy_cli_bins validator 并入 cli_bins——本测试锁定该行为。
"""

from __future__ import annotations

import pytest

from app.config.settings import Settings


def _build(
    monkeypatch: pytest.MonkeyPatch,
    *,
    cli_bins: dict[str, str] | None = None,
    legacy_env: dict[str, str] | None = None,
) -> Settings:
    """构造 Settings，禁用 .env 隔离进程，legacy 经 monkeypatch 注入环境。

    ``_env_file=None`` 是 pydantic-settings 构造函数的私有参数，进程 env 优先于
    .env；通过 kwargs dict 传 alias 以隔离本仓库 .env 中可能存在的 legacy 变量。
    动态 __init__ 在 mypy 下报 call-arg/arg-type，故局部 type: ignore。
    """
    for key in ("CLAUDE_CLI_BIN", "CODEX_CLI_BIN", "GEMINI_CLI_BIN", "CLI_BINS"):
        monkeypatch.delenv(key, raising=False)
    if legacy_env:
        for k, v in legacy_env.items():
            monkeypatch.setenv(k, v)
    kwargs: dict[str, object] = {
        "TG_BOT_TOKEN": "t",
        "TG_ALLOWED_USER_IDS": "1",
        "ALLOWED_WORKDIRS": ["/tmp"],
        "_env_file": None,
    }
    if cli_bins is not None:
        kwargs["CLI_BINS"] = cli_bins
    return Settings(**kwargs)  # type: ignore[arg-type]


class TestCliBinsLegacyAbsorption:
    def test_legacy_claude_env_overrides_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 回归路径：仅设进程 CLAUDE_CLI_BIN（非默认值），cli_bins 必须收到它。
        s = _build(monkeypatch, legacy_env={"CLAUDE_CLI_BIN": "/usr/local/bin/claude"})
        assert s.claude_cli_bin == "/usr/local/bin/claude"
        assert s.codex_cli_bin == "codex"
        assert s.gemini_cli_bin == "gemini"
        assert s.cli_bins == {
            "claude_code": "/usr/local/bin/claude",
            "codex": "codex",
            "gemini": "gemini",
        }

    def test_cli_bins_dict_takes_precedence_over_legacy_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        s = _build(
            monkeypatch,
            cli_bins={"claude_code": "/from/dict", "codex": "/from/dict_codex"},
            legacy_env={"CLAUDE_CLI_BIN": "/legacy/claude"},
        )
        assert s.claude_cli_bin == "/from/dict"
        assert s.codex_cli_bin == "/from/dict_codex"
        # CLI_BINS 未给 gemini → 走内置默认。
        assert s.gemini_cli_bin == "gemini"

    def test_partial_cli_bins_filled_with_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        s = _build(monkeypatch, cli_bins={"claude_code": "/only"})
        assert s.claude_cli_bin == "/only"
        assert s.codex_cli_bin == "codex"
        assert s.gemini_cli_bin == "gemini"

    def test_all_three_legacy_envs_absorbed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 典型老部署迁移：三个 legacy env 全设，cli_bins 全部收编。
        s = _build(
            monkeypatch,
            legacy_env={"CLAUDE_CLI_BIN": "/c", "CODEX_CLI_BIN": "/cx", "GEMINI_CLI_BIN": "/g"},
        )
        assert s.claude_cli_bin == "/c"
        assert s.codex_cli_bin == "/cx"
        assert s.gemini_cli_bin == "/g"

    def test_defaults_when_nothing_provided_and_legacy_fields_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        s = _build(monkeypatch)
        assert s.claude_cli_bin == "claude"
        assert s.codex_cli_bin == "codex"
        assert s.gemini_cli_bin == "gemini"
        # legacy 收集器未承接任何 env → None（而非混入内置默认）。
        assert s.legacy_claude_cli_bin is None
        assert s.legacy_codex_cli_bin is None
        assert s.legacy_gemini_cli_bin is None

    def test_cli_bins_dict_for_codex_outranks_legacy_for_codex(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 同一 provider 在 dict 与 legacy 都给：dict 胜。
        s = _build(
            monkeypatch,
            cli_bins={"codex": "/dict/codex"},
            legacy_env={"CODEX_CLI_BIN": "/legacy/codex"},
        )
        assert s.codex_cli_bin == "/dict/codex"
        assert s.claude_cli_bin == "claude"
