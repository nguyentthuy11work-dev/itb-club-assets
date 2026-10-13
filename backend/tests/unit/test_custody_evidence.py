"""Unit tests for private Custody evidence metadata invariants."""

from __future__ import annotations

import unittest
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

from ams.domain.assets import CustodyEvidence, InvalidAssetDataError

EVIDENCE_ID = UUID("00000000-0000-4000-8000-000000001801")
CUSTODY_ID = UUID("00000000-0000-4000-8000-000000001802")
USER_ID = UUID("00000000-0000-4000-8000-000000001803")
NOW = datetime(2026, 10, 4, 20, 0, tzinfo=UTC)


class CustodyEvidenceTests(unittest.TestCase):
    """Validate evidence storage metadata before persistence."""

    def test_valid_private_evidence_metadata_is_retained(self) -> None:
        """Approved image metadata creates a stable immutable evidence record."""

        evidence = self._evidence()
        self.assertEqual(evidence.content_type, "image/png")
        self.assertEqual(evidence.size_bytes, 1024)

    def test_invalid_key_type_hash_size_note_and_time_are_rejected(self) -> None:
        """Every SQL/storage boundary constraint is enforced in the domain."""

        factories: tuple[Callable[[], CustodyEvidence], ...] = (
            lambda: self._evidence(object_key=" "),
            lambda: self._evidence(evidence_type="UNKNOWN"),
            lambda: self._evidence(content_type="application/pdf"),
            lambda: self._evidence(content_sha256="bad"),
            lambda: self._evidence(content_sha256="G" * 64),
            lambda: self._evidence(size_bytes=0),
            lambda: self._evidence(size_bytes=10 * 1024 * 1024 + 1),
            lambda: self._evidence(note="x" * 1001),
            lambda: self._evidence(created_at=datetime(2026, 10, 4, 20, 0)),
        )
        for index, factory in enumerate(factories):
            with self.subTest(index=index), self.assertRaises(InvalidAssetDataError):
                factory()

    @staticmethod
    def _evidence(
        *,
        object_key: str = "objects/00000000000000000000000000000001",
        content_type: str = "image/png",
        content_sha256: str = "a" * 64,
        size_bytes: int = 1024,
        note: str | None = "Received",
        created_at: datetime = NOW,
        evidence_type: str = "ASSIGNMENT_CONFIRMATION",
    ) -> CustodyEvidence:
        return CustodyEvidence(
            id=EVIDENCE_ID,
            custody_id=CUSTODY_ID,
            uploaded_by_user_id=USER_ID,
            object_key=object_key,
            content_type=content_type,
            content_sha256=content_sha256,
            size_bytes=size_bytes,
            note=note,
            created_at=created_at,
            evidence_type=evidence_type,
        )
