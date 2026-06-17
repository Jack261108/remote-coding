# Telegram CLI Gateway (Python + aiogram)

[![CI](https://github.com/Jack261108/remote-coding/actions/workflows/ci.yml/badge.svg)](https://github.com/Jack261108/remote-coding/actions/workflows/ci.yml)
[![Quality](https://github.com/Jack261108/remote-coding/actions/workflows/quality.yml/badge.svg)](https://github.com/Jack261108/remote-coding/actions/workflows/quality.yml)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

Telegram 交互式远程 CLI 执行网关：在 Telegram 下发任务，服务在远程 Linux 执行本机 CLI（Claude Code / Codex CLI / Gemini CLI），并将过程与结果回传。

## 功能

### 核心能力

- Long Polling 接入 Telegram
- 统一 provider 适配层（`claude_code` / `codex` / `gemini`）
- 可选 Claude tmux 终端模式（可在本机 attach 查看真实终端）
- 安全执行：`subprocess_exec` 参数数组调用，禁止 `shell=True`
- 任务状态机：`PENDING -> RUNNING -> SUCCEEDED/FAILED/TIMEOUT/CANCELED`

### 会话管理

- 会话生命周期管理：创建、切换、恢复、销毁
- 外部会话绑定：支持 tmux 会话绑定和自动发现
- 会话状态缓存与持久化
- 会话所有权解析与权限控制

### 权限系统

- 统一权限网关：自动审批 + 内联键盘手动审批 + 回调状态机
- 风险评估器：基于操作类型和上下文的风险评估
- 白名单鉴权 + 用户维度限流
- 管理员密码验证机制

### 文件处理

- 文件上传队列与清理机制
- 文件发送与接收适配器
- 结果导出器：支持多种格式导出
- Diff 生成器：代码变更对比

### 运维支持

- 外部会话绑定清理：pid 存活检测 + 空闲 TTL 自动回收
- 后台任务注册表与监控
- 周期性清理任务
- 进程存活检测

### 用户交互

- 输出分片（<4096）+ 节流 + 结束总结
- 用户问题服务：支持交互式问答
- 结构化回复追踪
- 状态显示服务

## 目录结构

```
app/
├── main.py                              # 入口（CLI 参数：--version、--env-file）
├── bootstrap.py                         # 依赖装配
├── bootstrap_base.py                    # 基础引导类
├── bootstrap_mixins.py                  # 引导混入类
├── config/
│   ├── settings.py                      # 配置与校验
│   └── loader.py                        # 配置加载器
├── adapters/
│   ├── cli/
│   │   ├── base.py                      # CLI 适配器基类
│   │   ├── claude_code.py               # Claude Code 适配器
│   │   ├── codex_cli.py                 # Codex CLI 适配器
│   │   ├── gemini_cli.py                # Gemini CLI 适配器
│   │   └── factory.py                   # 适配器工厂
│   └── process/
│       └── subprocess_runner.py         # 进程执行与事件流
├── bot/
│   ├── router.py                        # 路由
│   ├── handlers/
│   │   ├── command_run.py               # /run 命令处理
│   │   ├── command_session.py           # /session 命令处理
│   │   ├── command_status.py            # /status 命令处理
│   │   ├── command_cancel.py            # /cancel 命令处理
│   │   ├── command_list.py              # /list 命令处理
│   │   ├── command_cmds.py              # /cmds 命令处理
│   │   ├── command_export.py            # /export 命令处理
│   │   ├── command_resume.py            # /resume 命令处理
│   │   ├── command_attach.py            # /attach 命令处理
│   │   ├── command_exit.py              # /exit 命令处理
│   │   ├── command_claude.py            # /claude 命令处理
│   │   ├── command_permission.py        # 权限命令处理
│   │   ├── command_user_question.py     # 用户问题处理
│   │   ├── file_upload.py               # 文件上传处理
│   │   ├── run_event_streamer.py        # 运行事件流
│   │   ├── run_presenter_dispatcher.py  # 运行展示调度器
│   │   ├── run_telegram_messenger.py    # Telegram 消息发送
│   │   ├── session_actions.py           # 会话操作
│   │   ├── external_permission.py       # 外部权限处理
│   │   ├── external_session.py          # 外部会话处理
│   │   ├── admin_challenge.py           # 管理员验证
│   │   └── callback_utils.py            # 回调工具
│   ├── middleware/
│   │   ├── auth.py                      # 鉴权中间件
│   │   └── rate_limit.py                # 限流中间件
│   └── presenters/
│       ├── chunk_sender.py              # 分片发送
│       └── permission_message_builder.py # 权限消息构建
├── services/
│   ├── task_service.py                  # 任务服务
│   ├── session_service.py               # 会话服务
│   ├── permission_gateway.py            # 统一权限网关
│   ├── permission_callback_registry.py  # 权限回调状态机
│   ├── permission_service.py            # 权限服务
│   ├── auto_approve_service.py          # 自动审批服务
│   ├── risk_evaluator.py                # 风险评估器
│   ├── session_registry.py              # 会话注册表
│   ├── session_store.py                 # 会话存储
│   ├── session_state_cache.py           # 会话状态缓存
│   ├── session_state_repository.py      # 会话状态仓库
│   ├── session_supervisor.py            # 会话监控器
│   ├── session_scanner.py               # 会话扫描器
│   ├── session_notifier.py              # 会话通知器
│   ├── session_lookup_service.py        # 会话查找服务
│   ├── session_id_resolver.py           # 会话 ID 解析器
│   ├── session_event_processor.py       # 会话事件处理器
│   ├── session_action_validator.py      # 会话操作验证器
│   ├── session_ownership_resolver.py    # 会话所有权解析器
│   ├── terminal_session_service.py      # 终端会话服务
│   ├── external_binding_store.py        # 外部绑定存储
│   ├── external_binding_reaper.py       # 过期绑定回收器
│   ├── external_binding_cleanup_service.py # 外部绑定清理服务
│   ├── external_binding_cleanup_task.py # 外部绑定清理任务
│   ├── external_session_binder.py       # 外部会话绑定器
│   ├── external_session_discovery.py    # 外部会话发现
│   ├── external_session_push_notifier.py # 外部会话推送通知
│   ├── external_user_question_state.py  # 外部用户问题状态
│   ├── unbound_permission_handler.py    # 未绑定权限处理器
│   ├── background_task_registry.py      # 后台任务注册表
│   ├── process_liveness.py              # 进程存活检测
│   ├── janitor_task.py                  # 清理任务
│   ├── periodic_janitor.py              # 周期性清理
│   ├── jsonl_file_watcher.py            # JSONL 文件监控
│   ├── claude_command_discovery.py      # Claude 命令发现
│   ├── claude_jsonl_parser.py           # Claude JSONL 解析器
│   ├── context_builder.py               # 上下文构建器
│   ├── diff_generator.py                # Diff 生成器
│   ├── file_receiver.py                 # 文件接收器
│   ├── file_sender.py                   # 文件发送器
│   ├── result_exporter.py               # 结果导出器
│   ├── status_display.py                # 状态显示
│   ├── structured_reply_tracker.py      # 结构化回复追踪
│   ├── structured_session_resolver.py   # 结构化会话解析器
│   ├── task_lifecycle_service.py        # 任务生命周期服务
│   ├── message_sender.py                # 消息发送器
│   ├── lock_registry.py                 # 锁注册表
│   ├── admin_password_service.py        # 管理员密码服务
│   ├── upload_queue.py                  # 上传队列
│   ├── upload_cleanup.py                # 上传清理
│   └── user_question_service.py         # 用户问题服务
├── domain/
│   └── ...                              # 领域模型
└── infra/
    └── ...                              # 基础设施

deploy/
├── env/.env.example                     # 配置模板
├── systemd/tg-cli-bot.service           # systemd unit
└── scripts/healthcheck.sh               # 健康检查脚本

docs/
├── architecture.md                      # 架构文档
├── middleware.md                         # 中间件文档
├── quality.md                           # 质量保证文档
├── testing.md                           # 测试文档
└── claude/                              # Claude 相关文档

scripts/
├── quality_check.sh                     # 代码质量门禁脚本
└── release_check.py                     # 发布检查脚本

packaging/
├── tg-cli-gateway.rb                    # Homebrew formula
└── VERIFICATION.md                      # 打包验证文档
```

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

### 核心命令

| 命令 | 说明 |
|------|------|
| `/start` | 帮助与当前 session |
| `/run <provider> <task>` | 执行任务 |
| `/status [task_id]` | 查询单任务/最近任务 |
| `/cancel <task_id>` | 取消任务 |
| `/session [provider] [workdir]` | 查看/切换 session |
| `/list` | 列出当前用户的活跃任务 |

### 高级命令

| 命令 | 说明 |
|------|------|
| `/cmds` | 列出可用的 Claude 命令 |
| `/export <task_id>` | 导出任务结果 |
| `/resume <task_id>` | 恢复已暂停的任务 |
| `/attach <session>` | 附加到现有会话 |
| `/exit` | 退出当前会话 |
| `/claude <task>` | 快速执行 Claude 任务 |

### Provider 支持

- `claude_code`：Claude Code CLI
- `codex`：Codex CLI
- `gemini`：Gemini CLI

支持常见别名（如 `claude`、`c`、`g` 等）。

### tmux 模式

若开启 `CLAUDE_TMUX_MODE=true`，`claude_code` 会在 tmux session 中运行，并在任务开始消息返回 `tmux_session`，可本机查看：

```bash
tmux attach -t <tmux_session>
```

## 安全边界

### 访问控制

- 默认仅白名单用户（`TG_ALLOWED_USER_IDS`）可执行；设置为 `*` 可放开所有用户（仅建议本地调试）
- 工作目录必须在 `ALLOWED_WORKDIRS` 内
- 管理员密码验证机制

### 执行安全

- 不使用 shell 拼接，避免命令注入
- 超时控制 + 取消控制
- 输出总量限制（`TASK_OUTPUT_CHAR_LIMIT`）

### 网络配置

- 网络受限时可配置 `TG_PROXY_URL`，并可通过 `TG_REQUEST_TIMEOUT_SEC` / `TG_POLLING_RETRY_DELAY_SEC` 调整连接超时与重试（配置代理需安装 `aiohttp-socks`）

### 权限网关

统一权限处理系统，支持两种审批模式：

- **自动审批**：对可信操作自动放行，无需人工干预
- **手动审批**：通过 Telegram 内联键盘展示操作详情，用户点击按钮批准/拒绝
- **回调状态机**：管理审批请求的生命周期，支持超时自动拒绝
- **风险评估**：基于操作类型、用户信任级别和配置策略进行风险评估

### 外部会话绑定清理

自动管理外部 CLI 进程的会话绑定：

- **pid 存活检测**：定期检查绑定进程是否仍在运行
- **空闲 TTL 回收**：超过指定时间无活动的绑定自动清理
- **决策矩阵**：综合 pid 存活状态和空闲时间决定是否回收

## 测试

```bash
# 运行所有测试
pytest -q

# 运行特定测试文件
pytest tests/test_task_service.py -v

# 运行测试并生成覆盖率报告
pytest --cov=app --cov-report=html

# 运行代码质量检查
./scripts/quality_check.sh
```

### 测试结构

```
tests/
├── unit/           # 单元测试
├── integration/    # 集成测试
├── property/       # 属性测试（Hypothesis）
└── fakes/          # 测试替身
```

当前用例覆盖：provider 映射、任务状态机、输出分片与节流、权限网关、会话管理、文件处理等。

## 开发 / 本地钩子（pre-commit）

本仓库通过 pre-commit 框架的多阶段钩子，让本地检查覆盖 GitHub Actions CI 的同一组核心检查。在项目虚拟环境已按最新 dev 依赖安装的前提下，可在 `git push` 到达远程之前拦截大多数会导致 CI 失败的问题。

### 钩子做的事

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

## CI/CD

项目使用 GitHub Actions 进行持续集成：

### CI 工作流（ci.yml）

- 代码风格检查（Ruff）
- 类型检查（mypy）
- 单元测试（pytest）
- 覆盖率报告

### 质量检查工作流（quality.yml）

- 代码复杂度分析
- 安全扫描
- 依赖检查

### 发布工作流（release.yml）

- 自动版本号管理
- PyPI 发布
- GitHub Release 创建

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

## 文档

- [架构设计](docs/architecture.md)
- [中间件指南](docs/middleware.md)
- [质量保证](docs/quality.md)
- [测试指南](docs/testing.md)
- [模块调用图](docs/module_call_diagram.md)
- [会话生命周期](docs/session-lifecycle-issues.md)
- [代码审查报告](CODE_REVIEW_REPORT.md)
- [贡献指南](CONTRIBUTING.md)

## 许可证

MIT License - 详见 [LICENSE](LICENSE)

## 链接

- [GitHub 仓库](https://github.com/Jack261108/remote-coding)
- [问题反馈](https://github.com/Jack261108/remote-coding/issues)
- [PyPI 包](https://pypi.org/project/tg-cli-gateway/)
