# 模块调用图（概览）

本文给出项目的高层模块依赖视图与关键调用链，不展开每个 `.py` 文件的逐行调用关系。
逐层文件清单见 [README 目录结构](../README.md)，分层职责见 [架构说明](architecture.md)。

## 分层依赖方向

依赖只能从上层指向下层，禁止反向依赖：

```
bot/      handlers · middleware · presenters   ← Telegram 接口、请求编排
  ↓
services/  TaskService · SessionService · PermissionGateway · 各 *_service
  ↓
adapters/  cli · process · storage · claude      ← 外部系统交互，实现 domain 协议
  ↓
domain/    models · protocols · *_models         ← 纯领域模型，不依赖任何框架
  ↓
infra/     async_utils · lock_registry · logging · periodic_task …   ← 通用技术能力
config/    settings · loader                     ← 配置与校验
```

`app/bootstrap.py`（`AppContainer`）负责跨层装配：实例化各层组件、注入依赖、注册路由与中间件、在 `start()`/`stop()` 中管理生命周期。

## 关键调用链

### 任务执行（`/run` / `/claude`）

```
bot/handlers/command_run.py · command_claude.py
  → services/task_service.TaskService.run()
    → adapters/cli/factory.CLIAdapterFactory.create()
        → adapters/process/subprocess_runner.SubprocessRunner
        ┊  或 adapters/process/tmux_runner.TmuxRunner   (TmuxSessionMixin + TmuxCommandMixin + TmuxLogMixin)
            → Claude/Codex/Gemini CLI 子进程
              → adapters/claude/hook_socket_server.HookSocketServer   (接 Claude Code hook 事件)
                  → services/permission_gateway.PermissionGateway   (auto_approve / 内联键盘手动审批)
                      → services/permission_callback_registry    (回调状态机，超时自动拒绝)
    → 事件流 adapters/process/* → services/structured_reply_tracker / status_display
        → bot/presenters/* · bot/handlers/run_event_streamer.py    (分片、节流、回传)
```

### 会话管理（`/claude` tmux 模式）

```
bot/handlers/command_claude.py
  → services/session_service.SessionService.create()
    → adapters/process/tmux_runner.TmuxRunner.ensure_terminal()
        → tmux 会话创建 / Claude CLI 启动
          → services/session_supervisor.SessionSupervisor    (监控 + 低频兜底)
              → services/jsonl_file_watcher   (watchdog 监听 .jsonl)
              → services/claude_jsonl_parser  (解析会话事件)
```

### 外部会话绑定清理

```
infra/periodic_task.PeriodicBackgroundTask
  → services/external_binding_cleanup_task.ExternalBindingCleanupTask
      → services/external_binding_cleanup_service    (pid 存活检测 + 空闲 TTL 回收)
          → services/external_binding_store · process_liveness
```

### 周期性清理（Janitor）

```
services/periodic_janitor.PeriodicJanitor.run()
  ├── services/janitor_task: upload_queue_cleanup / upload_file_cleanup
  ├── services/external_session_discovery: external_discovery_cleanup
  ├── services/session_supervisor: session_health_check
  └── periodic_recheck    (每 5 秒)
```

## 说明

- `adapters/process/` 内 `TmuxRunner` 组合三个 Mixin：`TmuxSessionMixin`（会话检查，`app/adapters/process/tmux_session.py`）、`TmuxCommandMixin`（tmux 命令，`tmux_commands.py`）、`TmuxLogMixin`（日志，`tmux_log.py`）。`claude_terminal_facade.py` 把 `TmuxRunner` 暴露为 Claude terminal capability。
- 中间件管道顺序：`AuthMiddleware → RateLimitMiddleware → ErrorHandlingMiddleware → SessionGuardMiddleware → CallbackValidatorMiddleware → Handler`（见 [中间件指南](middleware.md)）。
- 本图为概览，未覆盖全部次要调用；如需精确引用，以源码为准。
