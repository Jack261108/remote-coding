"""Tests for FileSenderService.

Covers: resolve_path, classify, build_caption, send_if_eligible (all branches).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.file_sender import FileSenderService
from app.services.message_sender import MessageSender

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_service(
    *,
    enabled: bool = True,
    extensions: set[str] | None = None,
    image_extensions: set[str] | None = None,
    photo_max_bytes: int = 10 * 1024 * 1024,
    document_max_bytes: int = 50 * 1024 * 1024,
) -> tuple[FileSenderService, MagicMock]:
    sender = MagicMock(spec=MessageSender)
    sender.send_photo = AsyncMock()
    sender.send_document = AsyncMock()
    service = FileSenderService(
        message_sender=sender,
        enabled=enabled,
        extensions=extensions or {".png", ".jpg", ".pdf", ".txt", ".tar.gz"},
        image_extensions=image_extensions or {".png", ".jpg"},
        photo_max_bytes=photo_max_bytes,
        document_max_bytes=document_max_bytes,
    )
    return service, sender


# ---------------------------------------------------------------------------
# resolve_path
# ---------------------------------------------------------------------------


class TestResolvePath:
    def test_absolute_path_returned_as_is(self):
        service, _ = _make_service()
        result = service.resolve_path("/absolute/file.txt", "/cwd")
        assert result == Path("/absolute/file.txt")

    def test_relative_path_joined_with_cwd(self):
        service, _ = _make_service()
        result = service.resolve_path("relative/file.txt", "/cwd")
        assert result == Path("/cwd/relative/file.txt")


# ---------------------------------------------------------------------------
# classify
# ---------------------------------------------------------------------------


class TestClassify:
    def test_classifies_image(self):
        service, _ = _make_service()
        assert service.classify("photo.png") == "image"
        assert service.classify("photo.jpg") == "image"

    def test_classifies_document(self):
        service, _ = _make_service()
        assert service.classify("report.pdf") == "document"
        assert service.classify("notes.txt") == "document"

    def test_classifies_tar_gz(self):
        service, _ = _make_service(extensions={".tar.gz", ".png"}, image_extensions=set())
        assert service.classify("archive.tar.gz") == "document"

    def test_returns_none_for_unsupported_extension(self):
        service, _ = _make_service()
        assert service.classify("script.py") is None

    def test_returns_none_for_no_extension(self):
        service, _ = _make_service()
        assert service.classify("Makefile") is None

    def test_case_insensitive(self):
        service, _ = _make_service()
        assert service.classify("photo.PNG") == "image"


# ---------------------------------------------------------------------------
# build_caption
# ---------------------------------------------------------------------------


class TestBuildCaption:
    def test_relative_path(self):
        service, _ = _make_service()
        caption = service.build_caption(Path("/project/src/file.txt"), "/project")
        assert "file.txt" in caption
        assert "./src/file.txt" in caption

    def test_path_outside_cwd(self):
        service, _ = _make_service()
        caption = service.build_caption(Path("/other/file.txt"), "/project")
        assert "file.txt" in caption
        assert "/other/file.txt" in caption


# ---------------------------------------------------------------------------
# send_if_eligible
# ---------------------------------------------------------------------------


class TestSendIfEligible:
    @pytest.mark.asyncio
    async def test_skips_when_disabled(self, tmp_path):
        service, sender = _make_service(enabled=False)
        await service.send_if_eligible(file_path_raw="test.png", cwd=str(tmp_path), chat_id=1)
        sender.send_photo.assert_not_awaited()
        sender.send_document.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_when_empty_path(self, tmp_path):
        service, sender = _make_service()
        await service.send_if_eligible(file_path_raw="", cwd=str(tmp_path), chat_id=1)
        sender.send_photo.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_when_unsupported_extension(self, tmp_path):
        service, sender = _make_service()
        f = tmp_path / "script.py"
        f.write_text("print('hello')")
        await service.send_if_eligible(file_path_raw=str(f), cwd=str(tmp_path), chat_id=1)
        sender.send_photo.assert_not_awaited()
        sender.send_document.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_when_file_not_exists(self, tmp_path):
        service, sender = _make_service()
        await service.send_if_eligible(file_path_raw=str(tmp_path / "missing.png"), cwd=str(tmp_path), chat_id=1)
        sender.send_photo.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_sends_image(self, tmp_path):
        service, sender = _make_service()
        f = tmp_path / "photo.png"
        f.write_bytes(b"\x89PNG")
        await service.send_if_eligible(file_path_raw=str(f), cwd=str(tmp_path), chat_id=42)
        sender.send_photo.assert_awaited_once()
        args = sender.send_photo.call_args
        assert args[0][0] == 42
        assert args[0][1] == f

    @pytest.mark.asyncio
    async def test_sends_document(self, tmp_path):
        service, sender = _make_service()
        f = tmp_path / "report.pdf"
        f.write_bytes(b"%PDF")
        await service.send_if_eligible(file_path_raw=str(f), cwd=str(tmp_path), chat_id=99)
        sender.send_document.assert_awaited_once()
        args = sender.send_document.call_args
        assert args[0][0] == 99

    @pytest.mark.asyncio
    async def test_skips_oversized_image(self, tmp_path):
        service, sender = _make_service(photo_max_bytes=10)
        f = tmp_path / "big.png"
        f.write_bytes(b"\x89PNG" + b"\x00" * 100)
        await service.send_if_eligible(file_path_raw=str(f), cwd=str(tmp_path), chat_id=1)
        sender.send_photo.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_oversized_document(self, tmp_path):
        service, sender = _make_service(document_max_bytes=10)
        f = tmp_path / "big.pdf"
        f.write_bytes(b"%PDF" + b"\x00" * 100)
        await service.send_if_eligible(file_path_raw=str(f), cwd=str(tmp_path), chat_id=1)
        sender.send_document.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_handles_unexpected_exception_gracefully(self, tmp_path):
        service, sender = _make_service()
        f = tmp_path / "photo.png"
        f.write_bytes(b"\x89PNG")
        sender.send_photo.side_effect = RuntimeError("telegram down")
        # Should not raise
        await service.send_if_eligible(file_path_raw=str(f), cwd=str(tmp_path), chat_id=1)


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
