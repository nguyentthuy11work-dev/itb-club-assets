"""Application orchestration for bounded private evidence batches."""

from __future__ import annotations

from collections.abc import AsyncIterable
from dataclasses import dataclass
from uuid import UUID

from ams.application.reliability import IFileStorage, ReliabilityError, StagedFile

MAX_EVIDENCE_FILES = 3


class EvidenceValidationError(ReliabilityError):
    """Raised when an evidence batch violates the approved image policy."""

    code = "INVALID_EVIDENCE"


@dataclass(frozen=True, slots=True)
class EvidenceFileInput:
    """One upload stream and its declared multipart content type."""

    content: AsyncIterable[bytes]
    declared_content_type: str


class EvidenceStorageService:
    """Stage, finalize, and clean up one evidence batch through a storage port."""

    def __init__(self, storage: IFileStorage) -> None:
        self._storage = storage

    async def stage_images(
        self, files: tuple[EvidenceFileInput, ...]
    ) -> tuple[StagedFile, ...]:
        """Stage one to three images and clean all prior files if any input fails."""

        if not 1 <= len(files) <= MAX_EVIDENCE_FILES:
            raise EvidenceValidationError("Evidence requires one to three image files.")
        staged: list[StagedFile] = []
        try:
            for file in files:
                staged.append(
                    await self._storage.stage(
                        file.content,
                        file.declared_content_type,
                    )
                )
        except Exception:
            await self.discard(tuple(staged))
            raise
        return tuple(staged)

    async def finalize(
        self,
        staged_files: tuple[StagedFile, ...],
        *,
        owner_type: str,
        owner_id: UUID,
    ) -> tuple[str, ...]:
        """Finalize all files or remove every finalized/staged object on failure."""

        finalized: list[str] = []
        try:
            for staged in staged_files:
                finalized.append(
                    await self._storage.finalize(
                        staged.staging_key,
                        owner_type,
                        owner_id,
                    )
                )
        except Exception:
            for object_key in finalized:
                await self._storage.delete(object_key)
            await self.discard(staged_files[len(finalized) :])
            raise
        return tuple(finalized)

    async def discard(self, staged_files: tuple[StagedFile, ...]) -> None:
        """Idempotently remove every staged object after rollback or deduplication."""

        for staged in staged_files:
            await self._storage.delete(staged.staging_key)

    async def delete_objects(self, object_keys: tuple[str, ...]) -> None:
        """Idempotently remove finalized objects after transaction failure."""

        for object_key in object_keys:
            await self._storage.delete(object_key)
