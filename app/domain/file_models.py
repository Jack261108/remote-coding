from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass
class FileUpload:
    user_id: int
    filename: str
    original_filename: str
    size_bytes: int
    path: Path
    uploaded_at: datetime
    workdir: str


@dataclass
class FileUploadResult:
    filename: str
    size_bytes: int
    path: Path


@dataclass
class FileValidationError:
    filename: str
    reason: str


@dataclass
class TaskContext:
    file_paths: list[Path]
    augmented_prompt: str
    cli_args: list[str]


@dataclass
class ExportResult:
    file_path: Path
    filename: str
    mime_type: str


@dataclass
class DiffResult:
    content: str
    file_count: int
    is_patch_file: bool  # True if content >= 4096 chars


@dataclass
class FileSnapshot:
    path: Path
    mtime: float
