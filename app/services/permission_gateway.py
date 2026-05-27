from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BackendDispatchSucceeded:
    pass


@dataclass(frozen=True, slots=True)
class BackendDispatchFailed:
    reason: str


@dataclass(frozen=True, slots=True)
class BackendDispatchUnknown:
    reason: str


BackendDispatchResult = BackendDispatchSucceeded | BackendDispatchFailed | BackendDispatchUnknown


@dataclass(frozen=True, slots=True)
class CallbackResponse:
    alert_text: str
    show_alert: bool
    edit_message_text: str
    clear_keyboard: bool
