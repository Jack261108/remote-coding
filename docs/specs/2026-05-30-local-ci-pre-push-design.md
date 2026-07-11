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

本设计遵循 `.kiro/specs/local-ci-parity-hooks/requirements.md` 和 `.kiro/specs/local-ci-parity-hooks/design.md`：

- Requirement 1：本地检查项与 CI 完全对齐。
- Requirement 2：ruff lint 与 format 保留在 pre-commit 阶段。
- Requirement 3：mypy 与 pytest 放在 pre-push 阶段。
- Requirement 4：本地通过后，在代码未变化的前提下可预期 CI 通过。
- Requirement 5：复用项目 dev 依赖中的工具，不引入新的外部钩子仓库。
- Requirement 6：说明 `--no-verify` 绕过钩子后，远程 CI 仍会执行检查。

## 目标

- 使用 pre-commit 框架的多阶段能力，让本地检查集与 CI 语义对齐。
- 在提交阶段保留 ruff 自动修复体验，避免把现有开发体验降级。
- 在推送阶段运行耗时较长的 mypy 与完整 pytest，失败时阻止 push。
- 使用 `repo: local` + `language: system` 调用项目环境中的 ruff、mypy、pytest，规避 ruff 版本错位。
- 修复当前已知的格式检查失败，让现有代码能通过 CI 的 format check。

## 非目标

- 不移除 GitHub CI。
- 不使用 `core.hooksPath` 或手写 `.githooks` 脚本替代 pre-commit 框架。
- 不引入新的构建系统、Makefile 或额外任务脚本。
- 不修改 CI 工作流本身。
- 不在 pre-push 阶段重复运行 ruff。

## 关键设计决策

### 1. 继续使用 pre-commit 框架多阶段

放弃 `.githooks + core.hooksPath`。原因是 `core.hooksPath` 是本地 Git 配置，无法随仓库共享；pre-commit 配置则可以通过 `.pre-commit-config.yaml` 进入仓库，并通过一次安装复现到每个 clone。

### 2. pre-commit 阶段采用 ruff 写入式超集

提交阶段使用：

```bash
python -m ruff check --fix app tests
python -m ruff format app tests
```

这不是与 CI 命令逐字相同，但它是 CI 只读校验的写入式超集：最终被提交的代码会满足 CI 的 `ruff check app tests` 与 `ruff format --check app tests`。如果钩子修改了文件，pre-commit 会阻止本次提交并提示重新 `git add`。

选择写入式超集的原因：

- 保留当前 `.pre-commit-config.yaml` 已有的自动修复体验。
- 避免开发者每次格式不对时手动运行修复命令。
- 通过后提交内容仍与 CI 只读校验语义等价。

### 3. 显式规避 ruff 版本错位

当前 `.pre-commit-config.yaml` 使用外部仓库 `ruff-pre-commit@v0.8.0`，而 CI 通过 `pip install -e ".[dev]"` 安装 `pyproject.toml` 中的 `ruff>=0.8,<1`，本地实测 dev 环境中的 ruff 为 0.15.x。

这会造成潜在不一致：本地 pre-commit 可能用旧 ruff 通过，但 CI 用新 ruff 失败。

因此四类检查都改为 `repo: local` + `language: system` + `python -m <tool>`，统一使用当前项目虚拟环境里的 dev 工具版本。这样本地钩子和 CI 都来自同一份 `pyproject.toml` dev 依赖声明。

## 方案

修改 `.pre-commit-config.yaml`，使它成为本地检查的唯一配置入口。

### pre-commit 阶段

提交阶段运行两个快速检查：

1. `ruff-check`
   - stage：`pre-commit`
   - entry：
     ```bash
     python -m ruff check --fix app tests
     ```
   - `language: system`
   - `pass_filenames: false`
   - `always_run: true`
   - `require_serial: true`

2. `ruff-format`
   - stage：`pre-commit`
   - entry：
     ```bash
     python -m ruff format app tests
     ```
   - `language: system`
   - `pass_filenames: false`
   - `always_run: true`
   - `require_serial: true`

提交阶段不运行 mypy 或 pytest，避免拖慢 `git commit`。

### pre-push 阶段

推送阶段运行两个耗时检查：

1. `mypy`
   - stage：`pre-push`
   - entry：
     ```bash
     python -m mypy --follow-imports=skip app/adapters/process/subprocess_runner.py app/bot/middleware/auth.py app/bot/middleware/rate_limit.py app/bot/handlers/command_permission.py app/bot/handlers/command_user_question.py app/bootstrap.py app/services/task_service.py
     ```
   - `language: system`
   - `pass_filenames: false`
   - `always_run: true`

2. `pytest`
   - stage：`pre-push`
   - entry：
     ```bash
     python -m pytest -q
     ```
   - `language: system`
   - `pass_filenames: false`
   - `always_run: true`

pre-push 阶段任一钩子失败时，pre-commit 框架返回非零退出码，Git 阻止 push。

### 安装配置

在 `.pre-commit-config.yaml` 中声明：

```yaml
default_install_hook_types: [pre-commit, pre-push]
```

开发者本地只需执行：

```bash
pre-commit install
```

也可显式执行：

```bash
pre-commit install --hook-type pre-commit --hook-type pre-push
```

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

该命令用于一次性修复当前工作区格式问题。之后 pre-commit 阶段会继续用 `ruff format app tests` 保持格式一致。

## 验证

实施后运行：

```bash
pre-commit validate-config
pre-commit run --all-files
pre-commit run --hook-stage pre-push --all-files
python -m ruff check app tests
python -m ruff format --check app tests
python -m mypy --follow-imports=skip app/adapters/process/subprocess_runner.py app/bot/middleware/auth.py app/bot/middleware/rate_limit.py app/bot/handlers/command_permission.py app/bot/handlers/command_user_question.py app/bootstrap.py app/services/task_service.py
python -m pytest -q
```

期望：

- `pre-commit validate-config` 通过。
- `pre-commit run --all-files` 运行 ruff 写入式检查；若首次修改文件，需要重新 `git add` 后再运行。
- `pre-commit run --hook-stage pre-push --all-files` 运行 mypy 与 pytest，并返回 0。
- ruff、mypy、pytest 的 CI 同款命令全部通过。
- 工作区不包含临时文件或中间产物。
