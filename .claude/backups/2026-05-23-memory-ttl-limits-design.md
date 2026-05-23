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
4. 清理行为配置化，默认启用。
5. 不影响现有限流、权限响应、JSONL sync、session event dispatch 的语义。

## 非目标

- 不引入 SQLite 或其他持久化任务存储。
- 不新增统一后台 `MemoryCleanupService`。
- 不重构 `AppContainer`、`bootstrap_mixins.py` 或任务服务架构。
- 不清理已经持久化到磁盘的 session 状态文件。

## 配置

新增配置项：

- `TASK_STORE_TTL_HOURS=168`
- `TASK_STORE_MAX_RECORDS=1000`
- `RATE_LIMIT_BUCKET_TTL_SEC`：默认使用 `RATE_LIMIT_WINDOW_SEC`
- `PERMISSION_LOCK_TTL_SEC`：默认使用 `CLAUDE_HOOK_PENDING_PERMISSION_TTL_SEC`
- `SESSION_LOCK_TTL_SEC`：默认使用 `EXTERNAL_SESSION_STALE_TIMEOUT_SEC`

所有配置必须为正整数。`.env.example` 同步补充默认值说明。

## 组件设计

### MemoryTaskStore

`MemoryTaskStore` 构造函数增加：

- `max_records: int`
- `ttl_hours: int`

清理策略：

1. 在 `add()`、`save()`、`list_by_user()`、`iter_all()` 中调用 `_prune_locked()`。
2. TTL 只删除 final 状态任务，即 `SUCCEEDED`、`FAILED`、`TIMEOUT`、`CANCELED`。
3. 未 final 的任务不会因 TTL 被删除。
4. 数量超过 `max_records` 时，优先删除最旧 final 任务。
5. 如果 final 任务不足以降到上限以下，保留未 final 任务，允许短暂超过上限以避免破坏运行中任务查询。

### RateLimitMiddleware

`RateLimitMiddleware` 增加 `bucket_ttl_sec`，默认等于限流窗口。

清理策略：

1. 每次请求先清理当前用户桶中过期时间戳。
2. 当前用户桶为空时删除该 user_id。
3. 顺手扫描并删除其他空桶或最后一次请求已超过 `bucket_ttl_sec` 的桶。
4. 保持现有限流判断不变。

### PermissionService 锁

权限锁不能简单在使用后 `pop`，否则并发等待同一把锁的协程可能仍持有旧锁引用，而新请求会创建新锁，破坏串行化。

设计一个轻量锁条目：

- `lock: asyncio.Lock`
- `ref_count: int`
- `last_used: datetime`

获取锁时增加 `ref_count`，退出临界区后减少 `ref_count` 并更新 `last_used`。懒清理只删除满足以下条件的条目：

- `ref_count == 0`
- `lock.locked() is False`
- `last_used` 超过 `PERMISSION_LOCK_TTL_SEC`

### JSONL sync locks

`_jsonl_sync_locks` 使用与权限锁相同的轻量锁条目和获取/释放流程。

清理策略：

1. `sync_claude_session()` 完成后更新该 session lock 的 `last_used`。
2. `_debounced_sync_claude_session()` 结束且没有 pending sync request 时触发懒清理。
3. 只删除无引用、未锁定、超过 `SESSION_LOCK_TTL_SEC` 的条目。
4. `stop()` 路径继续清空所有 JSONL sync 相关字典。

### Session event locks

`_session_event_locks` 使用与权限锁相同的轻量锁条目和获取/释放流程。

清理策略：

1. `_dispatch_session_event()` 完成后更新 `last_used`。
2. 收到 `SessionEnd` 事件后，可以清理该 session 的 event lock，但仍必须满足无引用、未锁定条件。
3. 其他访问路径顺手按 `SESSION_LOCK_TTL_SEC` 做懒清理。

## 数据流

### 任务记录

1. 新任务进入 `MemoryTaskStore.add()`。
2. store 清理过期 final 任务。
3. 写入新任务。
4. 如果记录数超过上限，删除最旧 final 任务。
5. 查询最近任务时只返回清理后的记录。

### 限流桶

1. Telegram 事件进入 `RateLimitMiddleware`。
2. middleware 清理当前用户的过期时间戳。
3. 判断是否超过限流。
4. 允许通过时写入当前时间戳。
5. 删除空桶和陈旧桶。

### 锁结构

1. 访问方按 key 获取锁条目。
2. 条目 `ref_count += 1`。
3. 进入 `async with lock`。
4. 临界区结束后 `ref_count -= 1`，更新 `last_used`。
5. 懒清理删除无引用、未锁定且过期的锁条目。

## 错误处理

- 清理逻辑不得阻断主流程。
- 清理中遇到异常时记录 warning，并继续执行原操作。
- 配置非法时沿用现有启动期校验风格，直接抛出配置错误。
- 任务存储优先保证运行中任务可查询，必要时允许短暂超过 `TASK_STORE_MAX_RECORDS`。

## 测试计划

新增或扩展测试：

1. `MemoryTaskStore`
   - 过期 final 任务会被清理。
   - 运行中任务不会因 TTL 被清理。
   - 超过 `max_records` 时删除最旧 final 任务。
   - final 任务不足时不会删除 running 任务。
2. `RateLimitMiddleware`
   - 限流行为保持不变。
   - 窗口过后空桶会被删除。
   - 访问多个用户后，陈旧用户桶会被懒清理。
3. `PermissionService`
   - 同一 `tool_use_id` 并发响应仍串行。
   - 锁在无引用且过期后可被懒清理。
4. session locks
   - JSONL sync 完成且无 pending request 后不会长期残留过期锁。
   - `SessionEnd` 后 event lock 可被安全清理。
5. 配置
   - 新配置项默认值正确。
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
