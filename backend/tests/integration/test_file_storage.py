"""Filesystem and provider-neutral object-storage adapter tests for F03.03."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from collections.abc import AsyncIterable
from pathlib import Path
from uuid import UUID

from ams.application.evidence import (
    EvidenceFileInput,
    EvidenceStorageService,
    EvidenceValidationError,
)
from ams.infrastructure.storage import (
    MAX_IMAGE_SIZE_BYTES,
    IObjectStorageClient,
    LocalFileStorage,
    ObjectStorageFileStorage,
)

OWNER_ID = UUID("00000000-0000-4000-8000-000000000901")
PNG = b"\x89PNG\r\n\x1a\nvalid-png-body"
JPEG = b"\xff\xd8\xffvalid-jpeg-body\xff\xd9"
WEBP = b"RIFF\x08\x00\x00\x00WEBPvalid-webp-body"


async def _stream(*chunks: bytes) -> AsyncIterable[bytes]:
    for chunk in chunks:
        yield chunk


class FakeObjectStorageClient(IObjectStorageClient):
    """In-memory private-object client for production adapter contract tests."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.moves: list[tuple[str, str, str, UUID]] = []

    async def put_private(
        self,
        object_key: str,
        content: bytes,
        content_type: str,
        content_sha256: str,
    ) -> None:
        """Store one private object with verified metadata."""

        if hashlib.sha256(content).hexdigest() != content_sha256:
            raise AssertionError("invalid digest")
        self.objects[object_key] = content

    async def move_private(
        self,
        source_key: str,
        target_key: str,
        owner_type: str,
        owner_id: UUID,
    ) -> None:
        """Move one object and record its resource ownership metadata."""

        self.objects[target_key] = self.objects.pop(source_key)
        self.moves.append((source_key, target_key, owner_type, owner_id))

    async def delete_private(self, object_key: str) -> None:
        """Delete an object idempotently."""

        self.objects.pop(object_key, None)


class LocalFileStorageTests(unittest.IsolatedAsyncioTestCase):
    """Verify signatures, limits, opaque keys, ownership moves, and cleanup."""

    async def asyncSetUp(self) -> None:
        """Create storage under the approved temporary workspace."""

        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.storage = LocalFileStorage(self.root)

    async def asyncTearDown(self) -> None:
        """Remove all temporary private objects."""

        self.temporary.cleanup()

    async def test_supported_images_stage_hash_finalize_and_delete(self) -> None:
        """JPEG/PNG/WebP signatures produce private opaque object keys."""

        for payload, content_type in (
            (PNG, "image/png"),
            (JPEG, "image/jpeg"),
            (WEBP, "image/webp"),
        ):
            with self.subTest(content_type=content_type):
                staged = await self.storage.stage(
                    _stream(payload[:5], payload[5:]), content_type
                )
                self.assertEqual(staged.content_sha256, hashlib.sha256(payload).hexdigest())
                self.assertEqual(staged.size_bytes, len(payload))
                self.assertTrue((self.root / staged.staging_key).is_file())

                object_key = await self.storage.finalize(
                    staged.staging_key, "CUSTODY", OWNER_ID
                )
                self.assertTrue(object_key.startswith("objects/"))
                self.assertFalse((self.root / staged.staging_key).exists())
                self.assertEqual((self.root / object_key).read_bytes(), payload)
                await self.storage.delete(object_key)
                await self.storage.delete(object_key)
                self.assertFalse((self.root / object_key).exists())

    async def test_invalid_type_signature_empty_and_size_are_rejected(self) -> None:
        """Declared and actual image policy is enforced before a file is written."""

        cases = (
            (_stream(PNG), "application/pdf"),
            (_stream(PNG), "image/jpeg"),
            (_stream(b"not-an-image"), "image/png"),
            (_stream(), "image/png"),
            (_stream(b"\x89PNG\r\n\x1a\n", b"x" * MAX_IMAGE_SIZE_BYTES), "image/png"),
        )
        for content, content_type in cases:
            with (
                self.subTest(content_type=content_type),
                self.assertRaises(EvidenceValidationError),
            ):
                await self.storage.stage(content, content_type)
        self.assertEqual(list(self.root.rglob("*")), [])

    async def test_batch_failure_and_path_traversal_leave_no_orphans(self) -> None:
        """Failed multipart staging cleans prior files and unsafe keys are refused."""

        service = EvidenceStorageService(self.storage)
        with self.assertRaises(EvidenceValidationError):
            await service.stage_images(
                (
                    EvidenceFileInput(_stream(PNG), "image/png"),
                    EvidenceFileInput(_stream(b"bad"), "image/png"),
                )
            )
        self.assertEqual([path for path in self.root.rglob("*") if path.is_file()], [])
        with self.assertRaises(EvidenceValidationError):
            await self.storage.delete("../outside")
        with self.assertRaises(EvidenceValidationError):
            await self.storage.finalize("objects/" + "a" * 32, "CUSTODY", OWNER_ID)
        with self.assertRaises(EvidenceValidationError):
            await self.storage.finalize("staging/" + "a" * 32, "bad/type", OWNER_ID)

    async def test_application_rollback_cleanup_removes_staged_blob(self) -> None:
        """A failed business transaction can remove every unreferenced staged blob."""

        service = EvidenceStorageService(self.storage)
        staged = await service.stage_images((EvidenceFileInput(_stream(PNG), "image/png"),))
        self.assertTrue((self.root / staged[0].staging_key).is_file())

        await service.discard(staged)

        self.assertEqual([path for path in self.root.rglob("*") if path.is_file()], [])


class ObjectStorageFileStorageTests(unittest.IsolatedAsyncioTestCase):
    """Verify the production adapter delegates only validated private objects."""

    async def test_provider_adapter_stage_finalize_and_delete(self) -> None:
        """Validated bytes and ownership metadata cross the provider boundary."""

        client = FakeObjectStorageClient()
        storage = ObjectStorageFileStorage(client)
        staged = await storage.stage(_stream(PNG), "image/png")
        self.assertIn(staged.staging_key, client.objects)

        object_key = await storage.finalize(staged.staging_key, "CUSTODY", OWNER_ID)
        self.assertNotIn(staged.staging_key, client.objects)
        self.assertEqual(client.objects[object_key], PNG)
        self.assertEqual(client.moves[0][2:], ("CUSTODY", OWNER_ID))

        await storage.delete(object_key)
        self.assertEqual(client.objects, {})

    async def test_provider_adapter_rejects_invalid_keys_and_owner(self) -> None:
        """Provider calls cannot bypass key or owner validation."""

        storage = ObjectStorageFileStorage(FakeObjectStorageClient())
        with self.assertRaises(EvidenceValidationError):
            await storage.finalize("objects/" + "a" * 32, "CUSTODY", OWNER_ID)
        with self.assertRaises(EvidenceValidationError):
            await storage.finalize("staging/" + "a" * 32, "bad", OWNER_ID)
        with self.assertRaises(EvidenceValidationError):
            await storage.delete("/absolute/path")
