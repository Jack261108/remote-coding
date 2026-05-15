# Telegram Status Icons Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add consistent status icons to Telegram agent and tool status messages for running, success, error, interrupted, and waiting states.

**Architecture:** Keep this as a presenter-only change in `app/bot/presenters/structured_reply_presenter.py`. Reuse `_tool_status_icon(status)` as the single icon source, then update presenter tests and message-manager assertions that depend on rendered tool status text.

**Tech Stack:** Python 3, pytest, aiogram message formatting, existing `ToolStatus` domain enum.

---

## File Structure

- Modify: `app/bot/presenters/structured_reply_presenter.py`
  - Add a small helper for plain tool status headings.
  - Prefix icons in plain tool status messages, subagent tool task lists, and subagent aggregate rows.
- Modify: `tests/test_structured_reply_presenter.py`
  - Update direct presenter expectations for plain tools, subagent tool lists, and agent aggregate rows.
- Modify: `tests/test_tool_message_manager.py`
  - Update assertions that inspect messages generated through `ToolMessageManager`.
- No model, service, persistence, Telegram send/edit, or polling files should change.

## Current Workspace Constraint

There is already an uncommitted simplification in `app/bot/presenters/structured_reply_presenter.py` that predates this feature. Handle it before feature work so the status-icon diff is isolated.

### Task 1: Preserve the existing simplification change

**Files:**
- Modify already present: `app/bot/presenters/structured_reply_presenter.py`

- [ ] **Step 1: Inspect current workspace**

Run:

```bash
git status --short
```

Expected before continuing:

```text
 M app/bot/presenters/structured_reply_presenter.py
?? .claude/worktrees/
```

If other tracked files are modified, stop and ask before continuing.

- [ ] **Step 2: Verify the existing simplification still passes focused tests**

Run:

```bash
pyenv version && python -m pytest tests/test_structured_reply_presenter.py tests/test_tool_message_manager.py tests/test_command_run.py -q
```

Expected:

```text
93 passed
```

The elapsed time may differ.

- [ ] **Step 3: Commit only the pre-existing simplification**

Run:

```bash
git add app/bot/presenters/structured_reply_presenter.py
git commit -m "$(cat <<'EOF'
refactor: share telegram text truncation
EOF
)"
```

Expected: a new commit containing only `app/bot/presenters/structured_reply_presenter.py`.

- [ ] **Step 4: Confirm clean tracked workspace before feature work**

Run:

```bash
git status --short
```

Expected:

```text
?? .claude/worktrees/
```

The untracked `.claude/worktrees/` directory predates this work. Do not delete it.

### Task 2: Write failing presenter tests for status icons

**Files:**
- Modify: `tests/test_structured_reply_presenter.py:507-568`
- Modify: `tests/test_structured_reply_presenter.py:770-876`

- [ ] **Step 1: Update plain tool status expectations**

Replace the existing `test_build_tool_progress_message_includes_specific_bash_command` and `test_build_tool_status_message_formats_final_states` bodies with:

```python
def test_build_tool_progress_message_includes_specific_bash_command() -> None:
    message = build_tool_progress_message(tool_name="Bash", tool_input={"command": "pytest -q"})

    assert message == "🔄 执行中\n工具: Bash\n命令: pytest -q"


def test_build_tool_status_message_formats_final_states() -> None:
    assert build_tool_status_message(
        tool_name="Bash",
        tool_input={"command": "pytest -q"},
        status=ToolStatus.SUCCESS.value,
    ) == "✅ 执行完成\n工具: Bash\n命令: pytest -q"
    assert build_tool_status_message(
        tool_name="Bash",
        tool_input={"command": "pytest -q"},
        status=ToolStatus.ERROR.value,
    ) == "❌ 执行失败\n工具: Bash\n命令: pytest -q"
    assert build_tool_status_message(
        tool_name="Bash",
        tool_input={"command": "pytest -q"},
        status=ToolStatus.INTERRUPTED.value,
    ) == "⏹️ 已中断\n工具: Bash\n命令: pytest -q"
    assert build_tool_status_message(
        tool_name="Bash",
        tool_input={"command": "rm file"},
        status=ToolStatus.WAITING_FOR_APPROVAL.value,
    ) == "⏳ 等待权限\n工具: Bash\n命令: rm file"
    assert build_tool_status_message(
        tool_name="Bash",
        tool_input={"command": "pytest -q"},
        status=ToolStatus.RUNNING.value,
        resumed=True,
    ) == "🔄 继续执行\n工具: Bash\n命令: pytest -q"
```

- [ ] **Step 2: Update subagent tool list expectations**

In `test_build_tool_task_list_message_marks_current_task`, replace the expected message with:

```python
    assert message == (
        "任务列表\n"
        "任务: 修复测试失败\n"
        "状态: 执行中\n"
        "当前: 🔄 2. Bash\n"
        "\n"
        "✅ 1. Read - 完成 - 文件: app/foo.py\n"
        "=> 🔄 2. Bash - 执行中 - 命令: pytest -q"
    )
```

- [ ] **Step 3: Update subagent aggregate expectations**

In `test_build_subagent_aggregate_status_message_shows_subagent_type`, replace the expected message with:

```python
    assert message == (
        "1 agents running\n"
        "\n"
        "- 🔄 Explore(项目优化点审计) · 3 tool uses · Running\n"
        "  名称: Read ×2、Glob"
    )
```

In `test_build_subagent_aggregate_status_message_formats_agent_summary`, replace the expected message with:

```python
    assert message == (
        "3 agents finished\n"
        "\n"
        "- ✅ 项目架构扫描 · 51 tool uses · Done\n"
        "  名称: Read ×51\n"
        "- ✅ 测试质量扫描 · 29 tool uses · Done\n"
        "  名称: Glob ×29\n"
        "- ✅ 安全性能扫描 · 40 tool uses · Done\n"
        "  名称: Grep ×40"
    )
```

- [ ] **Step 4: Run focused presenter tests and confirm they fail for icon expectations**

Run:

```bash
python -m pytest tests/test_structured_reply_presenter.py::test_build_tool_progress_message_includes_specific_bash_command tests/test_structured_reply_presenter.py::test_build_tool_status_message_formats_final_states tests/test_structured_reply_presenter.py::test_build_tool_task_list_message_marks_current_task tests/test_structured_reply_presenter.py::test_build_subagent_aggregate_status_message_shows_subagent_type tests/test_structured_reply_presenter.py::test_build_subagent_aggregate_status_message_formats_agent_summary -q
```

Expected: failures showing old strings without the new expected icons, such as `执行中` instead of `🔄 执行中` and `🟢 执行完成` instead of `✅ 执行完成`.

### Task 3: Implement presenter status icons

**Files:**
- Modify: `app/bot/presenters/structured_reply_presenter.py:255-315`
- Modify: `app/bot/presenters/structured_reply_presenter.py:352-364`

- [ ] **Step 1: Add a helper for plain tool status headings**

Insert this helper immediately before `build_tool_status_message`:

```python
def _tool_status_heading(status: str | None, *, resumed: bool = False) -> str:
    if status == ToolStatus.SUCCESS.value:
        text = "执行完成"
    elif status == ToolStatus.ERROR.value:
        text = "执行失败"
    elif status == ToolStatus.INTERRUPTED.value:
        text = "已中断"
    elif status == ToolStatus.WAITING_FOR_APPROVAL.value:
        text = "等待权限"
    elif status == ToolStatus.RUNNING.value and resumed:
        text = "继续执行"
    else:
        text = "执行中"
    return f"{_tool_status_icon(status)} {text}"
```

- [ ] **Step 2: Replace the heading branch in `build_tool_status_message`**

Change `build_tool_status_message` to use the helper:

```python
def build_tool_status_message(*, tool_name: str | None, tool_input: dict | None = None, status: str, resumed: bool = False) -> str:
    lines = [_tool_status_heading(status, resumed=resumed)]
    if tool_name:
        lines.append(f"工具: {tool_name}")

    detail = _format_tool_input_detail(tool_name, tool_input)
    if detail is not None:
        label, value = detail
        lines.append(f"{label}: {value}")

    return "\n".join(lines)
```

- [ ] **Step 3: Prefix icons in `build_tool_task_list_message` current and rows**

Update only the current line and row formatting inside `build_tool_task_list_message`:

```python
    active_index = _select_active_subagent_index(visible_tools)
    if active_index is None:
        lines.append("当前: 无（全部完成）")
    else:
        active_tool = visible_tools[active_index]
        lines.append(f"当前: {_tool_status_icon(active_tool.status)} {active_index + 1}. {active_tool.tool_name or 'Unknown'}")

    lines.append("")
    display_indexes = _select_visible_subagent_indexes(visible_tools, active_index=active_index)
    for index in display_indexes:
        tool = visible_tools[index]
        prefix = "=> " if index == active_index else ""
        detail = _format_tool_input_detail(tool.tool_name, tool.tool_input)
        detail_text = f" - {detail[0]}: {detail[1]}" if detail is not None else ""
        lines.append(f"{prefix}{_tool_status_icon(tool.status)} {index + 1}. {tool.tool_name or 'Unknown'} - {_tool_status_label(tool.status)}{detail_text}")
```

Keep the existing `状态: {_tool_status_label(output.status)}` line unchanged.

- [ ] **Step 4: Add a helper for aggregate row icons**

Insert this helper near `_subagent_container_status_text`:

```python
def _subagent_container_status_icon(container: ToolStatusOutput) -> str:
    statuses = _subagent_container_status_values((container,))
    if ToolStatus.WAITING_FOR_APPROVAL.value in statuses:
        return _tool_status_icon(ToolStatus.WAITING_FOR_APPROVAL.value)
    if ToolStatus.RUNNING.value in statuses:
        return _tool_status_icon(ToolStatus.RUNNING.value)
    if ToolStatus.ERROR.value in statuses:
        return _tool_status_icon(ToolStatus.ERROR.value)
    if ToolStatus.INTERRUPTED.value in statuses:
        return _tool_status_icon(ToolStatus.INTERRUPTED.value)
    return _tool_status_icon(ToolStatus.SUCCESS.value)
```

- [ ] **Step 5: Prefix icons in `build_subagent_aggregate_status_message` rows**

Replace the row append call in `build_subagent_aggregate_status_message` with:

```python
        lines.append(
            f"- {_subagent_container_status_icon(container)} {_subagent_container_title(container)} · {tool_use_count} tool uses · {_subagent_container_status_text(container)}"
        )
```

- [ ] **Step 6: Run focused presenter tests and confirm they pass**

Run:

```bash
python -m pytest tests/test_structured_reply_presenter.py::test_build_tool_progress_message_includes_specific_bash_command tests/test_structured_reply_presenter.py::test_build_tool_status_message_formats_final_states tests/test_structured_reply_presenter.py::test_build_tool_task_list_message_marks_current_task tests/test_structured_reply_presenter.py::test_build_subagent_aggregate_status_message_shows_subagent_type tests/test_structured_reply_presenter.py::test_build_subagent_aggregate_status_message_formats_agent_summary -q
```

Expected:

```text
5 passed
```

### Task 4: Update message-manager tests that depend on status text

**Files:**
- Modify: `tests/test_tool_message_manager.py:170-202`

- [ ] **Step 1: Update status text assertions**

Change the affected assertions to include the new icons:

```python
async def test_tool_message_manager_sends_first_status_message() -> None:
    root = DummyRootMessage()
    manager = ToolMessageManager(root_message=root, task_id="task-1", user_id=1, provider="claude_code")

    await manager.handle(_output(ToolStatus.RUNNING))

    assert len(root.sent) == 1
    assert "🔄 执行中" in root.sent[0].text
    assert "工具: Bash" in root.sent[0].text
    assert "命令: pytest -q" in root.sent[0].text
    assert root.sent[0].parse_mode == ParseMode.HTML


@pytest.mark.asyncio
async def test_tool_message_manager_edits_existing_message_to_success() -> None:
    root = DummyRootMessage()
    manager = ToolMessageManager(root_message=root, task_id="task-1", user_id=1, provider="claude_code")

    await manager.handle(_output(ToolStatus.RUNNING))
    await manager.handle(_output(ToolStatus.SUCCESS))

    assert len(root.sent) == 1
    assert "🔄 执行中" in root.sent[0].text
    assert root.sent[0].edits
    assert "✅ 执行完成" in root.sent[0].edits[-1]


@pytest.mark.asyncio
async def test_tool_message_manager_keeps_error_status_message() -> None:
    root = DummyRootMessage()
    manager = ToolMessageManager(root_message=root, task_id="task-1", user_id=1, provider="claude_code")

    await manager.handle(_output(ToolStatus.RUNNING))
    await manager.handle(_output(ToolStatus.ERROR))

    assert len(root.sent) == 1
    assert "🔄 执行中" in root.sent[0].text
    assert "❌ 执行失败" in root.sent[0].edits[-1]
```

This intentionally asserts that the original sent message remains the running text while edits contain the final status.

- [ ] **Step 2: Run the affected message-manager tests**

Run:

```bash
python -m pytest tests/test_tool_message_manager.py::test_tool_message_manager_sends_first_status_message tests/test_tool_message_manager.py::test_tool_message_manager_edits_existing_message_to_success tests/test_tool_message_manager.py::test_tool_message_manager_keeps_error_status_message -q
```

Expected:

```text
3 passed
```

### Task 5: Run full verification and commit the feature

**Files:**
- Verify: `app/bot/presenters/structured_reply_presenter.py`
- Verify: `tests/test_structured_reply_presenter.py`
- Verify: `tests/test_tool_message_manager.py`

- [ ] **Step 1: Run required focused tests**

Run:

```bash
python -m pytest tests/test_structured_reply_presenter.py tests/test_tool_message_manager.py tests/test_command_run.py -q
```

Expected:

```text
93 passed
```

The elapsed time may differ.

- [ ] **Step 2: Run the full test suite**

Run:

```bash
python -m pytest -q
```

Expected:

```text
332 passed
```

The elapsed time may differ.

- [ ] **Step 3: Check diff cleanliness**

Run:

```bash
git diff --check
git diff --stat
git status --short
```

Expected:

```text
```

`git diff --check` should print no output. `git status --short` should show only these tracked feature files plus the pre-existing untracked `.claude/worktrees/` directory:

```text
 M app/bot/presenters/structured_reply_presenter.py
 M tests/test_structured_reply_presenter.py
 M tests/test_tool_message_manager.py
?? .claude/worktrees/
```

- [ ] **Step 4: Commit the feature**

Run:

```bash
git add app/bot/presenters/structured_reply_presenter.py tests/test_structured_reply_presenter.py tests/test_tool_message_manager.py
git commit -m "$(cat <<'EOF'
feat: add icons to telegram agent and tool statuses
EOF
)"
```

Expected: one feature commit containing only the presenter and test changes for status icons.

- [ ] **Step 5: Confirm final workspace state**

Run:

```bash
git status --short
```

Expected:

```text
?? .claude/worktrees/
```

Do not remove `.claude/worktrees/`; it predates this task.

## Self-Review

- Spec coverage: Task 3 covers plain tool status messages, subagent tool task lists, and subagent aggregate rows. Task 4 covers `ToolMessageManager` assertions that depend on the presenter output. Task 5 covers focused and full-suite verification.
- Placeholder scan: No placeholder markers or unspecified implementation steps remain.
- Type consistency: The plan uses existing `ToolStatus`, `ToolStatusOutput`, `_tool_status_icon`, `_tool_status_label`, `_subagent_container_status_values`, and existing test function names.
