# Telegram Status Icons Design

## Goal

Add consistent status icons to Telegram messages for running and completed agents/tools, and apply the same icon treatment to all supported status states.

## Scope

Only the presenter layer changes. The implementation will update `app/bot/presenters/structured_reply_presenter.py` and related tests. It will not change task/session data models, polling behavior, Telegram send/edit behavior, or persistence.

## Status Icon Mapping

Use the existing `_tool_status_icon(status)` mapping as the single source of truth:

- `running` -> `🔄`
- `success` -> `✅`
- `error` -> `❌`
- `interrupted` -> `⏹️`
- `waiting_for_approval` and unknown statuses -> `⏳`

Existing status text remains unchanged except for the icon prefix.

## Messages to Update

1. Plain tool status messages from `build_tool_status_message`:
   - `🔄 执行中`
   - `✅ 执行完成`
   - `❌ 执行失败`
   - `⏹️ 已中断`
   - `⏳ 等待权限`
   - Resumed running remains running text with a running icon.

2. Subagent tool task lists from `build_tool_task_list_message`:
   - Add the icon to the current tool line.
   - Add the icon before each listed subagent tool name.
   - Keep the active marker `=>` unchanged.

3. Agent/task aggregate summaries from `build_subagent_aggregate_status_message`:
   - Add the container status icon before each displayed agent/task title.
   - Keep the aggregate heading text unchanged.
   - Keep tool name summaries unchanged.

4. Existing task-list and file-tool aggregate messages already use icons. They should remain behaviorally unchanged.

## Testing

Update presenter tests to assert icons in:

- Plain tool status messages for success, error, interrupted, waiting, and running paths.
- Subagent tool task list current line and list rows.
- Subagent aggregate rows for running and finished agents.
- Existing file-tool and task-list icon behavior should continue passing.

Run at least:

```bash
python -m pytest tests/test_structured_reply_presenter.py tests/test_tool_message_manager.py tests/test_command_run.py -q
```

Run the full test suite before claiming completion:

```bash
python -m pytest -q
```

## Implementation Note

Before code changes, preserve the current workspace separation. The existing uncommitted simplification in `app/bot/presenters/structured_reply_presenter.py` must be handled before applying this feature so the final diff stays reviewable.
