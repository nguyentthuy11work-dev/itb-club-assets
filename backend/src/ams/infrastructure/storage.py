"""Private local and object-storage evidence adapters."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
from abc import ABC, abstractmethod
from collections.abc import AsyncIterable
from pathlib import Path, PurePosixPath
from uuid import UUID, uuid4

from ams.application.evidence import EvidenceValidationError
from ams.application.reliability import IFileStorage, StagedFile

MAX_IMAGE_SIZE_BYTES = 10 * 1024 * 1024
SUPPORTED_CONTENT_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})
OWNER_TYPE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


class IObjectStorageClient(ABC):
    """Minimal private-object client implemented by the selected production provider."""

    @abstractmethod
    async def put_private(
        self,
        object_key: str,
        content: bytes,
        content_type: str,
        content_sha256: str,
    ) -> None:
        """Create a private object with integrity metadata."""

    @abstractmethod
    async def move_private(
        self,
        source_key: str,
        target_key: str,
        owner_type: str,
        owner_id: UUID,
    ) -> None:
        """Move a staged object under permanent resource ownership."""

    @abstractmethod
    async def delete_private(self, object_key: str) -> None:
        """Delete a private object idempotently."""


class LocalFileStorage(IFileStorage):
    """Development adapter storing private evidence beneath one configured root."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()
        self._root.mkdir(mode=0o700, parents=True, exist_ok=True)

    async def stage(
        self, content: AsyncIterable[bytes], declared_content_type: str
    ) -> StagedFile:
        """Validate one image stream and write it under an opaque staging key."""

        staged = await _read_and_validate(content, declared_content_type)
        staging_key = _new_key("staging")
        path = self._resolve_key(staging_key)
        await asyncio.to_thread(_write_exclusive, path, staged.content)
        return StagedFile(
            staging_key=staging_key,
            content_type=staged.content_type,
            content_sha256=staged.content_sha256,
            size_bytes=len(staged.content),
        )

    async def finalize(self, staging_key: str, owner_type: str, owner_id: UUID) -> str:
        """Atomically move a staged file to an opaque permanent key."""

        _validate_owner(owner_type)
        source = self._resolve_key(staging_key, required_prefix="staging")
        target_key = _new_key("objects")
        target = self._resolve_key(target_key)
        await asyncio.to_thread(_move_exclusive, source, target)
        return target_key

    async def delete(self, object_key: str) -> None:
        """Delete a private object idempotently without accepting path traversal."""

        path = self._resolve_key(object_key)
        await asyncio.to_thread(_unlink_missing_ok, path)

    def _resolve_key(self, object_key: str, required_prefix: str | None = None) -> Path:
        _validate_object_key(object_key, required_prefix)
        pure_key = PurePosixPath(object_key)
        path = self._root.joinpath(*pure_key.parts).resolve()
        if not path.is_relative_to(self._root):
            raise EvidenceValidationError("Storage object key is invalid.")
        return path


class ObjectStorageFileStorage(IFileStorage):
    """Production adapter using an injected private object-storage client."""

    def __init__(self, client: IObjectStorageClient) -> None:
        self._client = client

    async def stage(
        self, content: AsyncIterable[bytes], declared_content_type: str
    ) -> StagedFile:
        """Validate and upload one object under an opaque staging key."""

        staged = await _read_and_validate(content, declared_content_type)
        staging_key = _new_key("staging")
        await self._client.put_private(
            staging_key,
            staged.content,
            staged.content_type,
            staged.content_sha256,
        )
        return StagedFile(
            staging_key=staging_key,
            content_type=staged.content_type,
            content_sha256=staged.content_sha256,
            size_bytes=len(staged.content),
        )

    async def finalize(self, staging_key: str, owner_type: str, owner_id: UUID) -> str:
        """Move a staged private object under resource ownership."""

        _validate_staging_key(staging_key)
        _validate_owner(owner_type)
        target_key = _new_key("objects")
        await self._client.move_private(staging_key, target_key, owner_type, owner_id)
        return target_key

    async def delete(self, object_key: str) -> None:
        """Delete a private object idempotently through the provider client."""

        _validate_object_key(object_key)
        await self._client.delete_private(object_key)


class _ValidatedImage:
    def __init__(self, content: bytes, content_type: str, content_sha256: str) -> None:
        self.content = content
        self.content_type = content_type
        self.content_sha256 = content_sha256


async def _read_and_validate(
    content: AsyncIterable[bytes], declared_content_type: str
) -> _ValidatedImage:
    if declared_content_type not in SUPPORTED_CONTENT_TYPES:
        raise EvidenceValidationError("Evidence content type is not supported.")
    payload = bytearray()
    digest = hashlib.sha256()
    async for chunk in content:
        if not isinstance(chunk, bytes):
            raise EvidenceValidationError("Evidence stream must contain bytes.")
        if len(payload) + len(chunk) > MAX_IMAGE_SIZE_BYTES:
            raise EvidenceValidationError("Evidence image exceeds the 10 MB limit.")
        payload.extend(chunk)
        digest.update(chunk)
    if not payload:
        raise EvidenceValidationError("Evidence image must not be empty.")
    actual_content_type = _detect_content_type(bytes(payload))
    if actual_content_type != declared_content_type:
        raise EvidenceValidationError("Evidence content does not match its declared type.")
    return _ValidatedImage(bytes(payload), actual_content_type, digest.hexdigest())


def _detect_content_type(content: bytes) -> str:
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith(b"\xff\xd8\xff") and content.endswith(b"\xff\xd9"):
        return "image/jpeg"
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    raise EvidenceValidationError("Evidence file signature is not JPEG, PNG, or WebP.")


def _new_key(prefix: str) -> str:
    return f"{prefix}/{uuid4().hex}"


def _validate_owner(owner_type: str) -> None:
    if OWNER_TYPE_PATTERN.fullmatch(owner_type) is None:
        raise EvidenceValidationError("Storage owner type is invalid.")


def _validate_staging_key(object_key: str) -> None:
    _validate_object_key(object_key, required_prefix="staging")


def _validate_object_key(object_key: str, required_prefix: str | None = None) -> None:
    key = PurePosixPath(object_key)
    if (
        key.is_absolute()
        or ".." in key.parts
        or len(key.parts) != 2
        or key.parts[0] not in {"staging", "objects"}
        or (required_prefix is not None and key.parts[0] != required_prefix)
        or not re.fullmatch(r"[0-9a-f]{32}", key.parts[1])
    ):
        raise EvidenceValidationError("Storage object key is invalid.")


def _write_exclusive(path: Path, content: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as target:
        target.write(content)


def _move_exclusive(source: Path, target: Path) -> None:
    if not source.is_file():
        raise EvidenceValidationError("Staged evidence object is unavailable.")
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if target.exists():
        raise EvidenceValidationError("Permanent evidence key already exists.")
    os.replace(source, target)


def _unlink_missing_ok(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
