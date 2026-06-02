# Telegram CLI Gateway (Python + aiogram)

Telegram 交互式远程 CLI 执行网关：在 Telegram 下发任务，服务在远程 Linux 执行本机 CLI（Claude Code / Codex CLI / Gemini CLI），并将过程与结果回传。

## 功能

- Long Polling 接入 Telegram
- 统一 provider 适配层（`claude_code` / `codex` / `gemini`）
- 可选 Claude tmux 终端模式（可在本机 attach 查看真实终端）
- 安全执行：`subprocess_exec` 参数数组调用，禁止 `shell=True`
- 任务状态机：`PENDING -> RUNNING -> SUCCEEDED/FAILED/TIMEOUT/CANCELED`
- 命令：`/start`、`/run`、`/status`、`/cancel`、`/session`、`/list`
- 统一权限网关：自动审批 + 内联键盘手动审批 + 回调状态机
- 外部会话绑定清理：pid 存活检测 + 空闲 TTL 自动回收
- 白名单鉴权 + 用户维度限流
- 输出分片（<4096）+ 节流 + 结束总结
- 内存存储（预留后续扩展 sqlite）
- systemd 部署样例 + healthcheck 脚本

## 目录

- `app/main.py`：入口（CLI 参数：`--version`、`--env-file`）
- `app/bootstrap.py`：依赖装配
- `app/config/settings.py`：配置与校验
- `app/config/loader.py`：配置加载器（env 文件分类逻辑）
- `app/adapters/cli/*`：provider 适配器与工厂
- `app/adapters/process/subprocess_runner.py`：进程执行与事件流
- `app/services/task_service.py`：任务服务
- `app/services/session_service.py`：会话服务
- `app/services/permission_gateway.py`：统一权限网关
- `app/services/permission_callback_registry.py`：权限回调状态机
- `app/services/external_binding_cleanup_service.py`：外部绑定清理服务
- `app/services/external_binding_reaper.py`：过期绑定回收器
- `app/services/process_liveness.py`：进程存活检测
- `app/bot/router.py`：路由
- `app/bot/handlers/*`：命令处理
- `app/bot/middleware/*`：鉴权与限流
- `app/bot/presenters/chunk_sender.py`：分片发送
- `app/bot/presenters/permission_message_builder.py`：权限消息构建器
- `app/infra/tmux_preflight.py`：tmux 启动预检
- `deploy/env/.env.example`：配置模板
- `deploy/systemd/tg-cli-bot.service`：systemd unit
- `deploy/scripts/healthcheck.sh`：健康检查脚本
- `packaging/tg-cli-gateway.rb`：Homebrew formula
- `packaging/VERIFICATION.md`：打包验证文档

## 安装

### 通过 Homebrew（推荐）

```bash
brew tap Jack261108/tg-cli-gateway
brew install tg-cli-gateway
```

### 通过 pip（PyPI）

```bash
pip install tg-cli-gateway
```

### 从源码安装

```bash
python3 -m pip install -e ".[dev]"
```

### CLI 参数

安装后可使用 `tg-cli-gateway` 命令，支持以下参数：

```bash
tg-cli-gateway --version          # 显示版本号
tg-cli-gateway --env-file PATH    # 指定 env 配置文件路径
tg-cli-gateway                    # 使用默认配置启动
```

配置必填环境变量（二选一）：

```bash
# 方式 1：进程环境变量
export TG_BOT_TOKEN=<your-token>
export TG_ALLOWED_USER_IDS=<user-id>

# 方式 2：Env_File（默认读取当前目录 .env，或通过 --env-file 指定）
cp deploy/env/.env.example .env
# 修改 .env 中的 TG_BOT_TOKEN 和 TG_ALLOWED_USER_IDS
```

可选：当 `CLAUDE_TMUX_MODE=true` 启用 tmux 终端模式时，需额外安装 tmux：

```bash
brew install tmux
```

> **注意**：启用 tmux 模式时，启动会自动检测 tmux 是否可用。若 tmux 未安装，会输出错误信息并退出。

## 快速开始

1. 配置环境变量

```bash
cp deploy/env/.env.example .env
# 修改 .env 中的 TG_BOT_TOKEN 和 TG_ALLOWED_USER_IDS
```

2. 启动

```bash
# Homebrew / pip 安装后
tg-cli-gateway

# 指定 env 文件
tg-cli-gateway --env-file /path/to/config.env

# 或从源码
python3 -m app.main
```

## Telegram 命令

- `/start`：帮助与当前 session
- `/run <provider> <task text>`：执行任务
- `/status [task_id]`：查询单任务/最近任务
- `/cancel <task_id>`：取消任务
- `/session [provider] [workdir]`：查看/切换 session
- `/list`：列出当前用户的活跃任务

provider 支持：`claude_code`、`codex`、`gemini`（含常见别名）

若开启 `CLAUDE_TMUX_MODE=true`，`claude_code` 会在 tmux session 中运行，并在任务开始消息返回 `tmux_session`，可本机查看：

```bash
tmux attach -t <tmux_session>
```

## 安全边界

- 默认仅白名单用户（`TG_ALLOWED_USER_IDS`）可执行；设置为 `*` 可放开所有用户（仅建议本地调试）
- 网络受限时可配置 `TG_PROXY_URL`，并可通过 `TG_REQUEST_TIMEOUT_SEC` / `TG_POLLING_RETRY_DELAY_SEC` 调整连接超时与重试（配置代理需安装 `aiohttp-socks`）
- `CLAUDE_TMUX_MODE=true` 时需本机安装 tmux（启动时自动预检）
- 工作目录必须在 `ALLOWED_WORKDIRS` 内
- 不使用 shell 拼接，避免命令注入
- 超时控制 + 取消控制
- 输出总量限制（`TASK_OUTPUT_CHAR_LIMIT`）

### 权限网关

统一权限处理系统，支持两种审批模式：

- **自动审批**：对可信操作自动放行，无需人工干预
- **手动审批**：通过 Telegram 内联键盘展示操作详情，用户点击按钮批准/拒绝
- **回调状态机**：管理审批请求的生命周期，支持超时自动拒绝

权限决策基于操作类型、用户信任级别和配置策略。

### 外部会话绑定清理

自动管理外部 CLI 进程的会话绑定：

- **pid 存活检测**：定期检查绑定进程是否仍在运行
- **空闲 TTL 回收**：超过指定时间无活动的绑定自动清理
- **决策矩阵**：综合 pid 存活状态和空闲时间决定是否回收

## 测试

```bash
pytest -q
```

当前用例覆盖：provider 映射、任务状态机、输出分片与节流。

## 开发 / 本地钩子（pre-commit）

本仓库通过 pre-commit 框架的多阶段钩子，让本地检查覆盖 GitHub Actions CI 的同一组核心检查。在项目虚拟环境已按最新 dev 依赖安装的前提下，可在 `git push` 到达远程之前拦截大多数会导致 CI 失败的问题。

钩子做的事：

- **pre-commit 阶段（执行 `git commit` 时）**：运行 ruff，即 `ruff check --fix app tests`（lint，自动修复）与 `ruff format app tests`（格式化）。这些检查很快，保证提交流畅。
- **pre-push 阶段（执行 `git push` 时）**：运行 `mypy --follow-imports=skip`（针对与 CI 相同的 7 个文件）与 `pytest -q`（完整测试套件）。这些检查较慢，放在推送前一次性拦截。

### 一次性安装

`pre-commit` CLI 来自 dev 附加依赖，执行 `pip install -e ".[dev]"` 即可获得，**无需各自手动全局安装**。

安装钩子（推荐，模块名为下划线 `pre_commit`，可确保使用项目虚拟环境中的 `pre-commit`）：

```bash
python -m pre_commit install
```

等价的显式写法（不依赖默认声明，手动指定两类钩子）：

```bash
python -m pre_commit install --hook-type pre-commit --hook-type pre-push
```

如果已确认 PATH 上的 `pre-commit` 来自项目虚拟环境，也可使用简写：

```bash
pre-commit install
```

### 环境前置条件

执行 `git commit` / `git push` 时，shell 中的 `python` 必须解析到**已执行过 `pip install -e ".[dev]"` 的项目虚拟环境**。钩子通过 `python -m <tool>` 调用 ruff / mypy / pytest；只有在该环境已更新到当前 dev 依赖时，本地工具版本才与 CI 的安装来源一致，才能维持「本地通过则 CI 通常通过」的前置检查效果。

### 绕过钩子（`--no-verify`）

紧急情况下可跳过本地 pre-push 检查：

```bash
git push --no-verify
```

注意：`--no-verify` 只会跳过**本地** pre-push 钩子，**远端 CI 仍会执行完整的检查集**（ruff lint、ruff format 校验、mypy、pytest）。因此该选项只是绕过本地的提前反馈，并不能跳过 CI。

## Linux systemd 部署

1. 将项目部署到 `/opt/tg-cli-gateway`
2. 准备 `.env` 与虚拟环境
3. 复制 `deploy/systemd/tg-cli-bot.service` 到 `/etc/systemd/system/`
4. 执行：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now tg-cli-bot.service
sudo systemctl status tg-cli-bot.service
```

健康检查：

```bash
bash deploy/scripts/healthcheck.sh
```
