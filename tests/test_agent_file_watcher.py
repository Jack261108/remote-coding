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

    # Store task before forget pops it
    task = watcher._tasks["session-1"]
    watcher.forget("session-1")

    assert "session-1:tool-a:agent-a" not in watcher._seen_mtimes
    assert "session-1" in watcher._session_locks

    release_update.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)

    assert "session-1" not in watcher._session_locks
