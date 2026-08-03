# 已绑定外部 Ghostty 会话输入注入设计

## Summary

为直接运行在 Ghostty、未经过 tmux 的已绑定外部 Claude 会话增加输入能力。用户在绑定时显式选择对应的 Ghostty terminal，之后可从 `/list` 选择该会话并把普通 Telegram 文本或 Claude slash command 注入指定 terminal；现有 JSONL 同步、权限审批和外部回复推送链路保持不变。

## Goals

- 支持 Ghostty 1.3.1 中直接运行的外部 Claude 会话，不要求 tmux。
- 绑定时由用户显式选择对应 Ghostty terminal，使用稳定的 `terminal.id` 寻址。
- 用户从 `/list` 选择已绑定会话后进入外部输入模式，后续普通文本直接注入该 Claude TUI。
- 未被机器人占用的 slash command（例如 `/compact`）按 Claude 输入转发。
- 每次发送前重新验证 binding、Claude 进程、TTY、前台进程组和 Ghostty terminal，目标不确定时拒绝发送。
- 复用现有 Hook、JSONL、structured state 和 external reply pump 返回 Claude 回复。
- 同一会话的输入串行，不同会话可并发。

## Non-goals

- 不修改已有 tmux managed session 或外部 tmux AskUserQuestion 注入链路。
- 不支持 Ghostty 之外的 Terminal.app、iTerm2 等终端模拟器。
- 不通过 AppleScript 读取终端屏幕、scrollback 或命令输出。
- 不按 cwd、title、窗口顺序或当前前台 terminal 自动猜测目标。
- 不处理 Telegram 文件、图片、语音或其他非文本消息。
- 不在第一版依赖 Ghostty 1.4 的 `terminal.pid` / `terminal.tty` 属性。
- 不把输入能力实现为通用 shell 命令执行接口；目标始终是已验证的 Claude TUI。

## Context

当前外部会话由 Claude Hook 发现并显式绑定。`ExternalBinding` 保存 owner、cwd、PID、JSONL 路径、binding generation 和回复游标，但没有 Ghostty terminal 标识。绑定后的权限请求和 assistant reply 已能推送到 Telegram，普通文本却没有回到外部会话的输入路径。

本机 Ghostty 版本为 1.3.1。该版本的 AppleScript scripting dictionary 提供：

- `terminal.id`：surface 生命周期内稳定的 UUID；
- `terminal.name`；
- `terminal.working directory`；
- `input text <text> to <terminal>`；
- `send key <key> ... to <terminal>`。

Ghostty 1.3.1 不公开 terminal PID 或 TTY，因此无法从 Claude Hook PID/TTY 自动反查 terminal UUID。Claude 子进程环境也没有 surface ID。第一版必须把“用户显式选择 terminal”作为 PID/TTY 与 terminal UUID 之间的信任锚点。

Ghostty terminal UUID 在窗口、标签页和分屏顺序变化后仍保持稳定，但 surface 关闭或 Ghostty 重启后会失效。

## Proposed design

### 1. 外部 binding 目标信息

新增 Ghostty 输入目标模型，并作为 `ExternalBinding` 的可选字段持久化：

```python
@dataclass
class GhosttyInputTarget:
    terminal_id: str
    paired_tty: str
    paired_at: datetime
    name_snapshot: str | None = None
    cwd_snapshot: str | None = None
```

同时让 discovery/binding 保留 Hook 已有的 `tty` 字段。Hook 未提供 TTY 时，可在配对或发送前根据当前 binding PID 查询 controlling TTY。

字段语义：

- `terminal_id` 是唯一寻址依据；
- `paired_tty` 用于验证当前 Claude 仍运行在配对时的 PTY；
- `name_snapshot` 和 `cwd_snapshot` 只用于 Telegram 展示和人工识别，不参与自动匹配；
- PID 继续由现有 binding 活跃事件更新，不复制进 target；
- binding generation 继续使用现有 `binding_id`，防止解绑重绑后的 ABA 竞态。

持久化格式使用可选嵌套对象，加载旧 `external_bindings.json` 时缺失字段应正常回退为“尚未配对”。

### 2. Ghostty adapter

新增独立的 `GhosttyTerminalAdapter`，不把 Ghostty 分支塞入 `TmuxRunner`。

职责：

1. 检查当前平台及 Ghostty AppleScript 能力是否可用；
2. 枚举 Ghostty windows、tabs 和 terminals；
3. 返回 terminal UUID、标题、工作目录、window/tab 位置以及 selected/focused 展示状态；
4. 按完整 UUID 验证 terminal 是否恰好存在一个；
5. 向指定 terminal 输入文本并发送 Enter。

AppleScript/JXA 脚本必须是固定源码。terminal UUID 和用户文本通过 `osascript` 参数传入，不得插入脚本源码：

```text
osascript <固定脚本> -- <terminal-id> <payload>
```

所有子进程调用使用参数数组，不使用 `shell=True`。注入时不调用 `activate`、`focus` 或 `front window`，不改变用户当前焦点。

发送动作在同一个 `osascript` 调用内完成：

1. 按完整 UUID 找到 terminal；
2. 要求匹配数量恰好为 1；
3. `input text payload to targetTerminal`；
4. `send key "enter" to targetTerminal`。

如果调用在文本可能已经粘贴后失败，结果标记为“不确定”，不得自动重试，以免重复输入。

### 3. 进程与 TTY 校验

新增薄的本机进程检查能力，用于根据 PID 获取：

- controlling TTY；
- process group；
- terminal foreground process group；
- 可识别的进程命令信息。

发送前要求：

1. binding PID 为正且仍存活；
2. PID 当前 controlling TTY 与 `paired_tty` 一致；
3. Claude 仍属于该 TTY 的前台进程组；
4. 当前进程身份仍符合 Claude 会话，而不是已返回的 shell 或其他前台程序。

PID 本身不作为持久 terminal 身份，因为 PID 可能变化或复用。若同一 Claude session 在相同 Ghostty surface 中恢复并产生新 PID，只要新 Hook 已更新 binding PID，且 TTY 和前台进程校验仍成立，可以继续使用原配对；若 TTY 变化则要求重新配对。

### 4. 配对流程

现有外部 binding 仍负责 ownership、JSONL 基线和回复推送。Ghostty 配对作为绑定后的独立步骤，不阻断只读状态、权限审批和回复推送功能。

新绑定成功后：

1. 查询 Claude PID 的 controlling TTY；
2. 只读枚举 Ghostty terminals；
3. 按 cwd、title、focused 状态排序候选，但不自动选择；
4. Telegram 展示每个候选的标题、工作目录、window/tab 位置和 terminal ID 后缀；
5. 用户必须显式选择一个 terminal；
6. 保存完整 terminal UUID、当前 Claude TTY 和展示快照；
7. 立即执行一次完整目标校验，成功后确认配对。

callback data 不直接拼接完整 session ID 和 terminal UUID。使用短期 token/registry，将 token 解析为 `{session_id, binding_id, terminal_id, user_id}`，并在消费时再次校验 owner 和 binding generation。

已有 binding 没有 target 时，在用户首次尝试进入输入模式时触发同一配对流程。

候选为零、Ghostty 未运行、AppleScript 被关闭或 Automation 权限被拒绝时，binding 保持有效，但输入功能不可用。

### 5. 外部输入模式

新增进程内 `ExternalInputTargetStore`，按 Telegram user ID 保存：

```python
@dataclass
class ActiveExternalInputTarget:
    user_id: int
    session_id: str
    binding_id: str
    selected_at: datetime
```

该状态不持久化。应用重启后用户需要重新从 `/list` 选择会话，但只要已持久化的 Ghostty target 仍有效，无需重新配对。

`/list` 中已绑定外部会话的“继续”动作改为：

1. 校验 binding owner；
2. 若未配对或目标失效，进入配对流程；
3. 若目标有效，将其设为当前外部输入目标；
4. 回复当前会话标题、cwd 和退出方式。

进入外部输入模式后：

- 普通 Telegram 文本转发给选中的外部会话；
- 已注册的机器人命令仍优先由机器人处理；
- 未注册 slash command 作为 Claude 文本输入，例如 `/compact`；
- `/external leave` 或“退出输入模式”按钮清除当前目标；
- 选择另一个外部会话替换当前目标；
- 选择 managed session 时清除外部目标，避免路由歧义；
- unbind、SessionEnd、reaper cleanup 或 target 失效时清除相关目标。

普通文本路由应先检查显式外部输入目标；没有目标时继续走现有 managed-session `SessionGuard`，不得改变未进入外部输入模式用户的行为。

### 6. 输入服务与并发

新增 `ExternalSessionInputService`，集中处理 pairing、activation 和 send，handler 不直接调用 `osascript` 或读取 binding store 内部状态。

为每个 `session_id` 增加独立的 external input lock：

1. 获取 input lock；
2. 重新读取 active target 和 binding；
3. 校验 owner、`binding_id` 和 ended 状态；
4. 检查 structured session phase 和 pending interaction；
5. 校验 PID、TTY、前台进程组和 Ghostty terminal；
6. 调用 adapter 注入；
7. 成功后在释放锁前把会话状态推进为 processing，防止连续消息在 Hook 状态更新前重复提交。

不同 session 使用不同锁，可以并发。input lock 不复用 external reply-delivery lock，避免输入等待 Telegram 投递，并保持现有 reply-delivery → session-event 锁顺序不受影响。

状态变更继续通过 `SessionStore.process()` 或 `SessionStore.save()` 完成。调用 JSONL sync 时不得持有 session-event lock。

### 7. 可发送状态

第一版仅在以下条件同时满足时允许注入：

- phase 为 `idle` 或 `waiting_for_input`；
- `pending_permission` 为空；
- 没有 pending external AskUserQuestion；
- binding 未 ended；
- terminal 和 Claude 进程验证成功。

phase 为 `processing`、`compacting` 或 `waiting_for_approval` 时，新消息按 §9 队列暂存，而不是直接拒绝。AskUserQuestion 必须继续使用现有专用按钮/回答流程，队列和通用文本都不绕过它。

### 8. 文本语义

第一版只接受 Telegram 文本消息：

- 保留 Unicode 和多行内容；
- 将 CRLF/CR 规范化为 LF；
- 不把多行内容折叠成 shell 命令；
- 不执行 shell escaping，因为文本不会进入 shell；
- 受 Telegram 单条消息长度限制，不另设更大的输入协议。

`input text` 使用 Ghostty paste-style 语义，随后单独发送 Enter 提交给 Claude TUI。

### 9. 输入队列

外部输入模式下,Claude 处于 `processing`、`compacting` 或 `waiting_for_approval` 等不可发送状态时,新到达的 Telegram 文本暂存为队列,而非直接拒绝。

队列存储:

- 每 active target 维护 per-session 有序队列,只驻留内存,与输入模式一同不持久化。
- 队列元素包含文本、入队时间、入队时的 `binding_id`。

入队条件:

- 已进入外部输入模式、消息到达时,先复用 §6 的 owner/binding_id/进程/terminal 校验。
- 校验通过且当前可发送(§7) → 直接注入,不入队。
- 校验通过但当前不可发送 → 入队,回复"已排队,当前会话忙"。
- 校验失败(进程退出、terminal 失效、binding 代际变化等) → 不入队,直接拒绝并提示原因。

就绪触发与 drain:

- 复用现有 SessionEventProcessor 的 phase 推进,phase 回到可发送状态时触发该 session 的 drain。
- drain 在 §6 的 per-session input lock 内逐条取队首注入,每条成功后推进 processing,再发下一条。
- 进入 `waiting_for_approval` 或 AskUserQuestion 时暂停 drain,等对应流程结束。
- 队列空时停止 drain。

清理与上限:

- 单条消息在队列中等待超时(默认与审批/回复 TTL 对齐,具体值实现时统一)后丢弃并通知。
- 队列上限默认 5 条,超限拒绝入队并提示,避免堆积。
- unbind、SessionEnd、reaper cleanup、target 失效、退出输入模式 → 清空该 session 队列,并通知未发送条数。
- 解绑重绑 ABA: 入队记录的 `binding_id` 与当前不一致 → 丢弃该条,不注入旧世代。

顺序与并发:

- 队列先进先出。
- drain 与即时发送复用同一 per-session input lock,保证同一 session 不会并发注入。
- 不同 session 的队列互不影响。
- 队列只负责普通文本输入,不自动批准任何权限,也不替代 AskUserQuestion 专用回答流程。

## Error handling

用户可见错误应区分：

- 尚未配对 Ghostty terminal；
- Ghostty 未运行或不支持 AppleScript；
- macOS Automation 权限被拒绝；
- `macos-applescript = false`；
- 配对 terminal 已关闭或 Ghostty 已重启；
- Claude PID 已退出；
- Claude 已移动到其他 TTY；
- Claude 不再是该 TTY 的前台程序；
- 会话正在处理、压缩、等待审批或 AskUserQuestion；
- AppleScript 发送失败；
- 文本可能已粘贴但提交结果不确定。

目标身份校验失败时 fail closed：不发送、不切换到前台 terminal、不按 cwd/title 回退。terminal 不存在、TTY 变化或 binding generation 变化时清除输入模式，并要求重新选择或配对。

AppleScript subprocess 应设置有限超时，并在取消或超时时清理子进程。能力不可用不应阻止应用启动，也不影响外部 binding 的只读功能。

## Security considerations

- 只有 binding owner 可以配对、激活或发送。
- 配对 callback 必须绑定 user ID 和 binding generation，防止 token 被其他允许用户复用。
- terminal UUID 使用完整值验证，短后缀只用于展示。
- 用户文本只作为 subprocess argv 数据，不进入 shell，也不拼接 AppleScript 源码。
- 不使用 Accessibility 全局键盘事件，不依赖当前焦点。
- 不允许在无法确认 Claude 仍为目标前台程序时发送，避免文本落入 shell 后被 Enter 执行。
- 同一会话输入串行，并在发送成功后立即推进 structured phase，降低双击和快速连发风险。
- reaper、unbind、SessionEnd 和 generation barrier 必须清理或阻止旧输入任务。

## Alternatives considered

### 要求 Ghostty 1.4+

Ghostty 1.4 增加 `terminal.pid` 和 `terminal.tty` 后，可以按 Hook TTY 自动匹配，并在发送前直接重验 terminal TTY。该方案自动化和可验证性更好，但本机 Ghostty 1.3.1 当前无法使用，因此不作为第一版前提。

### 使用 terminal title nonce 自动配对

可以向 Claude TTY 写入临时唯一 title，再从 AppleScript terminal 列表中查找对应 UUID。该方案会修改用户界面，依赖 title 更新和恢复时序，并可能被 shell integration 或应用覆盖，脆弱且难以测试，因此不采用。

### 按 cwd、title 或 focused terminal 自动选择

这些字段可重复、可变化，也可能被程序主动设置。focused terminal 还存在用户切换窗口的竞态。误选后文本可能进入 shell 并被 Enter 执行，因此只能作为候选展示信息，不可作为寻址或 fallback。

### 复用 `SessionContext`

`SessionContext.terminal_id` 当前参与 tmux-owned ownership 判定。把 Ghostty target 塞入该字段会破坏 tmux-owned → external-bound → external-unbound 的显式优先级，因此使用独立的 binding target 和 active input store。

## Test / acceptance plan

### Domain and persistence

- `ExternalBinding` 的 tty/target JSON round-trip。
- 旧持久化数据缺少新字段时正常加载。
- target 更新后保持原 binding generation 和回复游标语义。

### Ghostty adapter

- 正确解析多 window/tab/terminal 列表。
- terminal title/cwd 包含 Unicode、换行和特殊字符时仍能安全传输。
- 注入始终按完整 UUID 定位。
- 用户文本只通过 argv 传入，不进入脚本源码或 shell。
- UUID 不存在、匹配异常、Ghostty 未运行、TCC 拒绝、AppleScript 禁用、timeout 和 subprocess failure。
- 发送 Enter 失败后的“不确定”结果不自动重试。

### Process validation

- PID 不存在或非正数时拒绝。
- TTY 与配对值不一致时拒绝。
- Claude 不属于前台进程组时拒绝。
- 新 Hook PID 仍在相同 TTY 时可继续；TTY 变化时要求重新配对。

### Input service

- 非 owner 拒绝 pairing、activation 和 send。
- stale `binding_id`、ended binding 和 unbind/rebind ABA 被拒绝。
- processing、compacting、waiting approval 和 AskUserQuestion 状态拒绝。
- 同一 session 输入严格串行,不同 session 可并发。
- 发送成功后在释放 input lock 前推进 processing。
- terminal 或进程失效时清除 active target。
- 队列入队、就绪 drain、超时丢弃、上限拒绝和解绑清理。

### Telegram handlers

- 新 binding 后展示 Ghostty 候选。
- 已有未配对 binding 首次“继续”时补配对。
- `/list` 选择后普通文本进入外部 input service。
- 已注册机器人命令保持原行为。
- 未注册 slash command 转发给 Claude。
- `/external leave`、退出按钮、选择其他会话、unbind 和 SessionEnd 清除输入模式。
- 没有外部输入目标时普通文本继续走原 managed-session handler。

### Integration and quality gate

- discovery → bind → explicit pair → select → send → Hook/JSONL Stop → external reply push 完整流程。
- 会话忙碌时入队、就绪 drain、超时丢弃、上限拒绝和解绑清理队列。
- Ghostty adapter 使用 fake subprocess/fixture，自动测试不依赖真实 GUI 和 TCC。
- 在本机 Ghostty 1.3.1 进行一次授权后的手工 smoke test，确认后台 terminal 可接收输入且不会抢焦点。
- 运行相关 unit/property/integration 测试，最后执行 `./scripts/quality_check.sh`。

## Acceptance criteria

- 直接运行在 Ghostty、没有 tmux 的外部 Claude 会话可被绑定并显式配对。
- 从 `/list` 选择后，普通文本和 Claude slash command 准确进入指定 Claude TUI。
- 输入后的 assistant reply 继续通过现有 external reply pump 返回 Telegram。
- 多个 terminal 具有相同 cwd/title 时不会自动选错。
- PID、TTY、前台进程、terminal UUID 或 binding generation 变化时不发生注入。
- 输入不会因 AppleScript 字符串拼接或 shell 解释造成命令注入。
- 并发消息、解绑重绑和生命周期清理不产生跨会话误投。
- 现有 tmux、权限、AskUserQuestion、binding 和回复推送行为保持兼容。

## Open questions

None.
