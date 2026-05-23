# 内存结构 TTL 与上限设计

日期：2026-05-23

## 背景

当前项目是 Telegram CLI Gateway，长期运行时存在若干只增不减的内存结构：

- `MemoryTaskStore._tasks`：任务记录常驻内存。
- `RateLimitMiddleware._buckets`：访问过的用户桶会保留。
- `PermissionService._permission_locks`：每个 `tool_use_id` 的锁会保留。
- `AppContainer` 中的 `_jsonl_sync_locks` 与 `_session_event_locks`：每个 Claude session 的锁会保留。

目标是在不引入后台清理服务、不改变主要架构的前提下，为这些结构增加可配置的 TTL/容量约束，降低长期运行的内存膨胀风险。

## 目标

1. 任务记录默认保留 7 天且最多 1000 条。
2. 限流桶、权限锁、session 锁不再因历史用户或历史 session 无限增长。
3. 清理逻辑采用懒清理：在现有访问路径中顺手清理，不新增长期运行协程。
4. 热路径清理必须有上限，避免在单个请求里扫描全部历史桶或全部历史锁。
5. 清理行为配置化，默认启用。
6. 不影响现有限流、权限响应、JSONL sync、session event dispatch 的语义。

## 非目标

- 不引入 SQLite 或其他持久化任务存储。
- 不新增统一后台 `MemoryCleanupService`。
- 不重构 `AppContainer`、`bootstrap_mixins.py` 或任务服务架构。
- 不清理已经持久化到磁盘的 session 状态文件。

## 配置

新增配置项：

- `TASK_STORE_TTL_HOURS=168`
- `TASK_STORE_MAX_RECORDS=1000`
- `RATE_LIMIT_BUCKET_TTL_SEC`：未设置时使用 `RATE_LIMIT_WINDOW_SEC` 的有效值。
- `RATE_LIMIT_BUCKET_CLEANUP_INTERVAL_SEC=60`
- `RATE_LIMIT_BUCKET_CLEANUP_BATCH_SIZE=50`
- `PERMISSION_LOCK_TTL_SEC`：未设置时使用 `CLAUDE_HOOK_PENDING_PERMISSION_TTL_SEC` 的有效值。
- `SESSION_LOCK_TTL_SEC=3600`
- `LOCK_CLEANUP_INTERVAL_SEC=60`
- `LOCK_CLEANUP_BATCH_SIZE=50`

所有配置的有效值必须为正整数。`.env.example` 同步补充默认值说明。

`SESSION_LOCK_TTL_SEC` 使用独立默认值，不复用 `EXTERNAL_SESSION_STALE_TIMEOUT_SEC`。外部 session stale timeout 表示“多久没收到事件就认为外部 session 失活”，session lock TTL 表示“锁条目无人持有且无人等待后多久可以回收”，两者语义不同。

## 组件设计

### MemoryTaskStore

`MemoryTaskStore` 构造函数增加：

- `max_records: int`
- `ttl_hours: int`

当前类已有 `self._lock: asyncio.Lock`。所有公开 async 方法继续先获取该锁，再读写 `_tasks`。淘汰 helper 命名为 `_evict_expired_and_overflow_locked()`；`locked` 后缀只表示“调用方已经持有 `self._lock`”。该 helper 是同步函数，执行过程中不允许 `await`，避免在遍历/删除 dict 时发生协程交错。

清理策略：

1. 在 `add()`、`save()`、`list_by_user()`、`iter_all()` 中调用 `_evict_expired_and_overflow_locked()`。
2. TTL 只删除 final 状态任务，即 `SUCCEEDED`、`FAILED`、`TIMEOUT`、`CANCELED`。
3. TTL 起算点是 `ended_at`。如果 final 任务缺少 `ended_at`，使用 `created_at` 作为兼容兜底。
4. 未 final 的任务不会因 TTL 或容量上限被删除。
5. 容量超过 `max_records` 时，优先删除最旧的 final 任务，排序键为 `ended_at or created_at`。
6. 如果 final 任务不足以降到上限以下，保留未 final 任务，允许短暂超过上限以避免破坏运行中任务查询。

TTL 与容量的关系：

1. 每次淘汰先执行 TTL 删除，移除所有超过 `TASK_STORE_TTL_HOURS` 的 final 任务。
2. 再执行容量删除；如果剩余记录数仍超过 `TASK_STORE_MAX_RECORDS`，继续删除最旧 final 任务，即使这些 final 任务尚未超过 TTL。
3. 因此二者是独立约束：TTL 可能让记录数低于上限，容量也可能在 TTL 未到期时删除旧 final 任务。
4. 未 final 任务始终受保护；当未 final 任务数量本身超过上限时，store 可以超过 `TASK_STORE_MAX_RECORDS`。

复杂度要求：TTL pass 是 O(N)，容量删除通过一次候选收集和一次排序完成，避免嵌套循环导致 O(N²)。

### RateLimitMiddleware

`RateLimitMiddleware` 增加：

- `bucket_ttl_sec`
- `cleanup_interval_sec`
- `cleanup_batch_size`

每个用户桶内的时间戳数量仍由 `limit` 约束。当前请求只无条件清理当前用户桶，因此单请求固定成本为 O(limit)。

全局陈旧桶清理必须节流并限量：

1. middleware 维护 `_last_cleanup_ts`，只有距离上次全局清理超过 `cleanup_interval_sec` 时才启动一批全局清理。
2. middleware 维护 `_cleanup_queue: deque[int]` 和 `_cleanup_queued: set[int]`；新 user_id 首次创建桶时入队一次。
3. 每批从队列左侧最多弹出 `cleanup_batch_size` 个 user_id。
4. 检查到空桶或最后一次请求已超过 `bucket_ttl_sec` 的桶时删除该 user_id，并从 `_cleanup_queued` 移除。
5. 检查到仍活跃的桶时保留该桶，并把 user_id 重新放回队列尾部。
6. 保持现有限流判断不变。

这样即使 `allow_all_users=true` 且历史用户很多，一个活跃用户的每次请求也不会遍历全部历史桶。

### PermissionService 锁

权限锁不能简单在使用后 `pop`，否则并发等待同一把锁的协程可能仍持有旧锁引用，而新请求会创建新锁，破坏串行化。

设计一个轻量锁条目：

- `lock: asyncio.Lock`
- `ref_count: int`
- `last_used: float`

`last_used` 使用事件循环单调时间，例如 `asyncio.get_running_loop().time()`，避免系统时间回拨影响 TTL 判断。

获取锁时增加 `ref_count`，退出临界区后减少 `ref_count` 并更新 `last_used`。懒清理只删除满足以下条件的条目：

- `ref_count == 0`
- `lock.locked() is False`
- `last_used` 距今超过 `PERMISSION_LOCK_TTL_SEC`

权限响应完成后必须尝试清理当前 `tool_use_id` 的锁。全局过期锁清理受 `LOCK_CLEANUP_INTERVAL_SEC` 和 `LOCK_CLEANUP_BATCH_SIZE` 约束，不能在热路径中无上限扫描全表。

### JSONL sync locks

`_jsonl_sync_locks` 使用与权限锁相同的轻量锁条目和获取/释放流程。

清理策略：

1. `sync_claude_session()` 完成后更新该 session lock 的 `last_used`。
2. `_debounced_sync_claude_session()` 结束且没有 pending sync request 时必须尝试清理当前 session 的 sync lock。
3. 全局过期 sync lock 清理受 `LOCK_CLEANUP_INTERVAL_SEC` 和 `LOCK_CLEANUP_BATCH_SIZE` 约束。
4. 只删除无引用、未锁定、超过 `SESSION_LOCK_TTL_SEC` 的条目。
5. `stop()` 路径继续清空所有 JSONL sync 相关字典。

活跃 session 不会因为创建时间超过 TTL 被清理；TTL 只从最后一次使用后开始计算。

### Session event locks

`_session_event_locks` 使用与权限锁相同的轻量锁条目和获取/释放流程。

清理策略：

1. `_dispatch_session_event()` 完成后更新 `last_used`。
2. 收到 `SessionEnd` 事件后，必须立即尝试清理该 session 的 event lock，但仍必须满足无引用、未锁定条件。
3. 全局过期 event lock 清理受 `LOCK_CLEANUP_INTERVAL_SEC` 和 `LOCK_CLEANUP_BATCH_SIZE` 约束，不能在热路径中无上限扫描全表。

活跃 session 不会因为创建时间超过 TTL 被清理；TTL 只从最后一次使用后开始计算。

## 数据流

### 任务记录

1. 新任务进入 `MemoryTaskStore.add()`。
2. store 在 `self._lock` 保护下执行同步淘汰。
3. 先删除超过 TTL 的 final 任务。
4. 写入或更新当前任务。
5. 如果记录数超过上限，删除最旧 final 任务直到达到上限或没有可删除 final 任务。
6. 查询最近任务时只返回清理后的记录。

### 限流桶

1. Telegram 事件进入 `RateLimitMiddleware`。
2. middleware 清理当前用户桶的过期时间戳。
3. 判断是否超过限流。
4. 允许通过时写入当前时间戳。
5. 如果距离上次全局清理超过 `RATE_LIMIT_BUCKET_CLEANUP_INTERVAL_SEC`，最多清理 `RATE_LIMIT_BUCKET_CLEANUP_BATCH_SIZE` 个历史用户桶。

### 锁结构

1. 访问方按 key 获取锁条目。
2. 条目 `ref_count += 1`。
3. 进入 `async with lock`。
4. 临界区结束后 `ref_count -= 1`，更新 `last_used`。
5. 当前 key 在释放后必须尝试清理。
6. 全局锁清理只有在距离上次清理超过 `LOCK_CLEANUP_INTERVAL_SEC` 时运行，且单批最多检查 `LOCK_CLEANUP_BATCH_SIZE` 个 key。
7. 懒清理删除无引用、未锁定且过期的锁条目。

## 错误处理与并发约束

- 清理逻辑不得阻断主流程。
- 清理中遇到异常时记录 warning，并继续执行原操作。
- 配置非法时沿用现有启动期校验风格，直接抛出配置错误。
- `MemoryTaskStore` 淘汰 helper 是同步函数，在 `self._lock` 内执行，内部不允许 `await`。
- 锁注册表删除条目前必须确认 `ref_count == 0` 且 `lock.locked() is False`。
- 任务存储优先保证运行中任务可查询，必要时允许短暂超过 `TASK_STORE_MAX_RECORDS`。

## 测试计划

新增或扩展测试：

1. `MemoryTaskStore`
   - 过期 final 任务会按 `ended_at` 被清理。
   - final 任务缺少 `ended_at` 时使用 `created_at` 兜底。
   - 运行中任务不会因 TTL 被清理。
   - 超过 `max_records` 时删除最旧 final 任务。
   - final 任务不足时不会删除 running 任务。
   - 10,000 条任务下执行一次淘汰，验证结果正确，避免实现退化成 O(N²)。该测试不使用严格耗时断言，重点验证大数据量路径可完成且排序/删除正确。
   - 使用 `asyncio.gather` 并发调用 `add()`、`save()`、`list_by_user()`，验证不会出现 dict mutation 异常且最终记录满足保护 running 任务的约束。
2. `RateLimitMiddleware`
   - 限流行为保持不变。
   - 窗口过后当前用户空桶会被删除。
   - 历史用户桶只在 cleanup interval 到期后清理。
   - 单次全局清理最多处理 `cleanup_batch_size` 个桶。
   - 在大量历史桶和单个活跃用户场景下，请求路径不会扫描全部历史桶。
3. `PermissionService`
   - 同一 `tool_use_id` 并发响应仍串行。
   - 当前 `tool_use_id` 锁在无引用且过期后可被懒清理。
   - 未过期或 `ref_count > 0` 的锁不会被清理。
   - 全局锁清理只在 cleanup interval 到期后运行，且单批最多处理 `LOCK_CLEANUP_BATCH_SIZE` 个 key。
4. session locks
   - JSONL sync 完成且无 pending request 后，过期 sync lock 可被清理。
   - `SessionEnd` 后 event lock 可被安全清理。
   - 活跃 session 的 lock 因 `last_used` 更新不会被 TTL 误删。
5. 配置
   - 新配置项默认值正确。
   - 派生默认值正确：`RATE_LIMIT_BUCKET_TTL_SEC` 未设置时使用 `RATE_LIMIT_WINDOW_SEC`，`PERMISSION_LOCK_TTL_SEC` 未设置时使用 `CLAUDE_HOOK_PENDING_PERMISSION_TTL_SEC`。
   - `SESSION_LOCK_TTL_SEC` 默认值独立为 3600，不受 `EXTERNAL_SESSION_STALE_TIMEOUT_SEC` 影响。
   - 非正整数配置启动校验失败。

验收命令：

```bash
pytest -q
```

必要时先单跑：

```bash
pytest -q tests/test_auth_settings.py tests/test_task_service.py tests/test_bootstrap_hooks.py
```

## 取舍

选择“配置化 + 懒清理”是为了在内存风险、可调节性和改动范围之间取得平衡。后台清理服务更完整，但会增加生命周期管理和测试复杂度；固定默认值改动更少，但后续调参需要改代码。

这版设计避免在请求热路径无上限扫描历史结构。任务记录仍可能在查询/写入时做 O(N) 或 O(N log N) 淘汰，但默认上限为 1000，且大数据量测试会防止实现出现 O(N²) 退化。
