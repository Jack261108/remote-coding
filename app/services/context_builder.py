from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from app.adapters.storage.upload_store import UploadStoreAdapter
from app.domain.file_models import TaskContext

logger = logging.getLogger(__name__)


class ContextBuilderService:
    """Collects uploaded files and builds CLI arguments for the task."""

    def __init__(self, *, upload_store: UploadStoreAdapter) -> None:
        self._upload_store = upload_store

    def build_context(
        self,
        *,
        user_id: int,
        workdir: str,
        provider: str,
        prompt: str,
        since: datetime,
    ) -> TaskContext:
        """Collect pending files and build CLI arguments for the provider."""
        file_paths = self._upload_store.collect_pending_files(user_id, workdir, since)

        if not file_paths:
            return TaskContext(
                file_paths=[],
                augmented_prompt=prompt,
                cli_args=[],
            )

        cli_args = self.build_cli_args(provider, file_paths)
        augmented_prompt = self.augment_prompt(prompt, file_paths)

        return TaskContext(
            file_paths=file_paths,
            augmented_prompt=augmented_prompt,
            cli_args=cli_args,
        )

    def build_cli_args(self, provider: str, file_paths: list[Path]) -> list[str]:
        """Build provider-specific CLI arguments for file context.

        claude_code: ["--file", path1, "--file", path2, ...]
        codex/gemini: referenced in prompt text (empty list)
        """
        if provider == "claude_code":
            args: list[str] = []
            for path in file_paths:
                args.append("--file")
                args.append(str(path))
            return args

        # For codex, gemini, and other providers: files are referenced in the prompt
        return []

    # Image extensions that Claude CLI can analyze when given a local path
    IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif"})

    def augment_prompt(self, prompt: str, file_paths: list[Path]) -> str:
        """Append file references to the prompt.

        For image files, includes the absolute path so Claude CLI can read and
        analyze them in interactive mode (where --file args are not available).
        For non-image files, includes only the filename as a summary.
        """
        if not file_paths:
            return prompt

        image_paths = [p for p in file_paths if p.suffix.lower() in self.IMAGE_EXTENSIONS]
        other_paths = [p for p in file_paths if p.suffix.lower() not in self.IMAGE_EXTENSIONS]

        parts: list[str] = []
        if image_paths:
            paths_str = " ".join(str(p.resolve()) for p in image_paths)
            parts.append(f"[Attached images: {paths_str}]")
        if other_paths:
            filenames = [p.name for p in other_paths]
            parts.append(f"[Attached files: {', '.join(filenames)}]")

        return prompt + "\n\n" + "\n".join(parts)

    async def cleanup_after_task(self, user_id: int, workdir: str) -> None:
        """Clear all upload files after task reaches final state.

        If cleanup fails, log the failure and leave files for manual intervention.
        """
        try:
            self._upload_store.clear_user_files(user_id, workdir)
        except Exception as exc:
            logger.warning(
                "Failed to clean up upload files for user=%d workdir=%s: %s",
                user_id,
                workdir,
                exc,
            )
