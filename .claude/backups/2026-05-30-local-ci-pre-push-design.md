# 本地 CI pre-commit 多阶段拦截设计

## 背景

当前仓库的 GitHub CI 在 push 和 pull_request 时执行以下检查：

1. `python -m ruff check app tests`
2. `python -m ruff format --check app tests`
3. `python -m mypy --follow-imports=skip app/adapters/process/subprocess_runner.py app/bot/middleware/auth.py app/bot/middleware/rate_limit.py app/bot/handlers/command_permission.py app/bot/handlers/command_user_question.py app/bootstrap.py app/services/task_service.py`
4. `python -m pytest -q`

本地复现结果显示：

- `ruff check` 通过。
- `mypy` 通过。
- `pytest` 通过，当前为 888 个测试通过。
- `ruff format --check app tests` 失败，有 10 个文件需要格式化。

仓库已有 `.pre-commit-config.yaml`，但当前配置只覆盖提交阶段的 ruff 检查，没有在推送前运行 mypy 和 pytest。因此本地钩子与 CI 检查集不一致。

## 需求依据

本设计遵循 `.kiro/specs/local-ci-parity-hooks/requirements.md`：

- Requirement 1：本地检查项与 CI 完全对齐。
- Requirement 2：ruff lint 与 format check 保留在 pre-commit 阶段。
- Requirement 3：mypy 与 pytest 放在 pre-push 阶段。
- Requirement 5.3：mypy 与 pytest 使用 pre-commit 框架的 local/system 钩子，不引入外部钩子仓库。
- Requirement 6：说明 `--no-verify` 绕过钩子后，远程 CI 仍会执行检查。

## 目标

- 使用 pre-commit 框架的多阶段能力，让本地检查集与 CI 命令语义对齐。
- 在提交阶段运行快速且只读的 ruff lint 与 format check。
- 在推送阶段运行耗时较长的 mypy 与完整 pytest，失败时阻止 push。
- 修复当前已知的格式检查失败，让现有代码能通过 CI 的 format check。

## 非目标

- 不移除 GitHub CI。
- 不使用 `core.hooksPath` 或手写 `.githooks` 脚本替代 pre-commit 框架。
- 不引入新的构建系统、Makefile 或额外任务脚本。
- 不修改 CI 工作流本身。
- 不在 pre-push 阶段自动修改工作区。

## 方案

修改 `.pre-commit-config.yaml`，继续使用 pre-commit 框架统一管理本地钩子。

为避免本地钩子命令与 CI 命令分叉，四类检查都使用 `repo: local` + `language: system` 调用当前 Python 环境中的工具。工具版本来自 `pyproject.toml` 的 dev 依赖声明。

### pre-commit 阶段

提交阶段运行两个快速只读检查：

1. `ruff-check-ci`
   - stage：`pre-commit`
   - entry：
     ```bash
     python -m ruff check app tests
     ```
   - `pass_filenames: false`

2. `ruff-format-ci`
   - stage：`pre-commit`
   - entry：
     ```bash
     python -m ruff format --check app tests
     ```
   - `pass_filenames: false`

提交阶段不运行 mypy 或 pytest，避免拖慢 `git commit`。

如需自动修复格式，开发者可手动运行：

```bash
python -m ruff check --fix app tests
python -m ruff format app tests
```

自动修复不放进钩子，以保持本地钩子与 CI 的只读校验行为一致。

### pre-push 阶段

推送阶段运行两个耗时检查：

1. `mypy-ci`
   - stage：`pre-push`
   - entry：
     ```bash
     python -m mypy --follow-imports=skip app/adapters/process/subprocess_runner.py app/bot/middleware/auth.py app/bot/middleware/rate_limit.py app/bot/handlers/command_permission.py app/bot/handlers/command_user_question.py app/bootstrap.py app/services/task_service.py
     ```
   - `pass_filenames: false`

2. `pytest-ci`
   - stage：`pre-push`
   - entry：
     ```bash
     python -m pytest -q
     ```
   - `pass_filenames: false`

pre-push 阶段任一钩子失败时，pre-commit 框架返回非零退出码，Git 阻止 push。

## 安装和共享方式

仓库共享的是 `.pre-commit-config.yaml`。开发者本地启用方式：

```bash
pre-commit install
pre-commit install --hook-type pre-push
```

这避免了 `core.hooksPath` 本地配置不共享的问题。每个 clone 仍需安装一次 hook，但配置来源统一在仓库文件中。

## `--no-verify` 行为

如果开发者使用：

```bash
git push --no-verify
```

Git 会跳过本地 pre-push 钩子，推送不会被本地拦截。但 GitHub CI 仍会在远程按 `.github/workflows/ci.yml` 执行完整 CI 检查。因此 `--no-verify` 只能绕过本地反馈，不能绕过远程 CI。

## 当前格式问题处理

当前 `ruff format --check app tests` 报告 10 个文件需要格式化。该修复是让现有代码重新满足 CI format check 的必要前置工作。

处理方式：

```bash
python -m ruff format app tests
```

该命令只用于修复当前工作区格式问题，不放入 pre-push 阶段自动执行。

## 验证

实施后运行：

```bash
python -m ruff check app tests
python -m ruff format --check app tests
python -m mypy --follow-imports=skip app/adapters/process/subprocess_runner.py app/bot/middleware/auth.py app/bot/middleware/rate_limit.py app/bot/handlers/command_permission.py app/bot/handlers/command_user_question.py app/bootstrap.py app/services/task_service.py
python -m pytest -q
pre-commit run --all-files
pre-commit run --hook-stage pre-push --all-files
```

期望：

- ruff、mypy、pytest 全部通过。
- `pre-commit run --all-files` 运行 ruff check 与 ruff format check，并返回 0。
- `pre-commit run --hook-stage pre-push --all-files` 运行 mypy 与 pytest，并返回 0。
- 工作区不包含临时文件或中间产物。
