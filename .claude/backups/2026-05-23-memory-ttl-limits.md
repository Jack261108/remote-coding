# Memory TTL Limits Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为长期运行的内存结构增加可配置 TTL、容量上限和有界懒清理，避免任务记录、限流桶、权限锁、JSONL sync 锁、session event 锁无限增长。

**Architecture:** 增加一个通用 `RefCountedLockRegistry` 负责 `asyncio.Lock + ref_count + last_used + 有界清理队列`。任务记录和限流桶在各自组件内部做懒清理；配置通过 `Settings` 注入，`AppContainer` 负责把配置传给 store、middleware 和 registry 使用方。

**Tech Stack:** Python 3.11, asyncio, aiogram middleware, pydantic-settings, pytest, pytest-asyncio.

---

## 执行安全约束

- 当前工作区已有无关变更：`app/bootstrap_mixins.py` 和 `_test_task21.py`。不要提交 `_test_task21.py`。如果在当前工作区执行本计划，修改 `app/bootstrap_mixins.py` 前先检查现有 diff，必须保留用户原有改动；更推荐在隔离 worktree 中执行。
- 不使用 `git add .` 或 `git add -A`。每次提交只添加本任务列出的文件。
- 运行 Python/pytest 前按用户全局要求确认 pyenv/pyenv-virtualenv 环境。若当前项目未绑定 pyenv virtualenv，先停止并按 pyenv-virtualenv 流程创建和绑定，不使用 venv、conda、poetry 自建环境。
- 修改非代码文件前已经存在备份约束：本计划会修改 `.env.example` 和新增计划外实现文件；执行时如改已有非代码文件，先备份到 `.claude/backups/`，同一文件最多一个备份。

## 文件结构

- Create: `app/services/lock_registry.py`  
  通用引用计数异步锁注册表，供权限、JSONL sync、session event dispatch 三处复用。
- Create: `tests/test_lock_registry.py`  
  覆盖 registry 串行化、TTL 清理、重建重新入队、批量清理上限、实例状态独立。
- Create: `tests/test_memory_task_store.py`  
  覆盖任务记录 TTL、容量、运行中任务保护、10,000 条压力路径、并发访问。
- Modify: `app/adapters/storage/memory.py`  
  `MemoryTaskStore` 增加 TTL 和 max records 淘汰。
- Modify: `app/bot/middleware/rate_limit.py`  
  限流桶增加当前桶清理、有界全局清理队列和 cleanup interval。
- Modify: `tests/test_auth_settings.py`  
  增加 settings 默认值/派生值校验，以及 rate limit cleanup 测试。
- Modify: `app/config/settings.py`  
  增加新配置字段、正数校验、派生属性。
- Modify: `deploy/env/.env.example`  
  补充新配置默认值。
- Modify: `app/services/permission_service.py`  
  用 `RefCountedLockRegistry` 替换 `_permission_locks` dict。
- Modify: `app/services/task_service.py`  
  将 settings 中的 permission lock 清理配置传给 `PermissionService`。
- Modify: `app/bootstrap.py`  
  将 task store、rate limit、JSONL sync registry、session event registry 接入配置。
- Modify: `app/bootstrap_base.py`  
  更新 `_jsonl_sync_locks` / `_session_event_locks` 类型声明。
- Modify: `app/bootstrap_mixins.py`  
  用 registry 的 async context manager 替换 dict lock 访问；stop 时调用 registry `clear()`。
- Modify: `tests/test_bootstrap_hooks.py`  
  更新已有 JSONL lock 测试，并补充 session event lock 清理验证。

---

### Task 1: RefCountedLockRegistry

**Files:**
- Create: `tests/test_lock_registry.py`
- Create: `app/services/lock_registry.py`

- [ ] **Step 1: Write failing registry tests**

Create `tests/test_lock_registry.py` with:

```python
from __future__ import annotations

import asyncio

import pytest

from app.services.lock_registry import RefCountedLockRegistry


@pytest.mark.asyncio
async def test_registry_serializes_same_key() -> None:
    registry = RefCountedLockRegistry(ttl_sec=10, cleanup_interval_sec=60, cleanup_batch_size=50)
    entered: list[str] = []
    release_first = asyncio.Event()
    first_entered = asyncio.Event()

    async def worker(name: str) -> None:
        async with registry.lock("tool-1"):
            entered.append(name)
            if name == "first":
                first_entered.set()
                await release_first.wait()

    first = asyncio.create_task(worker("first"))
    await first_entered.wait()
    second = asyncio.create_task(worker("second"))
    await asyncio.sleep(0)

    assert entered == ["first"]

    release_first.set()
    await first
    await second

    assert entered == ["first", "second"]


@pytest.mark.asyncio
async def test_registry_keeps_referenced_key_during_cleanup() -> None:
    now = 100.0

    def clock() -> float:
        return now

    registry = RefCountedLockRegistry(ttl_sec=10, cleanup_interval_sec=1, cleanup_batch_size=50, clock=clock)
    entered = await registry.lock("tool-1").__aenter__()
    assert entered is None

    now = 200.0
    await registry.cleanup_expired()

    assert len(registry) == 1

    await registry.lock("tool-1").__aexit__(None, None, None)
    await registry.cleanup_expired()

    assert len(registry) == 0


@pytest.mark.asyncio
async def test_registry_requeues_key_after_delete_and_recreate() -> None:
    now = 100.0

    def clock() -> float:
        return now

    registry = RefCountedLockRegistry(ttl_sec=10, cleanup_interval_sec=1, cleanup_batch_size=50, clock=clock)

    async with registry.lock("tool-1"):
        pass

    now = 200.0
    await registry.cleanup_expired()
    assert len(registry) == 0
    assert registry.queued_count == 0

    async with registry.lock("tool-1"):
        pass

    assert len(registry) == 1
    assert registry.queued_count == 1


@pytest.mark.asyncio
async def test_registry_cleanup_batch_size_limits_work_per_pass() -> None:
    now = 100.0

    def clock() -> float:
        return now

    registry = RefCountedLockRegistry(ttl_sec=10, cleanup_interval_sec=1, cleanup_batch_size=2, clock=clock)
    for key in ("a", "b", "c"):
        async with registry.lock(key):
            pass

    now = 200.0
    await registry.cleanup_expired()

    assert len(registry) == 1
    assert registry.queued_count == 1

    now = 202.0
    await registry.cleanup_expired()

    assert len(registry) == 0
    assert registry.queued_count == 0


@pytest.mark.asyncio
async def test_registry_instances_keep_cleanup_state_independent() -> None:
    now = 100.0

    def clock() -> float:
        return now

    first = RefCountedLockRegistry(ttl_sec=10, cleanup_interval_sec=1, cleanup_batch_size=1, clock=clock)
    second = RefCountedLockRegistry(ttl_sec=10, cleanup_interval_sec=1, cleanup_batch_size=1, clock=clock)

    async with first.lock("a"):
        pass
    async with second.lock("b"):
        pass

    now = 200.0
    await first.cleanup_expired()

    assert len(first) == 0
    assert len(second) == 1

    await second.cleanup_expired()
    assert len(second) == 0
```

- [ ] **Step 2: Run registry tests and verify they fail**

Run:

```bash
pytest -q tests/test_lock_registry.py
```

Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.lock_registry'`.

- [ ] **Step 3: Implement RefCountedLockRegistry**

Create `app/services/lock_registry.py` with:

```python
from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field


@dataclass(slots=True)
class _LockEntry:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    ref_count: int = 0
    last_used: float = 0.0


class RefCountedLockRegistry:
    def __init__(
        self,
        *,
        ttl_sec: int,
        cleanup_interval_sec: int,
        cleanup_batch_size: int,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if ttl_sec <= 0:
            raise ValueError("ttl_sec must be positive")
        if cleanup_interval_sec <= 0:
            raise ValueError("cleanup_interval_sec must be positive")
        if cleanup_batch_size <= 0:
            raise ValueError("cleanup_batch_size must be positive")
        self._ttl_sec = ttl_sec
        self._cleanup_interval_sec = cleanup_interval_sec
        self._cleanup_batch_size = cleanup_batch_size
        self._clock = clock
        self._entries: dict[str, _LockEntry] = {}
        self._cleanup_queue: deque[str] = deque()
        self._cleanup_queued: set[str] = set()
        self._last_cleanup_ts = 0.0
        self._registry_lock = asyncio.Lock()

    def __len__(self) -> int:
        return len(self._entries)

    @property
    def queued_count(self) -> int:
        return len(self._cleanup_queued)

    @asynccontextmanager
    async def lock(self, key: str) -> AsyncIterator[None]:
        entry = await self._acquire_entry(key)
        try:
            async with entry.lock:
                yield None
        finally:
            await self._release_entry(key, entry)

    async def cleanup_key(self, key: str, *, require_expired: bool = True) -> None:
        async with self._registry_lock:
            self._cleanup_key_locked(key, now=self._now(), require_expired=require_expired)

    async def cleanup_expired(self) -> None:
        async with self._registry_lock:
            now = self._now()
            if now - self._last_cleanup_ts < self._cleanup_interval_sec:
                return
            self._last_cleanup_ts = now
            for _ in range(min(self._cleanup_batch_size, len(self._cleanup_queue))):
                key = self._cleanup_queue.popleft()
                self._cleanup_queued.discard(key)
                entry = self._entries.get(key)
                if entry is None:
                    continue
                if self._can_delete_entry(entry, now=now, require_expired=True):
                    self._entries.pop(key, None)
                    continue
                self._enqueue_locked(key)

    async def clear(self) -> None:
        async with self._registry_lock:
            self._entries.clear()
            self._cleanup_queue.clear()
            self._cleanup_queued.clear()

    async def _acquire_entry(self, key: str) -> _LockEntry:
        async with self._registry_lock:
            now = self._now()
            entry = self._entries.get(key)
            if entry is None:
                entry = _LockEntry(last_used=now)
                self._entries[key] = entry
            entry.ref_count += 1
            self._enqueue_locked(key)
            return entry

    async def _release_entry(self, key: str, entry: _LockEntry) -> None:
        async with self._registry_lock:
            current = self._entries.get(key)
            if current is entry:
                entry.ref_count = max(0, entry.ref_count - 1)
                entry.last_used = self._now()
                self._cleanup_key_locked(key, now=entry.last_used, require_expired=True)
        await self.cleanup_expired()

    def _cleanup_key_locked(self, key: str, *, now: float, require_expired: bool) -> None:
        entry = self._entries.get(key)
        if entry is None:
            self._cleanup_queued.discard(key)
            return
        if self._can_delete_entry(entry, now=now, require_expired=require_expired):
            self._entries.pop(key, None)
            self._cleanup_queued.discard(key)

    def _can_delete_entry(self, entry: _LockEntry, *, now: float, require_expired: bool) -> bool:
        if entry.ref_count != 0 or entry.lock.locked():
            return False
        if require_expired and now - entry.last_used < self._ttl_sec:
            return False
        return True

    def _enqueue_locked(self, key: str) -> None:
        if key not in self._cleanup_queued:
            self._cleanup_queue.append(key)
            self._cleanup_queued.add(key)

    def _now(self) -> float:
        if self._clock is not None:
            return self._clock()
        return asyncio.get_running_loop().time()
```

- [ ] **Step 4: Run registry tests and verify they pass**

Run:

```bash
pytest -q tests/test_lock_registry.py
```

Expected: PASS.

- [ ] **Step 5: Commit registry**

Run:

```bash
git add app/services/lock_registry.py tests/test_lock_registry.py
git commit -m "$(cat <<'EOF'
feat: add ref-counted lock registry
EOF
)"
```

---

### Task 2: MemoryTaskStore TTL and capacity eviction

**Files:**
- Create: `tests/test_memory_task_store.py`
- Modify: `app/adapters/storage/memory.py`

- [ ] **Step 1: Write failing MemoryTaskStore tests**

Create `tests/test_memory_task_store.py` with:

```python
from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from app.adapters.storage.memory import MemoryTaskStore
from app.domain.models import TaskRecord, TaskStatus, utc_now


def _task(task_id: str, *, status: TaskStatus, created_offset_hours: int = 0, ended_offset_hours: int | None = None) -> TaskRecord:
    now = utc_now()
    created_at = now + timedelta(hours=created_offset_hours)
    ended_at = None if ended_offset_hours is None else now + timedelta(hours=ended_offset_hours)
    return TaskRecord(
        task_id=task_id,
        session_id=f"session-{task_id}",
        user_id=1,
        provider="claude_code",
        prompt="hi",
        workdir="/tmp",
        timeout_sec=10,
        status=status,
        created_at=created_at,
        ended_at=ended_at,
    )


@pytest.mark.asyncio
async def test_memory_task_store_evicts_expired_final_task_by_ended_at() -> None:
    store = MemoryTaskStore(max_records=100, ttl_hours=24)
    await store.add(_task("old", status=TaskStatus.SUCCEEDED, created_offset_hours=-200, ended_offset_hours=-25))
    await store.add(_task("new", status=TaskStatus.SUCCEEDED, created_offset_hours=-200, ended_offset_hours=-23))

    assert await store.get("old") is None
    assert await store.get("new") is not None


@pytest.mark.asyncio
async def test_memory_task_store_falls_back_to_created_at_when_final_task_has_no_ended_at() -> None:
    store = MemoryTaskStore(max_records=100, ttl_hours=24)
    await store.add(_task("old", status=TaskStatus.FAILED, created_offset_hours=-25, ended_offset_hours=None))

    assert await store.get("old") is None


@pytest.mark.asyncio
async def test_memory_task_store_never_evicts_running_task_for_ttl_or_capacity() -> None:
    store = MemoryTaskStore(max_records=1, ttl_hours=1)
    await store.add(_task("running", status=TaskStatus.RUNNING, created_offset_hours=-100, ended_offset_hours=None))
    await store.add(_task("final", status=TaskStatus.SUCCEEDED, created_offset_hours=-1, ended_offset_hours=0))

    assert await store.get("running") is not None
    assert await store.get("final") is None


@pytest.mark.asyncio
async def test_memory_task_store_evicts_oldest_final_tasks_when_over_capacity() -> None:
    store = MemoryTaskStore(max_records=2, ttl_hours=168)
    await store.add(_task("old", status=TaskStatus.SUCCEEDED, created_offset_hours=-5, ended_offset_hours=-5))
    await store.add(_task("middle", status=TaskStatus.SUCCEEDED, created_offset_hours=-4, ended_offset_hours=-4))
    await store.add(_task("new", status=TaskStatus.SUCCEEDED, created_offset_hours=-3, ended_offset_hours=-3))

    ids = [task.task_id for task in await store.list_by_user(user_id=1, limit=10)]

    assert ids == ["new", "middle"]


@pytest.mark.asyncio
async def test_memory_task_store_large_eviction_path_keeps_latest_records() -> None:
    store = MemoryTaskStore(max_records=1000, ttl_hours=168)
    now = utc_now()
    for i in range(10_000):
        await store.add(
            TaskRecord(
                task_id=f"task-{i}",
                session_id=f"session-{i}",
                user_id=1,
                provider="claude_code",
                prompt="hi",
                workdir="/tmp",
                timeout_sec=10,
                status=TaskStatus.SUCCEEDED,
                created_at=now + timedelta(seconds=i),
                ended_at=now + timedelta(seconds=i),
            )
        )

    records = await store.list_by_user(user_id=1, limit=2000)

    assert len(records) == 1000
    assert records[0].task_id == "task-9999"
    assert records[-1].task_id == "task-9000"


@pytest.mark.asyncio
async def test_memory_task_store_concurrent_access_keeps_running_tasks() -> None:
    store = MemoryTaskStore(max_records=5, ttl_hours=1)

    async def add_running(index: int) -> None:
        await store.add(_task(f"running-{index}", status=TaskStatus.RUNNING, created_offset_hours=-10, ended_offset_hours=None))

    async def add_final(index: int) -> None:
        await store.add(_task(f"final-{index}", status=TaskStatus.SUCCEEDED, created_offset_hours=-10, ended_offset_hours=-10))

    await asyncio.gather(*(add_running(i) for i in range(10)), *(add_final(i) for i in range(10)))
    records = list(await store.iter_all())

    assert {record.task_id for record in records} == {f"running-{i}" for i in range(10)}
```

- [ ] **Step 2: Run MemoryTaskStore tests and verify they fail**

Run:

```bash
pytest -q tests/test_memory_task_store.py
```

Expected: FAIL with `TypeError: MemoryTaskStore.__init__() got an unexpected keyword argument 'max_records'`.

- [ ] **Step 3: Implement MemoryTaskStore eviction**

Replace the `MemoryTaskStore` class in `app/adapters/storage/memory.py` with:

```python
class MemoryTaskStore:
    def __init__(self, *, max_records: int = 1000, ttl_hours: int = 168) -> None:
        if max_records <= 0:
            raise ValueError("max_records must be positive")
        if ttl_hours <= 0:
            raise ValueError("ttl_hours must be positive")
        self._tasks: dict[str, TaskRecord] = {}
        self._max_records = max_records
        self._ttl = timedelta(hours=ttl_hours)
        self._lock = asyncio.Lock()

    async def add(self, record: TaskRecord) -> None:
        async with self._lock:
            self._evict_expired_and_overflow_locked()
            self._tasks[record.task_id] = record
            self._evict_expired_and_overflow_locked()

    async def get(self, task_id: str) -> TaskRecord | None:
        async with self._lock:
            self._evict_expired_and_overflow_locked()
            return self._tasks.get(task_id)

    async def save(self, record: TaskRecord) -> None:
        async with self._lock:
            self._evict_expired_and_overflow_locked()
            self._tasks[record.task_id] = record
            self._evict_expired_and_overflow_locked()

    async def list_by_user(self, user_id: int, limit: int = 10) -> list[TaskRecord]:
        async with self._lock:
            self._evict_expired_and_overflow_locked()
            items = [x for x in self._tasks.values() if x.user_id == user_id]
        items.sort(key=lambda x: x.created_at, reverse=True)
        return items[:limit]

    async def iter_all(self) -> Iterable[TaskRecord]:
        async with self._lock:
            self._evict_expired_and_overflow_locked()
            return list(self._tasks.values())

    def _evict_expired_and_overflow_locked(self) -> None:
        now = utc_now()
        expired_ids = [
            task_id
            for task_id, record in self._tasks.items()
            if record.is_final and now - self._retention_time(record) > self._ttl
        ]
        for task_id in expired_ids:
            self._tasks.pop(task_id, None)

        overflow = len(self._tasks) - self._max_records
        if overflow <= 0:
            return

        final_records = sorted(
            (record for record in self._tasks.values() if record.is_final),
            key=self._retention_time,
        )
        for record in final_records[:overflow]:
            self._tasks.pop(record.task_id, None)

    def _retention_time(self, record: TaskRecord) -> datetime:
        return record.ended_at or record.created_at
```

Also update imports at the top of `app/adapters/storage/memory.py`:

```python
from datetime import datetime, timedelta

from app.domain.models import SessionContext, TaskRecord, utc_now
```

- [ ] **Step 4: Run MemoryTaskStore tests and existing task tests**

Run:

```bash
pytest -q tests/test_memory_task_store.py tests/test_task_service.py
```

Expected: PASS.

- [ ] **Step 5: Commit MemoryTaskStore eviction**

Run:

```bash
git add app/adapters/storage/memory.py tests/test_memory_task_store.py
git commit -m "$(cat <<'EOF'
feat: bound in-memory task store
EOF
)"
```

---

### Task 3: RateLimitMiddleware bounded bucket cleanup

**Files:**
- Modify: `tests/test_auth_settings.py`
- Modify: `app/bot/middleware/rate_limit.py`

- [ ] **Step 1: Add failing rate-limit cleanup tests**

Append these tests after `test_rate_limit_middleware_limits_callback_query_user` in `tests/test_auth_settings.py`:

```python
@pytest.mark.asyncio
async def test_rate_limit_middleware_deletes_empty_current_bucket_after_window() -> None:
    middleware = RateLimitMiddleware(limit=1, window_sec=1, bucket_ttl_sec=1, cleanup_interval_sec=60, cleanup_batch_size=50)
    callback = DummyCallbackQuery(user_id=1)

    first = await middleware(_passing_handler, callback, {})
    assert first == "ok"
    assert 1 in middleware._buckets

    now = __import__("asyncio").get_running_loop().time()
    middleware._buckets[1].clear()
    middleware._buckets[1].append(now - 2)

    second = await middleware(_passing_handler, callback, {})

    assert second == "ok"
    assert list(middleware._buckets[1])


@pytest.mark.asyncio
async def test_rate_limit_global_cleanup_is_interval_and_batch_limited() -> None:
    middleware = RateLimitMiddleware(limit=2, window_sec=10, bucket_ttl_sec=10, cleanup_interval_sec=60, cleanup_batch_size=2)
    loop = __import__("asyncio").get_running_loop()
    now = loop.time()
    for user_id in range(1, 6):
        middleware._buckets[user_id].append(now - 100)
        middleware._enqueue_bucket_locked(user_id)
    middleware._last_cleanup_ts = now

    callback = DummyCallbackQuery(user_id=99)
    await middleware(_passing_handler, callback, {})

    assert {1, 2, 3, 4, 5}.issubset(middleware._buckets)

    middleware._last_cleanup_ts = now - 61
    await middleware(_passing_handler, callback, {})

    remaining_old_ids = {user_id for user_id in range(1, 6) if user_id in middleware._buckets}
    assert len(remaining_old_ids) == 3


@pytest.mark.asyncio
async def test_rate_limit_bucket_recreated_after_delete_reenters_cleanup_queue() -> None:
    middleware = RateLimitMiddleware(limit=2, window_sec=10, bucket_ttl_sec=10, cleanup_interval_sec=1, cleanup_batch_size=50)
    loop = __import__("asyncio").get_running_loop()
    now = loop.time()
    middleware._buckets[1].append(now - 100)
    middleware._enqueue_bucket_locked(1)
    middleware._last_cleanup_ts = now - 2

    await middleware(DummyCallbackQuery(user_id=2).__class__.answer, DummyCallbackQuery(user_id=None), {})
```

Replace the last test above immediately with this corrected version, which uses the normal handler and verifies requeue behavior:

```python
@pytest.mark.asyncio
async def test_rate_limit_bucket_recreated_after_delete_reenters_cleanup_queue() -> None:
    middleware = RateLimitMiddleware(limit=2, window_sec=10, bucket_ttl_sec=10, cleanup_interval_sec=1, cleanup_batch_size=50)
    loop = __import__("asyncio").get_running_loop()
    now = loop.time()
    middleware._buckets[1].append(now - 100)
    middleware._enqueue_bucket_locked(1)
    middleware._last_cleanup_ts = now - 2

    await middleware(_passing_handler, DummyCallbackQuery(user_id=2), {})

    assert 1 not in middleware._buckets
    assert 1 not in middleware._cleanup_queued

    await middleware(_passing_handler, DummyCallbackQuery(user_id=1), {})

    assert 1 in middleware._buckets
    assert 1 in middleware._cleanup_queued
```

- [ ] **Step 2: Run rate-limit tests and verify they fail**

Run:

```bash
pytest -q tests/test_auth_settings.py::test_rate_limit_middleware_deletes_empty_current_bucket_after_window tests/test_auth_settings.py::test_rate_limit_global_cleanup_is_interval_and_batch_limited tests/test_auth_settings.py::test_rate_limit_bucket_recreated_after_delete_reenters_cleanup_queue
```

Expected: FAIL with `TypeError: RateLimitMiddleware.__init__() got an unexpected keyword argument 'bucket_ttl_sec'`.

- [ ] **Step 3: Implement bounded bucket cleanup**

Replace `app/bot/middleware/rate_limit.py` with:

```python
from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware


class RateLimitMiddleware(BaseMiddleware):
    def __init__(
        self,
        *,
        limit: int,
        window_sec: int,
        bucket_ttl_sec: int | None = None,
        cleanup_interval_sec: int = 60,
        cleanup_batch_size: int = 50,
    ) -> None:
        super().__init__()
        if limit <= 0:
            raise ValueError("limit must be positive")
        if window_sec <= 0:
            raise ValueError("window_sec must be positive")
        effective_bucket_ttl_sec = bucket_ttl_sec if bucket_ttl_sec is not None else window_sec
        if effective_bucket_ttl_sec <= 0:
            raise ValueError("bucket_ttl_sec must be positive")
        if cleanup_interval_sec <= 0:
            raise ValueError("cleanup_interval_sec must be positive")
        if cleanup_batch_size <= 0:
            raise ValueError("cleanup_batch_size must be positive")
        self._limit = limit
        self._window_sec = window_sec
        self._bucket_ttl_sec = effective_bucket_ttl_sec
        self._cleanup_interval_sec = cleanup_interval_sec
        self._cleanup_batch_size = cleanup_batch_size
        self._buckets: dict[int, deque[float]] = {}
        self._cleanup_queue: deque[int] = deque()
        self._cleanup_queued: set[int] = set()
        self._last_cleanup_ts = 0.0
        self._lock = asyncio.Lock()

    async def __call__(
        self,
        handler: Callable[[Any, dict], Awaitable],
        event: Any,
        data: dict,
    ):
        user = event.from_user
        if user is None:
            return await handler(event, data)

        limited = False
        now = asyncio.get_running_loop().time()
        async with self._lock:
            bucket = self._buckets.get(user.id)
            if bucket is None:
                bucket = deque()
                self._buckets[user.id] = bucket
                self._enqueue_bucket_locked(user.id)

            self._prune_bucket_locked(bucket, now=now)
            if len(bucket) >= self._limit:
                limited = True
            else:
                bucket.append(now)

            self._maybe_cleanup_buckets_locked(now=now)

        if limited:
            await event.answer("请求过于频繁，请稍后再试。")
            return None

        return await handler(event, data)

    def _maybe_cleanup_buckets_locked(self, *, now: float) -> None:
        if now - self._last_cleanup_ts < self._cleanup_interval_sec:
            return
        self._last_cleanup_ts = now
        for _ in range(min(self._cleanup_batch_size, len(self._cleanup_queue))):
            user_id = self._cleanup_queue.popleft()
            self._cleanup_queued.discard(user_id)
            bucket = self._buckets.get(user_id)
            if bucket is None:
                continue
            self._prune_bucket_locked(bucket, now=now)
            if not bucket or now - bucket[-1] > self._bucket_ttl_sec:
                self._delete_bucket_locked(user_id)
                continue
            self._enqueue_bucket_locked(user_id)

    def _prune_bucket_locked(self, bucket: deque[float], *, now: float) -> None:
        while bucket and now - bucket[0] > self._window_sec:
            bucket.popleft()

    def _delete_bucket_locked(self, user_id: int) -> None:
        self._buckets.pop(user_id, None)
        self._cleanup_queued.discard(user_id)

    def _enqueue_bucket_locked(self, user_id: int) -> None:
        if user_id not in self._cleanup_queued:
            self._cleanup_queue.append(user_id)
            self._cleanup_queued.add(user_id)
```

- [ ] **Step 4: Run rate-limit tests**

Run:

```bash
pytest -q tests/test_auth_settings.py
```

Expected: PASS.

- [ ] **Step 5: Commit rate-limit cleanup**

Run:

```bash
git add app/bot/middleware/rate_limit.py tests/test_auth_settings.py
git commit -m "$(cat <<'EOF'
feat: bound rate limit buckets
EOF
)"
```

---

### Task 4: Settings and environment configuration

**Files:**
- Modify: `tests/test_auth_settings.py`
- Modify: `app/config/settings.py`
- Modify: `deploy/env/.env.example`

- [ ] **Step 1: Back up `.env.example` before editing**

Run:

```bash
mkdir -p /Users/jack/project/remote-coding/.claude/backups
cp /Users/jack/project/remote-coding/deploy/env/.env.example /Users/jack/project/remote-coding/.claude/backups/.env.example
```

Expected: backup exists at `.claude/backups/.env.example`.

- [ ] **Step 2: Add failing settings tests**

Add this test after `test_settings_parse_claude_hook_fields` in `tests/test_auth_settings.py`:

```python
def test_settings_parse_memory_cleanup_fields_and_effective_defaults() -> None:
    settings = Settings.model_validate(
        {
            "TG_BOT_TOKEN": "token",
            "TG_ALLOWED_USER_IDS": "1",
            "DEFAULT_PROVIDER": "claude_code",
            "DEFAULT_TIMEOUT_SEC": 10,
            "MAX_CONCURRENT_TASKS": 1,
            "CLAUDE_TMUX_MODE": False,
            "CLAUDE_CLI_BIN": "claude",
            "CLAUDE_HOOK_PENDING_PERMISSION_TTL_SEC": 45,
            "CODEX_CLI_BIN": "codex",
            "GEMINI_CLI_BIN": "gemini",
            "ALLOWED_WORKDIRS": "/tmp",
            "RATE_LIMIT_WINDOW_SEC": 12,
        }
    )

    assert settings.task_store_ttl_hours == 168
    assert settings.task_store_max_records == 1000
    assert settings.effective_rate_limit_bucket_ttl_sec == 12
    assert settings.rate_limit_bucket_cleanup_interval_sec == 60
    assert settings.rate_limit_bucket_cleanup_batch_size == 50
    assert settings.effective_permission_lock_ttl_sec == 45
    assert settings.session_lock_ttl_sec == 3600
    assert settings.lock_cleanup_interval_sec == 60
    assert settings.lock_cleanup_batch_size == 50


def test_settings_accepts_explicit_memory_cleanup_overrides() -> None:
    settings = Settings.model_validate(
        {
            "TG_BOT_TOKEN": "token",
            "TG_ALLOWED_USER_IDS": "1",
            "DEFAULT_PROVIDER": "claude_code",
            "DEFAULT_TIMEOUT_SEC": 10,
            "MAX_CONCURRENT_TASKS": 1,
            "CLAUDE_TMUX_MODE": False,
            "CLAUDE_CLI_BIN": "claude",
            "CODEX_CLI_BIN": "codex",
            "GEMINI_CLI_BIN": "gemini",
            "ALLOWED_WORKDIRS": "/tmp",
            "TASK_STORE_TTL_HOURS": 24,
            "TASK_STORE_MAX_RECORDS": 10,
            "RATE_LIMIT_BUCKET_TTL_SEC": 30,
            "RATE_LIMIT_BUCKET_CLEANUP_INTERVAL_SEC": 5,
            "RATE_LIMIT_BUCKET_CLEANUP_BATCH_SIZE": 3,
            "PERMISSION_LOCK_TTL_SEC": 40,
            "SESSION_LOCK_TTL_SEC": 50,
            "LOCK_CLEANUP_INTERVAL_SEC": 6,
            "LOCK_CLEANUP_BATCH_SIZE": 4,
        }
    )

    assert settings.task_store_ttl_hours == 24
    assert settings.task_store_max_records == 10
    assert settings.effective_rate_limit_bucket_ttl_sec == 30
    assert settings.rate_limit_bucket_cleanup_interval_sec == 5
    assert settings.rate_limit_bucket_cleanup_batch_size == 3
    assert settings.effective_permission_lock_ttl_sec == 40
    assert settings.session_lock_ttl_sec == 50
    assert settings.lock_cleanup_interval_sec == 6
    assert settings.lock_cleanup_batch_size == 4


def test_settings_rejects_non_positive_memory_cleanup_fields() -> None:
    base_payload = {
        "TG_BOT_TOKEN": "token",
        "TG_ALLOWED_USER_IDS": "1",
        "DEFAULT_PROVIDER": "claude_code",
        "DEFAULT_TIMEOUT_SEC": 10,
        "MAX_CONCURRENT_TASKS": 1,
        "CLAUDE_TMUX_MODE": False,
        "CLAUDE_CLI_BIN": "claude",
        "CODEX_CLI_BIN": "codex",
        "GEMINI_CLI_BIN": "gemini",
        "ALLOWED_WORKDIRS": "/tmp",
    }

    for field in (
        "TASK_STORE_TTL_HOURS",
        "TASK_STORE_MAX_RECORDS",
        "RATE_LIMIT_BUCKET_TTL_SEC",
        "RATE_LIMIT_BUCKET_CLEANUP_INTERVAL_SEC",
        "RATE_LIMIT_BUCKET_CLEANUP_BATCH_SIZE",
        "PERMISSION_LOCK_TTL_SEC",
        "SESSION_LOCK_TTL_SEC",
        "LOCK_CLEANUP_INTERVAL_SEC",
        "LOCK_CLEANUP_BATCH_SIZE",
    ):
        with pytest.raises(ValidationError):
            Settings.model_validate({**base_payload, field: 0})
```

Extend `test_env_example_matches_supported_claude_settings` with:

```python
    assert "TASK_STORE_TTL_HOURS=168" in content
    assert "TASK_STORE_MAX_RECORDS=1000" in content
    assert "RATE_LIMIT_BUCKET_TTL_SEC=" in content
    assert "RATE_LIMIT_BUCKET_CLEANUP_INTERVAL_SEC=60" in content
    assert "RATE_LIMIT_BUCKET_CLEANUP_BATCH_SIZE=50" in content
    assert "PERMISSION_LOCK_TTL_SEC=" in content
    assert "SESSION_LOCK_TTL_SEC=3600" in content
    assert "LOCK_CLEANUP_INTERVAL_SEC=60" in content
    assert "LOCK_CLEANUP_BATCH_SIZE=50" in content
```

- [ ] **Step 3: Run settings tests and verify they fail**

Run:

```bash
pytest -q tests/test_auth_settings.py::test_settings_parse_memory_cleanup_fields_and_effective_defaults tests/test_auth_settings.py::test_settings_accepts_explicit_memory_cleanup_overrides tests/test_auth_settings.py::test_settings_rejects_non_positive_memory_cleanup_fields tests/test_auth_settings.py::test_env_example_matches_supported_claude_settings
```

Expected: FAIL with missing `task_store_ttl_hours` or missing `.env.example` entries.

- [ ] **Step 4: Add settings fields and validators**

In `app/config/settings.py`, add fields after `rate_limit_window_sec`:

```python
    task_store_ttl_hours: int = Field(168, alias="TASK_STORE_TTL_HOURS")
    task_store_max_records: int = Field(1000, alias="TASK_STORE_MAX_RECORDS")
    rate_limit_bucket_ttl_sec: int | None = Field(None, alias="RATE_LIMIT_BUCKET_TTL_SEC")
    rate_limit_bucket_cleanup_interval_sec: int = Field(60, alias="RATE_LIMIT_BUCKET_CLEANUP_INTERVAL_SEC")
    rate_limit_bucket_cleanup_batch_size: int = Field(50, alias="RATE_LIMIT_BUCKET_CLEANUP_BATCH_SIZE")
    permission_lock_ttl_sec: int | None = Field(None, alias="PERMISSION_LOCK_TTL_SEC")
    session_lock_ttl_sec: int = Field(3600, alias="SESSION_LOCK_TTL_SEC")
    lock_cleanup_interval_sec: int = Field(60, alias="LOCK_CLEANUP_INTERVAL_SEC")
    lock_cleanup_batch_size: int = Field(50, alias="LOCK_CLEANUP_BATCH_SIZE")
```

Add these names to the existing `validate_positive_int` field list:

```python
        "task_store_ttl_hours",
        "task_store_max_records",
        "rate_limit_bucket_cleanup_interval_sec",
        "rate_limit_bucket_cleanup_batch_size",
        "session_lock_ttl_sec",
        "lock_cleanup_interval_sec",
        "lock_cleanup_batch_size",
```

Add this validator below `validate_positive_int`:

```python
    @field_validator("rate_limit_bucket_ttl_sec", "permission_lock_ttl_sec")
    @classmethod
    def validate_optional_positive_int(cls, value: int | None) -> int | None:
        if value is not None and value <= 0:
            raise ValueError("配置值必须大于 0")
        return value
```

Add these properties near the existing properties:

```python
    @property
    def effective_rate_limit_bucket_ttl_sec(self) -> int:
        return self.rate_limit_bucket_ttl_sec or self.rate_limit_window_sec

    @property
    def effective_permission_lock_ttl_sec(self) -> int:
        return self.permission_lock_ttl_sec or self.claude_hook_pending_permission_ttl_sec
```

- [ ] **Step 5: Update `.env.example`**

Insert after `RATE_LIMIT_WINDOW_SEC=20` in `deploy/env/.env.example`:

```dotenv
RATE_LIMIT_BUCKET_TTL_SEC=
RATE_LIMIT_BUCKET_CLEANUP_INTERVAL_SEC=60
RATE_LIMIT_BUCKET_CLEANUP_BATCH_SIZE=50

# In-memory cleanup
TASK_STORE_TTL_HOURS=168
TASK_STORE_MAX_RECORDS=1000
PERMISSION_LOCK_TTL_SEC=
SESSION_LOCK_TTL_SEC=3600
LOCK_CLEANUP_INTERVAL_SEC=60
LOCK_CLEANUP_BATCH_SIZE=50
```

- [ ] **Step 6: Run settings tests**

Run:

```bash
pytest -q tests/test_auth_settings.py
```

Expected: PASS.

- [ ] **Step 7: Commit settings and env config**

Run:

```bash
git add app/config/settings.py deploy/env/.env.example tests/test_auth_settings.py .claude/backups/.env.example
git commit -m "$(cat <<'EOF'
feat: add memory cleanup settings
EOF
)"
```

---

### Task 5: Wire MemoryTaskStore and RateLimitMiddleware configuration

**Files:**
- Modify: `app/bootstrap.py`
- Test: `tests/test_auth_settings.py`, `tests/test_memory_task_store.py`

- [ ] **Step 1: Write failing AppContainer wiring test**

Add this test to `tests/test_auth_settings.py`:

```python
def test_app_container_uses_memory_cleanup_settings(tmp_path) -> None:
    from app.bootstrap import AppContainer

    settings = Settings.model_validate(
        {
            "TG_BOT_TOKEN": "123456:TESTTOKEN",
            "TG_ALLOWED_USER_IDS": "1",
            "DEFAULT_PROVIDER": "claude_code",
            "DEFAULT_TIMEOUT_SEC": 10,
            "MAX_CONCURRENT_TASKS": 1,
            "CLAUDE_TMUX_MODE": False,
            "TMUX_DATA_DIR": str(tmp_path),
            "CLAUDE_CLI_BIN": "claude",
            "CLAUDE_INSTALL_HOOKS": False,
            "CLAUDE_CONFIG_DIR": str(tmp_path / ".claude"),
            "CLAUDE_HOOK_SOCKET_PATH": str(tmp_path / "hook.sock"),
            "CODEX_CLI_BIN": "codex",
            "GEMINI_CLI_BIN": "gemini",
            "ALLOWED_WORKDIRS": str(tmp_path),
            "TASK_STORE_TTL_HOURS": 24,
            "TASK_STORE_MAX_RECORDS": 12,
        }
    )

    container = AppContainer(settings)

    assert container.task_store._ttl.total_seconds() == 24 * 60 * 60
    assert container.task_store._max_records == 12
```

- [ ] **Step 2: Run wiring test and verify it fails**

Run:

```bash
pytest -q tests/test_auth_settings.py::test_app_container_uses_memory_cleanup_settings
```

Expected: FAIL because `container.task_store._max_records` remains `1000`.

- [ ] **Step 3: Wire settings in AppContainer**

In `app/bootstrap.py`, replace:

```python
        self.task_store = MemoryTaskStore()
```

with:

```python
        self.task_store = MemoryTaskStore(
            max_records=settings.task_store_max_records,
            ttl_hours=settings.task_store_ttl_hours,
        )
```

In `app/bootstrap.py`, replace the `RateLimitMiddleware` construction with:

```python
        rate_limit_middleware = RateLimitMiddleware(
            limit=self.settings.rate_limit_max_requests,
            window_sec=self.settings.rate_limit_window_sec,
            bucket_ttl_sec=self.settings.effective_rate_limit_bucket_ttl_sec,
            cleanup_interval_sec=self.settings.rate_limit_bucket_cleanup_interval_sec,
            cleanup_batch_size=self.settings.rate_limit_bucket_cleanup_batch_size,
        )
```

- [ ] **Step 4: Run wiring and related tests**

Run:

```bash
pytest -q tests/test_auth_settings.py tests/test_memory_task_store.py
```

Expected: PASS.

- [ ] **Step 5: Commit bootstrap wiring for store and rate limit**

Run:

```bash
git add app/bootstrap.py tests/test_auth_settings.py
git commit -m "$(cat <<'EOF'
feat: wire memory cleanup settings
EOF
)"
```

---

### Task 6: PermissionService lock registry wiring

**Files:**
- Modify: `app/services/permission_service.py`
- Modify: `app/services/task_service.py`
- Test: `tests/test_task_service.py`, `tests/test_lock_registry.py`

- [ ] **Step 1: Add failing PermissionService wiring test**

Add this test near the existing permission tests in `tests/test_task_service.py`:

```python
@pytest.mark.asyncio
async def test_permission_service_uses_configured_lock_registry(tmp_path: Path) -> None:
    adapter = StubAdapter(events=[])
    factory = StubFactory(adapter)
    session_service = make_file_backed_session_service(tmp_path)
    structured_store = SessionStore(FileSessionStore(str(tmp_path)))
    hook_socket_server = DummyHookSocketServer()
    service = TaskService(
        settings=make_settings(tmp_path, claude_tmux_mode=True),
        task_store=MemoryTaskStore(),
        session_service=session_service,
        cli_factory=factory,
        semaphore=asyncio.Semaphore(2),
        structured_session_store=structured_store,
        hook_socket_server=hook_socket_server,
    )

    registry = service._permission_service._permission_locks

    assert registry._ttl_sec == service._settings.effective_permission_lock_ttl_sec
    assert registry._cleanup_interval_sec == service._settings.lock_cleanup_interval_sec
    assert registry._cleanup_batch_size == service._settings.lock_cleanup_batch_size
```

- [ ] **Step 2: Run PermissionService wiring test and verify it fails**

Run:

```bash
pytest -q tests/test_task_service.py::test_permission_service_uses_configured_lock_registry
```

Expected: FAIL because `_permission_locks` is still a `dict`.

- [ ] **Step 3: Update PermissionService constructor and lock usage**

In `app/services/permission_service.py`, import registry:

```python
from app.services.lock_registry import RefCountedLockRegistry
```

Replace `_permission_locks` initialization in `PermissionService.__init__` with constructor parameters and registry:

```python
        permission_lock_ttl_sec: int = 600,
        lock_cleanup_interval_sec: int = 60,
        lock_cleanup_batch_size: int = 50,
```

and inside the body:

```python
        self._permission_locks = RefCountedLockRegistry(
            ttl_sec=permission_lock_ttl_sec,
            cleanup_interval_sec=lock_cleanup_interval_sec,
            cleanup_batch_size=lock_cleanup_batch_size,
        )
```

Replace:

```python
        async with self._get_permission_lock(lock_tool_use_id):
```

with:

```python
        async with self._permission_locks.lock(lock_tool_use_id):
```

Delete the `_get_permission_lock()` method from `PermissionService`.

- [ ] **Step 4: Pass settings from TaskService**

In `app/services/task_service.py`, update `PermissionService(...)` construction to include:

```python
            permission_lock_ttl_sec=settings.effective_permission_lock_ttl_sec,
            lock_cleanup_interval_sec=settings.lock_cleanup_interval_sec,
            lock_cleanup_batch_size=settings.lock_cleanup_batch_size,
```

- [ ] **Step 5: Run permission and registry tests**

Run:

```bash
pytest -q tests/test_lock_registry.py tests/test_task_service.py::test_permission_service_uses_configured_lock_registry tests/test_task_service.py::test_respond_to_pending_permission_uses_resolved_structured_session_not_stale_context tests/test_task_service.py::test_respond_to_pending_permission_keeps_state_when_socket_response_fails
```

Expected: PASS.

- [ ] **Step 6: Commit PermissionService registry wiring**

Run:

```bash
git add app/services/permission_service.py app/services/task_service.py tests/test_task_service.py
git commit -m "$(cat <<'EOF'
feat: bound permission locks
EOF
)"
```

---

### Task 7: JSONL sync and session event lock registry wiring

**Files:**
- Modify: `app/bootstrap.py`
- Modify: `app/bootstrap_base.py`
- Modify: `app/bootstrap_mixins.py`
- Modify: `tests/test_bootstrap_hooks.py`

- [ ] **Step 1: Add failing bootstrap lock tests**

Update `tests/test_bootstrap_hooks.py::test_sync_claude_session_uses_per_session_lock` by replacing manual dict lock setup:

```python
    lock = asyncio.Lock()
    await lock.acquire()
    container._jsonl_sync_locks["claude-session-1"] = lock
```

with registry manual entry:

```python
    held_lock = container._jsonl_sync_locks.lock("claude-session-1")
    await held_lock.__aenter__()
```

and replace:

```python
    lock.release()
```

with:

```python
    await held_lock.__aexit__(None, None, None)
```

Add this new test after `test_session_end_keeps_pending_sync_until_flushed`:

```python
@pytest.mark.asyncio
async def test_session_end_cleans_event_lock_registry(tmp_path) -> None:
    container = AppContainer(make_settings(tmp_path, install_hooks=False))

    await container._dispatch_session_event(
        container.structured_session_store.process.__globals__["SessionEvent"](
            session_id="claude-session-ended",
            type=container.structured_session_store.process.__globals__["SessionEventType"].SESSION_ENDED,
            payload={"cwd": str(tmp_path)},
        )
    )

    assert len(container._session_event_locks) == 0
```

- [ ] **Step 2: Run bootstrap lock tests and verify they fail**

Run:

```bash
pytest -q tests/test_bootstrap_hooks.py::test_sync_claude_session_uses_per_session_lock tests/test_bootstrap_hooks.py::test_session_end_cleans_event_lock_registry
```

Expected: FAIL because `_jsonl_sync_locks` is still a dict and `_session_event_locks` has no registry semantics.

- [ ] **Step 3: Update AppContainer lock registry construction**

In `app/bootstrap.py`, add import:

```python
from app.services.lock_registry import RefCountedLockRegistry
```

Replace:

```python
        self._jsonl_sync_locks: dict[str, asyncio.Lock] = {}
        self._session_event_locks: dict[str, asyncio.Lock] = {}
```

with:

```python
        self._jsonl_sync_locks = RefCountedLockRegistry(
            ttl_sec=settings.session_lock_ttl_sec,
            cleanup_interval_sec=settings.lock_cleanup_interval_sec,
            cleanup_batch_size=settings.lock_cleanup_batch_size,
        )
        self._session_event_locks = RefCountedLockRegistry(
            ttl_sec=settings.session_lock_ttl_sec,
            cleanup_interval_sec=settings.lock_cleanup_interval_sec,
            cleanup_batch_size=settings.lock_cleanup_batch_size,
        )
```

- [ ] **Step 4: Update AppContainerBase type declarations**

In `app/bootstrap_base.py`, remove the top-level `import asyncio` if it is no longer needed after this change. Add import:

```python
from app.services.lock_registry import RefCountedLockRegistry
```

Replace type declarations:

```python
    _jsonl_sync_locks: dict[str, asyncio.Lock]
    _session_event_locks: dict[str, asyncio.Lock]
```

with:

```python
    _jsonl_sync_locks: RefCountedLockRegistry
    _session_event_locks: RefCountedLockRegistry
```

Keep `asyncio` imported if `_jsonl_sync_tasks: dict[str, asyncio.Task[None]]` still needs it.

- [ ] **Step 5: Update JsonlSyncMixin**

In `app/bootstrap_mixins.py`, replace `sync_claude_session()` lock acquisition:

```python
        lock = self._jsonl_sync_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
```

with:

```python
        async with self._jsonl_sync_locks.lock(session_id):
```

In `_stop_jsonl_sync_tasks()`, replace:

```python
        self._jsonl_sync_locks.clear()
```

with:

```python
        await self._jsonl_sync_locks.clear()
```

In `_debounced_sync_claude_session()` finally block, after the existing task bookkeeping, add:

```python
            if session_id not in self._jsonl_sync_requests:
                await self._jsonl_sync_locks.cleanup_key(session_id)
```

The final block should still preserve the existing behavior that reschedules when `session_id in self._jsonl_sync_requests`.

- [ ] **Step 6: Update EventDispatchMixin**

In `app/bootstrap_mixins.py`, replace `_dispatch_session_event()` with:

```python
    async def _dispatch_session_event(self, event: SessionEvent) -> None:
        async with self._session_event_locks.lock(event.session_id):
            self.structured_session_store.get_or_create(
                session_id=event.session_id,
                provider="claude_code",
                workdir=str(event.payload.get("cwd", ".")),
                claude_session_id=event.session_id,
            )
            self.structured_session_store.process(event)
        if event.type == SessionEventType.SESSION_ENDED:
            await self._session_event_locks.cleanup_key(event.session_id, require_expired=False)
```

- [ ] **Step 7: Run bootstrap tests**

Run:

```bash
pytest -q tests/test_bootstrap_hooks.py
```

Expected: PASS.

- [ ] **Step 8: Commit bootstrap lock registry wiring**

Before committing, verify the unrelated pre-existing `app/bootstrap_mixins.py` changes were preserved and that only intended hunks are staged:

```bash
git diff -- app/bootstrap_mixins.py
git add app/bootstrap.py app/bootstrap_base.py app/bootstrap_mixins.py tests/test_bootstrap_hooks.py
git diff --cached -- app/bootstrap_mixins.py
```

If the cached diff contains unrelated user changes, stop and ask for direction. If it contains only feature hunks, commit:

```bash
git commit -m "$(cat <<'EOF'
feat: bound session lock registries
EOF
)"
```

---

### Task 8: Final verification and cleanup

**Files:**
- Verify all files changed in Tasks 1-7.

- [ ] **Step 1: Run targeted tests**

Run:

```bash
pytest -q tests/test_lock_registry.py tests/test_memory_task_store.py tests/test_auth_settings.py tests/test_task_service.py::test_permission_service_uses_configured_lock_registry tests/test_bootstrap_hooks.py
```

Expected: PASS.

- [ ] **Step 2: Run full test suite**

Run:

```bash
pytest -q
```

Expected: PASS.

- [ ] **Step 3: Run lint on touched Python files**

Run:

```bash
ruff check app/services/lock_registry.py app/adapters/storage/memory.py app/bot/middleware/rate_limit.py app/config/settings.py app/services/permission_service.py app/services/task_service.py app/bootstrap.py app/bootstrap_base.py app/bootstrap_mixins.py tests/test_lock_registry.py tests/test_memory_task_store.py tests/test_auth_settings.py tests/test_task_service.py tests/test_bootstrap_hooks.py
```

Expected: PASS.

- [ ] **Step 4: Inspect git status**

Run:

```bash
git status --short
```

Expected: only intentional tracked changes are present. `_test_task21.py` remains untracked and must not be committed.

- [ ] **Step 5: Remove backup if it is no longer needed**

If `.claude/backups/.env.example` exists and the `.env.example` change is verified, remove the backup in a dedicated cleanup commit or include it in the final cleanup commit only if project policy says backups should not remain. If the backup is intentionally tracked, keep it.

Run this check:

```bash
git status --short .claude/backups/.env.example
```

Expected: decision is explicit; no accidental backup is left unstaged.

---

## Self-review

- Spec coverage: task store TTL/capacity is in Task 2; rate limit bounded queue is in Task 3; settings/env are in Task 4; AppContainer wiring is in Tasks 5 and 7; permission locks are in Task 6; JSONL/session event locks are in Task 7; tests include large task store, concurrent task store, queue reentry, registry state independence, and configuration semantics.
- Placeholder scan: no `TBD`, no empty test descriptions, no unspecified validation steps.
- Type consistency: `RefCountedLockRegistry.lock()` is used as an async context manager; registry exposes `cleanup_key()`, `cleanup_expired()`, `clear()`, `__len__()`, and `queued_count`; settings properties use `effective_rate_limit_bucket_ttl_sec` and `effective_permission_lock_ttl_sec` consistently.
