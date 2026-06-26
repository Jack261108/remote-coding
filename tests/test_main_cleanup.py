from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.main as main_module


@pytest.mark.asyncio
async def test_run_stops_container_when_start_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    container = SimpleNamespace(
        wire=MagicMock(),
        start=AsyncMock(side_effect=RuntimeError("start failed")),
        stop=AsyncMock(),
        dispatcher=SimpleNamespace(start_polling=AsyncMock()),
        bot=SimpleNamespace(),
    )
    monkeypatch.setattr(main_module, "AppContainer", lambda settings: container)
    monkeypatch.setattr(main_module, "configure_logging", lambda: None)

    with pytest.raises(RuntimeError, match="start failed"):
        await main_module.run(SimpleNamespace(tg_polling_retry_delay_sec=0))

    container.wire.assert_called_once()
    container.start.assert_awaited_once()
    container.stop.assert_awaited_once()
    container.dispatcher.start_polling.assert_not_awaited()
