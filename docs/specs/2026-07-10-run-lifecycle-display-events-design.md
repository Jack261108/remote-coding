# `/run` 生命周期展示事件标准化设计

## Summary

将 `/run` 的 stdout/stderr 和终态生命周期消息接入现有 `DisplayEvent → RenderCommand → PresenterOutputDispatcher` 链路，让 `RunEventStreamer` 只负责事件编排，不再直接决定 Telegram 文本的发送方式，同时保持现有用户可见行为不变。

## Goals

- 将 stdout/stderr 映射为 typed display event 和 render command。
- 将 success/failed/timeout/canceled 生命周期消息映射为 typed display event 和 render command。
- 由 dispatcher 统一执行 lifecycle message 编辑及编辑失败后的补发。
- 保持现有文本、图标、文本清洗、flush、fallback 和状态清理行为。
- 为新增映射和 dispatcher 执行路径补充直接测试。

## Non-goals

- 不收编 `ExternalSessionPushNotifier`。
- 不修改 external user question PTY injection。
- 不修改 permission callback、registry、auto approve 或 keyboard 语义。
- 不修改 diff、auto-export、queued upload 和附件发送流程。
- 不修改 spinner 动画和 `StatusDisplayService` 状态机。
- 不建立全局事件总线。
- 不改变现有 presenter output DTO。

## Context

项目已完成三个兼容层阶段：

1. typed `SessionEvent` payload。
2. canonical source text normalization。
3. `/run` structured presenter output 的 `DisplayEvent / RenderCommand` 映射与 dispatcher 执行层。

当前 `RunEventStreamer` 仍有两类展示路径绕过新的映射层：

- stdout/stderr 在 streamer 中拼接前缀后直接调用 `dispatcher.push_text()`。
- EXITED/FAILED/TIMEOUT/CANCELED 在 streamer 中构造消息后直接调用 `RunTelegramMessenger.edit_message_safely()`，编辑失败时再调用 `answer_safely()`。

这使 streamer 同时承担事件编排和 Telegram 展示决策。本阶段只收敛 `/run` 内部生命周期展示，不跨到 external-bound session。

## Proposed design

### Display event payload

在 `run_display_models.py` 中新增小型、不可变的 payload DTO：

- `StreamTextDisplayPayload`
  - `text: str`
  - `is_stderr: bool = False`
- `TaskSucceededDisplayPayload`
  - `task_id: str`
  - `duration: str`
  - `truncated: bool`
  - `exit_code: int | None`
- `TaskFailedDisplayPayload`
  - `event_type: EventType`
  - `task_id: str`
  - `error_text: str`
  - `duration: str`
  - `truncated: bool`

终态 payload 只携带展示所需数据，不透传完整 `CLIEvent`，避免把 runner/domain 事件对象绑定到执行层。

### DisplayEventKind

新增：

- `STREAM_TEXT`
- `TASK_SUCCEEDED`
- `TASK_FAILED`

`FAILED`、`TIMEOUT`、`CANCELED` 共用 `TASK_FAILED`，具体图标和标签继续由 `EventType` 决定，避免创建三个重复 command 分支。

### RenderCommandKind

新增：

- `BUFFER_STREAM_TEXT`
- `COMPLETE_LIFECYCLE`
- `FAIL_LIFECYCLE`

映射规则：

- `STREAM_TEXT` → `BUFFER_STREAM_TEXT`，`flush_before=False`。
- `TASK_SUCCEEDED` → `COMPLETE_LIFECYCLE`，`flush_before=True`。
- `TASK_FAILED` → `FAIL_LIFECYCLE`，`flush_before=True`。

### Text construction boundary

现有 `_build_success_message()` 和 `_build_error_message()` 从 `run_event_streamer.py` 移到 run display/render 模块，作为纯格式化函数，或者由 mapper 构造 command 时调用。推荐由 dispatcher helper 调用纯格式化函数：

- mapper 只分类并保留 typed payload。
- dispatcher 负责把 payload 渲染为 Telegram 文本并发送。
- source text normalization 继续在构造 failed payload 前执行，保持当前清洗边界。

`stderr` 前缀继续为 `[stderr] `，由 `BUFFER_STREAM_TEXT` 执行 helper 添加，以免 streamer 决定展示文本。

### Dispatcher execution

`PresenterOutputDispatcher` 增加公开入口：

- `execute_display_event(event: DisplayEvent) -> None`

该入口负责 `DisplayEvent → RenderCommand → _execute_render_command()`，供 `RunEventStreamer` 使用。现有 presenter output 路径仍可使用 `render_command_from_presenter_output()`。

新增私有执行 helper：

- `_buffer_stream_text(payload)`
- `_complete_lifecycle(payload, lifecycle_message)`
- `_fail_lifecycle(payload, lifecycle_message)`

生命周期命令需要目标 message。为避免把 aiogram `Message` 放入 display payload，dispatcher 在构造时继续持有可选 lifecycle message，或者执行入口显式接收 lifecycle message。推荐执行入口显式接收：

```python
await dispatcher.execute_display_event(event, lifecycle_message=self._lifecycle_message)
```

`RenderCommand` 保持通道动作描述，不携带 Telegram `Message`。

执行行为保持：

- success：先尝试编辑 lifecycle message；失败则 `answer_safely()`。
- failed/timeout/canceled：同样先编辑，失败则补发。
- 如果 lifecycle message 为 `None`，`edit_message_safely(None, ...)` 的当前安全行为应保持；若 messenger 接口不接受 `None`，则直接补发。
- stdout/stderr 继续进入 chunk sender；不提前 flush。

### RunEventStreamer

`RunEventStreamer` 保留：

- 消费 `CLIEvent`。
- 判断 interactive/non-interactive。
- 获取 duration/truncated。
- spinner、status display、diff、export、queued upload、snapshot、pump 编排。

调整：

- stdout/stderr 构造 `DisplayEvent(STREAM_TEXT, ...)` 并交给 dispatcher。
- EXITED 构造 `TASK_SUCCEEDED` display event。
- FAILED/TIMEOUT/CANCELED 清洗 error text 后构造 `TASK_FAILED` display event。
- status display 已启用时，成功路径仍删除 lifecycle message 并 clear，不发送完成文本；该判断仍留在 streamer，因为它属于状态显示模式的编排决策。
- error 路径的 status display clear 时机保持不变。

## Alternatives considered

### 直接收编 external push

暂不采用。external push 使用 `MessageSender`、retry、external origin、PTY user question 和独立 callback 语义，当前直接复用 `/run` payload 会造成通道耦合。

### 只移动消息构造函数

改动最小，但不能解决 streamer 直接操作 messenger 的展示分叉，也不能推进事件通道标准化。

### 建立全局事件总线

当前迁移成本和顺序/重复投递风险过高，不符合小步兼容策略。

## Edge cases and risks

- structured reply command 已在 dispatcher 内进行 pre-flush；新增 lifecycle command 必须避免重复 flush。
- terminal event 前已有未发送 raw text 时，必须先 flush，确保输出先于完成/失败消息。
- status display 模式下 success 不应额外发送完成消息。
- lifecycle message 编辑失败时必须补发，不能静默丢失终态。
- `FAILED`、`TIMEOUT`、`CANCELED` 的图标和标签必须保持当前值。
- stderr 的 `[stderr] ` 前缀必须保持，且原始内容仍由 `send_text()` 做 canonical normalization。
- `exit_code` 当前未进入成功文本，但保留在 payload 中以忠实表达现有输入；本阶段不新增展示。

## Test / acceptance plan

- 扩展 `test_run_display_mapping.py`：覆盖三个新增 display event 到 render command 的映射和 flush 策略。
- 扩展 `test_run_presenter_dispatcher.py`：
  - stdout 不带前缀、stderr 带 `[stderr] `，且不预先 flush。
  - success 先 flush，再编辑 lifecycle message。
  - success 编辑失败时补发。
  - failed/timeout/canceled 文案、图标正确。
  - failed 编辑失败时补发。
- 扩展/保持 `test_run_event_streamer.py`：
  - non-interactive stdout/stderr 走 display event 入口。
  - success/error 走 lifecycle display event 入口。
  - status display success 路径仍不发送重复完成消息。
- 回归 `test_command_run.py`、`test_run_event_streamer_diff.py`、upload queue、tool manager 和 structured reply tests。
- 运行 `ruff check`、`ruff format --check`、`mypy app` 和全量 `pytest`。

验收标准：所有现有用户可见文本和发送顺序不变，全量测试通过。

## Open questions

None.
