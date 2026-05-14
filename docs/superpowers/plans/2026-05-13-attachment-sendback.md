# Attachment Send-Back Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add explicit Telegram attachment send-back through `/sendfile` and `/sendimage`, with allowlisted paths, file types, and size limits.

**Architecture:** Add a small `AttachmentService` that validates local paths without depending on aiogram, then add Telegram handlers that use aiogram `FSInputFile` to send validated files. Wire the service through `AppContainer` and router registration, with settings/env/docs updated alongside focused tests.

**Tech Stack:** Python 3.11, aiogram 3, pydantic-settings, pytest, ruff, mypy.

---

## File Structure

- Create: `app/services/attachment_service.py`
  - Owns attachment validation only: feature switch, path containment, regular-file check, size check, suffix allowlists.
  - Does not import aiogram and does not send messages.
- Create: `app/bot/handlers/command_attachment.py`
  - Registers `/sendfile` and `/sendimage`.
  - Parses command arguments with `shlex.split()` so quoted paths with spaces work.
  - Uses `FSInputFile`, `message.answer_document()`, and `message.answer_photo()`.
- Modify: `app/config/settings.py`
  - Adds `ATTACHMENT_SEND` and `ATTACHMENT_MAX_BYTES` settings.
  - Reuses existing bool and positive-int validators.
- Modify: `app/bot/router.py`
  - Imports and registers attachment handlers.
  - Shows the new commands in `/start` output.
- Modify: `app/bootstrap.py`
  - Instantiates `AttachmentService` and passes it into `create_router()`.
- Modify: `deploy/env/.env.example`
  - Documents attachment send-back settings.
- Modify: `README.md`
  - Documents commands and security boundaries.
- Create: `tests/test_attachment_service.py`
  - Unit tests for validation logic.
- Create: `tests/test_command_attachment.py`
  - Handler tests for Telegram send behavior.
- Modify: `tests/test_auth_settings.py`
  - Settings and env example coverage.

## Task 0: Preflight checks

**Files:**
- Verify: working tree and Python environment

- [ ] **Step 1: Confirm the working tree state**

Run:

```bash
git status --short
```

Expected: either no output, or only the implementation plan file if it has not been committed yet. Do not start code changes with unrelated uncommitted code present.

- [ ] **Step 2: Confirm the Python virtual environment**

Run:

```bash
python -c 'import os, sys; print(sys.executable); print(os.environ.get("VIRTUAL_ENV", ""))'
```

Expected: the executable path points to the project Python environment, and the second line is not empty.

If the second line is empty, run:

```bash
pyenv virtualenv 3.11.13 remote-coding
pyenv local remote-coding
python -m pip install -e ".[dev]"
```

Then rerun the environment check and confirm the second line is not empty.

## Task 1: Add attachment settings

**Files:**
- Modify: `app/config/settings.py`
- Modify: `tests/test_auth_settings.py`
- Modify: `deploy/env/.env.example`

- [ ] **Step 1: Write failing settings tests**

Edit `tests/test_auth_settings.py` and add these tests after `test_settings_parse_claude_hook_fields()`:

```python
def test_settings_parse_attachment_fields() -> None:
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
            "ATTACHMENT_SEND": "off",
            "ATTACHMENT_MAX_BYTES": 1234,
        }
    )

    assert settings.attachment_send is False
    assert settings.attachment_max_bytes == 1234


def test_settings_rejects_non_positive_attachment_max_bytes() -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate(
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
                "ATTACHMENT_MAX_BYTES": 0,
            }
        )
```

Then extend `test_env_example_matches_supported_claude_settings()` with these assertions:

```python
    assert "ATTACHMENT_SEND=true" in content
    assert "ATTACHMENT_MAX_BYTES=20971520" in content
```

- [ ] **Step 2: Run the new settings tests and verify they fail**

Run:

```bash
pytest tests/test_auth_settings.py::test_settings_parse_attachment_fields tests/test_auth_settings.py::test_settings_rejects_non_positive_attachment_max_bytes tests/test_auth_settings.py::test_env_example_matches_supported_claude_settings -q
```

Expected: FAIL because `Settings` does not yet expose `attachment_send` / `attachment_max_bytes`, and `.env.example` does not yet include those lines.

- [ ] **Step 3: Add settings fields and validators**

Edit `app/config/settings.py`.

Add these fields after `task_output_char_limit`:

```python
    attachment_send: bool = Field(True, alias="ATTACHMENT_SEND")
    attachment_max_bytes: int = Field(20 * 1024 * 1024, alias="ATTACHMENT_MAX_BYTES")
```

Update the bool validator decorator from:

```python
    @field_validator("claude_tmux_mode", "claude_install_hooks", mode="before")
```

to:

```python
    @field_validator("claude_tmux_mode", "claude_install_hooks", "attachment_send", mode="before")
```

Add `"attachment_max_bytes",` to the positive-int validator field list after `"task_output_char_limit",`:

```python
        "task_output_char_limit",
        "attachment_max_bytes",
        "tg_request_timeout_sec",
```

- [ ] **Step 4: Document settings in `.env.example`**

Edit `deploy/env/.env.example` and add this block after `TASK_OUTPUT_CHAR_LIMIT=120000`:

```env

# Attachment send-back
ATTACHMENT_SEND=true
ATTACHMENT_MAX_BYTES=20971520
```

- [ ] **Step 5: Run settings tests and verify they pass**

Run:

```bash
pytest tests/test_auth_settings.py::test_settings_parse_attachment_fields tests/test_auth_settings.py::test_settings_rejects_non_positive_attachment_max_bytes tests/test_auth_settings.py::test_env_example_matches_supported_claude_settings -q
```

Expected:

```text
3 passed
```

- [ ] **Step 6: Commit settings changes**

Run:

```bash
git add app/config/settings.py deploy/env/.env.example tests/test_auth_settings.py
git commit -m "feat: add attachment send settings"
```

## Task 2: Add attachment validation service

**Files:**
- Create: `app/services/attachment_service.py`
- Create: `tests/test_attachment_service.py`

- [ ] **Step 1: Write failing service tests**

Create `tests/test_attachment_service.py` with this content:

```python
from __future__ import annotations

import os
from pathlib import Path

from app.services.attachment_service import AttachmentService


def _service(tmp_path: Path, *, enabled: bool = True, max_bytes: int = 1024) -> AttachmentService:
    return AttachmentService(
        attachment_send=enabled,
        attachment_max_bytes=max_bytes,
        allowed_workdirs=[str(tmp_path)],
    )


def test_validate_file_accepts_allowed_document(tmp_path: Path) -> None:
    report = tmp_path / "report.pdf"
    report.write_bytes(b"pdf")

    result = _service(tmp_path).validate(str(report), kind="file")

    assert result.ok is True
    assert result.path == report.resolve()
    assert result.error is None


def test_validate_image_accepts_allowed_image(tmp_path: Path) -> None:
    image = tmp_path / "chart.png"
    image.write_bytes(b"png")

    result = _service(tmp_path).validate(str(image), kind="image")

    assert result.ok is True
    assert result.path == image.resolve()
    assert result.error is None


def test_validate_rejects_disabled_feature(tmp_path: Path) -> None:
    report = tmp_path / "report.pdf"
    report.write_bytes(b"pdf")

    result = _service(tmp_path, enabled=False).validate(str(report), kind="file")

    assert result.ok is False
    assert result.path is None
    assert result.error == "附件回传已关闭"


def test_validate_rejects_path_outside_allowed_workdirs(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"pdf")
    service = AttachmentService(
        attachment_send=True,
        attachment_max_bytes=1024,
        allowed_workdirs=[str(allowed)],
    )

    result = service.validate(str(outside), kind="file")

    assert result.ok is False
    assert result.error == "文件不在 ALLOWED_WORKDIRS 白名单内"


def test_validate_rejects_missing_file_inside_allowed_workdir(tmp_path: Path) -> None:
    missing = tmp_path / "missing.pdf"

    result = _service(tmp_path).validate(str(missing), kind="file")

    assert result.ok is False
    assert result.error == "文件不存在"


def test_validate_rejects_directory(tmp_path: Path) -> None:
    directory = tmp_path / "reports.pdf"
    directory.mkdir()

    result = _service(tmp_path).validate(str(directory), kind="file")

    assert result.ok is False
    assert result.error == "不是普通文件"


def test_validate_rejects_file_over_size_limit(tmp_path: Path) -> None:
    report = tmp_path / "report.pdf"
    report.write_bytes(b"12345")

    result = _service(tmp_path, max_bytes=4).validate(str(report), kind="file")

    assert result.ok is False
    assert result.error == "文件超过大小限制"


def test_validate_file_rejects_disallowed_suffix(tmp_path: Path) -> None:
    secret = tmp_path / ".env"
    secret.write_text("TOKEN=secret", encoding="utf-8")

    result = _service(tmp_path).validate(str(secret), kind="file")

    assert result.ok is False
    assert result.error == "文件类型不允许"


def test_validate_image_rejects_non_image_suffix(tmp_path: Path) -> None:
    report = tmp_path / "report.pdf"
    report.write_bytes(b"pdf")

    result = _service(tmp_path).validate(str(report), kind="image")

    assert result.ok is False
    assert result.error == "文件类型不允许"


def test_validate_rejects_symlink_escape(tmp_path: Path) -> None:
    if not hasattr(os, "symlink"):
        return

    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"pdf")
    link = allowed / "linked.pdf"
    link.symlink_to(outside)
    service = AttachmentService(
        attachment_send=True,
        attachment_max_bytes=1024,
        allowed_workdirs=[str(allowed)],
    )

    result = service.validate(str(link), kind="file")

    assert result.ok is False
    assert result.error == "文件不在 ALLOWED_WORKDIRS 白名单内"
```

- [ ] **Step 2: Run service tests and verify they fail**

Run:

```bash
pytest tests/test_attachment_service.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.attachment_service'`.

- [ ] **Step 3: Implement `AttachmentService`**

Create `app/services/attachment_service.py` with this content:

```python
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from app.config.settings import is_workdir_allowed

AttachmentKind = Literal["file", "image"]

IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})
FILE_SUFFIXES = frozenset({".pdf", ".txt", ".md", ".csv", ".json", ".log", ".zip", ".tar", ".gz", ".tgz"})


@dataclass(frozen=True)
class AttachmentValidationResult:
    original: str
    path: Path | None
    ok: bool
    error: str | None = None

    @property
    def display_path(self) -> str:
        return str(self.path) if self.path is not None else self.original


class AttachmentService:
    def __init__(self, *, attachment_send: bool, attachment_max_bytes: int, allowed_workdirs: list[str]) -> None:
        self._attachment_send = attachment_send
        self._attachment_max_bytes = attachment_max_bytes
        self._allowed_workdirs = allowed_workdirs

    @property
    def enabled(self) -> bool:
        return self._attachment_send

    def validate(self, raw_path: str, *, kind: AttachmentKind) -> AttachmentValidationResult:
        if not self._attachment_send:
            return AttachmentValidationResult(original=raw_path, path=None, ok=False, error="附件回传已关闭")

        try:
            resolved = Path(raw_path).expanduser().resolve(strict=False)
        except (OSError, ValueError):
            return AttachmentValidationResult(original=raw_path, path=None, ok=False, error="路径无效")

        if not is_workdir_allowed(str(resolved), self._allowed_workdirs):
            return AttachmentValidationResult(original=raw_path, path=resolved, ok=False, error="文件不在 ALLOWED_WORKDIRS 白名单内")

        if not resolved.exists():
            return AttachmentValidationResult(original=raw_path, path=resolved, ok=False, error="文件不存在")

        if not resolved.is_file():
            return AttachmentValidationResult(original=raw_path, path=resolved, ok=False, error="不是普通文件")

        if resolved.stat().st_size > self._attachment_max_bytes:
            return AttachmentValidationResult(original=raw_path, path=resolved, ok=False, error="文件超过大小限制")

        allowed_suffixes = IMAGE_SUFFIXES if kind == "image" else FILE_SUFFIXES
        if resolved.suffix.lower() not in allowed_suffixes:
            return AttachmentValidationResult(original=raw_path, path=resolved, ok=False, error="文件类型不允许")

        return AttachmentValidationResult(original=raw_path, path=resolved, ok=True)
```

- [ ] **Step 4: Run service tests and verify they pass**

Run:

```bash
pytest tests/test_attachment_service.py -q
```

Expected:

```text
10 passed
```

- [ ] **Step 5: Commit service changes**

Run:

```bash
git add app/services/attachment_service.py tests/test_attachment_service.py
git commit -m "feat: validate attachment send-back files"
```

## Task 3: Add Telegram attachment commands

**Files:**
- Create: `app/bot/handlers/command_attachment.py`
- Create: `tests/test_command_attachment.py`

- [ ] **Step 1: Write failing command handler tests**

Create `tests/test_command_attachment.py` with this content:

```python
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.bot.handlers.command_attachment import _handle_send_attachments, parse_attachment_paths
from app.services.attachment_service import AttachmentService


class DummyMessage:
    def __init__(self) -> None:
        self.answers: list[str] = []
        self.documents: list[object] = []
        self.photos: list[object] = []

    async def answer(self, text: str) -> None:
        self.answers.append(text)

    async def answer_document(self, document) -> None:
        self.documents.append(document)

    async def answer_photo(self, photo) -> None:
        self.photos.append(photo)


def _service(tmp_path: Path, *, enabled: bool = True) -> AttachmentService:
    return AttachmentService(
        attachment_send=enabled,
        attachment_max_bytes=1024,
        allowed_workdirs=[str(tmp_path)],
    )


def test_parse_attachment_paths_supports_quoted_paths() -> None:
    assert parse_attachment_paths('"/tmp/a b/report.pdf" /tmp/chart.png') == ["/tmp/a b/report.pdf", "/tmp/chart.png"]


def test_parse_attachment_paths_rejects_unclosed_quote() -> None:
    with pytest.raises(ValueError, match="路径参数解析失败"):
        parse_attachment_paths('"/tmp/report.pdf')


@pytest.mark.asyncio
async def test_sendfile_sends_document(tmp_path: Path) -> None:
    report = tmp_path / "report.pdf"
    report.write_bytes(b"pdf")
    message = DummyMessage()

    await _handle_send_attachments(
        message=message,
        command=SimpleNamespace(args=str(report)),
        attachment_service=_service(tmp_path),
        kind="file",
    )

    assert len(message.documents) == 1
    assert len(message.photos) == 0
    assert getattr(message.documents[0], "path") == str(report.resolve())
    assert message.answers == ["附件发送完成\n成功: 1\n失败: 0"]


@pytest.mark.asyncio
async def test_sendimage_sends_photo(tmp_path: Path) -> None:
    image = tmp_path / "chart.png"
    image.write_bytes(b"png")
    message = DummyMessage()

    await _handle_send_attachments(
        message=message,
        command=SimpleNamespace(args=str(image)),
        attachment_service=_service(tmp_path),
        kind="image",
    )

    assert len(message.photos) == 1
    assert len(message.documents) == 0
    assert getattr(message.photos[0], "path") == str(image.resolve())
    assert message.answers == ["附件发送完成\n成功: 1\n失败: 0"]


@pytest.mark.asyncio
async def test_sendfile_reports_usage_without_paths(tmp_path: Path) -> None:
    message = DummyMessage()

    await _handle_send_attachments(
        message=message,
        command=SimpleNamespace(args=""),
        attachment_service=_service(tmp_path),
        kind="file",
    )

    assert message.documents == []
    assert message.photos == []
    assert message.answers == ["用法: /sendfile <path> [path...]"]


@pytest.mark.asyncio
async def test_sendimage_reports_disabled_feature(tmp_path: Path) -> None:
    image = tmp_path / "chart.png"
    image.write_bytes(b"png")
    message = DummyMessage()

    await _handle_send_attachments(
        message=message,
        command=SimpleNamespace(args=str(image)),
        attachment_service=_service(tmp_path, enabled=False),
        kind="image",
    )

    assert message.documents == []
    assert message.photos == []
    assert message.answers == ["附件回传已关闭。"]


@pytest.mark.asyncio
async def test_sendfile_reports_validation_failure_and_continues(tmp_path: Path) -> None:
    report = tmp_path / "report.pdf"
    report.write_bytes(b"pdf")
    secret = tmp_path / ".env"
    secret.write_text("TOKEN=secret", encoding="utf-8")
    message = DummyMessage()

    await _handle_send_attachments(
        message=message,
        command=SimpleNamespace(args=f"{secret} {report}"),
        attachment_service=_service(tmp_path),
        kind="file",
    )

    assert len(message.documents) == 1
    assert getattr(message.documents[0], "path") == str(report.resolve())
    assert message.answers == [
        "附件发送完成\n"
        "成功: 1\n"
        "失败: 1\n"
        f"- {secret}: 文件类型不允许"
    ]
```

- [ ] **Step 2: Run command handler tests and verify they fail**

Run:

```bash
pytest tests/test_command_attachment.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'app.bot.handlers.command_attachment'`.

- [ ] **Step 3: Implement command handlers**

Create `app/bot/handlers/command_attachment.py` with this content:

```python
from __future__ import annotations

import shlex

from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.types import FSInputFile, Message

from app.services.attachment_service import AttachmentKind, AttachmentService


def parse_attachment_paths(args: str | None) -> list[str]:
    text = (args or "").strip()
    if not text:
        return []
    try:
        return shlex.split(text)
    except ValueError as exc:
        raise ValueError(f"路径参数解析失败: {exc}") from exc


def _usage_for(kind: AttachmentKind) -> str:
    command = "/sendimage" if kind == "image" else "/sendfile"
    return f"用法: {command} <path> [path...]"


def _render_attachment_summary(*, success_count: int, failures: list[str]) -> str:
    lines = ["附件发送完成", f"成功: {success_count}", f"失败: {len(failures)}"]
    lines.extend(failures)
    return "\n".join(lines)


async def _handle_send_attachments(
    *,
    message: Message,
    command: CommandObject,
    attachment_service: AttachmentService,
    kind: AttachmentKind,
) -> None:
    if not attachment_service.enabled:
        await message.answer("附件回传已关闭。")
        return

    try:
        paths = parse_attachment_paths(command.args)
    except ValueError as exc:
        await message.answer(str(exc))
        return

    if not paths:
        await message.answer(_usage_for(kind))
        return

    success_count = 0
    failures: list[str] = []

    for raw_path in paths:
        validation = attachment_service.validate(raw_path, kind=kind)
        if not validation.ok or validation.path is None:
            failures.append(f"- {raw_path}: {validation.error or '发送失败'}")
            continue

        try:
            attachment = FSInputFile(str(validation.path))
            if kind == "image":
                await message.answer_photo(attachment)
            else:
                await message.answer_document(attachment)
            success_count += 1
        except TelegramAPIError as exc:
            failures.append(f"- {validation.display_path}: 发送失败: {exc}")

    await message.answer(_render_attachment_summary(success_count=success_count, failures=failures))


def register_attachment_handlers(router, *, attachment_service: AttachmentService) -> None:
    @router.message(Command("sendfile"))
    async def command_sendfile(message: Message, command: CommandObject) -> None:
        await _handle_send_attachments(
            message=message,
            command=command,
            attachment_service=attachment_service,
            kind="file",
        )

    @router.message(Command("sendimage"))
    async def command_sendimage(message: Message, command: CommandObject) -> None:
        await _handle_send_attachments(
            message=message,
            command=command,
            attachment_service=attachment_service,
            kind="image",
        )
```

- [ ] **Step 4: Run command handler tests and verify they pass**

Run:

```bash
pytest tests/test_command_attachment.py -q
```

Expected:

```text
8 passed
```

- [ ] **Step 5: Commit command handler changes**

Run:

```bash
git add app/bot/handlers/command_attachment.py tests/test_command_attachment.py
git commit -m "feat: add telegram attachment commands"
```

## Task 4: Wire attachment service into the app

**Files:**
- Modify: `app/bootstrap.py`
- Modify: `app/bot/router.py`

- [ ] **Step 1: Update bootstrap to create the service**

Edit `app/bootstrap.py`.

Add this import near the other service imports:

```python
from app.services.attachment_service import AttachmentService
```

After the existing `self.task_service = TaskService(...)` block, add:

```python
        self.attachment_service = AttachmentService(
            attachment_send=settings.attachment_send,
            attachment_max_bytes=settings.attachment_max_bytes,
            allowed_workdirs=settings.allowed_workdirs,
        )
```

Update the router creation in `wire()` from:

```python
        router = create_router(
            settings=self.settings,
            task_service=self.task_service,
            session_service=self.session_service,
        )
```

to:

```python
        router = create_router(
            settings=self.settings,
            task_service=self.task_service,
            session_service=self.session_service,
            attachment_service=self.attachment_service,
        )
```

- [ ] **Step 2: Update router signature and registrations**

Edit `app/bot/router.py`.

Add this import with the handler imports:

```python
from app.bot.handlers.command_attachment import register_attachment_handlers
```

Add this import with service imports:

```python
from app.services.attachment_service import AttachmentService
```

Change `create_router()` from:

```python
def create_router(*, settings: Settings, task_service: TaskService, session_service: SessionService) -> Router:
```

to:

```python
def create_router(
    *,
    settings: Settings,
    task_service: TaskService,
    session_service: SessionService,
    attachment_service: AttachmentService,
) -> Router:
```

Add the new commands to the `/start` help text after the `/session` line:

```python
            "/sendfile <path> [path...]\n"
            "/sendimage <path> [path...]\n"
```

Register the handlers after `register_session_handler(...)`:

```python
    register_attachment_handlers(router, attachment_service=attachment_service)
```

- [ ] **Step 3: Run focused import and router checks**

Run:

```bash
python - <<'PY'
from app.config.settings import Settings
from app.services.attachment_service import AttachmentService
from app.bot.router import create_router

settings = Settings.model_validate({
    "TG_BOT_TOKEN": "123:abc",
    "TG_ALLOWED_USER_IDS": "1",
    "DEFAULT_PROVIDER": "claude_code",
    "DEFAULT_TIMEOUT_SEC": 10,
    "MAX_CONCURRENT_TASKS": 1,
    "CLAUDE_TMUX_MODE": False,
    "CLAUDE_CLI_BIN": "claude",
    "CODEX_CLI_BIN": "codex",
    "GEMINI_CLI_BIN": "gemini",
    "ALLOWED_WORKDIRS": "/tmp",
})
attachment_service = AttachmentService(
    attachment_send=settings.attachment_send,
    attachment_max_bytes=settings.attachment_max_bytes,
    allowed_workdirs=settings.allowed_workdirs,
)
router = create_router(
    settings=settings,
    task_service=object(),
    session_service=object(),
    attachment_service=attachment_service,
)
print(type(router).__name__)
PY
```

Expected:

```text
Router
```

- [ ] **Step 4: Run attachment and settings tests**

Run:

```bash
pytest tests/test_attachment_service.py tests/test_command_attachment.py tests/test_auth_settings.py -q
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit wiring changes**

Run:

```bash
git add app/bootstrap.py app/bot/router.py
git commit -m "feat: wire attachment send-back handlers"
```

## Task 5: Update user documentation

**Files:**
- Modify: `README.md`
- Verify: `deploy/env/.env.example`

- [ ] **Step 1: Update feature list and command list in README**

Edit `README.md`.

In the feature list after line containing `输出分片（<4096）+ 节流 + 结束总结`, add:

```markdown
- 附件回传：`/sendfile` / `/sendimage` 可发送 `ALLOWED_WORKDIRS` 内的图片、PDF、报告或压缩包
```

In the Telegram commands section after `/session [provider] [workdir]` add:

```markdown
- `/sendfile <path> [path...]`：发送文件附件（路径必须在 `ALLOWED_WORKDIRS` 内）
- `/sendimage <path> [path...]`：发送图片附件（路径必须在 `ALLOWED_WORKDIRS` 内）
```

In the security boundary section after `工作目录必须在 ALLOWED_WORKDIRS 内`, add:

```markdown
- 附件路径必须在 `ALLOWED_WORKDIRS` 内，且受 `ATTACHMENT_MAX_BYTES` 与文件类型白名单限制
```

After the tmux paragraph and before `## 安全边界`, add this new section:

```markdown
## 附件回传

当 Claude/Agent 在本机生成截图、图表、PDF、日志或报告后，可通过 Telegram 命令回传附件：

```text
/sendimage /absolute/path/to/chart.png
/sendfile /absolute/path/to/report.pdf
/sendfile /absolute/path/to/report.pdf /absolute/path/to/bundle.zip
```

限制：

- `ATTACHMENT_SEND=false` 时禁用附件回传；普通文本回复不受影响
- 单文件大小默认限制为 20MB，可通过 `ATTACHMENT_MAX_BYTES` 调整
- 文件必须在 `ALLOWED_WORKDIRS` 白名单目录内
- `/sendimage` 仅允许 `.png`、`.jpg`、`.jpeg`、`.webp`、`.gif`
- `/sendfile` 仅允许 `.pdf`、`.txt`、`.md`、`.csv`、`.json`、`.log`、`.zip`、`.tar`、`.gz`、`.tgz`
```

- [ ] **Step 2: Verify README renders as intended around the new section**

Run:

```bash
python - <<'PY'
from pathlib import Path
text = Path('README.md').read_text(encoding='utf-8')
required = [
    '附件回传：`/sendfile` / `/sendimage`',
    '- `/sendfile <path> [path...]`：发送文件附件',
    '- `/sendimage <path> [path...]`：发送图片附件',
    '## 附件回传',
    'ATTACHMENT_MAX_BYTES',
]
missing = [item for item in required if item not in text]
if missing:
    raise SystemExit(f'missing README content: {missing}')
print('README attachment docs ok')
PY
```

Expected:

```text
README attachment docs ok
```

- [ ] **Step 3: Commit documentation changes**

Run:

```bash
git add README.md
git commit -m "docs: document attachment send-back"
```

## Task 6: Run full verification

**Files:**
- Verify: all changed files

- [ ] **Step 1: Run ruff**

Run:

```bash
python -m ruff check app tests
```

Expected:

```text
All checks passed!
```

- [ ] **Step 2: Run existing targeted mypy command**

Run:

```bash
python -m mypy --follow-imports=skip app/adapters/process/subprocess_runner.py app/bot/middleware/auth.py app/bot/middleware/rate_limit.py app/bot/handlers/command_permission.py app/bot/handlers/command_user_question.py app/bootstrap.py app/services/task_service.py
```

Expected:

```text
Success: no issues found in 7 source files
```

- [ ] **Step 3: Run the full test suite**

Run:

```bash
pytest -q
```

Expected: all tests pass.

- [ ] **Step 4: Check whitespace in the final diff**

Run:

```bash
git diff --check
```

Expected: no output and exit code `0`.

- [ ] **Step 5: Inspect final status**

Run:

```bash
git status --short
```

Expected: no uncommitted files, because each task has already committed its changes.

## Self-Review Notes

Spec coverage:

- `/sendfile` and `/sendimage`: Task 3 and Task 4.
- `ATTACHMENT_SEND` and `ATTACHMENT_MAX_BYTES`: Task 1.
- `ALLOWED_WORKDIRS` containment, regular-file, symlink escape, type allowlist, and size checks: Task 2.
- README and `.env.example`: Task 1 and Task 5.
- Unit tests for settings, validation, and Telegram send calls: Tasks 1, 2, and 3.
- Local verification commands: Task 6.

Type consistency:

- `AttachmentKind`, `AttachmentValidationResult`, `AttachmentService.validate()`, and `AttachmentService.enabled` are introduced before handler usage.
- `create_router(..., attachment_service=...)` is introduced in router and used from bootstrap in the same task.

Scope check:

- This plan intentionally excludes automatic scanning, local CLI, MCP tools, and multi-platform abstraction, matching the approved first-version spec.
