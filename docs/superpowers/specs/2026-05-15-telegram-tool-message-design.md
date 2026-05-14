# Telegram 工具调用独立消息设计

## 背景

当前 Telegram 端会把 Claude 输出通过 `ChunkSender` 分块发送。结构化会话已经能识别工具调用、权限请求和用户提问，但工具调用进度主要以普通进度消息形式发送，长任务中会产生较多过程消息，也不方便在工具完成后清理。

本设计只改工具调用展示，不改变普通 stdout/stderr 和普通 Claude 回复的发送方式。

## 目标

- 每个非交互工具调用在 Telegram 中有一条独立状态消息。
- 交互工具继续用现有交互消息展示，避免重复消息。
- 工具成功后删除对应状态消息，减少刷屏。
- 工具失败或中断时保留状态消息，方便排查。
- 权限请求和 `AskUserQuestion` 继续使用现有独立交互消息，不复用工具状态消息承载按钮；其中 `AskUserQuestion` 不额外发送泛化工具状态消息。
- Telegram 消息操作失败不能影响 Claude 任务执行。

## 非目标

- 不把普通 Claude 输出改成 edit/freeze 模型。
- 不新增工具消息持久化；工具消息只在当前任务生命周期内维护。
- 不改变权限审批和用户提问 callback 协议。
- 不改 Claude hooks、JSONL 同步或 `SessionStore` 的工具状态来源。

## 架构

数据流保持现有结构化会话为源头：

```text
Claude hooks / JSONL
  -> SessionStore.tool_calls
  -> StructuredReplyPresenter
  -> ToolStatusOutput
  -> ToolMessageManager
  -> Telegram 单工具消息
```

组件职责：

- `SessionStore`：继续维护 `tool_calls` 状态，不新增职责。
- `StructuredReplyPresenter`：检测工具状态变化，产出结构化的 `ToolStatusOutput`。
- `ToolMessageManager`：管理 Telegram 工具消息生命周期，包括发送、编辑、删除和失败降级。
- `command_run.py`：作为编排层，把 presenter 输出分发给对应 presenter/manager。
- `ChunkSender`：继续处理普通文本输出，不承担工具消息生命周期。

## 数据结构

新增结构化输出：

```python
@dataclass(frozen=True)
class ToolStatusOutput:
    tool_use_id: str
    tool_name: str | None
    tool_input: dict | None
    status: str
```

`ProgressUpdateOutput` 继续保留给非工具进度，例如 compacting。

## Presenter 行为

`StructuredReplyPresenter` 维护每个非交互工具上次已发状态。当本次快照中的工具状态与上次不同，则产出 `ToolStatusOutput`。

规则：

- 非交互工具 `RUNNING`：产出执行中状态。
- 非交互工具 `WAITING_FOR_APPROVAL`：产出等待权限状态；权限按钮仍由 `PermissionRequestOutput` 生成。
- 非交互工具 `SUCCESS`：产出成功状态，供 Telegram 层删除工具消息。
- 非交互工具 `ERROR`：产出失败状态，保留工具消息。
- 非交互工具 `INTERRUPTED`：产出中断状态，保留工具消息。
- `AskUserQuestion` 工具优先产出 `UserQuestionOutput`，不额外产出泛化工具状态，避免与现有提问交互消息重复。

## Telegram 工具消息管理

新增 `ToolMessageManager`，按 `tool_use_id` 维护当前任务内的 Telegram 消息：

```text
tool_use_id -> Telegram Message
```

处理规则：

1. 收到非成功状态且没有现有消息：发送一条工具状态消息并记录。
2. 收到非成功状态且已有消息：编辑该消息文本。
3. 收到成功状态且已有消息：删除该消息并移除记录。
4. 收到成功状态但没有现有消息：不发送新消息。
5. 删除失败时：尝试编辑为“执行完成”。
6. 编辑失败时：尝试重新发送一条工具状态消息并替换记录。
7. 发送、编辑、删除及降级都失败时：记录日志，不向上抛出，不中断任务流。

## 消息文案

沿用当前工具输入摘要逻辑，优先显示最有用字段：

- `Bash.command` -> `命令`
- `WebFetch.url` -> `目标`
- `Task/Agent.description` -> `任务`
- `file_path/path/url/command/pattern/query/question/description` 等通用字段
- 其他参数压缩为 JSON 摘要

文案格式：

```text
执行中
工具: Bash
命令: pytest -q
```

```text
等待权限
工具: Bash
命令: rm file
```

```text
执行失败
工具: Bash
命令: pytest -q
```

```text
已中断
工具: Bash
命令: pytest -q
```

成功时默认删除消息。删除失败时降级为：

```text
执行完成
工具: Bash
命令: pytest -q
```

## 交互工具

权限请求和用户提问继续走现有消息：

- `PermissionRequestOutput` 继续发送带“允许/拒绝”按钮的消息。
- `UserQuestionOutput` 继续发送带选项按钮的消息。
- 工具状态消息不挂按钮，避免工具状态生命周期与 callback 协议耦合。

## 错误处理

Telegram API 错误不能影响 Claude 任务执行：

- `sendMessage` 失败：记录日志并丢弃该状态。
- `editMessageText` 失败：尝试重新发送状态消息。
- `deleteMessage` 失败：尝试编辑为“执行完成”。
- 降级操作也失败：记录日志后继续处理后续输出。

## 测试计划

### `StructuredReplyPresenter`

- 工具 `RUNNING` 时产出 `ToolStatusOutput`。
- 工具 `RUNNING -> SUCCESS` 时产出成功状态。
- 工具 `RUNNING -> ERROR` 时产出失败状态。
- 工具 `RUNNING -> INTERRUPTED` 时产出中断状态。
- `AskUserQuestion` 继续只产出 `UserQuestionOutput`，不产出泛化工具状态。
- compacting 仍产出 `ProgressUpdateOutput`。

### `ToolMessageManager`

- 首次 `RUNNING` 发送工具消息。
- 状态更新时编辑同一条消息。
- `SUCCESS` 删除消息。
- 删除失败时编辑为“执行完成”。
- `ERROR` / `INTERRUPTED` 保留并编辑状态消息。
- 发送、编辑、删除异常不会向上抛出。

### `command_run.py`

- `ToolStatusOutput` 被交给 `ToolMessageManager`。
- 普通字符串输出仍走 `ChunkSender`。
- `PermissionRequestOutput` 和 `UserQuestionOutput` 行为保持不变。

## 验收标准

- Claude 调用非交互工具时，Telegram 出现该工具的独立状态消息。
- 非交互工具成功后，该工具状态消息被删除；删除失败时显示“执行完成”。
- 非交互工具失败或中断时，状态消息保留并显示最终状态。
- 权限请求和用户提问仍使用现有带按钮消息。
- 普通 Claude 输出的发送行为不变。
- 相关测试通过。
