# Priority Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Repair upload queue behavior, permission callback routing, and stale in-memory state cleanup without broad refactors.

**Architecture:** Add two focused in-memory services: `UploadQueueManager` for bounded per-user upload queues and `PermissionCallbackRegistry` for short callback tokens. Inject them through `AppContainer` and `create_router`, then keep behavior changes localized to upload handlers, run streaming completion hooks, permission handlers, and lock-owning services.

**Tech Stack:** Python 3.11, asyncio, aiogram 3.x, pydantic-settings, pytest/pytest-asyncio, pyenv/pyenv-virtualenv (`.python-version` is `remote-coding`).

---

## File Structure

**Create:**
- `app/services/upload_queue.py` — bounded in-memory FIFO upload queue, per-user byte totals, drain API.
- `app/services/permission_callback_registry.py` — TTL token registry mapping short callback tokens to full `tool_use_id` values.
- `tests/test_upload_queue.py` — unit tests for queue limits, FIFO drain, disabled queue.
- `tests/test_permission_callback_registry.py` — unit tests for token length, TTL expiry, collision retry.
- `tests/test_run_event_streamer_upload_queue.py` — focused tests for scheduling queued uploads after the final task message.
- `tests/test_agent_file_watcher.py` — focused watcher cleanup tests.

**Modify:**
- `app/config/settings.py` — upload queue settings and derived byte limit.
- `deploy/env/.env.example` — document upload queue environment variables.
- `app/bootstrap.py` — instantiate and inject `UploadQueueManager` and `PermissionCallbackRegistry`; pass tmux lock cleanup settings.
- `app/bot/router.py` — thread upload queue and permission callback registry through handler registration.
- `app/bot/handlers/file_upload.py` — remove global raw queue, reject oversize metadata before download, enqueue through `UploadQueueManager`, schedule tracked background processing.
- `app/bot/handlers/command_run.py` — accept a queued-upload scheduler and pass it to `RunEventStreamer`.
- `app/bot/handlers/run_event_streamer.py` — call the scheduler immediately after each final task result is displayed.
- `app/bot/handlers/command_permission.py` — build normal permission buttons with short tokens and resolve tokens on callback.
- `app/bot/handlers/external_permission.py` — resolve `ext_perm:<token>:<decision>` callbacks.
- `app/services/unbound_permission_handler.py` — build external permission buttons with short tokens and atomically remove pending state on response/expiry.
- `app/adapters/process/tmux_runner.py` — replace persistent-session lock dictionary with `RefCountedLockRegistry`.
- Existing tests under `tests/test_file_upload_handler.py`, `tests/test_session_handlers.py`, `tests/property/test_unbound_permission_properties.py`, `tests/integration/test_external_session_pipeline.py`, `tests/test_tmux_runner.py`, `tests/test_auth_settings.py`, and `tests/test_bootstrap_hooks.py`.

---

### Task 0: Pre-flight environment check

**Files:**
- Read: `.python-version`

- [ ] **Step 1: Confirm pyenv environment**

Run:

```bash
pyenv version
```

Expected: output contains `remote-coding` and this repository path. If it does not, run this before any Python test command:

```bash
pyenv local remote-coding
```

- [ ] **Step 2: Confirm the working tree before coding**

Run:

```bash
git status --short
```

Expected: only the uncommitted plan file may appear. Do not overwrite unrelated user changes.

---

### Task 1: Settings and upload queue service

**Files:**
- Create: `app/services/upload_queue.py`
- Create: `tests/test_upload_queue.py`
- Modify: `app/config/settings.py:113-121,232-273,293-299`
- Modify: `deploy/env/.env.example:52-57`
- Modify: `tests/test_auth_settings.py:149-237`

- [ ] **Step 1: Write failing upload queue tests**

Create `tests/test_upload_queue.py` with this content:

```python
from __future__ import annotations

import pytest

from app.services.upload_queue import UploadQueueManager


@pytest.mark.asyncio
async def test_upload_queue_accepts_and_drains_fifo() -> None:
    queue = UploadQueueManager(max_files_per_user=3, max_bytes_per_user=100)

    first = await queue.enqueue(user_id=1, filename="a.txt", data=b"a")
    second = await queue.enqueue(user_id=1, filename="b.txt", data=b"bb")

    assert first.accepted is True
    assert second.accepted is True
    drained = await queue.drain(user_id=1)
    assert [(item.filename, item.data, item.size_bytes) for item in drained] == [
        ("a.txt", b"a", 1),
        ("b.txt", b"bb", 2),
    ]
    assert await queue.drain(user_id=1) == []


@pytest.mark.asyncio
async def test_upload_queue_rejects_when_file_count_limit_is_reached() -> None:
    queue = UploadQueueManager(max_files_per_user=1, max_bytes_per_user=100)

    accepted = await queue.enqueue(user_id=1, filename="a.txt", data=b"a")
    rejected = await queue.enqueue(user_id=1, filename="b.txt", data=b"b")

    assert accepted.accepted is True
    assert rejected.accepted is False
    assert "队列已满" in rejected.reason
    drained = await queue.drain(user_id=1)
    assert [item.filename for item in drained] == ["a.txt"]


@pytest.mark.asyncio
async def test_upload_queue_rejects_when_byte_limit_would_be_exceeded() -> None:
    queue = UploadQueueManager(max_files_per_user=5, max_bytes_per_user=3)

    accepted = await queue.enqueue(user_id=1, filename="a.txt", data=b"aa")
    rejected = await queue.enqueue(user_id=1, filename="b.txt", data=b"bb")

    assert accepted.accepted is True
    assert rejected.accepted is False
    assert "队列容量" in rejected.reason
    drained = await queue.drain(user_id=1)
    assert [(item.filename, item.size_bytes) for item in drained] == [("a.txt", 2)]


@pytest.mark.asyncio
async def test_upload_queue_zero_file_limit_disables_queueing() -> None:
    queue = UploadQueueManager(max_files_per_user=0, max_bytes_per_user=100)

    result = await queue.enqueue(user_id=1, filename="a.txt", data=b"a")

    assert result.accepted is False
    assert "上传队列已关闭" in result.reason
    assert await queue.drain(user_id=1) == []
```

- [ ] **Step 2: Run upload queue tests and verify they fail**

Run:

```bash
python -m pytest tests/test_upload_queue.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.upload_queue'`.

- [ ] **Step 3: Implement `UploadQueueManager`**

Create `app/services/upload_queue.py` with this content:

```python
from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class QueuedUpload:
    filename: str
    data: bytes
    size_bytes: int


@dataclass(frozen=True, slots=True)
class UploadQueueEnqueueResult:
    accepted: bool
    reason: str = ""


class UploadQueueManager:
    def __init__(self, *, max_files_per_user: int, max_bytes_per_user: int) -> None:
        if max_files_per_user < 0:
            raise ValueError("max_files_per_user must be non-negative")
        if max_bytes_per_user < 0:
            raise ValueError("max_bytes_per_user must be non-negative")
        self._max_files_per_user = max_files_per_user
        self._max_bytes_per_user = max_bytes_per_user
        self._queues: dict[int, deque[QueuedUpload]] = defaultdict(deque)
        self._byte_totals: dict[int, int] = defaultdict(int)
        self._lock = asyncio.Lock()

    async def enqueue(self, *, user_id: int, filename: str, data: bytes) -> UploadQueueEnqueueResult:
        size_bytes = len(data)
        async with self._lock:
            if self._max_files_per_user == 0:
                return UploadQueueEnqueueResult(False, "上传队列已关闭，请等待当前任务完成后重新上传。")

            queue = self._queues[user_id]
            if len(queue) >= self._max_files_per_user:
                return UploadQueueEnqueueResult(False, f"队列已满，最多允许排队 {self._max_files_per_user} 个文件。")

            current_total = self._byte_totals[user_id]
            if current_total + size_bytes > self._max_bytes_per_user:
                return UploadQueueEnqueueResult(
                    False,
                    f"队列容量不足，当前排队 {current_total} 字节，本文件 {size_bytes} 字节，上限 {self._max_bytes_per_user} 字节。",
                )

            queue.append(QueuedUpload(filename=filename, data=data, size_bytes=size_bytes))
            self._byte_totals[user_id] = current_total + size_bytes
            return UploadQueueEnqueueResult(True)

    async def drain(self, *, user_id: int) -> list[QueuedUpload]:
        async with self._lock:
            queue = self._queues.pop(user_id, deque())
            self._byte_totals.pop(user_id, None)
            return list(queue)

    async def queued_count(self, *, user_id: int) -> int:
        async with self._lock:
            return len(self._queues.get(user_id, ()))
```

- [ ] **Step 4: Run upload queue tests and verify they pass**

Run:

```bash
python -m pytest tests/test_upload_queue.py -q
```

Expected: PASS.

- [ ] **Step 5: Write failing settings tests**

In `tests/test_auth_settings.py`, extend `test_settings_new_fields_defaults`:

```python
    assert settings.upload_queue_max_files_per_user == 5
    assert settings.upload_queue_max_bytes_per_user is None
    assert settings.effective_upload_queue_max_bytes_per_user == 5 * 20 * 1024 * 1024
```

Extend `test_settings_explicit_override_new_fields` payload:

```python
        "UPLOAD_QUEUE_MAX_FILES_PER_USER": 2,
        "UPLOAD_QUEUE_MAX_BYTES_PER_USER": 1234,
```

Extend the assertions in that same test:

```python
    assert settings.upload_queue_max_files_per_user == 2
    assert settings.upload_queue_max_bytes_per_user == 1234
    assert settings.effective_upload_queue_max_bytes_per_user == 1234
```

Add this test near the other settings validation tests:

```python
def test_settings_allows_upload_queue_disabled_with_zero_files() -> None:
    settings = Settings.model_validate({**_BASE_PAYLOAD, "UPLOAD_QUEUE_MAX_FILES_PER_USER": 0})

    assert settings.upload_queue_max_files_per_user == 0
    assert settings.effective_upload_queue_max_bytes_per_user == 0


def test_settings_rejects_invalid_upload_queue_values() -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({**_BASE_PAYLOAD, "UPLOAD_QUEUE_MAX_FILES_PER_USER": -1})
    with pytest.raises(ValidationError):
        Settings.model_validate({**_BASE_PAYLOAD, "UPLOAD_QUEUE_MAX_BYTES_PER_USER": 0})
```

Extend `test_env_example_contains_new_entries`:

```python
    assert "UPLOAD_MAX_FILE_SIZE_MB=20" in content
    assert "UPLOAD_QUEUE_MAX_FILES_PER_USER=5" in content
    assert "UPLOAD_QUEUE_MAX_BYTES_PER_USER=" in content
```

- [ ] **Step 6: Run settings tests and verify they fail**

Run:

```bash
python -m pytest tests/test_auth_settings.py::test_settings_new_fields_defaults tests/test_auth_settings.py::test_settings_explicit_override_new_fields tests/test_auth_settings.py::test_settings_allows_upload_queue_disabled_with_zero_files tests/test_auth_settings.py::test_settings_rejects_invalid_upload_queue_values tests/test_auth_settings.py::test_env_example_contains_new_entries -q
```

Expected: FAIL because the new settings and `.env.example` entries do not exist yet.

- [ ] **Step 7: Implement upload queue settings**

In `app/config/settings.py`, add these fields after `upload_max_file_size_mb`:

```python
    upload_queue_max_files_per_user: int = Field(5, alias="UPLOAD_QUEUE_MAX_FILES_PER_USER")
    upload_queue_max_bytes_per_user: int | None = Field(None, alias="UPLOAD_QUEUE_MAX_BYTES_PER_USER")
```

Add this validator after `validate_positive_int`:

```python
    @field_validator("upload_queue_max_files_per_user")
    @classmethod
    def validate_non_negative_int(cls, value: int) -> int:
        if value < 0:
            raise ValueError("配置值必须大于等于 0")
        return value
```

Extend `validate_optional_positive_int` to include `upload_queue_max_bytes_per_user`:

```python
    @field_validator("rate_limit_bucket_ttl_sec", "permission_lock_ttl_sec", "upload_queue_max_bytes_per_user", mode="before")
```

Add this property after `effective_permission_lock_ttl_sec`:

```python
    @property
    def effective_upload_queue_max_bytes_per_user(self) -> int:
        if self.upload_queue_max_bytes_per_user is not None:
            return self.upload_queue_max_bytes_per_user
        return self.upload_queue_max_files_per_user * self.upload_max_file_size_mb * 1024 * 1024
```

In `deploy/env/.env.example`, add this section after line `TASK_OUTPUT_CHAR_LIMIT=120000`:

```dotenv

# File upload
UPLOAD_MAX_FILE_SIZE_MB=20
UPLOAD_QUEUE_MAX_FILES_PER_USER=5
# Blank means UPLOAD_QUEUE_MAX_FILES_PER_USER * UPLOAD_MAX_FILE_SIZE_MB * 1024 * 1024
UPLOAD_QUEUE_MAX_BYTES_PER_USER=
```

- [ ] **Step 8: Run settings and upload queue tests**

Run:

```bash
python -m pytest tests/test_upload_queue.py tests/test_auth_settings.py::test_settings_new_fields_defaults tests/test_auth_settings.py::test_settings_explicit_override_new_fields tests/test_auth_settings.py::test_settings_allows_upload_queue_disabled_with_zero_files tests/test_auth_settings.py::test_settings_rejects_invalid_upload_queue_values tests/test_auth_settings.py::test_env_example_contains_new_entries -q
```

Expected: PASS.

- [ ] **Step 9: Commit Task 1**

```bash
git add app/services/upload_queue.py app/config/settings.py deploy/env/.env.example tests/test_upload_queue.py tests/test_auth_settings.py
git commit -m "feat: add bounded upload queue settings"
```

---

### Task 2: File upload handler queue limits and pre-download size rejection

**Files:**
- Modify: `app/bot/handlers/file_upload.py:17-180`
- Modify: `app/bot/router.py:50-177`
- Modify: `app/bootstrap.py:134-139,279-296`
- Modify: `tests/test_file_upload_handler.py`
- Modify: `tests/test_auth_settings.py:425-457`

- [ ] **Step 1: Write failing file upload handler tests**

In `tests/test_file_upload_handler.py`, replace imports of `_pending_uploads` with `UploadQueueManager`:

```python
from app.services.upload_queue import UploadQueueManager
```

Add this helper below `_make_services()`:

```python
class DummyRouter:
    def __init__(self) -> None:
        self.handlers = []

    def message(self, *args, **kwargs):
        def decorator(fn):
            self.handlers.append(fn)
            return fn

        return decorator


def _register_upload_handlers(*, max_file_size_mb: int = 20, max_files: int = 5, max_bytes: int | None = None):
    from app.bot.handlers.file_upload import register_file_upload_handler

    file_receiver, session_service, task_service = _make_services()
    queue = UploadQueueManager(
        max_files_per_user=max_files,
        max_bytes_per_user=max_bytes if max_bytes is not None else max_files * max_file_size_mb * 1024 * 1024,
    )
    router = DummyRouter()
    register_file_upload_handler(
        router,
        file_receiver=file_receiver,
        session_service=session_service,
        task_service=task_service,
        upload_queue=queue,
        upload_max_file_size_mb=max_file_size_mb,
    )
    return router.handlers[0], router.handlers[1], queue, file_receiver, session_service, task_service
```

Remove the `clear_pending_uploads` fixture and add these tests:

```python
@pytest.mark.asyncio
async def test_document_size_metadata_rejects_before_download() -> None:
    document_handler, _, _, _, _, _ = _register_upload_handlers(max_file_size_mb=1)
    message = _make_message()
    message.document = MagicMock()
    message.document.file_name = "big.txt"
    message.document.file_id = "file123"
    message.document.file_size = 2 * 1024 * 1024
    bot = AsyncMock()
    message.bot = bot

    await document_handler(message)

    bot.get_file.assert_not_called()
    bot.download_file.assert_not_called()
    assert message.answer.await_args_list
    assert "文件被拒绝" in message.answer.await_args_list[0].args[0]
    assert "1 MB" in message.answer.await_args_list[0].args[0]


@pytest.mark.asyncio
async def test_photo_size_metadata_rejects_before_download() -> None:
    _, photo_handler, _, _, _, _ = _register_upload_handlers(max_file_size_mb=1)
    message = _make_message()
    photo = MagicMock()
    photo.file_unique_id = "unique-photo"
    photo.file_id = "photo123"
    photo.file_size = 2 * 1024 * 1024
    message.photo = [photo]
    bot = AsyncMock()
    message.bot = bot

    await photo_handler(message)

    bot.get_file.assert_not_called()
    bot.download_file.assert_not_called()
    assert "文件被拒绝" in message.answer.await_args_list[0].args[0]


@pytest.mark.asyncio
async def test_running_task_queue_reply_mentions_restart_loss() -> None:
    document_handler, _, queue, _, _, task_service = _register_upload_handlers(max_file_size_mb=1)
    running_task = MagicMock(spec=TaskRecord)
    running_task.status = TaskStatus.RUNNING
    task_service.list_recent = AsyncMock(return_value=[running_task])

    message = _make_message()
    message.document = MagicMock()
    message.document.file_name = "queued.txt"
    message.document.file_id = "file123"
    message.document.file_size = 10
    bot = AsyncMock()
    message.bot = bot
    file_obj = MagicMock()
    file_obj.file_path = "documents/queued.txt"
    bot.get_file = AsyncMock(return_value=file_obj)
    bot.download_file = AsyncMock(return_value=io.BytesIO(b"queued"))

    await document_handler(message)

    assert await queue.queued_count(user_id=42) == 1
    reply = message.answer.await_args_list[0].args[0]
    assert "已加入队列" in reply
    assert "bot 重启" in reply
    assert "丢失" in reply


@pytest.mark.asyncio
async def test_running_task_rejects_when_queue_count_limit_reached() -> None:
    document_handler, _, queue, _, _, task_service = _register_upload_handlers(max_file_size_mb=1, max_files=1)
    running_task = MagicMock(spec=TaskRecord)
    running_task.status = TaskStatus.RUNNING
    task_service.list_recent = AsyncMock(return_value=[running_task])
    await queue.enqueue(user_id=42, filename="existing.txt", data=b"x")

    message = _make_message()
    message.document = MagicMock()
    message.document.file_name = "second.txt"
    message.document.file_id = "file456"
    message.document.file_size = 10
    bot = AsyncMock()
    message.bot = bot
    file_obj = MagicMock()
    file_obj.file_path = "documents/second.txt"
    bot.get_file = AsyncMock(return_value=file_obj)
    bot.download_file = AsyncMock(return_value=io.BytesIO(b"second"))

    await document_handler(message)

    assert await queue.queued_count(user_id=42) == 1
    reply = message.answer.await_args_list[0].args[0]
    assert "文件未加入队列" in reply
    assert "队列已满" in reply


@pytest.mark.asyncio
async def test_running_task_rejects_downloaded_file_over_size_limit_before_queueing() -> None:
    document_handler, _, queue, _, _, task_service = _register_upload_handlers(max_file_size_mb=1)
    running_task = MagicMock(spec=TaskRecord)
    running_task.status = TaskStatus.RUNNING
    task_service.list_recent = AsyncMock(return_value=[running_task])

    message = _make_message()
    message.document = MagicMock()
    message.document.file_name = "big.txt"
    message.document.file_id = "file789"
    message.document.file_size = None
    bot = AsyncMock()
    message.bot = bot
    file_obj = MagicMock()
    file_obj.file_path = "documents/big.txt"
    bot.get_file = AsyncMock(return_value=file_obj)
    bot.download_file = AsyncMock(return_value=io.BytesIO(b"x" * (1024 * 1024 + 1)))

    await document_handler(message)

    assert await queue.queued_count(user_id=42) == 0
    reply = message.answer.await_args_list[0].args[0]
    assert "文件被拒绝" in reply
    assert "1 MB" in reply
```

- [ ] **Step 2: Run new file upload tests and verify they fail**

Run:

```bash
python -m pytest tests/test_file_upload_handler.py::test_document_size_metadata_rejects_before_download tests/test_file_upload_handler.py::test_photo_size_metadata_rejects_before_download tests/test_file_upload_handler.py::test_running_task_queue_reply_mentions_restart_loss tests/test_file_upload_handler.py::test_running_task_rejects_when_queue_count_limit_reached tests/test_file_upload_handler.py::test_running_task_rejects_downloaded_file_over_size_limit_before_queueing -q
```

Expected: FAIL because `register_file_upload_handler` does not accept `upload_queue` and `upload_max_file_size_mb`.

- [ ] **Step 3: Implement file upload handler changes**

In `app/bot/handlers/file_upload.py`, remove `_pending_uploads` and import the queue service:

```python
from app.services.upload_queue import UploadQueueManager
```

Add these helpers after `_format_size`:

```python
def _max_upload_size_bytes(upload_max_file_size_mb: int) -> int:
    return upload_max_file_size_mb * 1024 * 1024


def _metadata_exceeds_limit(file_size: int | None, *, max_size_bytes: int) -> bool:
    return file_size is not None and file_size > max_size_bytes


async def _answer_oversized(message: Message, *, filename: str, size_bytes: int, upload_max_file_size_mb: int) -> None:
    await message.answer(
        f"❌ 文件被拒绝: {filename}\n原因: 文件大小 {_format_size(size_bytes)} 超过 {upload_max_file_size_mb} MB 限制。"
    )
```

Change `process_pending_uploads` to drain from the manager:

```python
async def process_pending_uploads(
    message: Message,
    *,
    file_receiver: FileReceiverService,
    session_service: SessionService,
    upload_queue: UploadQueueManager,
    user_id: int,
) -> None:
    """Process queued uploads for a user after their task completes."""
    pending = await upload_queue.drain(user_id=user_id)
    for item in pending:
        try:
            await _process_upload(
                message,
                file_receiver=file_receiver,
                session_service=session_service,
                filename=item.filename,
                data=item.data,
            )
        except Exception:
            logger.exception("queued upload processing failed", extra={"user_id": user_id, "filename": item.filename})
```

Change `register_file_upload_handler` signature:

```python
def register_file_upload_handler(
    router: Router,
    *,
    file_receiver: FileReceiverService,
    session_service: SessionService,
    task_service: TaskService,
    upload_queue: UploadQueueManager,
    upload_max_file_size_mb: int,
) -> None:
```

At the start of `handle_document`, after `filename = ...`, add:

```python
        max_size_bytes = _max_upload_size_bytes(upload_max_file_size_mb)
        if _metadata_exceeds_limit(document.file_size, max_size_bytes=max_size_bytes):
            await _answer_oversized(
                message,
                filename=filename,
                size_bytes=document.file_size or 0,
                upload_max_file_size_mb=upload_max_file_size_mb,
            )
            return
```

After downloading document data and before queueing/direct processing, add:

```python
        if len(data) > max_size_bytes:
            await _answer_oversized(
                message,
                filename=filename,
                size_bytes=len(data),
                upload_max_file_size_mb=upload_max_file_size_mb,
            )
            return
```

Replace document queueing logic with:

```python
        if await _user_has_running_task(task_service, user_id):
            queued = await upload_queue.enqueue(user_id=user_id, filename=filename, data=data)
            if not queued.accepted:
                await message.answer(f"❌ 文件未加入队列: {filename}\n原因: {queued.reason}")
                return
            await message.answer(
                f"⏳ 任务运行中，文件 {filename} 已加入队列，将在任务完成后处理。\n"
                "注意：队列仅保存在内存中，如果 bot 在任务完成前重启，已排队文件会丢失。"
            )
            return
```

In `handle_photo`, after `filename = ...`, add the same metadata and downloaded-size checks, using `photo.file_size` and `filename`. Replace the photo queueing block with the same `upload_queue.enqueue(...)` logic.

- [ ] **Step 4: Inject upload queue through router and container**

In `app/bot/router.py`, import `UploadQueueManager` for runtime type use:

```python
from app.services.upload_queue import UploadQueueManager
```

Add `upload_queue` to `create_router` parameters:

```python
    upload_queue: UploadQueueManager | None = None,
```

Change the file upload registration condition and call:

```python
    if file_receiver is not None and upload_queue is not None:
        register_file_upload_handler(
            router,
            file_receiver=file_receiver,
            session_service=session_service,
            task_service=task_service,
            upload_queue=upload_queue,
            upload_max_file_size_mb=settings.upload_max_file_size_mb,
        )
```

In `app/bootstrap.py`, import and create the queue:

```python
from app.services.upload_queue import UploadQueueManager
```

After `self.file_receiver = FileReceiverService(...)`, add:

```python
        self.upload_queue = UploadQueueManager(
            max_files_per_user=settings.upload_queue_max_files_per_user,
            max_bytes_per_user=settings.effective_upload_queue_max_bytes_per_user,
        )
```

Pass it to `create_router`:

```python
            upload_queue=self.upload_queue,
```

- [ ] **Step 5: Add container wiring assertion**

In `tests/test_auth_settings.py::test_container_wiring_passes_settings_to_task_store`, after `container = AppContainer(settings)`, add:

```python
    assert container.upload_queue._max_files_per_user == settings.upload_queue_max_files_per_user
    assert container.upload_queue._max_bytes_per_user == settings.effective_upload_queue_max_bytes_per_user
```

- [ ] **Step 6: Run file upload and container tests**

Run:

```bash
python -m pytest tests/test_file_upload_handler.py tests/test_auth_settings.py::test_container_wiring_passes_settings_to_task_store -q
```

Expected: PASS.

- [ ] **Step 7: Commit Task 2**

```bash
git add app/bot/handlers/file_upload.py app/bot/router.py app/bootstrap.py tests/test_file_upload_handler.py tests/test_auth_settings.py
git commit -m "fix: bound queued upload memory usage"
```

---

### Task 3: Background queued upload processing after final task messages

**Files:**
- Modify: `app/bot/handlers/file_upload.py`
- Modify: `app/bot/handlers/run_event_streamer.py:85-113,224-298`
- Modify: `app/bot/handlers/command_run.py:43-180,183-209`
- Modify: `app/bot/router.py:107-177,218-228`
- Create: `tests/test_run_event_streamer_upload_queue.py`

- [ ] **Step 1: Write failing streamer scheduling tests**

Create `tests/test_run_event_streamer_upload_queue.py`:

```python
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.bot.handlers.command_run import run_prompt_and_stream
from app.bot.presenters.chunk_sender import ChunkSender
from app.domain.file_models import FileUploadResult, FileValidationError
from app.domain.models import CLIEvent, EventType, TaskRecord, TaskStatus
from app.services.upload_queue import UploadQueueManager
from tests.fakes.telegram import DummyMessage


class DummyTaskService:
    def __init__(self, events: list[CLIEvent], status: TaskRecord) -> None:
        self._events = events
        self._status = status

    async def create_and_run(self, *, user_id: int, provider: str | None, prompt: str, workdir: str | None = None):
        task = SimpleNamespace(
            task_id="task-queued-1",
            provider="claude_code",
            session_id="session-1",
            workdir=workdir or "/tmp/work",
            started_at=None,
            created_at=None,
        )
        return SimpleNamespace(task=task, events=self._stream(), interactive=False)

    async def get_status(self, task_id: str, user_id: int):
        return self._status

    async def get_structured_session(self, user_id: int, *, log_missing: bool = True):
        return None

    async def get_structured_session_for_task(self, *, task_id: str, user_id: int, log_missing: bool = True):
        return None

    async def get_structured_session_cursor(self, user_id: int, *, task_id: str | None = None) -> int:
        return 0

    async def get_structured_reply_cursor(self, user_id: int, *, task_id: str | None = None):
        return None, None

    async def acknowledge_structured_reply(self, user_id: int, **kwargs) -> None:
        return None

    async def get_structured_user_question_cursor(self, user_id: int, *, task_id: str | None = None):
        return None

    async def acknowledge_structured_user_question(self, user_id: int, **kwargs) -> None:
        return None

    async def wait_for_structured_session_update(self, **kwargs) -> bool:
        await asyncio.sleep(0.01)
        return False

    async def _stream(self):
        for event in self._events:
            yield event


@pytest.mark.asyncio
async def test_queued_upload_scheduler_runs_after_success_message_is_displayed(tmp_path: Path) -> None:
    events = [CLIEvent(type=EventType.STARTED, task_id="task-queued-1"), CLIEvent(type=EventType.EXITED, task_id="task-queued-1", exit_code=0)]
    status = TaskRecord(
        task_id="task-queued-1",
        session_id="session-1",
        user_id=1,
        provider="claude_code",
        prompt="hello",
        workdir=str(tmp_path),
        timeout_sec=60,
        status=TaskStatus.SUCCEEDED,
    )
    service = DummyTaskService(events=events, status=status)
    message = DummyMessage()
    scheduler_calls: list[tuple[int, str]] = []

    def queued_upload_scheduler(root_message, user_id: int) -> None:
        scheduler_calls.append((user_id, root_message.sent_messages[0].text))

    task = await run_prompt_and_stream(
        message=message,
        task_service=service,
        sender_factory=lambda: ChunkSender(chunk_size=50, flush_interval_sec=0.01),
        user_id=1,
        provider="claude_code",
        prompt="hello",
        workdir=str(tmp_path),
        queued_upload_scheduler=queued_upload_scheduler,
    )
    assert task is not None
    await task

    assert scheduler_calls == [(1, message.sent_messages[0].text)]
    assert "✅ 完成" in scheduler_calls[0][1]


@pytest.mark.asyncio
async def test_queued_upload_processing_continues_after_failed_file(tmp_path: Path) -> None:
    from app.bot.handlers.file_upload import schedule_pending_upload_processing

    queue = UploadQueueManager(max_files_per_user=5, max_bytes_per_user=100)
    await queue.enqueue(user_id=1, filename="bad.exe", data=b"bad")
    await queue.enqueue(user_id=1, filename="good.txt", data=b"good")

    message = DummyMessage(user_id=1)
    session_service = AsyncMock()
    session = SimpleNamespace(workdir=str(tmp_path))
    session_service.get = AsyncMock(return_value=session)
    file_receiver = AsyncMock()
    file_receiver.receive_file = AsyncMock(
        side_effect=[
            FileValidationError(filename="bad.exe", reason="Extension .exe is not allowed."),
            FileUploadResult(filename="good.txt", size_bytes=4, path=tmp_path / ".tg-uploads" / "1" / "good.txt"),
        ]
    )

    task = schedule_pending_upload_processing(
        message,
        file_receiver=file_receiver,
        session_service=session_service,
        upload_queue=queue,
        user_id=1,
    )
    await task

    assert [call.args[0] for call in message.answer.await_args_list] == [
        "❌ 文件被拒绝: bad.exe\n原因: Extension .exe is not allowed.",
        "✅ 文件已接收: good.txt (4 B)",
    ]
    assert await queue.drain(user_id=1) == []
```

- [ ] **Step 2: Run streamer scheduling tests and verify they fail**

Run:

```bash
python -m pytest tests/test_run_event_streamer_upload_queue.py -q
```

Expected: FAIL because `queued_upload_scheduler` and `schedule_pending_upload_processing` do not exist.

- [ ] **Step 3: Add tracked upload processing scheduler**

In `app/bot/handlers/file_upload.py`, add imports:

```python
import asyncio
```

Add this module-level set after `logger = logging.getLogger(__name__)`:

```python
_ACTIVE_UPLOAD_TASKS: set[asyncio.Task[None]] = set()
```

Add this function after `process_pending_uploads`:

```python
def schedule_pending_upload_processing(
    message: Message,
    *,
    file_receiver: FileReceiverService,
    session_service: SessionService,
    upload_queue: UploadQueueManager,
    user_id: int,
) -> asyncio.Task[None]:
    task = asyncio.create_task(
        process_pending_uploads(
            message,
            file_receiver=file_receiver,
            session_service=session_service,
            upload_queue=upload_queue,
            user_id=user_id,
        )
    )
    _ACTIVE_UPLOAD_TASKS.add(task)

    def _on_done(done_task: asyncio.Task[None]) -> None:
        _ACTIVE_UPLOAD_TASKS.discard(done_task)
        if done_task.cancelled():
            return
        exc = done_task.exception()
        if exc is None:
            return
        logger.error(
            "queued upload background task failed",
            extra={"user_id": user_id, "error": str(exc)},
            exc_info=(type(exc), exc, exc.__traceback__),
        )

    task.add_done_callback(_on_done)
    return task
```

- [ ] **Step 4: Thread scheduler into run streaming**

In `app/bot/handlers/run_event_streamer.py`, import `Callable`:

```python
from collections.abc import Callable
```

Add a constructor parameter and field:

```python
        queued_upload_scheduler: Callable[[], None] | None = None,
```

```python
        self._queued_upload_scheduler = queued_upload_scheduler
        self._queued_upload_scheduled = False
```

Add this method before `stream_events`:

```python
    def _schedule_queued_uploads_once(self) -> None:
        if self._queued_upload_scheduled or self._queued_upload_scheduler is None:
            return
        self._queued_upload_scheduled = True
        try:
            self._queued_upload_scheduler()
        except Exception:
            logger.exception("failed to schedule queued upload processing", extra={"user_id": self._user_id})
```

In the `EXITED` branch, immediately after sending/editing `success_msg`, add:

```python
                    self._schedule_queued_uploads_once()
```

In the `{FAILED, TIMEOUT, CANCELED}` branch, immediately after sending/editing `error_msg`, add:

```python
                    self._schedule_queued_uploads_once()
```

In `app/bot/handlers/command_run.py`, import `Callable` and add a parameter to `run_prompt_and_stream`:

```python
from collections.abc import Callable
```

```python
    queued_upload_scheduler: Callable[[Message, int], None] | None = None,
```

Pass this to `RunEventStreamer`:

```python
        queued_upload_scheduler=(
            (lambda: queued_upload_scheduler(message, user_id)) if queued_upload_scheduler is not None else None
        ),
```

Add `queued_upload_scheduler` to `register_run_handler` and pass it through to `run_prompt_and_stream`.

In `app/bot/router.py`, import the scheduler:

```python
from app.bot.handlers.file_upload import register_file_upload_handler, schedule_pending_upload_processing
```

Before `register_run_handler(...)`, build a scheduler only when upload dependencies exist:

```python
    queued_upload_scheduler = None
    if file_receiver is not None and upload_queue is not None:
        queued_upload_scheduler = lambda message, user_id: schedule_pending_upload_processing(
            message,
            file_receiver=file_receiver,
            session_service=session_service,
            upload_queue=upload_queue,
            user_id=user_id,
        )
```

Pass it to `register_run_handler`:

```python
        queued_upload_scheduler=queued_upload_scheduler,
```

Pass it to the plain text Claude chat `run_prompt_and_stream(...)` call:

```python
            queued_upload_scheduler=queued_upload_scheduler,
```

- [ ] **Step 5: Run upload streaming tests**

Run:

```bash
python -m pytest tests/test_run_event_streamer_upload_queue.py tests/test_run_event_streamer_diff.py tests/test_command_run.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit Task 3**

```bash
git add app/bot/handlers/file_upload.py app/bot/handlers/run_event_streamer.py app/bot/handlers/command_run.py app/bot/router.py tests/test_run_event_streamer_upload_queue.py
git commit -m "fix: process queued uploads after task completion"
```

---

### Task 4: Permission callback token registry

**Files:**
- Create: `app/services/permission_callback_registry.py`
- Create: `tests/test_permission_callback_registry.py`

- [ ] **Step 1: Write failing registry tests**

Create `tests/test_permission_callback_registry.py`:

```python
from __future__ import annotations

from app.services.permission_callback_registry import PermissionCallbackRegistry


def test_registry_resolves_full_tool_use_id_from_short_token() -> None:
    registry = PermissionCallbackRegistry(ttl_sec=60, token_factory=lambda: "abc12345", clock=lambda: 100.0)
    tool_use_id = "toolu_" + "x" * 200

    token = registry.register(tool_use_id)

    assert token == "abc12345"
    assert registry.resolve(token) == tool_use_id
    assert len(token.encode("utf-8")) < len(tool_use_id.encode("utf-8"))


def test_registry_expires_tokens() -> None:
    now = 100.0

    def clock() -> float:
        return now

    registry = PermissionCallbackRegistry(ttl_sec=10, token_factory=lambda: "token001", clock=clock)
    token = registry.register("tool-1")

    now = 111.0

    assert registry.resolve(token) is None


def test_registry_retries_live_token_collision() -> None:
    tokens = iter(["same001", "same001", "next002"])
    registry = PermissionCallbackRegistry(ttl_sec=60, token_factory=lambda: next(tokens), clock=lambda: 100.0)

    first = registry.register("tool-1")
    second = registry.register("tool-2")

    assert first == "same001"
    assert second == "next002"
    assert registry.resolve(first) == "tool-1"
    assert registry.resolve(second) == "tool-2"
```

- [ ] **Step 2: Run registry tests and verify they fail**

Run:

```bash
python -m pytest tests/test_permission_callback_registry.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.permission_callback_registry'`.

- [ ] **Step 3: Implement registry**

Create `app/services/permission_callback_registry.py`:

```python
from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class _PermissionCallbackEntry:
    tool_use_id: str
    expires_at: float


class PermissionCallbackRegistry:
    def __init__(
        self,
        *,
        ttl_sec: int,
        token_factory: Callable[[], str] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if ttl_sec <= 0:
            raise ValueError("ttl_sec must be positive")
        self._ttl_sec = ttl_sec
        self._token_factory = token_factory or (lambda: secrets.token_urlsafe(6))
        self._clock = clock or time.monotonic
        self._entries: dict[str, _PermissionCallbackEntry] = {}

    def register(self, tool_use_id: str) -> str:
        self._prune_expired()
        for _ in range(16):
            token = self._token_factory()
            if token not in self._entries:
                self._entries[token] = _PermissionCallbackEntry(
                    tool_use_id=tool_use_id,
                    expires_at=self._clock() + self._ttl_sec,
                )
                return token
        raise RuntimeError("failed to generate unique permission callback token")

    def resolve(self, token: str) -> str | None:
        self._prune_expired()
        entry = self._entries.get(token)
        if entry is None:
            return None
        return entry.tool_use_id

    def _prune_expired(self) -> None:
        now = self._clock()
        expired = [token for token, entry in self._entries.items() if entry.expires_at <= now]
        for token in expired:
            self._entries.pop(token, None)
```

- [ ] **Step 4: Run registry tests**

Run:

```bash
python -m pytest tests/test_permission_callback_registry.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit Task 4**

```bash
git add app/services/permission_callback_registry.py tests/test_permission_callback_registry.py
git commit -m "feat: add permission callback token registry"
```

---

### Task 5: Normal permission callbacks use short tokens

**Files:**
- Modify: `app/bot/handlers/command_permission.py:18-202`
- Modify: `app/bot/router.py:50-124`
- Modify: `app/bootstrap.py:101-107,199-204,279-296`
- Modify: `tests/test_session_handlers.py:250-397`
- Modify: `tests/test_bootstrap_hooks.py`

- [ ] **Step 1: Write failing normal permission callback tests**

In `tests/test_session_handlers.py`, import the registry:

```python
from app.services.permission_callback_registry import PermissionCallbackRegistry
```

Add this test before `test_permission_callback_handler_approves_pending_request`:

```python
def test_permission_callback_data_uses_short_token_for_long_tool_use_id() -> None:
    from app.bot.handlers.command_permission import build_permission_callback_data, build_permission_keyboard

    registry = PermissionCallbackRegistry(ttl_sec=60, token_factory=lambda: "tok12345", clock=lambda: 100.0)
    long_tool_use_id = "toolu_" + "x" * 200

    keyboard = build_permission_keyboard(tool_use_id=long_tool_use_id, permission_callback_registry=registry)
    callback_data = [button.callback_data for row in keyboard.inline_keyboard for button in row]

    assert callback_data == [
        "perm:allow:tok12345",
        "perm:deny:tok12345",
        "perm:auto_approve:tok12345",
    ]
    assert all(data is not None and len(data.encode("utf-8")) <= 64 for data in callback_data)
    assert registry.resolve("tok12345") == long_tool_use_id
    assert build_permission_callback_data(decision="allow", token="tok12345") == "perm:allow:tok12345"
```

Update `test_permission_callback_handler_approves_pending_request` registration and callback setup:

```python
    registry = PermissionCallbackRegistry(ttl_sec=60, token_factory=lambda: "tok12345", clock=lambda: 100.0)
    token = registry.register("tool-1")
    router = DummyRouter()
    register_permission_handlers(router, task_service=service, permission_callback_registry=registry)
    callback_handler = router.callback_handlers[0]
    message = DummyMessage("权限请求")
    callback = DummyCallbackQuery(f"perm:allow:{token}", message=message)
```

Update `test_permission_callback_handler_rejects_stale_button` to pass an empty registry and assert the new recovery message:

```python
    registry = PermissionCallbackRegistry(ttl_sec=60, token_factory=lambda: "tok12345", clock=lambda: 100.0)
    router = DummyRouter()
    register_permission_handlers(router, task_service=service, permission_callback_registry=registry)
    callback_handler = router.callback_handlers[0]
    message = DummyMessage("权限请求")
    callback = DummyCallbackQuery("perm:allow:missing", message=message)
```

Replace its assertions:

```python
    assert hook_socket_server.calls == []
    assert "权限按钮已失效" in message.answers[0]
    assert "重新触发" in message.answers[0]
    assert message.edited_reply_markups == []
    assert callback.answers == [(message.answers[0], True)]
```

Update `test_permission_callback_handler_rejects_cross_user_button` to register `tool-1` and pass the token:

```python
    registry = PermissionCallbackRegistry(ttl_sec=60, token_factory=lambda: "tok12345", clock=lambda: 100.0)
    token = registry.register("tool-1")
    router = DummyRouter()
    register_permission_handlers(router, task_service=service, permission_callback_registry=registry)
    callback_handler = router.callback_handlers[0]
    message = DummyMessage("权限请求", user_id=2)
    callback = DummyCallbackQuery(f"perm:allow:{token}", user_id=2, message=message)
```

- [ ] **Step 2: Run normal permission tests and verify they fail**

Run:

```bash
python -m pytest tests/test_session_handlers.py::test_permission_callback_data_uses_short_token_for_long_tool_use_id tests/test_session_handlers.py::test_permission_callback_handler_approves_pending_request tests/test_session_handlers.py::test_permission_callback_handler_rejects_stale_button tests/test_session_handlers.py::test_permission_callback_handler_rejects_cross_user_button -q
```

Expected: FAIL because handler signatures and callback parsing still use `tool_use_id` directly.

- [ ] **Step 3: Implement normal callback token use**

In `app/bot/handlers/command_permission.py`, import the registry:

```python
from app.services.permission_callback_registry import PermissionCallbackRegistry
```

Add stale text near constants:

```python
_STALE_PERMISSION_CALLBACK_TEXT = "权限按钮已失效：请求可能已过期或 bot 已重启。请重新触发操作，或等待 Claude 再次请求权限。"
```

Replace `build_permission_callback_data`:

```python
def build_permission_callback_data(*, decision: str, token: str) -> str:
    return f"{_PERMISSION_CALLBACK_PREFIX}:{decision}:{token}"
```

Keep `parse_permission_callback_data` but rename the parsed third value in local variables to `token`:

```python
    decision, sep, token = rest.partition(":")
    if not sep or decision not in {"allow", "deny", "auto_approve"} or not token:
        return None
    return decision, token
```

Change `build_permission_keyboard`:

```python
def build_permission_keyboard(*, tool_use_id: str, permission_callback_registry: PermissionCallbackRegistry) -> InlineKeyboardMarkup:
    token = permission_callback_registry.register(tool_use_id)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="允许",
                    callback_data=build_permission_callback_data(decision="allow", token=token),
                ),
                InlineKeyboardButton(
                    text="拒绝",
                    callback_data=build_permission_callback_data(decision="deny", token=token),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="不再询问，全部允许",
                    callback_data=build_permission_callback_data(decision="auto_approve", token=token),
                ),
            ],
        ]
    )
```

Add `permission_callback_registry` to `register_permission_handlers` parameters:

```python
    permission_callback_registry: PermissionCallbackRegistry,
```

In `callback_permission`, resolve token before calling services:

```python
        decision, token = parsed
        tool_use_id = permission_callback_registry.resolve(token)
        if tool_use_id is None:
            if callback.message is not None:
                await callback.message.answer(_STALE_PERMISSION_CALLBACK_TEXT)
            await callback.answer(_STALE_PERMISSION_CALLBACK_TEXT, show_alert=True)
            return
```

Remove the prefix-matching loop from `_resolve_session_id_for_tool_use_id`; the fallback should only check the exact key:

```python
        if hook_socket_server is not None:
            async with hook_socket_server._lock:
                pending = hook_socket_server._pending_permissions.get(tool_use_id)
                if pending is not None:
                    return pending.session_id
```

- [ ] **Step 4: Inject registry through router and container**

In `app/bot/router.py`, import the registry and add a parameter:

```python
from app.services.permission_callback_registry import PermissionCallbackRegistry
```

```python
    permission_callback_registry: PermissionCallbackRegistry | None = None,
```

Only register normal permission handlers when the registry exists:

```python
    if permission_callback_registry is not None:
        register_permission_handlers(
            router,
            task_service=task_service,
            auto_approve_service=auto_approve_service,
            hook_socket_server=hook_socket_server,
            structured_session_store=structured_session_store,
            permission_callback_registry=permission_callback_registry,
        )
```

In `app/bootstrap.py`, import and instantiate:

```python
from app.services.permission_callback_registry import PermissionCallbackRegistry
```

After `self.hook_socket_server = HookSocketServer(...)`, add:

```python
        self.permission_callback_registry = PermissionCallbackRegistry(
            ttl_sec=settings.claude_hook_pending_permission_ttl_sec,
        )
```

Pass it to `create_router`:

```python
            permission_callback_registry=self.permission_callback_registry,
```

- [ ] **Step 5: Update bootstrap hook tests**

In `tests/test_bootstrap_hooks.py::test_container_uses_independent_session_lock_registries`, add:

```python
    assert container.permission_callback_registry._ttl_sec == settings.claude_hook_pending_permission_ttl_sec
```

- [ ] **Step 6: Run normal permission tests**

Run:

```bash
python -m pytest tests/test_permission_callback_registry.py tests/test_session_handlers.py::test_permission_callback_data_uses_short_token_for_long_tool_use_id tests/test_session_handlers.py::test_permission_callback_handler_approves_pending_request tests/test_session_handlers.py::test_permission_callback_handler_rejects_stale_button tests/test_session_handlers.py::test_permission_callback_handler_rejects_cross_user_button tests/test_bootstrap_hooks.py::test_container_uses_independent_session_lock_registries -q
```

Expected: PASS.

- [ ] **Step 7: Commit Task 5**

```bash
git add app/bot/handlers/command_permission.py app/bot/router.py app/bootstrap.py tests/test_session_handlers.py tests/test_bootstrap_hooks.py
git commit -m "fix: route permission callbacks through short tokens"
```

---

### Task 6: External permission tokens and unbound pending cleanup

**Files:**
- Modify: `app/services/unbound_permission_handler.py:22-247`
- Modify: `app/bot/handlers/external_permission.py:18-126`
- Modify: `app/bot/router.py:162-169`
- Modify: `app/bootstrap.py:199-204,279-296`
- Modify: `tests/property/test_unbound_permission_properties.py`
- Modify: `tests/integration/test_external_session_pipeline.py`

- [ ] **Step 1: Write failing unbound cleanup and external token tests**

In `tests/property/test_unbound_permission_properties.py`, import the registry:

```python
from app.services.permission_callback_registry import PermissionCallbackRegistry
```

Add this helper near the top:

```python
def _registry(token: str = "tok12345") -> PermissionCallbackRegistry:
    return PermissionCallbackRegistry(ttl_sec=60, token_factory=lambda: token, clock=lambda: 100.0)
```

Update every `UnboundPermissionHandler(...)` construction in this file to include:

```python
            permission_callback_registry=_registry(),
```

Add these tests near the first-responder tests:

```python
@pytest.mark.asyncio
async def test_response_removes_unbound_pending_and_expiry_task() -> None:
    bot = MagicMock()
    bot.send_message = AsyncMock()
    hook_socket_server = MagicMock()
    hook_socket_server.respond_to_permission = AsyncMock(return_value=True)
    handler = UnboundPermissionHandler(
        bot=bot,
        hook_socket_server=hook_socket_server,
        allowed_user_ids={1},
        permission_callback_registry=_registry(),
    )
    event = HookEvent(session_id="sess", cwd="/tmp/project", event="PermissionRequest", status="waiting_for_approval", tool="Bash", tool_use_id="tool-1")

    await handler.handle_unbound_permission(event)
    result = await handler.handle_response(tool_use_id="tool-1", user_id=1, decision="allow")

    assert result.accepted is True
    assert result.forwarded is True
    assert handler.is_unbound_permission("tool-1") is False
    assert handler._pending == {}
    assert handler._expiry_tasks == {}


@pytest.mark.asyncio
async def test_expiry_removes_unbound_pending_and_expiry_task() -> None:
    bot = MagicMock()
    bot.send_message = AsyncMock()
    hook_socket_server = MagicMock()
    hook_socket_server.respond_to_permission = AsyncMock(return_value=True)
    handler = UnboundPermissionHandler(
        bot=bot,
        hook_socket_server=hook_socket_server,
        allowed_user_ids={1},
        permission_ttl_sec=0,
        permission_callback_registry=_registry(),
    )
    event = HookEvent(session_id="sess", cwd="/tmp/project", event="PermissionRequest", status="waiting_for_approval", tool="Bash", tool_use_id="tool-expire")

    await handler.handle_unbound_permission(event)
    await asyncio.sleep(0.05)

    assert handler.is_unbound_permission("tool-expire") is False
    assert handler._pending == {}
    assert handler._expiry_tasks == {}


@pytest.mark.asyncio
async def test_concurrent_unbound_responses_preserve_first_responder_wins() -> None:
    bot = MagicMock()
    bot.send_message = AsyncMock()
    release = asyncio.Event()
    hook_socket_server = MagicMock()

    async def respond_to_permission(**kwargs):
        await release.wait()
        return True

    hook_socket_server.respond_to_permission = AsyncMock(side_effect=respond_to_permission)
    handler = UnboundPermissionHandler(
        bot=bot,
        hook_socket_server=hook_socket_server,
        allowed_user_ids={1, 2},
        permission_callback_registry=_registry(),
    )
    event = HookEvent(session_id="sess", cwd="/tmp/project", event="PermissionRequest", status="waiting_for_approval", tool="Bash", tool_use_id="tool-race")
    await handler.handle_unbound_permission(event)

    first = asyncio.create_task(handler.handle_response(tool_use_id="tool-race", user_id=1, decision="allow"))
    second = asyncio.create_task(handler.handle_response(tool_use_id="tool-race", user_id=2, decision="deny"))
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.gather(first, second)

    assert sum(1 for result in results if result.accepted) == 1
    assert hook_socket_server.respond_to_permission.await_count == 1
    assert handler._pending == {}
```

In `tests/integration/test_external_session_pipeline.py`, update handler construction to pass a registry, and add this test:

```python
@pytest.mark.asyncio
async def test_unbound_permission_keyboard_uses_external_short_token() -> None:
    mock_bot = AsyncMock()
    mock_hook_socket = AsyncMock()
    registry = PermissionCallbackRegistry(ttl_sec=60, token_factory=lambda: "tok12345", clock=lambda: 100.0)
    handler = UnboundPermissionHandler(
        bot=mock_bot,
        hook_socket_server=mock_hook_socket,
        allowed_user_ids={100},
        permission_callback_registry=registry,
    )
    event = _make_hook_event(
        session_id="sess-unbound01",
        cwd="/tmp/project",
        event="PermissionRequest",
        status="waiting_for_approval",
        tool="Write",
        tool_use_id="toolu_" + "x" * 200,
    )

    await handler.handle_unbound_permission(event)

    markup = mock_bot.send_message.await_args.kwargs["reply_markup"]
    callback_data = [button.callback_data for row in markup.inline_keyboard for button in row]
    assert callback_data == [
        "ext_perm:tok12345:allow",
        "ext_perm:tok12345:deny",
        "ext_perm:tok12345:auto_approve",
    ]
    assert all(data is not None and len(data.encode("utf-8")) <= 64 for data in callback_data)
    assert registry.resolve("tok12345") == event.tool_use_id
```

- [ ] **Step 2: Run unbound tests and verify they fail**

Run:

```bash
python -m pytest tests/property/test_unbound_permission_properties.py tests/integration/test_external_session_pipeline.py::test_unbound_permission_keyboard_uses_external_short_token -q
```

Expected: FAIL because `UnboundPermissionHandler` does not accept `permission_callback_registry`, still returns bool from `handle_response`, and still embeds truncated IDs.

- [ ] **Step 3: Implement unbound token keyboard and response result**

In `app/services/unbound_permission_handler.py`, import dataclass and registry:

```python
from dataclasses import dataclass
from app.services.permission_callback_registry import PermissionCallbackRegistry
```

Add this result type above `UnboundPermissionHandler`:

```python
@dataclass(frozen=True, slots=True)
class UnboundPermissionResponseResult:
    accepted: bool
    forwarded: bool
```

Add constructor parameter and fields:

```python
        permission_callback_registry: PermissionCallbackRegistry,
```

```python
        self._permission_callback_registry = permission_callback_registry
        self._state_lock = asyncio.Lock()
```

In `handle_unbound_permission`, wrap pending and expiry mutations:

```python
        async with self._state_lock:
            self._pending[tool_use_id] = state
            self._cancel_expiry_task_locked(tool_use_id)
            self._expiry_tasks[tool_use_id] = asyncio.create_task(self._expire_permission(tool_use_id))
```

Replace `handle_response`:

```python
    async def handle_response(self, *, tool_use_id: str, user_id: int, decision: str) -> UnboundPermissionResponseResult:
        async with self._state_lock:
            state = self._pending.pop(tool_use_id, None)
            if state is None or state.responded:
                return UnboundPermissionResponseResult(accepted=False, forwarded=False)
            state.responded = True
            state.responded_by = user_id
            self._cancel_expiry_task_locked(tool_use_id)

        forwarded = await self._hook_socket_server.respond_to_permission(
            tool_use_id=tool_use_id,
            decision=decision,
            reason=f"responded by user {user_id}",
        )
        if not forwarded:
            logger.warning(
                "unbound permission response forwarding failed after claim",
                extra={"tool_use_id": tool_use_id, "user_id": user_id, "decision": decision, "session_id": state.session_id},
            )
        else:
            logger.info(
                "unbound permission responded",
                extra={"tool_use_id": tool_use_id, "user_id": user_id, "decision": decision, "session_id": state.session_id},
            )
        return UnboundPermissionResponseResult(accepted=True, forwarded=forwarded)
```

Replace `_expire_permission` cleanup path:

```python
        async with self._state_lock:
            state = self._pending.pop(tool_use_id, None)
            self._expiry_tasks.pop(tool_use_id, None)
            if state is None or state.responded:
                return
            state.responded = True

        await self._hook_socket_server.respond_to_permission(
            tool_use_id=tool_use_id,
            decision="deny",
            reason="no user responded within TTL",
        )
```

Replace `_build_permission_keyboard`:

```python
    def _build_permission_keyboard(self, tool_use_id: str) -> InlineKeyboardMarkup:
        token = self._permission_callback_registry.register(tool_use_id)
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ Approve", callback_data=f"ext_perm:{token}:allow"),
                    InlineKeyboardButton(text="❌ Deny", callback_data=f"ext_perm:{token}:deny"),
                ],
                [
                    InlineKeyboardButton(text="🟢 Auto-approve All", callback_data=f"ext_perm:{token}:auto_approve"),
                ],
            ]
        )
```

Replace `_cancel_expiry_task` with a locked helper and a public helper:

```python
    def _cancel_expiry_task_locked(self, tool_use_id: str) -> None:
        task = self._expiry_tasks.pop(tool_use_id, None)
        if task is not None:
            task.cancel()

    def _cancel_expiry_task(self, tool_use_id: str) -> None:
        task = self._expiry_tasks.pop(tool_use_id, None)
        if task is not None:
            task.cancel()
```

The public helper remains for existing synchronous call sites if any remain; new locked mutations use `_cancel_expiry_task_locked`.

- [ ] **Step 4: Implement external callback token resolution**

In `app/bot/handlers/external_permission.py`, import the registry:

```python
from app.services.permission_callback_registry import PermissionCallbackRegistry
```

Add a parameter to `register_external_permission_handler`:

```python
    permission_callback_registry: PermissionCallbackRegistry,
```

Add stale text after `logger`:

```python
_STALE_EXTERNAL_PERMISSION_CALLBACK_TEXT = "Permission button expired or bot restarted. Trigger the action again or wait for Claude to request permission again."
```

After parsing `parts`, resolve token:

```python
        _, token, decision = parts
        if decision not in ("allow", "deny", "auto_approve"):
            await callback.answer("Invalid decision", show_alert=True)
            return
        tool_use_id = permission_callback_registry.resolve(token)
        if tool_use_id is None:
            await callback.answer(_STALE_EXTERNAL_PERMISSION_CALLBACK_TEXT, show_alert=True)
            return
```

Update unbound response handling for the new result object:

```python
                result = await unbound_permission_handler.handle_response(
                    tool_use_id=tool_use_id,
                    user_id=user_id,
                    decision="allow",
                )
                if not result.accepted:
                    await callback.answer("Already responded by another user", show_alert=True)
                    return
                if not result.forwarded:
                    await callback.answer("Permission request expired or not found", show_alert=True)
                    return
```

Apply the same `result.accepted` / `result.forwarded` checks in the non-auto-approve unbound path.

- [ ] **Step 5: Inject registry into unbound and external handlers**

In `app/bootstrap.py`, pass the existing registry to `UnboundPermissionHandler`:

```python
            permission_callback_registry=self.permission_callback_registry,
```

In `app/bot/router.py`, require the registry for external permission handler registration:

```python
    if hook_socket_server is not None and unbound_permission_handler is not None and permission_callback_registry is not None:
        register_external_permission_handler(
            router,
            hook_socket_server=hook_socket_server,
            unbound_permission_handler=unbound_permission_handler,
            external_uq_state=external_uq_state,
            auto_approve_service=auto_approve_service,
            permission_callback_registry=permission_callback_registry,
        )
```

- [ ] **Step 6: Update existing tests for new response result**

In `tests/property/test_unbound_permission_properties.py`, replace assertions like:

```python
assert result_first is True
assert result is False
```

with:

```python
assert result_first.accepted is True
assert result.accepted is False
```

In `tests/integration/test_external_session_pipeline.py`, update `first_response` and `second_response` assertions:

```python
        assert first_response.accepted is True
        assert first_response.forwarded is True
```

```python
        assert second_response.accepted is False
```

- [ ] **Step 7: Run external/unbound tests**

Run:

```bash
python -m pytest tests/property/test_unbound_permission_properties.py tests/integration/test_external_session_pipeline.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit Task 6**

```bash
git add app/services/unbound_permission_handler.py app/bot/handlers/external_permission.py app/bot/router.py app/bootstrap.py tests/property/test_unbound_permission_properties.py tests/integration/test_external_session_pipeline.py
git commit -m "fix: clean unbound permission state and tokenize external callbacks"
```

---

### Task 7: Tmux persistent session lock cleanup

**Files:**
- Modify: `app/adapters/process/tmux_runner.py:71-100,204-212,569-589,712-717`
- Modify: `app/bootstrap.py:121-127`
- Modify: `tests/test_tmux_runner.py`

- [ ] **Step 1: Write failing tmux lock cleanup test**

In `tests/test_tmux_runner.py`, add:

```python
@pytest.mark.asyncio
async def test_persistent_session_locks_are_ref_counted_and_cleaned(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    now = 0.0

    def clock() -> float:
        return now

    runner = TmuxRunner(
        data_dir=str(tmp_path),
        session_lock_ttl_sec=1,
        lock_cleanup_interval_sec=1,
        lock_cleanup_batch_size=10,
        lock_clock=clock,
    )

    async def fake_run_task(*, meta, timeout_sec: int, env, workdir: str, command: str):
        yield CLIEvent(type=EventType.STARTED, task_id=meta.task_id)
        yield CLIEvent(type=EventType.EXITED, task_id=meta.task_id, exit_code=0)

    monkeypatch.setattr(runner, "_run_task", fake_run_task)

    nonlocal_now = {"value": now}

    def advance(seconds: float) -> None:
        nonlocal now
        now += seconds
        nonlocal_now["value"] = now

    for idx in range(3):
        events = await _collect_events(
            runner.run(
                task_id=f"task-{idx}",
                argv=["echo", "ok"],
                workdir=str(tmp_path),
                timeout_sec=10,
                terminal_key=f"user-{idx}",
            )
        )
        assert events[-1].type == EventType.EXITED
        advance(2.0)

    assert len(runner._session_locks) <= 1
```

If Python rejects `nonlocal now` inside `advance`, use this simpler mutable clock instead:

```python
    current = {"now": 0.0}

    def clock() -> float:
        return current["now"]

    def advance(seconds: float) -> None:
        current["now"] += seconds
```

Use only the mutable-clock version in the final test file.

- [ ] **Step 2: Run tmux lock test and verify it fails**

Run:

```bash
python -m pytest tests/test_tmux_runner.py::test_persistent_session_locks_are_ref_counted_and_cleaned -q
```

Expected: FAIL because `TmuxRunner` does not accept lock registry settings.

- [ ] **Step 3: Implement ref-counted locks in `TmuxRunner`**

In `app/adapters/process/tmux_runner.py`, import the registry and async context manager tools:

```python
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from app.services.lock_registry import RefCountedLockRegistry
```

Add constructor parameters:

```python
        session_lock_ttl_sec: int = 3600,
        lock_cleanup_interval_sec: int = 60,
        lock_cleanup_batch_size: int = 50,
        lock_clock: Callable[[], float] | None = None,
```

Replace `_session_locks` initialization:

```python
        self._session_locks = RefCountedLockRegistry(
            ttl_sec=session_lock_ttl_sec,
            cleanup_interval_sec=lock_cleanup_interval_sec,
            cleanup_batch_size=lock_cleanup_batch_size,
            clock=lock_clock,
        )
```

Replace `_get_session_lock` with:

```python
    @asynccontextmanager
    async def _session_lock(self, session_name: str) -> AsyncIterator[None]:
        async with self._session_locks.lock(session_name):
            yield
```

Update persistent sections:

```python
        if persistent_terminal:
            async with self._session_lock(session_name):
                async for event in self._run_task(meta=meta, timeout_sec=timeout_sec, env=env, workdir=workdir, command=command):
                    yield event
            return
```

Update `ensure_terminal`, `ensure_claude_interactive_session`, and `ensure_claude_resume_session` to use:

```python
        async with self._session_lock(session_name):
```

- [ ] **Step 4: Pass settings from container**

In `app/bootstrap.py`, extend `TmuxRunner(...)` construction:

```python
            session_lock_ttl_sec=settings.session_lock_ttl_sec,
            lock_cleanup_interval_sec=settings.lock_cleanup_interval_sec,
            lock_cleanup_batch_size=settings.lock_cleanup_batch_size,
```

- [ ] **Step 5: Run tmux tests**

Run:

```bash
python -m pytest tests/test_tmux_runner.py tests/test_lock_registry.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit Task 7**

```bash
git add app/adapters/process/tmux_runner.py app/bootstrap.py tests/test_tmux_runner.py
git commit -m "fix: clean up tmux session locks"
```

---

### Task 8: Agent file watcher cleanup

**Files:**
- Modify: `app/services/agent_file_watcher.py:28-78,120-140`
- Create: `tests/test_agent_file_watcher.py`

- [ ] **Step 1: Write failing watcher cleanup tests**

Create `tests/test_agent_file_watcher.py`:

```python
from __future__ import annotations

import asyncio

import pytest

from app.services.agent_file_watcher import AgentFileWatcher


class DummySessionStore:
    def get(self, session_id: str):
        return None


class DummyParser:
    def subagent_file_path(self, *, session_id: str, agent_id: str, cwd: str):
        raise AssertionError("subagent_file_path should not be called in these cleanup tests")

    def reset_state(self, session_id: str) -> None:
        return None


@pytest.mark.asyncio
async def test_forget_clears_all_seen_mtime_keys_for_session() -> None:
    watcher = AgentFileWatcher(
        session_store=DummySessionStore(),
        claude_jsonl_parser=DummyParser(),
        on_update=lambda session_id, workdir: asyncio.sleep(0),
    )
    watcher._seen_mtimes = {
        "session-1:tool-a:agent-a": 1.0,
        "session-1:tool-b:agent-b": 2.0,
        "session-2:tool-c:agent-c": 3.0,
    }
    watcher._session_locks["session-1"] = asyncio.Lock()

    watcher.forget("session-1")

    assert watcher._seen_mtimes == {"session-2:tool-c:agent-c": 3.0}
    assert "session-1" not in watcher._session_locks


@pytest.mark.asyncio
async def test_forget_defers_lock_cleanup_until_running_watcher_exits() -> None:
    release_update = asyncio.Event()
    update_started = asyncio.Event()

    async def on_update(session_id: str, workdir: str) -> None:
        update_started.set()
        await release_update.wait()

    watcher = AgentFileWatcher(
        session_store=DummySessionStore(),
        claude_jsonl_parser=DummyParser(),
        on_update=on_update,
    )

    async def fake_watch_session(*, session_id: str, workdir: str) -> None:
        lock = watcher._session_locks.setdefault(session_id, asyncio.Lock())
        task = asyncio.current_task()
        try:
            async with lock:
                await on_update(session_id, workdir)
        finally:
            watcher._cleanup_finished_session(session_id=session_id, task=task)

    watcher._tasks["session-1"] = asyncio.create_task(fake_watch_session(session_id="session-1", workdir="/tmp/project"))
    await update_started.wait()
    watcher._seen_mtimes = {"session-1:tool-a:agent-a": 1.0}

    watcher.forget("session-1")

    assert "session-1:tool-a:agent-a" not in watcher._seen_mtimes
    assert "session-1" in watcher._session_locks

    release_update.set()
    with pytest.raises(asyncio.CancelledError):
        await watcher._tasks.get("session-1", asyncio.create_task(asyncio.sleep(0)))
    await asyncio.sleep(0)

    assert "session-1" not in watcher._session_locks
```

If the second test is brittle because the task is popped by `forget`, store the task before calling `forget`:

```python
    task = watcher._tasks["session-1"]
    watcher.forget("session-1")
    ...
    with pytest.raises(asyncio.CancelledError):
        await task
```

Use the stored-task version in the final test file.

- [ ] **Step 2: Run watcher tests and verify they fail**

Run:

```bash
python -m pytest tests/test_agent_file_watcher.py -q
```

Expected: FAIL because lock cleanup helpers do not exist and `forget()` only pops an exact mtime key.

- [ ] **Step 3: Implement watcher cleanup helpers**

In `app/services/agent_file_watcher.py`, add these helpers after `stop_all`:

```python
    def _clear_seen_mtimes_for_session(self, session_id: str) -> None:
        prefix = f"{session_id}:"
        stale_keys = [key for key in self._seen_mtimes if key == session_id or key.startswith(prefix)]
        for key in stale_keys:
            self._seen_mtimes.pop(key, None)

    def _cleanup_finished_session(self, *, session_id: str, task: asyncio.Task[None] | None) -> None:
        active_task = self._tasks.get(session_id)
        if active_task is not None and active_task is not task:
            return
        if active_task is task:
            self._tasks.pop(session_id, None)
        self._clear_seen_mtimes_for_session(session_id)
        self._session_locks.pop(session_id, None)
```

Replace `forget`:

```python
    def forget(self, session_id: str) -> None:
        task = self._tasks.pop(session_id, None)
        self._clear_seen_mtimes_for_session(session_id)
        if task is None or task.done():
            self._session_locks.pop(session_id, None)
            return
        task.cancel()
```

At the end of `stop_all`, after awaiting all tasks, add:

```python
        self._session_locks.clear()
```

Replace the watcher `finally` block:

```python
        finally:
            self._cleanup_finished_session(session_id=session_id, task=task)
```

- [ ] **Step 4: Run watcher tests**

Run:

```bash
python -m pytest tests/test_agent_file_watcher.py tests/test_bootstrap_hooks.py::test_agent_file_watcher_syncs_when_subagent_file_changes tests/test_bootstrap_hooks.py::test_start_restores_agent_file_watcher_for_existing_subagent_container -q
```

Expected: PASS.

- [ ] **Step 5: Commit Task 8**

```bash
git add app/services/agent_file_watcher.py tests/test_agent_file_watcher.py
git commit -m "fix: clean agent watcher session state"
```

---

### Task 9: Final verification and cleanup

**Files:**
- Verify all modified files.

- [ ] **Step 1: Run targeted test groups**

Run:

```bash
python -m pytest tests/test_upload_queue.py tests/test_file_upload_handler.py tests/test_run_event_streamer_upload_queue.py tests/test_permission_callback_registry.py tests/test_session_handlers.py tests/property/test_unbound_permission_properties.py tests/integration/test_external_session_pipeline.py tests/test_tmux_runner.py tests/test_agent_file_watcher.py tests/test_auth_settings.py tests/test_bootstrap_hooks.py -q
```

Expected: PASS.

- [ ] **Step 2: Run lint and formatting checks**

Run:

```bash
python -m ruff check app tests && python -m ruff format --check app tests
```

Expected: both commands PASS.

- [ ] **Step 3: Run full test suite**

Run:

```bash
python -m pytest -q
```

Expected: PASS.

- [ ] **Step 4: Inspect final diff**

Run:

```bash
git status --short && git diff --stat
```

Expected: only intentional files are modified. No temporary files or debug artifacts remain.

- [ ] **Step 5: Final commit if verification changed files**

If formatting changed files in Step 2, commit those changes:

```bash
git add app tests deploy/env/.env.example
git commit -m "chore: format priority stability fixes"
```

If Step 2 did not change files, skip this commit.

---

## Self-Review

**Spec coverage:**
- Upload queue behavior, metadata size rejection, count/byte bounds, restart-loss wording, and background post-final processing are covered by Tasks 1-3.
- Permission short tokens, callback length, stale recovery, token expiry, collision retry, and normal/external callback resolution are covered by Tasks 4-6.
- Unbound pending cleanup, first-responder-wins, tmux lock cleanup, and agent watcher cleanup are covered by Tasks 6-8.
- Configuration summary and rollback-oriented off switch are covered by Task 1 settings and `.env.example` changes.

**No-placeholder scan:** This plan contains concrete paths, code snippets, commands, and expected outcomes for each implementation step.

**Type consistency:** `UploadQueueManager`, `PermissionCallbackRegistry`, `UnboundPermissionResponseResult`, `queued_upload_scheduler`, and `effective_upload_queue_max_bytes_per_user` use the same names across tests, implementation steps, and wiring steps.
