from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.adapters.process import pty_injector


class _FakeProcess:
    def __init__(self, *, stdout: bytes, returncode: int = 0) -> None:
        self._stdout = stdout
        self.returncode = returncode

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, b""


@pytest.mark.asyncio
async def test_find_tmux_targets_for_pid_walks_ancestors_and_preserves_pane_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout = b"tgcli_user_1 %1 100\nother-session %2 200\nmalformed\ninvalid %3 nope\n"
    calls: list[tuple[object, ...]] = []

    async def fake_create_subprocess_exec(*args: object, **_kwargs: object) -> _FakeProcess:
        calls.append(args)
        return _FakeProcess(stdout=stdout)

    parents = {300: 250, 250: 100}

    async def fake_get_ppid(pid: int) -> int | None:
        return parents.get(pid)

    monkeypatch.setattr(pty_injector.shutil, "which", lambda _bin: "/usr/bin/tmux")
    monkeypatch.setattr(pty_injector.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(pty_injector, "_get_ppid", fake_get_ppid)

    pane_id = await pty_injector.find_tmux_pane_for_pid(300)
    session_name = await pty_injector.find_tmux_session_for_pid(300)

    assert pane_id == "%1"
    assert session_name == "tgcli_user_1"
    assert calls
    assert "#{session_name}" in str(calls[0][-1])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("which_result", "returncode"),
    [(None, 0), ("/usr/bin/tmux", 1)],
)
async def test_find_tmux_session_for_pid_returns_none_when_tmux_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    which_result: str | None,
    returncode: int,
) -> None:
    monkeypatch.setattr(pty_injector.shutil, "which", lambda _bin: which_result)
    monkeypatch.setattr(
        pty_injector.asyncio,
        "create_subprocess_exec",
        lambda *_args, **_kwargs: SimpleNamespace(),
    )
    if which_result is not None:

        async def fake_create_subprocess_exec(*_args: object, **_kwargs: object) -> _FakeProcess:
            return _FakeProcess(stdout=b"tgcli_user_1 %1 100\n", returncode=returncode)

        monkeypatch.setattr(pty_injector.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    assert await pty_injector.find_tmux_session_for_pid(300) is None
