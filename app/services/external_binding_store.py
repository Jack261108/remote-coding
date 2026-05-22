from __future__ import annotations

import json
import logging
import tempfile
from datetime import datetime
from pathlib import Path

from app.domain.external_session_models import ExternalBinding

logger = logging.getLogger(__name__)


class ExternalBindingStore:
    """Persists external session bindings as JSON for restart survival."""

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        self._file_path = data_dir / "external_bindings.json"
        self._bindings: dict[str, ExternalBinding] = self.load_all()

    def save_binding(self, binding: ExternalBinding) -> None:
        self._bindings[binding.session_id] = binding
        self._persist()

    def remove_binding(self, session_id: str) -> None:
        self._bindings.pop(session_id, None)
        self._persist()

    def get_binding(self, session_id: str) -> ExternalBinding | None:
        return self._bindings.get(session_id)

    def get_bindings_for_user(self, user_id: int) -> list[ExternalBinding]:
        return [b for b in self._bindings.values() if b.user_id == user_id]

    def load_all(self) -> dict[str, ExternalBinding]:
        if not self._file_path.exists():
            return {}
        try:
            data = json.loads(self._file_path.read_text(encoding="utf-8"))
            bindings: dict[str, ExternalBinding] = {}
            for session_id, entry in data.items():
                bindings[session_id] = ExternalBinding(
                    session_id=session_id,
                    user_id=entry["user_id"],
                    cwd=entry["cwd"],
                    bound_at=datetime.fromisoformat(entry["bound_at"]),
                    jsonl_path=entry.get("jsonl_path"),
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
