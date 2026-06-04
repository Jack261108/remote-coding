# `/list` 活跃会话列表显示优化设计

## 背景

当前 Telegram bot 的 `/list` 会把 tmux 会话、未绑定外部会话、已绑定外部会话平铺展示。会话数量增多后，用户需要在多个相似路径和短 ID 中手动判断哪个最值得继续，按钮也容易变多，降低了“快速回到最近会话”的效率。

本设计将 `/list` 的主要目标确定为：**快速继续当前用户最近活跃的已绑定会话**。完整排查能力保留，但不作为默认视图的重点。

## 目标

1. 默认突出最近 3 个可继续的已绑定会话。
2. 普通旧会话不展开，避免消息过长。
3. 等待审批、等待输入、正在处理、可绑定新会话等需要操作的项目仍然可见。
4. 保留查看完整列表的入口，便于排查异常或旧会话。
5. 显示规则集中在可测试的渲染层中，避免 `command_list.py` 继续膨胀。

## 非目标

1. 不改变会话绑定、解绑、关闭的底层行为。
2. 不引入新的持久化字段；优先复用现有活跃时间字段。
3. 不在默认 `/list` 中展示所有历史/旧会话详情。
4. 不自动清理旧会话；清理仍沿用现有 reaper/liveness 机制。

## 推荐显示结构

默认 `/list` 输出改为三段式：

```text
📋 会话

🚀 最近可继续
1. 🔗 权限状态的双向同步有点问题...
   project/remote-coding · 2 分钟前 · 已绑定
2. 🔗 权限的双向同步依旧有问题...
   project/remote-coding · 8 分钟前 · 已绑定
3. 🔗 给我出一道小学的选择题...
   project/remote-coding · 20 分钟前 · 已绑定

⚠️ 需要处理
🔐 等待审批 · jack/project · user_874 · 刚刚
📡 可绑定新会话 · project/remote-coding · 97532ccf · 5 分钟前

📦 其他
还有 4 个旧会话未显示
```

对应按钮建议：

```text
[1 继续] [2 继续] [3 继续]
[处理审批 user_874]
[绑定 97532ccf]
[查看全部]
```

## 排序与分区规则

### 最近可继续

“最近可继续”区域优先展示当前用户的已绑定外部会话，最多 3 个。

排序依据：

```text
ExternalBinding.last_activity_at 倒序
```

如果后续需要把当前用户拥有的 tmux 会话也纳入顶部候选，可使用以下优先级：

```text
已绑定外部会话 > 当前用户拥有的 tmux 会话 > 未绑定/可绑定会话
```

第一版默认保持更保守的策略：顶部优先展示已绑定外部会话，未绑定会话不进入“最近可继续”。

### 需要处理

“需要处理”区域展示默认视图中不能静默隐藏的项目，包括：

1. 等待审批。
2. 等待输入。
3. 正在处理但尚未绑定或需要用户识别的会话。
4. 新发现的未绑定外部会话。
5. 其他现有逻辑判断为异常或需要关注的会话。

排序优先级：

```text
等待审批 > 等待输入 > 正在处理 > 新发现未绑定 > 其他异常
```

同一优先级内按活跃时间倒序。

### 其他

未进入“最近可继续”和“需要处理”的普通旧会话只显示数量摘要：

```text
📦 其他
还有 N 个旧会话未显示
```

如果 `N = 0`，不显示该区域。

## 活跃时间来源

统一 view model 中使用 `activity_at` 表示排序和相对时间展示。

| 会话类型 | 时间来源 |
| --- | --- |
| 已绑定外部会话 | `ExternalBinding.last_activity_at` |
| 未绑定外部会话 | `UnboundExternalSession.last_seen` |
| tmux/内部会话 | `SessionState.last_activity` |

显示时使用相对时间，减少视觉噪音：

```text
刚刚
2 分钟前
1 小时前
昨天
```

## 按钮交互

### 最近可继续按钮

顶部 3 个会话用编号按钮：

```text
[1 继续] [2 继续] [3 继续]
```

按钮 callback 沿用现有选择流程：

```text
sess:select:<sid-prefix>
```

### 需要处理按钮

每个需要处理项最多给一个主动作按钮：

| 状态 | 按钮文案 | 建议 callback |
| --- | --- | --- |
| 等待审批 | `处理审批 <sid>` | `sess:select:<sid-prefix>` |
| 等待输入 | `继续输入 <sid>` | `sess:select:<sid-prefix>` |
| 正在处理 | `查看 <sid>` | `sess:select:<sid-prefix>` |
| 可绑定新会话 | `绑定 <sid>` | `sess:bind:<sid-prefix>` |

如果后续已有能力直接跳转审批消息，可以把等待审批的 callback 替换为更直接的审批入口；第一版不要求新增跳转机制。

### 查看全部

默认 `/list` 不再给每条会话展示关闭按钮。关闭、取消绑定等风险操作应放到会话详情页中，避免误触。

默认视图保留完整列表入口：

```text
[查看全部]
```

建议 callback：

```text
sess:list:all
```

第一版可以复用旧版完整列表渲染逻辑作为兜底视图；如果实现 callback 成本较高，也可以先提供 `/list_all` 命令，但首选内联按钮以减少用户记忆成本。

## 实现结构

建议新增专门的渲染模块：

```text
app/bot/session_list_renderer.py
```

`command_list.py` 保持为编排层：

1. 收集 tmux sessions。
2. 收集未绑定外部 sessions。
3. 收集已绑定外部 sessions。
4. 转换为统一 view model。
5. 调用 renderer 生成 Telegram HTML 文案和 inline keyboard。

建议 view model：

```python
@dataclass
class ListSessionView:
    session_id: str
    title: str | None
    cwd: str
    source: Literal["bound", "tmux", "unbound"]
    state: str
    activity_at: datetime
    priority: int
    action: str
```

建议渲染入口：

```python
def build_session_list_message(items: Sequence[ListSessionView], *, now: datetime) -> SessionListRenderResult:
    ...
```

返回对象包含：

```python
@dataclass
class SessionListRenderResult:
    text: str
    keyboard: InlineKeyboardMarkup | None
```

渲染层负责：

1. 选出最近 3 个可继续会话。
2. 选出需要处理会话。
3. 统计隐藏会话数量。
4. 生成 HTML 安全文案。
5. 生成内联按钮。

## 错误处理与安全

1. 标题、路径、状态文案必须 HTML 转义，避免 Telegram HTML parse error。
2. callback 仍使用短 ID 前缀时，继续沿用现有 `_resolve_session_id` 的歧义处理能力。
3. 如果 `activity_at` 缺失，应在转换 view model 时回退到创建/绑定时间；不在 renderer 内读取底层对象。
4. 如果没有任何会话，保留当前行为：

```text
当前无活跃会话。
```

5. 风险操作如关闭会话不出现在默认 `/list` 首页，只在详情页展示。

## 测试策略

新增或扩展单元测试，优先覆盖纯渲染函数。

### 必测场景

1. **最近 3 个已绑定优先**
   - 输入 5 个已绑定会话，确认只展示最新 3 个。
   - 剩余 2 个计入“其他”。

2. **未绑定不进入最近可继续**
   - 未绑定会话即使比已绑定更新，也只进入“需要处理”。

3. **需要处理排序**
   - 等待审批排在等待输入之前。
   - 等待输入排在正在处理之前。
   - 同类按 `activity_at` 倒序。

4. **隐藏普通旧会话**
   - 普通旧会话不展开。
   - 只显示数量摘要。

5. **无会话兼容**
   - 无输入时仍返回“当前无活跃会话。”或由 `command_list.py` 保持原逻辑。

6. **HTML 转义**
   - 标题和路径包含 `<`, `>`, `&` 时能正确转义。

7. **按钮 callback**
   - 最近会话按钮使用 `sess:select:<sid-prefix>`。
   - 可绑定新会话按钮使用 `sess:bind:<sid-prefix>`。
   - 查看全部按钮使用 `sess:list:all` 或对应 `/list_all` 兜底入口。

## 迁移步骤建议

1. 提取旧版完整列表渲染逻辑，作为 “查看全部” 的兜底实现。
2. 新增统一 view model 和摘要 renderer。
3. 将 `/list` 默认输出切换到摘要 renderer。
4. 增加 `sess:list:all` callback 或 `/list_all` 命令。
5. 补齐单元测试和必要的集成测试。

## 验收标准

1. `/list` 默认最多突出 3 个最近活跃的已绑定会话。
2. 多个旧会话存在时，默认消息长度明显缩短。
3. 未绑定新会话、等待审批、等待输入等需要处理项不会被隐藏。
4. 用户仍可通过“查看全部”看到完整会话列表。
5. 所有新增/修改测试通过。
