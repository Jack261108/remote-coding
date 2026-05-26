# Priority Fixes Design

## Scope

This design covers the first repair batch approved by the user:

1. Fix upload queue behavior and memory limits.
2. Replace permission callback `tool_use_id` truncation with short tokens.
3. Clean up pending permission state and session lock state.

This batch intentionally does not include broad performance rewrites, AppContainer/TaskService decomposition, or persistent upload/permission state.

## Goals

- Files uploaded during an active task are actually processed after the task finishes.
- Oversized uploads are rejected before download when Telegram exposes file size metadata.
- Upload queues cannot grow without per-user bounds.
- Telegram permission buttons work for long `tool_use_id` values without truncation or prefix matching.
- Unbound permission pending entries and session lock entries do not accumulate indefinitely.
- Existing command behavior and user-facing flows remain compatible.

## Non-goals

- Persist queued uploads across process restarts.
- Persist callback tokens across process restarts.
- Redesign all pending state registries into a single generic framework.
- Replace filesystem scanning/diff behavior.
- Refactor the full bootstrap or task service architecture.

## Design 1: Upload Queue Repair

### Current problem

`app/bot/handlers/file_upload.py` queues uploads in `_pending_uploads` when a user has a running task, but `process_pending_uploads()` has no current app-level caller. The queue stores raw `bytes`, has no count or byte limit, and files are downloaded before size checks in `FileReceiverService`.

### Proposed behavior

Introduce a small upload queue manager for queued uploads. It remains in memory and user-scoped, but enforces:

- `UPLOAD_QUEUE_MAX_FILES_PER_USER`, default `5`.
- `UPLOAD_QUEUE_MAX_BYTES_PER_USER`, default `UPLOAD_MAX_FILE_SIZE_MB * 1024 * 1024`.
- FIFO processing order.

The file upload handlers check Telegram file size metadata before downloading:

- `document.file_size` for documents.
- `photo.file_size` for photos when present.

If the size is over `UPLOAD_MAX_FILE_SIZE_MB`, the handler rejects the file before downloading. After download, the existing `FileReceiverService` validation still runs as the final authority.

When a task reaches a final state, `RunEventStreamer.stream_events()` consumes queued uploads for that user. Each queued file is saved against the user's current session workdir by reusing the existing file-processing path; this matches existing direct-upload behavior, which always resolves the workdir from `SessionService` at processing time. A failed file does not stop later queued files.

### User-facing behavior

- If a task is running and the queue has capacity, the bot replies that the file was queued.
- If the queue is full or byte limit is exceeded, the bot rejects the file with a clear reason.
- After task completion, each queued file produces the same success/rejection message as direct upload.

### Tests

Add or update tests for:

- Download is skipped when Telegram file size exceeds the configured limit.
- Queued uploads are processed after task completion.
- Queue count and byte limits reject additional files.
- One failed queued file does not prevent later files from being processed.

## Design 2: Permission Callback Short Tokens

### Current problem

Permission callback data currently embeds `tool_use_id` and truncates it to fit Telegram's 64-byte callback data limit. The permission response path expects the full `tool_use_id`, so long IDs can make buttons fail as stale or expired.

The same issue exists for external permission callbacks.

### Proposed behavior

Add `PermissionCallbackRegistry`, an in-memory TTL registry mapping short tokens to full `tool_use_id` values.

Callback data changes to:

- Normal permissions: `perm:<decision>:<token>`.
- External permissions: `ext_perm:<token>:<decision>`.

Button builders register the real `tool_use_id`, receive a short token, and place only that token into `callback_data`. Callback handlers resolve the token before calling permission services. If the token is missing or expired, the user sees a stale-button message.

The token TTL should use the existing hook pending permission TTL so callback tokens do not outlive the permission request.

### Placement

Place the registry in `app/services/permission_callback_registry.py`. Create one instance in `AppContainer` and inject it into:

- Normal permission handlers.
- External permission handlers.
- Unbound permission handler button creation.

This keeps token ownership explicit without introducing a broad state framework.

### Tests

Add or update tests for:

- Long `tool_use_id` values are not truncated.
- Generated callback data stays under 64 bytes.
- Normal permission callbacks resolve tokens to the full ID.
- External permission callbacks resolve tokens to the full ID.
- Expired or unknown tokens produce a clear stale-button response.

## Design 3: Pending and Lock Cleanup

### Unbound permissions

`UnboundPermissionHandler` should remove entries from `_pending` when:

- A user response is accepted and forwarded.
- The TTL expiry path auto-denies the request.

To preserve first-responder-wins semantics under concurrent callbacks, protect `_pending` and `_expiry_tasks` mutations with a small `asyncio.Lock`.

### Tmux session locks

`TmuxRunner` currently keeps `_session_locks` indefinitely. Replace this dictionary with the existing `RefCountedLockRegistry`, or wrap the persistent-session critical section with an equivalent ref-counted cleanup path. Reusing `RefCountedLockRegistry` is preferred because it is already configured and tested elsewhere.

Only persistent terminal runs need per-session serialization. Ephemeral runs keep current behavior.

### Agent file watcher locks

`AgentFileWatcher.forget()` and the watcher `finally` block should remove the session's lock entry and stale mtime keys. This prevents sessions that end or are forgotten from leaving lock and mtime state behind.

### Tests

Add or update tests for:

- Unbound permission response removes pending state and expiry task.
- Unbound permission expiry removes pending state.
- Tmux persistent session lock count does not grow after repeated runs for completed sessions.
- Agent watcher `forget()` clears the lock and mtime keys.

## Error Handling

- Upload queue processing logs per-file failures and continues.
- Callback token lookup failures return stale-button messages and do not call permission responders.
- Unbound permission response failures should not leave a request indefinitely pending; failed forwarding should be logged and the request should be removed only after the response attempt completes.
- Lock cleanup must never delete a lock while it is held or while another coroutine is waiting for it.

## Implementation Notes

- Keep changes focused and avoid changing unrelated command behavior.
- Prefer existing service injection patterns in `AppContainer.wire()` and router registration.
- Keep user-facing messages consistent with current Chinese bot messages where the surrounding handler already uses Chinese.
- Do not add persistence unless a later batch explicitly asks for restart-safe behavior.

## Verification

Run targeted tests for the changed modules, then the full test suite:

- File upload handler tests.
- Permission handler tests.
- External permission/unbound permission tests.
- Tmux runner and agent watcher cleanup tests.
- Full `pytest -q`.
