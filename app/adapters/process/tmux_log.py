from __future__ import annotations

from pathlib import Path

from app.domain.models import CLIEvent, EventType


class TmuxLogMixin:
    def _read_new_text(self, path: Path, position: int) -> tuple[str, int]:
        if not path.exists():
            return "", position
        file_size = path.stat().st_size
        if position > file_size:
            position = 0
        with path.open("rb") as fh:
            fh.seek(position)
            data = fh.read()
            return data.decode("utf-8", errors="replace"), fh.tell()

    def _split_to_events(self, *, task_id: str, text: str) -> tuple[str, list[CLIEvent]]:
        events: list[CLIEvent] = []
        parts = text.splitlines(keepends=True)
        partial = ""
        for chunk in parts:
            if chunk.endswith("\n"):
                events.append(CLIEvent(type=EventType.STDOUT, task_id=task_id, content=chunk))
            else:
                partial += chunk
        return partial, events

    def _read_exit_code(self, path: Path) -> int | None:
        try:
            return int(path.read_text(encoding="utf-8").strip())
        except Exception:
            return None

    def _safe_unlink(self, path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except Exception:
            return

    def _interactive_log_position(self, path: Path) -> int:
        try:
            return path.stat().st_size
        except FileNotFoundError:
            return 0
        except Exception:
            return 0
