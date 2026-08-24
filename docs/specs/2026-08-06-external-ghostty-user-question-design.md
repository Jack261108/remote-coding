# 非 tmux Ghostty AskUserQuestion 设计

## Summary

为已绑定、已配对的非 tmux Ghostty Claude 会话补齐 `AskUserQuestion` 端到端能力：Telegram 推送单选、多选和自由文本问题，用户在 Telegram 作答后，由系统在重新验证 binding、PID、TTY、前台进程和 Ghostty terminal UUID 的前提下操作 Claude TUI，并在最后一题成功提交后才向 Hook 返回 `allow`。

设计复用现有 `UserQuestionService` 的问答状态机和 `ask:` Telegram callback，不复制 managed tmux 的单选、多选、自由文本及多问题编排逻辑；新增 Ghostty question transport 和 external pending context。

## Goals

- 非 tmux external-bound Ghostty 会话可在 Telegram 收到实际问题和选项。
- 支持单选、多选勾选/取消/提交、Other/自由文本和多问题连续作答。
- Telegram 体验尽量复用现有 managed tmux `AskUserQuestion` 流程。
- 最后一题成功操作 Claude TUI 后，才向 Hook 返回一次 `allow`。
- 每次 TUI 操作前都重新验证 owner、binding generation、target UUID、TTY 和前台 Claude 进程。
- 解绑、重绑、重新配对、会话结束或 TTL 过期后，旧 callback 必须失效。
- 待回答问题期间，普通 Telegram 文本优先作为问题答案处理，不进入普通 Ghostty 输入队列。
- 问题完成后恢复已有普通输入 drain 行为。
- 保持 managed tmux 与现有 external tmux 行为兼容。

## Non-goals

- 不读取或解析 Ghostty 屏幕内容。
- 不自动猜测 Claude TUI 当前光标位置。
- 不按 cwd/title 自动选择 Ghostty terminal。
- 不协调 Telegram 与 Ghostty 本地键盘的同时操作；问题卡会明确提示作答期间不要在本地操作。
- 不持久化 pending question；bot 重启后 pending question 仍按现有内存状态失效。
- 不重构或替换现有 tmux question transport。
- 不为无配对 target 的外部会话自动建立 Ghostty 配对。

## Context

### 当前已支持

Commit `027051c` 后，非 tmux external-bound + `ghostty_target` 已支持：

- 显式绑定和 Ghostty terminal UUID 配对；
- bind-time controlling TTY 回填；
- PID/TTY/foreground/command identity 校验；
- 普通文本及未注册 slash command 注入；
- 忙碌时排队和恢复后 drain；
- assistant reply 推送；
- 输入模式进入/退出。

### 当前缺口

现有 external `AskUserQuestion` 流程只在 PID 能映射到 tmux pane 时生成交互按钮：

- `app/bootstrap_mixins.py` 的 bound external Hook 通知先查 tmux pane；
- `PendingExternalUserQuestion` 只保存 `pane_id`；
- `ext_uq:` callback 只调用 tmux `send-keys`；
- 没有 pane 时退化成通用权限 allow/deny 卡，不展示实际选项；
- 普通 Ghostty 输入在 pending permission/question 时被排队，不能作为答案。

### Ghostty 1.3.1 限制

Ghostty AppleScript terminal 只暴露：

- `id`
- `name`
- `working directory`

不暴露 terminal PID/TTY，也不能 capture terminal screen 或读取 Claude TUI 当前光标。Claude 子进程环境也没有 surface UUID。因此：

- terminal UUID 必须继续由用户显式配对；
- 回答选择题时无法像 tmux `capture-pane` 那样读取当前选项；
- Telegram 作答期间必须要求用户不要在 Ghostty 本地同时操作；
- 每次回答通过重复 Up 键将光标夹到第一项，再移动到目标项。

## Proposed design

### 1. 复用 managed `UserQuestionService`

继续使用现有：

- `_UserQuestionDraft`
- 单选回答
- 多选 toggle/submit
- Other/自由文本
- 多问题顺序推进
- `ask:` callback 格式
- `command_user_question.py` Telegram handlers
- 最后一题完成后 Hook permission allow

`UserQuestionService` 增加可选 external question context/transport。transport 由问题身份决定：

1. callback/token 或自由文本已精确匹配 external pending context 时，只允许使用该 external transport；
2. external transport 拒绝、失效或结果不确定时 fail-closed，不得回退 managed terminal 或 managed text transport；
3. 没有 external context 的现有 managed 问题继续使用原 tmux terminal transport 和 fallback。

不得把 external Ghostty 伪装成 managed `terminal_id`；external transport 必须显式接收 owner 和安全快照。

### 2. External pending context

pending target 使用 discriminated union，避免 transport 类型与必填字段不一致：

```python
@dataclass(frozen=True, slots=True)
class ExternalTmuxQuestionTarget:
    kind: Literal["tmux"]
    pane_id: str
    tmux_bin: str


@dataclass(frozen=True, slots=True)
class ExternalGhosttyQuestionTarget:
    kind: Literal["ghostty"]
    binding_id: str
    terminal_id: str
    paired_tty: str
    paired_at: datetime


ExternalUserQuestionTarget = ExternalTmuxQuestionTarget | ExternalGhosttyQuestionTarget


@dataclass(slots=True)
class PendingExternalUserQuestion:
    tool_use_id: str
    session_id: str
    user_id: int
    prompts: tuple[UserQuestionPrompt, ...]
    target: ExternalUserQuestionTarget
    phase: ExternalUserQuestionPhase
    created_at: datetime = field(default_factory=utc_now)
    failure_reason: str | None = None
```

不 snapshot PID；每次 action 从当前 binding 读取 live PID，允许 Hook 合法刷新 PID。Ghostty target 必须包含 `paired_at`，使同 binding、同 terminal/TTY 的重新配对也能让旧 callback 失效。

`ExternalUserQuestionState` 增加：

- 按 `tool_use_id` 获取 active/tombstone；
- 按 `user_id` 解析 unique/ambiguous/none；
- generation-safe phase transition；
- session/binding/target/tool invalidation；
- TTL 清理并返回清理记录，供 draft/token 同步失效。

phase 至少包含 `ACTIVE`、`TERMINAL_ACTION_APPLIED`、`COMPLETED`、`INDETERMINATE` 和 `INVALIDATED`。callback 时必须与当前 binding/target fingerprint 完全一致。

### 3. Hook notification routing

`_notify_bound_external_event()` 处理 `AskUserQuestion` 时：

1. 解析全部 prompts；
2. 验证 external binding owner；
3. 若当前流程能找到 tmux pane，保持现有 external tmux 路径；
4. 否则检查 external input service 已启用且 binding 有持久化 Ghostty target；
5. 保存 `transport_kind="ghostty"` pending context；
6. 推送第一题的完整 Telegram 卡片；
7. 不推送通用 AskUserQuestion allow/deny 卡；
8. 不提前向 Hook 返回 `allow`。

无有效 Ghostty target 时保持现有通用权限 fallback，不自动配对。

### 4. Telegram callback 复用

Ghostty 和 managed 问题都使用短 opaque callback token：

- managed/Ghostty：`ask:{token}`；
- legacy external tmux：`ext_uq:{token}`。

完整 `tool_use_id`、owner、session、question index、action、option index 和 origin 只保存在 TTL registry，不进入 Telegram callback。token 可重复 resolve，支持 multi toggle；completion、TTL 和 lifecycle invalidation 后失效。所有 callback_data 必须在发送前验证 UTF-8 长度不超过 64 bytes，禁止截断 identity 字段。

将 callback registry 和共享 builder 供 `command_user_question.py` 与 `ExternalSessionPushNotifier` 共同使用，避免格式漂移。

问题卡包括：

- session short id；
- header/question；
- options 和 description；
- 单选或多选按钮；
- “可直接回复文字作为 Other/自由文本”；
- “作答期间请勿在 Ghostty 本地操作”。

下一题继续复用 `_acknowledge_and_send_next_prompt()`。

### 5. 自由文本路由

新增独立的 early external-question text router，不依赖 active input mode，注册在 ordinary Ghostty text router 之前：

```text
UNIQUE pending：按精确 tool context 作为自由文本答案处理并消费消息
AMBIGUOUS pending：明确报错并消费消息，绝不猜测
NONE：fall through 到 active-target ordinary Ghostty send 或 managed chat
```

规则：

- 一个 pending external question：正常回答；
- 多个 pending external question：fail-closed，提示存在多个问题；
- filter 与 handler 间 pending 消失时提示 stale，不把同一文本误投普通终端；
- 没有 pending question：保持普通输入行为；
- 已注册 Telegram slash command 仍由更早的 command router 处理；
- 未注册 slash text 在 pending question 时可作为自由文本答案；
- 仅用 `.strip()` 判断空白，传给 Ghostty 的答案保留原始文本。

### 6. Ghostty question transport

在 domain protocols 增加 external question transport protocol，方法携带显式安全 context，而不是复用 managed `terminal_key`：

- `select_option(...)`
- `answer_with_text(...)`
- `advance_after_multi_select(...)`
- `question_completed(...)`（清理 pending state并唤醒普通输入 drain）

实现由 `ExternalSessionInputService` 或其薄包装器提供，以复用：

- binding store；
- structured session store；
- Ghostty adapter；
- LocalProcessProbe；
- per-session input locks；
- target validation；
- queue/drain 调度。

`TaskService`/`UserQuestionService` 在 external services 完成构造后通过明确的一次性配置方法接入 transport/state，避免重排现有 bootstrap 依赖环。

### 7. Fixed AppleScript TUI operations

新增固定 AppleScript question 脚本。所有动态值只通过 argv 传入，不插值到脚本源码。

支持受限 action 集合，禁止任意 key/script 输入。

#### 单选

```text
Up × (option_count + 1)
Down × option_index
Enter
if final_question:
    delay
    Enter
```

#### 多选 toggle

```text
Up × (option_count + 1)
Down × option_index
Enter
```

每次 toggle 都从第一项重新定位；已勾选状态由 Claude TUI 保存，Telegram draft 同步保存选中 index。

#### 多选提交/进入下一题

```text
Up × (option_count + 1)
Right
if final_question:
    delay
    Enter
```

#### Other/自由文本

```text
Up × (option_count + 1)
Down × option_count
Enter
wait for text input
input text <answer>
wait for paste
Enter
if final_question:
    delay
    Enter
```

固定延迟沿用已验证的 Ghostty paste/Enter 时序：

- key step delay：约 0.05s；
- Enter/next transition：约 0.15s；
- paste before Enter：约 0.1s。

脚本返回成功只表示 AppleEvent 完成；timeout/partial failure 视为 indeterminate，不自动重试。

### 8. 每次 TUI 操作的安全校验

在 per-session external input lock 内：

1. external input enabled；
2. pending context 存在且 `tool_use_id` 匹配；
3. pending `user_id` 等于 Telegram user；
4. structured state 仍显示同一 active AskUserQuestion；
5. binding 存在且 owner 正确；
6. session 未结束；
7. binding generation 等于 pending snapshot；
8. current target binding generation 匹配；
9. terminal UUID 等于 pending snapshot；
10. paired tty 等于 pending snapshot；
11. PID 存活且 controlling tty 匹配；
12. Claude 仍是该 tty 前台进程；
13. Ghostty terminal UUID 仍唯一存在；
14. AppleScript await 后重新读取 binding/target；
15. 再次校验 foreground；
16. 执行固定 question action。

任何失败均不注入、不自动 fallback 到普通文本。

不要求当前 active input mode；持久化 pairing 有效即可回答问题。

### 9. Hook allow 和问题完成

- 中间题或多选 toggle 成功后不响应 Hook permission；
- 最后一题 TUI 操作成功时，先在 input lock 内原子执行 `ACTIVE -> TERMINAL_ACTION_APPLIED`；
- 释放 input lock 后，`UserQuestionService` 才调用 Hook `allow`；
- Hook 成功后重新按既定锁顺序完成 `COMPLETED`、structured state、pending/token/draft 清理和 drain wakeup；
- Hook false、异常、timeout，或 final action 后协程取消时，收敛到 `INDETERMINATE` tombstone，后续 callback 不得重做 TUI，也不自动重发 Hook allow；
- adapter timeout/post-start unknown 同样进入 `INDETERMINATE`，不调用 Hook allow；
- duplicate callback 由 per-user question lock、phase、completed tool-use set 和 token invalidation 共同拒绝。

### 10. Locking

全局允许的嵌套顺序：

```text
user-question lock -> external input lock
```

规则：

- `UserQuestionService` 在 per-user lock 内编排 draft 和 exactly-once；
- question transport 在 input lock 内验证 target、发送 TUI action并更新 external phase；
- 不允许 input lock 反向获取 user-question、reply-delivery 或 session-event lock；
- 不在 input lock 内调用 `sync_claude_session()`、Telegram send 或 Hook permission response；
- TUI action 完成并释放 input lock 后，再调用 Hook `allow`；
- completion 时可按 `user-question -> input` 重新获取 input lock；
- target mutation（尤其 re-pair）必须使用同一 session input lock，使 action 与 target replacement 串行。

## Alternatives considered

### A. Reuse `UserQuestionService` with Ghostty transport（selected）

优点：复用完整状态机、Telegram callbacks 和下一题流程；tmux/Ghostty 体验一致；避免重复。

代价：需要 optional external transport、pending context 和 bootstrap 后装配。

### B. Expand `ext_uq:` into a second full state machine

优点：managed 流程不动。

缺点：复制单选、多选、自由文本、多题、draft、completion 和 Hook allow 逻辑；维护成本高且容易漂移。因此拒绝。

### C. Route all answers through Other text

优点：减少真实 option 选择操作。

缺点：破坏单选/多选语义，体验和 tmux 不一致，仍需操作 Other 和多题推进。因此拒绝。

## Edge cases and risks

### Local keyboard race

Ghostty 不能 capture 当前 TUI。问题卡明确提示 Telegram 作答期间不要在本地操作；每次动作先 Up 重置。若本地仍同时操作，无法完全消除竞态。系统通过 active tool/binding/TTY/foreground/UUID 校验缩小风险，但不声称可协调双输入源。

### Multiple pending external questions

按钮含 `tool_use_id`，可精确处理。自由文本无 callback context；存在多个 pending 时拒绝并提示，绝不猜测。

### Stale binding/target

unbind、rebind、re-pair、SessionEnd 和 reaper 必须 invalid pending context。旧按钮返回“问题已过期或目标已变化”。

### Partial AppleScript action

多键序列可能部分成功后 timeout。返回 indeterminate，提示用户查看 Ghostty，不自动重试，避免重复 toggle/submit。

### Permission response failure

如果 TUI action 成功但 Hook allow 已失效、失败或超时，external question 转为 `INDETERMINATE` tombstone；不再次执行 TUI action，也不自动重发 allow。pending/draft/token 的 active 入口立即失效，tombstone 由 Hook lifecycle/TTL 有界清理。

### Bot restart

pending question 不持久化。重启后旧 callback 失效；Claude 终端中的问题仍需用户本地回答，或等待新的 Hook/状态同步重新发出。此行为与现有内存 permission/question 状态一致。

## Test / acceptance plan

### Domain/state

- external pending context JSON-independent in-memory lifecycle；
- owner/tool/session/binding/target snapshot 匹配；
- TTL、session invalidation、rebind/re-pair invalidation；
- 同一用户多个 pending 的自由文本 ambiguity。

### Ghostty adapter

- fixed script 不包含用户 text、tool id 或 terminal UUID；
- 单选 key sequence；
- multi toggle key sequence；
- multi submit/next sequence；
- Other text sequence和 paste delay；
- invalid index/action fail-closed；
- terminal missing/non-unique/TCC/timeout/partial failure。

### Input service/transport

- 每个安全校验失败均不调用 adapter；
- validation await 后 ABA re-check；
- 不要求 active mode，但要求 persisted target；
- binding/target generation 变化拒绝；
- foreground shell takeover 拒绝；
- final action 后 drain wakeup。

### UserQuestionService

- external single choice；
- multi toggle/cancel/submit；
- Other/free text；
- multi-question progression；
- final Hook allow exactly once；
- stale/duplicate callback；
- managed tmux regression。

### Telegram/router

- external Hook 推送 `ask:` keyboard；
- multi-select keyboard refresh；
- next prompt；
- external text优先回答 pending question；
- 无问题时继续普通 Ghostty send；
- 多 pending 自由文本拒绝；
- 无 target 时保持 generic permission fallback。

### Integration/property

- discovery → bind → pair → AskUserQuestion → Telegram answer → Ghostty TUI → Hook allow → Claude resume；
- unbind/rebind/re-pair/SessionEnd 后 callback 不注入；
- input lock 与 session-event/reply-delivery 锁顺序无反转；
- `./scripts/quality_check.sh` 全量通过，覆盖率不低于 80%。

## Rollback and compatibility

- Ghostty external question transport 为 optional；关闭 `GHOSTTY_INPUT_ENABLED` 时完全不启用。
- managed tmux question transport 保持原接口和行为。
- 现有 external tmux `ext_uq:` 可保留作为兼容路径，本次不强制迁移。
- 新 pending 字段均为内存态，不涉及持久化迁移。
- 回滚时移除 optional transport wiring 即恢复当前 generic permission fallback。

## Open questions

None。用户已确认：

- 完整支持单选、多选、Other/自由文本和多问题连续回答；
- Telegram 作答期间禁止在 Ghostty 本地同时操作；
- 采用方案 A，复用现有 `UserQuestionService`。
