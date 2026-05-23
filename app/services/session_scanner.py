"""Service for discovering past Claude Code sessions from JSONL files on disk."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.adapters.claude.paths import ClaudePaths

logger = logging.getLogger(__name__)


@dataclass
class SessionInfo:
    session_id: str
    modified_at: datetime
    summary: str


class SessionScanner:
    """Scans Claude session JSONL files for a given workdir and returns metadata."""

    @staticmethod
    def encode_workdir(workdir: str) -> str:
        """Encode a workdir path the same way Claude CLI does: replace `/` with `-`."""
        return workdir.replace("/", "-")

    def scan(
        self,
        workdir: str,
        claude_paths: ClaudePaths,
        max_results: int = 10,
    ) -> list[SessionInfo]:
        """Scan for session files and return the most recent ones.

        Only reads `.jsonl` files directly in the encoded workdir directory,
        excluding any files inside a `subagents/` subdirectory.
        """
        encoded = self.encode_workdir(workdir)
        session_dir = claude_paths.projects_dir / encoded

        if not session_dir.is_dir():
            return []

        sessions: list[SessionInfo] = []

        for path in session_dir.iterdir():
            if not path.is_file() or path.suffix != ".jsonl":
                continue

            session_id = path.stem
            try:
                mtime = path.stat().st_mtime
                modified_at = datetime.fromtimestamp(mtime, tz=timezone.utc)
            except OSError:
                logger.debug("Cannot stat %s, skipping", path)
                continue

            summary = self._extract_first_human_message(path)

            sessions.append(
                SessionInfo(
                    session_id=session_id,
                    modified_at=modified_at,
                    summary=summary,
                )
            )

        sessions.sort(key=lambda s: s.modified_at, reverse=True)
        return sessions[:max_results]

    def _extract_first_human_message(self, path: Path) -> str:
        """Extract the content of the first human message from a JSONL file."""
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    if payload.get("type") != "human":
                        continue

                    message = payload.get("message")
                    if not isinstance(message, dict):
                        continue

                    content = message.get("content")
                    if isinstance(content, str):
                        return content.strip()

                    if isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                text = block.get("text", "")
                                if isinstance(text, str) and text.strip():
                                    return text.strip()
        except OSError:
            logger.debug("Cannot read %s, returning empty summary", path)

        return ""
