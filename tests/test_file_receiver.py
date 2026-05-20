from __future__ import annotations

import pytest

from app.adapters.storage.upload_store import UploadStoreAdapter
from app.domain.file_models import FileUploadResult, FileValidationError
from app.services.file_receiver import FileReceiverService


@pytest.fixture
def upload_store(tmp_path):
    return UploadStoreAdapter(base_dir=str(tmp_path))


@pytest.fixture
def service(upload_store):
    return FileReceiverService(
        upload_store=upload_store,
        allowed_extensions={".py", ".txt", ".md", ".json"},
        max_file_size_bytes=1024,  # 1 KB for testing
    )


class TestValidateExtension:
    def test_allowed_extension(self, service):
        assert service.validate_extension("hello.py") is True

    def test_allowed_extension_case_insensitive(self, service):
        assert service.validate_extension("README.MD") is True
        assert service.validate_extension("data.JSON") is True

    def test_disallowed_extension(self, service):
        assert service.validate_extension("malware.exe") is False

    def test_no_extension(self, service):
        assert service.validate_extension("Makefile") is False

    def test_dotfile(self, service):
        assert service.validate_extension(".gitignore") is False


class TestValidateSize:
    def test_within_limit(self, service):
        assert service.validate_size(512) is True

    def test_at_limit(self, service):
        assert service.validate_size(1024) is True

    def test_exceeds_limit(self, service):
        assert service.validate_size(1025) is False

    def test_zero_size(self, service):
        assert service.validate_size(0) is True


class TestReceiveFile:
    @pytest.mark.asyncio
    async def test_successful_upload(self, service, tmp_path):
        workdir = str(tmp_path / "workdir")
        result = await service.receive_file(
            user_id=42,
            workdir=workdir,
            filename="code.py",
            data=b"print('hello')",
        )
        assert isinstance(result, FileUploadResult)
        assert result.filename == "code.py"
        assert result.size_bytes == len(b"print('hello')")
        assert result.path.exists()
        assert result.path.read_bytes() == b"print('hello')"

    @pytest.mark.asyncio
    async def test_rejected_extension(self, service, tmp_path):
        workdir = str(tmp_path / "workdir")
        result = await service.receive_file(
            user_id=42,
            workdir=workdir,
            filename="virus.exe",
            data=b"bad stuff",
        )
        assert isinstance(result, FileValidationError)
        assert result.filename == "virus.exe"
        assert ".exe" in result.reason

    @pytest.mark.asyncio
    async def test_rejected_size(self, service, tmp_path):
        workdir = str(tmp_path / "workdir")
        result = await service.receive_file(
            user_id=42,
            workdir=workdir,
            filename="big.txt",
            data=b"x" * 2048,
        )
        assert isinstance(result, FileValidationError)
        assert result.filename == "big.txt"
        assert "exceeds" in result.reason

    @pytest.mark.asyncio
    async def test_extension_checked_before_size(self, service, tmp_path):
        """Extension validation happens first — a bad extension with large data still gets extension error."""
        workdir = str(tmp_path / "workdir")
        result = await service.receive_file(
            user_id=42,
            workdir=workdir,
            filename="big.exe",
            data=b"x" * 2048,
        )
        assert isinstance(result, FileValidationError)
        assert ".exe" in result.reason
