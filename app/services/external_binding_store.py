from __future__ import annotations

import json
import logging
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from app.domain.external_session_models import ExternalBinding

logger = logging.getLogger(__name__)


def _normalize_to_utc(value: datetime) -> datetime:
    """Return ``value`` as a timezone-aware UTC datetime.

    Naive datetimes are assumed to represent UTC and have ``timezone.utc``
    attached. Aware datetimes in non-UTC timezones are converted to UTC via
    ``astimezone``. This guarantees all loaded timestamps participate safely
    in idle-age arithmetic (`utc_now() - last_activity_at`) without raising
    naive/aware comparison errors.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class ExternalBindingStore:
    """Persists external session bindings as JSON for restart survival."""

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        self._file_path = data_dir / "external_bindings.json"
        self._bindings: dict[str, ExternalBinding] = self.load_all()
        # Per-session monotonic timestamps of the last on-disk persist driven
        # by ``touch_activity``. Used to throttle writes to at most once per
        # ``persist_min_interval_sec`` per session. Missing entry means the
        # next ``touch_activity`` call SHALL persist immediately.
        self._last_persist_at: dict[str, float] = {}

    def save_binding(self, binding: ExternalBinding) -> None:
        self._bindings[binding.session_id] = binding
        # Drop any stale throttle entry so the next ``touch_activity`` for a
        # (re-)bound session persists immediately. We pop rather than set-to-now
        # to ensure the first touch after a re-bind always hits disk.
        self._last_persist_at.pop(binding.session_id, None)
        self._persist()

    def remove_binding(self, session_id: str) -> None:
        self._bindings.pop(session_id, None)
        self._last_persist_at.pop(session_id, None)
        self._persist()

    def get_binding(self, session_id: str) -> ExternalBinding | None:
        return self._bindings.get(session_id)

    def get_bindings_for_user(self, user_id: int) -> list[ExternalBinding]:
        return [b for b in self._bindings.values() if b.user_id == user_id]

    def list_all(self) -> list[ExternalBinding]:
        """Return a snapshot list of all current bindings.

        The returned list is a fresh copy of ``self._bindings.values()`` so
        callers can iterate it safely while other code mutates the store
        (e.g. ``save_binding`` or ``remove_binding``).
        """
        return list(self._bindings.values())

    def touch_activity(
        self,
        session_id: str,
        last_activity_at: datetime,
        *,
        persist_min_interval_sec: int = 60,
    ) -> None:
        """Update the in-memory ``last_activity_at`` for ``session_id``.

        The in-memory update is always immediate. Persistence to disk is
        throttled per session: if no prior touch has persisted (or the binding
        was just (re-)saved), the call persists right away; otherwise it
        persists only when at least ``persist_min_interval_sec`` seconds have
        elapsed since the previous touch-driven persist for the same session.

        No-op if ``session_id`` is not present in the store.
        """
        binding = self._bindings.get(session_id)
        if binding is None:
            return

        # Always update in memory immediately so subsequent reads (e.g. the
        # cleanup service's re-read) observe the fresh activity timestamp.
        binding.last_activity_at = last_activity_at

        now = time.monotonic()
        last_persist = self._last_persist_at.get(session_id)
        if last_persist is None or (now - last_persist) >= persist_min_interval_sec:
            self._persist()
            self._last_persist_at[session_id] = now

    def load_all(self) -> dict[str, ExternalBinding]:
        if not self._file_path.exists():
            return {}
        try:
            data = json.loads(self._file_path.read_text(encoding="utf-8"))
            bindings: dict[str, ExternalBinding] = {}
            for session_id, entry in data.items():
                bound_at = _normalize_to_utc(datetime.fromisoformat(entry["bound_at"]))
                last_activity_raw = entry.get("last_activity_at")
                if last_activity_raw is None:
                    last_activity_at = bound_at
                else:
                    last_activity_at = _normalize_to_utc(datetime.fromisoformat(last_activity_raw))
                bindings[session_id] = ExternalBinding(
                    session_id=session_id,
                    user_id=entry["user_id"],
                    cwd=entry["cwd"],
                    bound_at=bound_at,
                    jsonl_path=entry.get("jsonl_path"),
                    last_activity_at_init=last_activity_at,
                )
            return bindings
        except (json.JSONDecodeError, KeyError, ValueError, OSError) as exc:
            logger.error("Failed to load external bindings from %s: %s", self._file_path, exc)
            return {}

    def _persist(self) -> None:
        self._data_dir.mkdir(parents=True, exist_ok=True)
        data: dict[str, dict] = {}
        for session_id, binding in self._bindings.items():
            data[session_id] = {
                "user_id": binding.user_id,
                "cwd": binding.cwd,
                "bound_at": binding.bound_at.isoformat(),
                "last_activity_at": binding.last_activity_at.isoformat(),
                "jsonl_path": binding.jsonl_path,
            }
        # Atomic write: write to temp file then rename to avoid corruption
        try:
            fd, tmp_path = tempfile.mkstemp(dir=str(self._data_dir), suffix=".tmp", prefix="external_bindings_")
            try:
                with open(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
                Path(tmp_path).replace(self._file_path)
            except BaseException:
                Path(tmp_path).unlink(missing_ok=True)
                raise
        except OSError as exc:
            logger.error("Failed to persist external bindings: %s", exc)
