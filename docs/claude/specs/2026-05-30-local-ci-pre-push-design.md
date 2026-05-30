# 本地 CI pre-push 拦截设计

## 背景

当前仓库的 GitHub CI 在 push 和 pull_request 时执行以下检查：

1. `python -m ruff check app tests`
2. `python -m ruff format --check app tests`
3. `python -m mypy --follow-imports=skip ...`
4. `python -m pytest -q`

本地复现结果显示：

- `ruff check` 通过。
- `mypy` 通过。
- `pytest` 通过，当前为 888 个测试通过。
- `ruff format --check app tests` 失败，有 10 个文件需要格式化。

仓库已有 `.pre-commit-config.yaml`，本机也已安装 `.git/hooks/pre-commit`，但仓库没有托管的 `pre-push` hook。当前 `core.hooksPath` 指向 `.git/hooks`，导致 hook 不在仓库中共享。

## 目标

- 在本地 push 前运行与 CI 对齐的检查。
- 任一检查失败时阻止 push，避免把明显失败的提交推到远程。
- 保留现有 pre-commit 的快速检查和自动修复体验。
- 修复当前已知的格式检查失败。

## 非目标

- 不移除 GitHub CI。
- 不引入新的构建系统、Makefile 或额外任务脚本。
- 不修改 CI 工作流本身。
- 不自动在 pre-push 阶段修改工作区。

## 方案

采用两层 hook：

1. **pre-commit**
   - 新增仓库托管的 `.githooks/pre-commit`。
   - 调用本机可用的 `pre-commit` 执行 `.pre-commit-config.yaml`。
   - 保持提交前轻量检查和自动修复行为。

2. **pre-push**
   - 新增仓库托管的 `.githooks/pre-push`。
   - 从仓库根目录执行完整 CI 同款检查。
   - 按 CI 顺序运行：
     - `python -m ruff check app tests`
     - `python -m ruff format --check app tests`
     - `python -m mypy --follow-imports=skip app/adapters/process/subprocess_runner.py app/bot/middleware/auth.py app/bot/middleware/rate_limit.py app/bot/handlers/command_permission.py app/bot/handlers/command_user_question.py app/bootstrap.py app/services/task_service.py`
     - `python -m pytest -q`
   - 任一命令失败即退出非 0，Git 阻止 push。

3. **Git hooks 路径**
   - 本地执行 `git config core.hooksPath .githooks`。
   - `.githooks` 目录纳入 git 管理。
   - `core.hooksPath` 是本地配置，不提交到仓库。

## 错误处理

- `pre-push` 使用只读检查，不执行 `ruff format`。
- 当前格式问题单独通过 `python -m ruff format app tests` 修复。
- 如果缺少 `pre-commit` 命令，`.githooks/pre-commit` 给出明确提示并失败。
- 如果任一 CI 检查失败，`.githooks/pre-push` 输出失败项并阻止 push。

## 验证

实施后运行：

```bash
python -m ruff format app tests
python -m ruff check app tests
python -m ruff format --check app tests
python -m mypy --follow-imports=skip app/adapters/process/subprocess_runner.py app/bot/middleware/auth.py app/bot/middleware/rate_limit.py app/bot/handlers/command_permission.py app/bot/handlers/command_user_question.py app/bootstrap.py app/services/task_service.py
python -m pytest -q
.githooks/pre-push
git config --get core.hooksPath
```

期望：

- ruff、mypy、pytest 全部通过。
- `.githooks/pre-push` 返回 0。
- `git config --get core.hooksPath` 输出 `.githooks`。
- 工作区不包含临时文件或中间产物。
