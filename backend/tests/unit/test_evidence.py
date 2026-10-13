"""Unit tests for evidence batch orchestration and cleanup."""

from __future__ import annotations

import unittest
from collections.abc import AsyncIterable
from uuid import UUID

from ams.application.evidence import (
    EvidenceFileInput,
    EvidenceStorageService,
    EvidenceValidationError,
)
from ams.application.reliability import IFileStorage, StagedFile

OWNER_ID = UUID("00000000-0000-4000-8000-000000000801")


async def _content(value: bytes = b"content") -> AsyncIterable[bytes]:
    yield value


class FakeFileStorage(IFileStorage):
    """Controllable storage double recording cleanup behavior."""

    def __init__(self) -> None:
        self.stage_calls = 0
        self.finalize_calls = 0
        self.fail_stage_at: int | None = None
        self.fail_finalize_at: int | None = None
        self.deleted: list[str] = []

    async def stage(
        self, content: AsyncIterable[bytes], declared_content_type: str
    ) -> StagedFile:
        """Return deterministic staged metadata or fail at the configured call."""

        self.stage_calls += 1
        if self.stage_calls == self.fail_stage_at:
            raise EvidenceValidationError("stage failed")
        return StagedFile(
            f"staging/{self.stage_calls:032x}",
            declared_content_type,
            "a" * 64,
            7,
        )

    async def finalize(self, staging_key: str, owner_type: str, owner_id: UUID) -> str:
        """Return deterministic permanent keys or fail at the configured call."""

        self.finalize_calls += 1
        if self.finalize_calls == self.fail_finalize_at:
            raise EvidenceValidationError("finalize failed")
        return f"objects/{self.finalize_calls:032x}"

    async def delete(self, object_key: str) -> None:
        """Record idempotent cleanup requests."""

        self.deleted.append(object_key)


class EvidenceStorageServiceTests(unittest.IsolatedAsyncioTestCase):
    """Prove batch limits, staging cleanup, and partial-finalize cleanup."""

    async def test_stage_and_finalize_valid_batch(self) -> None:
        """One valid batch returns ordered staged and permanent opaque keys."""

        storage = FakeFileStorage()
        service = EvidenceStorageService(storage)
        staged = await service.stage_images(
            (
                EvidenceFileInput(_content(), "image/png"),
                EvidenceFileInput(_content(), "image/jpeg"),
            )
        )
        finalized = await service.finalize(
            staged,
            owner_type="CUSTODY",
            owner_id=OWNER_ID,
        )

        self.assertEqual(len(staged), 2)
        self.assertEqual(
            finalized,
            (
                "objects/00000000000000000000000000000001",
                "objects/00000000000000000000000000000002",
            ),
        )
        self.assertEqual(storage.deleted, [])

    async def test_batch_requires_one_to_three_files(self) -> None:
        """Empty and excessive batches fail before touching storage."""

        storage = FakeFileStorage()
        service = EvidenceStorageService(storage)
        for files in (
            (),
            tuple(EvidenceFileInput(_content(), "image/png") for _ in range(4)),
        ):
            with self.subTest(count=len(files)), self.assertRaises(EvidenceValidationError):
                await service.stage_images(files)
        self.assertEqual(storage.stage_calls, 0)

    async def test_stage_failure_removes_every_prior_staged_file(self) -> None:
        """A later invalid upload cannot orphan earlier files in its batch."""

        storage = FakeFileStorage()
        storage.fail_stage_at = 2
        service = EvidenceStorageService(storage)
        with self.assertRaises(EvidenceValidationError):
            await service.stage_images(
                (
                    EvidenceFileInput(_content(), "image/png"),
                    EvidenceFileInput(_content(), "image/png"),
                )
            )
        self.assertEqual(storage.deleted, ["staging/00000000000000000000000000000001"])

    async def test_finalize_failure_removes_finalized_and_remaining_staged_files(self) -> None:
        """Partial ownership transfer is reversed when a later move fails."""

        storage = FakeFileStorage()
        storage.fail_finalize_at = 2
        service = EvidenceStorageService(storage)
        staged = (
            StagedFile("staging/00000000000000000000000000000001", "image/png", "a" * 64, 1),
            StagedFile("staging/00000000000000000000000000000002", "image/png", "b" * 64, 1),
            StagedFile("staging/00000000000000000000000000000003", "image/png", "c" * 64, 1),
        )
        with self.assertRaises(EvidenceValidationError):
            await service.finalize(staged, owner_type="CUSTODY", owner_id=OWNER_ID)
        self.assertEqual(
            storage.deleted,
            [
                "objects/00000000000000000000000000000001",
                "staging/00000000000000000000000000000002",
                "staging/00000000000000000000000000000003",
            ],
        )

    async def test_discard_deletes_every_staged_key(self) -> None:
        """Rollback/dedup cleanup delegates every key idempotently."""

        storage = FakeFileStorage()
        service = EvidenceStorageService(storage)
        staged = (
            StagedFile("staging/00000000000000000000000000000001", "image/png", "a" * 64, 1),
            StagedFile("staging/00000000000000000000000000000002", "image/png", "b" * 64, 1),
        )
        await service.discard(staged)
        self.assertEqual(storage.deleted, [item.staging_key for item in staged])
