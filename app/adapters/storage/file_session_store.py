from __future__ import annotations

import json
from pathlib import Path

from app.domain.models import SessionContext
from app.domain.session_models import ConversationTurn, ParserCheckpoint, SessionState


class FileSessionStore:
    def __init__(self, base_dir: str) -> None:
        root = Path(base_dir)
        self._base_dir = root / "sessions"
        self._context_dir = root / "session_contexts"
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._context_dir.mkdir(parents=True, exist_ok=True)

    def session_dir(self, session_id: str) -> Path:
        path = self._base_dir / session_id
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
        self.cursor_path(session_id).write_text(
            json.dumps(checkpoint.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def load_session_state(self, session_id: str) -> SessionState | None:
        path = self.state_path(session_id)
        if not path.exists():
            return None
        return SessionState.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def load_conversation(self, session_id: str) -> list[ConversationTurn]:
        path = self.conversation_path(session_id)
        if not path.exists():
            return []
        return [ConversationTurn.from_dict(item) for item in json.loads(path.read_text(encoding="utf-8"))]

    def save_session_state(self, state: SessionState) -> None:
        self.state_path(state.session_id).write_text(
            json.dumps(state.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self.conversation_path(state.session_id).write_text(
            json.dumps([turn.to_dict() for turn in state.turns], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

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
        self.session_context_path(session.user_id).write_text(
            json.dumps(session.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
