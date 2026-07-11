# 架构说明

本文档描述 Telegram CLI Gateway 项目的整体架构设计。

## 目录结构

```
app/
├── adapters/          # 外部系统适配器层
│   ├── cli/           # CLI 工具适配器（Claude、Codex、Gemini）
│   ├── claude/        # Claude 专用适配器（Hook、路径、JSONL）
│   ├── process/       # 进程管理适配器（子进程、Tmux）
│   └── storage/       # 存储适配器（文件会话、任务存储）
├── bot/               # Telegram Bot 层
│   ├── adapters/      # Bot 适配器（消息发送）
│   ├── handlers/      # 命令和事件处理器
│   ├── middleware/     # 中间件（认证、限流、错误处理）
│   └── presenters/    # 消息格式化和展示
├── config/            # 配置管理
├── domain/            # 领域模型层
├── infra/             # 基础设施层
└── services/          # 业务服务层
```

## 分层架构

项目采用经典的分层架构，各层职责明确：

### 1. 领域层（Domain）

**目录**: `app/domain/`

定义核心业务模型和协议，不依赖任何外部框架。

- **models.py** - 核心数据模型（SessionContext、TaskRecord 等）
- **protocols.py** - 接口协议定义
- **session_models.py** - 会话状态模型
- **session_tombstone.py** - 会话墓碑存储
- **external_session_models.py** - 外部会话模型
- **file_models.py** - 文件相关模型
- **hook_models.py** - Hook 事件模型
- **permission_models.py** - 权限相关模型
- **user_question_models.py** - 用户问题模型

### 2. 服务层（Services）

**目录**: `app/services/`

实现核心业务逻辑，依赖领域层定义的协议。

- **SessionService** - 会话生命周期管理
- **TaskService** - 任务调度和执行
- **PermissionGateway** - 权限审批网关
- **ExternalSessionDiscoveryService** - 外部会话发现
- **PeriodicJanitor** - 周期性任务调度器

### 3. 适配器层（Adapters）

**目录**: `app/adapters/`

封装外部系统交互，实现领域层定义的协议。

- **CLI 适配器** - 封装 Claude、Codex、Gemini 等 CLI 工具
- **进程适配器** - 管理子进程和 Tmux 会话
- **存储适配器** - 文件系统和内存存储

### 4. Bot 层

**目录**: `app/bot/`

Telegram Bot 接口层，处理用户交互。

- **handlers/** - 命令处理器（/run、/claude、/session 等）
- **middleware/** - 请求处理中间件
- **presenters/** - 消息格式化

### 5. 基础设施层（Infra）

**目录**: `app/infra/`

提供通用技术能力。

- **periodic_task.py** - 周期性任务基类
- **file_mtime_utils.py** - 文件修改时间追踪
- **gitignore_utils.py** - Gitignore 模式加载
- **lock_registry.py** - 引用计数锁注册表
- **async_utils.py** - 异步工具
- **logging.py** - 日志工厂
- **scan_filter.py** - 扫描过滤
- **source_text_normalization.py** - 源文本归一化
- **text_formatting.py** - 文本格式化
- **tmux_preflight.py** - Tmux 启动预检
- **user_question_constants.py** - 用户问题常量

## 核心组件

### AppContainer

**文件**: `app/bootstrap.py`

应用的依赖注入容器，负责创建和组装所有组件。

```python
class AppContainer:
    def __init__(self, settings: Settings) -> None:
        # 创建所有组件实例
        ...

    async def start(self) -> None:
        # 启动所有服务
        ...

    async def stop(self) -> None:
        # 停止所有服务
        ...

    def wire(self) -> None:
        # 注册中间件和路由
        ...
```

### 中间件管道

请求处理流程：

```
请求 → AuthMiddleware → RateLimitMiddleware → ErrorHandlingMiddleware → SessionGuardMiddleware → Handler
```

1. **AuthMiddleware** - 用户身份验证
2. **RateLimitMiddleware** - 请求限流
3. **ErrorHandlingMiddleware** - 统一错误处理
4. **SessionGuardMiddleware** - 会话状态检查
5. **CallbackValidatorMiddleware** - 回调数据验证

### 周期性任务系统

采用两级调度架构：

1. **PeriodicBackgroundTask** - 单任务后台循环基类
2. **PeriodicJanitor** - 多任务调度器

```
JanitorTask (PeriodicBackgroundTask)
    └── PeriodicJanitor.run()
        ├── upload_queue_cleanup (每 60 秒)
        ├── upload_file_cleanup (每 30 分钟)
        ├── external_discovery_cleanup (每 60 秒)
        ├── session_health_check (每 60 秒)
        └── periodic_recheck (每 5 秒)

ExternalBindingCleanupTask (PeriodicBackgroundTask)
    └── ExternalBindingCleanupService.run_cleanup()
```

## 数据流

### 任务执行流程

```
用户消息 → TaskService.run()
    → CLIAdapterFactory.create()
        → SubprocessRunner / TmuxRunner
            → Claude CLI 执行
                → HookSocketServer 处理权限请求
                    → PermissionGateway 审批
                        → 结果流式返回
```

### 会话管理流程

```
/claude 命令 → SessionService.create()
    → TmuxRunner.ensure_terminal()
        → Claude CLI 启动
            → SessionSupervisor 监控
                → JSONL 解析和同步
```

## 设计原则

1. **依赖倒置** - 高层模块不依赖低层模块，都依赖抽象
2. **单一职责** - 每个类/模块只负责一件事
3. **开闭原则** - 对扩展开放，对修改关闭
4. **接口隔离** - 使用小而专一的协议接口

## 扩展指南

### 添加新的 CLI 适配器

1. 在 `app/adapters/cli/` 创建新的适配器类
2. 实现 `CLIAdapter` 协议
3. 在 `CLIAdapterFactory` 中注册
4. 在 `Settings` 中添加配置项

### 添加新的中间件

1. 在 `app/bot/middleware/` 创建新的中间件类
2. 继承 `BaseMiddleware`
3. 实现 `__call__` 方法
4. 在 `app/bot/router.py` 中注册

### 添加新的周期性任务

1. 继承 `PeriodicBackgroundTask` 或使用 `PeriodicJanitor.register()`
2. 实现 `_execute()` 方法
3. 在 `AppContainer` 中创建实例
4. 在 `start()` 方法中启动
