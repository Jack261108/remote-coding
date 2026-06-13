# Session 生命周期问题清单

记录时间: 2026-06-13

---

## 严重

### 1. FileSessionContextStore 的 claude_session_index 在 save() 后不重建

**文件**: `app/adapters/storage/file_session_context_store.py`

`save()` 将 `_list_cache` 设为 None，但没有重建 `_claude_session_index`。index 更新依赖 "last-writer-wins" 逻辑，两个不同 SessionContext 共享同一个 `claude_session_id` 时会导致旧用户的 index 条目被覆盖但旧 user 的文件仍在磁盘。在下次 `list_all()` 之前，`get_by_claude_session_id()` 可能返回过时的 SessionContext 引用。

### 2. SessionContext 切换 provider/workdir 时旧 tmux 终端孤立

**文件**: `app/services/session_service.py`

`_update_or_create_session()` 在 workdir 或 provider 变更时直接修改 SessionContext 字段并清除 `claude_session_id`，但没有：
- 关闭旧的 tmux 终端
- 清理旧 terminal_id 关联的 SessionState
- 取消旧 session 上的 supervisor watcher

导致"僵尸 tmux 终端"：旧 tmux 会话仍在后台运行，占用资源，旧 SessionState 可能仍处于 PROCESSING 状态。

### 3. _bind_hook_session 中 get_or_create 和 save 之间无锁保护

**文件**: `app/bootstrap_mixins.py`, 第 621-633 行

```python
state = self.structured_session_store.get_or_create(...)
state.terminal_id = matched.terminal_id
state.user_id = matched.user_id
state.workdir = workdir
state.claude_session_id = event.session_id
self.structured_session_store.save(state)
```

`_bind_hook_session` 在 stage pipeline 中是独立 stage，不持有 session event lock。两个 hook 事件同时到达同一 session_id 时可能并发修改同一 SessionState 对象，导致数据竞争。

---

## 高

### 4. MemorySessionStore 无 delete 能力

**文件**: `app/adapters/storage/memory.py`, 第 84-106 行

`MemorySessionStore` 以 `user_id` 为 key，只有 `save()` 没有 `delete()`。session 一旦创建只能被覆盖，无法删除。用户被 ban 等场景下无法清除其 session context。

### 5. SessionState 文件永不清理

**文件**: `app/adapters/storage/file_session_store.py`

`FileSessionStore` 将每个 session 状态保存为 `sessions/<session_id>/session.state.json` 和 `conversation.snapshot.json`，但没有清理已结束 session 文件的机制。`SessionStateCache` 的 LRU 驱逐（512 条）只移除内存缓存，磁盘文件永久保留。长期运行后 `sessions/` 目录持续增长。

### 6. ExternalBinding cleanup 中 now 在 await 后过时

**文件**: `app/services/external_binding_cleanup_service.py`, 第 136 行

`_cleanup()` 在循环开始时获取 `now = utc_now()`，但后续 `await` 操作后 `now` 已过时。重新计算 `idle_age = now - current.last_activity_at` 时判断不够精确。

---

## 中

### 7. terminal_locks 字典永不清理

**文件**: `app/services/session_service.py`, 第 16 行

`terminal_group_lock()` 为每个 terminal_id 创建 `asyncio.Lock`，但永远不会从字典中移除。对比 `_session_event_locks` 和 `_jsonl_sync_locks` 使用了 `RefCountedLockRegistry`（有 TTL 清理），此处不一致。

### 8. _match_session_context fallback 全量扫描性能问题

**文件**: `app/bootstrap_mixins.py`, 第 654-776 行

`lookup_by_claude_session_id()` 返回 None 时，代码调用 `list_all()` 全量扫描匹配，包含多个 await 调用和 `Path.resolve()` 操作。`_has_active_interactive_task()` 还调用 `self.task_store.iter_all()` 全量遍历。

### 9. FileSessionContextStore 的 list_all() 在 save() 后频繁磁盘 I/O

**文件**: `app/adapters/storage/file_session_context_store.py`

每次 `save()` 都将 `_list_cache` 设为 None，之后的 `list_all()` 从磁盘重新加载。高频 hook 事件场景下（Claude Code 执行多个工具），每次事件触发 save 后紧接着 ownership resolver 的 `list_all()` 又触发磁盘读取。

### 10. SessionSupervisor 的 _locks 和 _tasks 字典清理时序不安全

**文件**: `app/services/session_supervisor.py`

`forget()` 不清理 `_locks` 字典，而 `_watch_session` 的 finally 块会清理。两者可能在不同时间点执行，导致 `_locks` 与 `_tasks` 不一致。

### 11. ExternalBindingStore.touch_activity 节流可能导致重启后丢失活动时间

**文件**: `app/services/external_binding_store.py`, 第 70-113 行

`touch_activity()` 对磁盘持久化有 60 秒节流。进程在两次持久化之间崩溃时，`last_activity_at` 和 `pid` 的内存更新丢失。重启后 cleanup service 可能使用过时数据判断 binding 是否过期，错误移除活跃 binding。

---

## 低

### 12. SessionRestoreMixin 恢复时可能创建孤立 SessionState

**文件**: `app/bootstrap_mixins.py`, 第 876-909 行

`_restore_session_bindings()` 对每个有 `claude_session_id` 的 SessionContext 调用 `get_or_create()`，在磁盘上创建目录。如果后续判断 session 不需要 watch，已创建的文件不删除。

### 13. SessionOwnershipResolver.resolve() 使用 list_all() 全量扫描

**文件**: `app/services/session_ownership_resolver.py`, 第 48 行

每次 hook 事件到达时 ownership resolver 调用 `list_all()`。虽然 `FileSessionContextStore` 有 list_cache，但在频繁 save 导致 cache 失效的情况下会频繁触发磁盘读取。应优先使用 `get_by_claude_session_id()` 的 O(1) 查找。

### 14. ExternalUserQuestionState 的 TTL 清理只在 store/get 时触发

**文件**: `app/services/external_user_question_state.py`, 第 64-67 行

`_prune_stale()` 只在 `store()` 和 `get()` 时调用。既没有新 question 存入也没有人查询的 session 的 pending question 会一直留在内存中直到 `invalidate_session()` 或进程重启。

### 15. periodic_janitor 串行执行可能阻塞后续 job

**文件**: `app/services/periodic_janitor.py`, 第 58-78 行

`_run()` 在循环中串行执行所有注册的 job。某个耗时较长的 job（如 `session_health_check`）会推迟后续所有 job 的执行。

---

## 修复优先级建议

| 优先级 | 问题 | 改动量 |
|--------|------|--------|
| P0 | #2 僵尸 tmux 终端 | 中 |
| P0 | #3 并发竞态 | 小 |
| P1 | #5 SessionState 文件清理 | 小（加定期清理 job） |
| P1 | #1 claude_session_index 不重建 | 小 |
| P2 | #9 list_all 频繁磁盘 I/O | 小 |
| P2 | #7 terminal_locks 不清理 | 小（改用 RefCountedLockRegistry） |
| P2 | #8 fallback 全量扫描 | 中 |
| P3 | #6 #11 时间精度 / 持久化节流 | 小 |
| P3 | #4 #10 #12 #13 #14 #15 | 小 |
