# `/cmds` 权限网关透传修复设计

## Summary

修复 `/cmds` 内联按钮启动 Claude 命令时遗漏 `PermissionGateway` 的问题，确保该入口与 `/run`、普通聊天入口使用相同的权限审批链路。

## Goals

- 将 `create_router()` 已持有的 `PermissionGateway` 透传给 `/cmds` 处理器。
- 将同一 gateway 继续传给 `run_prompt_and_stream()`。
- 增加回归测试，验证 `/cmds` 回调不会再次遗漏权限依赖。
- 保持现有权限处理、structured reply pump 和终端审批语义不变。

## Non-goals

- 不修改 hook `tool_use_id` 回填逻辑；日志证明本次请求已经从 `PreToolUse` 成功回填真实 ID。
- 不改变 interactive pump 的十次错误停止策略。
- 不让缺少 gateway 的权限请求静默降级或自动 acknowledge。
- 不把全项目所有可选 `PermissionGateway` 参数改为必填。

## Context

`/run` 和普通聊天入口会将 `create_router()` 接收的 `permission_gateway` 传给 `run_prompt_and_stream()`。`/cmds` 按钮回调则通过 `register_cmds_handler()` 独立调用 `run_prompt_and_stream()`，但当前注册函数既不接收 gateway，调用时也没有传入 gateway。

该入口最终创建的 `PresenterOutputDispatcher` 因而持有 `None`。当 presenter 生成 `PermissionRequestOutput` 时，dispatcher 抛出 `RuntimeError("permission gateway is not configured")`。输出未被 acknowledge，interactive pump 重复处理同一输出十次后停止；终端批准时也没有对应的 gateway registry 记录可供同步。

`PermissionRequest` 接收日志里的 `tool_use_id=null` 出现在 `HookSocketServer` 执行缓存回填之前。后续 `terminal_approved` 日志包含与 `PreToolUse` 相同的真实 ID，因此它不是本次故障根因。

## Proposed design

1. 扩展 `register_cmds_handler()` 的参数，接收 `PermissionGateway | None`。
2. `create_router()` 注册 `/cmds` 处理器时，将已有的 `permission_gateway` 传入。
3. `/cmds` 回调调用 `run_prompt_and_stream()` 时，将该 gateway 原样传入。
4. 增加处理器级回归测试：触发合法 `clcmd:` 回调并断言 `run_prompt_and_stream()` 收到同一个 gateway。

保留 `None` 类型是为了维持现有可选依赖和测试构造方式；修复只保证主应用已经提供 gateway 时，`/cmds` 不会在中途遗漏它。

## Alternatives considered

### 将 `PermissionGateway` 改为全链路必填

能够通过类型检查更早发现遗漏，但会扩大 router、handler 和大量测试构造的改动范围，也会移除现有无 gateway 的降级组装能力。本次故障只涉及一个明确的漏传点，因此不采用。

### dispatcher 缺少 gateway 时跳过权限输出

可以避免 pump 重试，但会静默丢失真实审批请求，甚至让用户误以为任务仍能继续，不符合权限链路的安全语义，因此不采用。

### 修改 hook ID 回填或 terminal resolution fallback

现有日志已经证明真实 `tool_use_id` 成功回填；修改这部分既不能解决 gateway 为 `None`，又会扩大安全敏感路径的行为范围，因此不采用。

## Edge cases and risks

- 测试或精简部署若本来就没有提供 gateway，行为仍与当前一致：只有实际出现权限请求时才会报配置错误。
- 修复不会改变 `/cmds` 回调的消息发送、diff/export 或 spinner 参数；这些当前同样没有从 router 透传，但与本次故障无关。
- 回归测试需要隔离 aiogram 路由注册细节，直接验证回调调用参数，避免依赖真实 Telegram 或 Claude 进程。

## Test / acceptance plan

- 新增测试验证 `register_cmds_handler()` 的回调将传入的 `permission_gateway` 原样交给 `run_prompt_and_stream()`。
- 运行新增测试及相关 router、command、presenter 测试。
- 运行项目既有 lint/type/test 质量检查。
- 使用项目运行验证流程，从 `/cmds` 按钮触发一个需要权限的命令，确认 Telegram 能收到权限请求，日志不再出现 `permission gateway is not configured`，且 pump 不会因此连续失败。

## Open questions

None.
