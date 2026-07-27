# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概览

这是一个基于 Python 3.11+、aiogram 和 asyncio 的 Telegram 远程 CLI 网关。用户通过 Telegram 执行 Claude Code、Codex、Gemini，或绑定本机已有的 Claude 会话；系统负责进程/tmux 生命周期、结构化会话状态、权限审批、文件传输和消息回传。

`app/bootstrap.py` 中的 `AppContainer` 是唯一组合根。新增功能应通过它装配依赖，不要在 handler 中直接创建 runner、store、Hook server 或后台任务。

## 开发环境与常用命令

项目通过 `.python-version` 绑定 pyenv/pyenv-virtualenv 环境 `remote-coding`。执行 Python 命令前先确认解释器：

```bash
pyenv version
python -c "import sys; print(sys.executable)"
```

安装项目及开发依赖：

```bash
python -m pip install -e ".[dev]"
```

准备配置并从源码启动：

```bash
cp deploy/env/.env.example .env
# 设置 TG_BOT_TOKEN 和 TG_ALLOWED_USER_IDS
python -m app.main
```

其他启动方式：

```bash
tg-cli-gateway
tg-cli-gateway --env-file /path/to/config.env
tg-cli-gateway --version
```

未指定 `--env-file` 时读取当前工作目录的 `.env`，进程环境变量优先。`CLAUDE_TMUX_MODE=true` 时必须能从 `PATH` 找到 `tmux`。

### 测试

```bash
# 全量测试
python -m pytest -q

# 单个文件
python -m pytest tests/test_task_service.py -v

# 单个测试类
python -m pytest tests/unit/test_session_actions.py::TestSessionTombstoneStore

# 单个测试用例
python -m pytest tests/unit/test_session_actions.py::TestSessionTombstoneStore::test_mark_ended

# 失败即停、显示输出、仅重跑上次失败
python -m pytest -x
python -m pytest -s
python -m pytest --lf

# 覆盖率门槛为 80%
python -m pytest tests/ --cov=app --cov-report=term-missing --cov-fail-under=80
```

pytest 使用 `asyncio_mode = "auto"`，异步测试通常无需单独配置 event loop fixture。

### Lint、格式化、类型检查

```bash
python -m ruff check app/ tests/
python -m ruff format --check app/ tests/
python -m mypy app/
```

会修改文件的命令：

```bash
python -m ruff check --fix app/ tests/
python -m ruff format app/ tests/
```

完整质量门禁必须从仓库根目录执行：

```bash
./scripts/quality_check.sh
```

该脚本依次运行 Ruff lint、Ruff format check、Mypy、全量测试和 80% 覆盖率检查。

### 构建

`build` 不在 dev extras 中，需要单独安装：

```bash
python -m pip install build
python -m build
```

发布由 GitHub Actions 完成；仓库没有本地 `twine upload` 流程。

## 高层架构

依赖和职责大致如下：

```text
bot/        Telegram handlers、middleware、presenters
  ↓
services/   任务、会话、权限、外部绑定、文件和生命周期编排
  ↓
adapters/   CLI、subprocess/tmux、Claude Hook、文件存储实现
  ↓
domain/     纯模型、事件和协议

infra/      锁、后台任务、日志、文本处理等通用能力
config/     Settings、环境加载和启动校验
```

- `app/main.py`：解析 `--env-file`/`--version`，加载配置，执行 tmux 预检，启动 polling，并在 Telegram 网络错误后重试。
- `app/bootstrap.py`：创建全部共享服务，管理启动、恢复、周期任务和停机顺序。
- `app/bootstrap_mixins.py`：拆分 JSONL 同步、Hook pipeline、会话匹配、watcher、恢复和事件分发；这些 mixin 只作为 `AppContainer` 内部实现。
- `app/bot/router.py`：组合命令和 callback 路由。Dispatcher 层先执行认证和限流；Router 层再执行错误处理、会话守卫和 callback 校验。

## 关键数据流

### Telegram 任务执行

```text
Telegram update
  → AuthMiddleware / RateLimitMiddleware
  → Router + SessionGuard/CallbackValidator
  → handler
  → TaskService
  → CLIAdapterFactory
  → SubprocessRunner 或 TmuxRunner
  → CLIEvent 异步流
  → RunEventStreamer / StructuredReplyPresenter
  → Telegram
```

- `TaskService` 负责 provider/workdir 校验、并发 semaphore、任务生命周期和取消，不要绕过它直接修改 `TaskRecord`。
- `MemoryTaskStore` 是带 TTL/容量限制的进程内任务历史，重启后不会恢复。
- `SubprocessRunner` 使用参数数组和独立进程组；不要引入 `shell=True` 或 shell 字符串拼接。
- Claude 持久交互使用 tmux；一次性命令和 Codex/Gemini 通常走 subprocess runner。

### 结构化 Claude 会话

Claude JSONL 是会话内容的事实来源：

```text
Claude Hook / JSONL 文件变化
  → ClaudeJSONLParser.parse_incremental()
  → SessionEvent
  → SessionEventProcessor
  → SessionStateCache + SessionStateRepository
  → SessionStore revision / SessionNotifier
  → presenter、完成判定、外部回复推送
```

`SessionState` 保存 phase、turn、工具调用、待审批权限、subagent、解析 checkpoint、revision 和结构化展示游标。状态变更应通过 `SessionStore.process()` 或 `SessionStore.save()`，以保持缓存、checkpoint、持久化和 notifier 一致。

`SessionSupervisor` 为每个会话维护统一 watcher，负责 JSONL debounce、中断检测和 subagent 文件同步。真正的 JSONL 同步在 watcher 内部锁之外执行，避免回调重入死锁。

### Claude Hook、权限与外部会话

Claude Hook 经 Unix socket 进入 `HookSocketServer`，经过消息大小、字段、session id 和 workdir 白名单校验后进入 Hook pipeline。PermissionRequest 的连接会保持到 Telegram/终端作出 allow/deny 或 TTL 超时。

ownership 必须使用显式优先级，禁止为外部会话重新加入仅按 workdir 猜归属的逻辑：

1. `tmux-owned`：`SessionContext.claude_session_id` 命中且有 `terminal_id`。
2. `external-bound`：`ExternalBindingStore` 中有明确绑定。
3. `external-unbound`：其余外部会话，仅进入 discovery/未绑定权限流程。

绑定外部会话时：

- `ExternalSessionBinder` 保存绑定并立即同步 JSONL；
- 绑定时间之前的最后一条 assistant 回复被设为基线，不回放历史；
- 回复推送使用 `ExternalBinding.last_pushed_reply_turn_id`，不要复用 `SessionState.structured_reply_turn_id`；
- 普通 `Stop` 同步并推送新的完整 assistant turn；真正的 `status=ended` 只走结束通知和原有同步路径；
- 同一会话的回复投递由独立 reply-delivery lock 串行，不同会话可并发；
- Telegram HTML 分片必须不超过 4096，无法安全拆分的超长 HTML 标签应降级为纯文本。

权限处理统一经过 `PermissionGateway`、`PermissionCallbackRegistry` 和 Hook socket 回写。`AskUserQuestion` 不参与自动批准；外部 tmux 会话可通过 pane 注入回答。

## 持久化边界

默认持久目录由 `TMUX_DATA_DIR` 控制（默认 `/tmp/tg-cli-gateway`）：

```text
$TMUX_DATA_DIR/
  session_contexts/<user-id>.json
  sessions/<session-id>/
    session.state.json
    conversation.snapshot.json
    parser.cursor.json
    transcript.raw.log
  external_bindings.json
```

- `SessionContext`、结构化 `SessionState`、checkpoint、对话快照和外部绑定会持久化。
- `external_bindings.json` 包含 owner、cwd、PID、活动时间、标题和外部回复游标。
- Claude 原始 JSONL 位于 Claude 配置目录的 `projects/` 下，本项目只解析它。
- 权限 callback registry、未绑定 discovery、Hook pending connection、自动批准、锁和 watcher 是进程内状态。

## 并发约束

容器使用多套按 `session_id` 的 `RefCountedLockRegistry`：

- `_jsonl_sync_locks`：串行增量 JSONL 解析；
- `_session_event_locks`：串行 `SessionState` 变更；
- `_external_reply_delivery_locks`：串行外部回复投递和游标推进。

外部回复流程的锁顺序是 reply-delivery lock，再短暂获取 session-event lock；不要反转顺序。调用 `sync_claude_session()` 时不要预先持有 session-event lock，因为同步会派发事件并再次获取该锁。

任务终态、取消和超时必须继续走 TaskService 的生命周期锁。tmux pane 输入和用户问题注入必须继续使用现有 pane/session 锁，避免并发 `send-keys`。

## 测试组织

- `tests/`：主要回归和组件测试。
- `tests/unit/`：小范围单元测试。
- `tests/property/`：Hypothesis 性质测试，重点覆盖权限状态机、ownership、绑定清理、PID/liveness 和并发不变量。
- `tests/integration/`：启动、Hook、权限和外部会话等跨模块流程。
- `tests/fakes/`：Telegram、CLI 和结构化状态替身。

修改 Hook/会话/权限代码时，除目标单测外，优先运行相关 integration/property 测试，最后执行 `./scripts/quality_check.sh`。
