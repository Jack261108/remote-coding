from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.domain.hook_models import validate_session_id
from app.domain.models import SessionContext
from app.domain.session_models import ConversationTurn, ParserCheckpoint, SessionPhase, SessionState

logger = logging.getLogger(__name__)


class FileSessionStore:
    def __init__(self, base_dir: str) -> None:
        root = Path(base_dir)
        self._base_dir = root / "sessions"
        self._context_dir = root / "session_contexts"
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._context_dir.mkdir(parents=True, exist_ok=True)

    def _write_json_atomic(self, path: Path, payload: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as fh:
            tmp_path = Path(fh.name)
            try:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            except BaseException:
                tmp_path.unlink(missing_ok=True)
                raise
        tmp_path.replace(path)

    def session_dir(self, session_id: str) -> Path:
        safe_session_id = validate_session_id(session_id)
        base_dir = self._base_dir.resolve()
        path = (base_dir / safe_session_id).resolve()
        if path != base_dir and base_dir not in path.parents:
            raise ValueError("session_id 路径非法")
        path.mkdir(parents=True, exist_ok=True)
        return path

    def raw_transcript_path(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "transcript.raw.log"

    def cursor_path(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "parser.cursor.json"

    def state_path(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "session.state.json"

    def conversation_path(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "conversation.snapshot.json"

    def load_checkpoint(self, session_id: str) -> ParserCheckpoint:
        path = self.cursor_path(session_id)
        if not path.exists():
            return ParserCheckpoint()
        return ParserCheckpoint.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def save_checkpoint(self, session_id: str, checkpoint: ParserCheckpoint) -> None:
        self._write_json_atomic(self.cursor_path(session_id), checkpoint.to_dict())

    def load_session_state(self, session_id: str) -> SessionState | None:
        path = self.state_path(session_id)
        if not path.exists():
            return None
        return SessionState.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def list_session_states(self) -> list[SessionState]:
        states: list[SessionState] = []
        for path in sorted(self._base_dir.glob("*/session.state.json")):
            try:
                states.append(SessionState.from_dict(json.loads(path.read_text(encoding="utf-8"))))
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
                continue
        return states

    def load_conversation(self, session_id: str) -> list[ConversationTurn]:
        path = self.conversation_path(session_id)
        if not path.exists():
            return []
        return [ConversationTurn.from_dict(item) for item in json.loads(path.read_text(encoding="utf-8"))]

    def save_session_state(self, state: SessionState) -> None:
        self._write_json_atomic(self.state_path(state.session_id), state.to_dict())
        self._write_json_atomic(self.conversation_path(state.session_id), [turn.to_dict() for turn in state.turns])

    def session_context_path(self, user_id: int) -> Path:
        return self._context_dir / f"{user_id}.json"

    def load_session_context(self, user_id: int) -> SessionContext | None:
        path = self.session_context_path(user_id)
        if not path.exists():
            return None
        return SessionContext.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def list_session_contexts(self) -> list[SessionContext]:
        contexts: list[SessionContext] = []
        for path in sorted(self._context_dir.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                contexts.append(SessionContext.from_dict(payload))
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
                continue
        return contexts

    def save_session_context(self, session: SessionContext) -> None:
        self._write_json_atomic(self.session_context_path(session.user_id), session.to_dict())

    def delete_session(self, session_id: str) -> bool:
        """Delete a session directory and all its contents.

        Returns:
            True if the session was deleted, False if it didn't exist.
        """
        safe_session_id = validate_session_id(session_id)
        base_dir = self._base_dir.resolve()
        path = (base_dir / safe_session_id).resolve()
        if path == base_dir or base_dir not in path.parents:
            return False
        if not path.exists():
            return False
        shutil.rmtree(path)
        logger.info("deleted session directory", extra={"session_id": session_id})
        return True

    def cleanup_stale_sessions(self, max_age_hours: int = 24) -> int:
        """Clean up session directories for sessions that have ended and are older than max_age_hours.

        Returns:
            Number of sessions deleted.
        """
        now = datetime.now(UTC)
        max_age = timedelta(hours=max_age_hours)
        deleted_count = 0

        for state_path in sorted(self._base_dir.glob("*/session.state.json")):
            try:
                state = SessionState.from_dict(json.loads(state_path.read_text(encoding="utf-8")))
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
                continue

            # Only clean up ended or interrupted sessions.
            if state.phase != SessionPhase.ENDED and not state.interrupted:
                continue

            # Check if session is old enough.
            ended_at = state.last_activity
            if now - ended_at < max_age:
                continue

            # Delete the session directory
            session_dir = state_path.parent
            try:
                shutil.rmtree(session_dir)
                deleted_count += 1
                logger.info(
                    "cleaned up stale session",
                    extra={"session_id": state.session_id, "ended_at": ended_at.isoformat()},
                )
            except OSError as exc:
                logger.warning(
                    "failed to clean up stale session",
                    extra={"session_id": state.session_id, "error": str(exc)},
                )

        return deleted_count
