"""Unit tests for durable baseline-import domain invariants."""

from __future__ import annotations

import unittest
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

from ams.domain.asset_imports import (
    AssetImport,
    AssetImportError,
    AssetImportRow,
    AssetImportStatus,
    BaselineAssetData,
)
from ams.domain.assets import AssetCondition

IMPORT_ID = UUID("00000000-0000-4000-8000-000000002401")
USER_ID = UUID("00000000-0000-4000-8000-000000002402")
CATEGORY_ID = UUID("00000000-0000-4000-8000-000000002403")
NOW = datetime(2026, 10, 4, 4, 0, tzinfo=UTC)


class AssetImportDomainTests(unittest.TestCase):
    """Protect reviewed-row and preview lifecycle consistency."""

    def test_rows_and_counts_distinguish_valid_from_rejected(self) -> None:
        """A preview reports valid and invalid rows without losing row numbers."""

        asset_import = self._import()
        self.assertEqual(asset_import.valid_row_count, 1)
        self.assertEqual(asset_import.invalid_row_count, 1)
        self.assertTrue(asset_import.rows[0].valid)
        self.assertFalse(asset_import.rows[1].valid)

    def test_live_preview_completes_once_before_expiry(self) -> None:
        """Explicit confirmation creates one terminal completion timestamp."""

        asset_import = self._import()
        asset_import.complete(NOW + timedelta(hours=1))
        self.assertEqual(asset_import.status, AssetImportStatus.COMPLETED)
        with self.assertRaises(AssetImportError):
            asset_import.complete(NOW + timedelta(hours=2))

    def test_invalid_rows_and_preview_metadata_are_rejected(self) -> None:
        """Ambiguous rows, fingerprints, timestamps, and lifecycle pairs cannot exist."""

        with self.assertRaises(AssetImportError):
            AssetImportRow(1, self._data(), ())
        with self.assertRaises(AssetImportError):
            AssetImportRow(2, None, ())
        with self.assertRaises(AssetImportError):
            AssetImportRow(2, self._data(), ("error",))

        factories: tuple[Callable[[], AssetImport], ...] = (
            lambda: self._import(source_filename=" "),
            lambda: self._import(source_content_type=" "),
            lambda: self._import(source_sha256="bad"),
            lambda: self._import(rows=()),
            lambda: self._import(expires_at=NOW),
            lambda: self._import(created_at=datetime(2026, 10, 4, 4, 0)),
            lambda: self._import(status=AssetImportStatus.PREVIEWED, confirmed_at=NOW),
            lambda: self._import(status=AssetImportStatus.COMPLETED),
        )
        for factory in factories:
            with self.subTest(factory=factory), self.assertRaises(AssetImportError):
                factory()

    def test_invalid_completion_times_are_rejected(self) -> None:
        """Confirmation must be UTC, after creation, and strictly before expiry."""

        values = (
            datetime(2026, 10, 4, 5, 0),
            NOW - timedelta(seconds=1),
            NOW + timedelta(hours=24),
        )
        for value in values:
            with self.subTest(value=value), self.assertRaises(AssetImportError):
                self._import().complete(value)

    @staticmethod
    def _data() -> BaselineAssetData:
        return BaselineAssetData(
            "Camera",
            None,
            "DONATION",
            1,
            CATEGORY_ID,
            None,
            USER_ID,
            AssetCondition.GOOD,
            None,
        )

    @classmethod
    def _import(
        cls,
        *,
        status: AssetImportStatus = AssetImportStatus.PREVIEWED,
        source_filename: str = "baseline.csv",
        source_content_type: str = "text/csv",
        source_sha256: str = "a" * 64,
        rows: tuple[AssetImportRow, ...] | None = None,
        created_at: datetime = NOW,
        expires_at: datetime = NOW + timedelta(hours=24),
        confirmed_at: datetime | None = None,
    ) -> AssetImport:
        return AssetImport(
            id=IMPORT_ID,
            created_by_user_id=USER_ID,
            status=status,
            source_filename=source_filename,
            source_content_type=source_content_type,
            source_sha256=source_sha256,
            rows=(
                rows
                if rows is not None
                else (
                    AssetImportRow(2, cls._data(), ()),
                    AssetImportRow(3, None, ("name is required",)),
                )
            ),
            expires_at=expires_at,
            created_at=created_at,
            confirmed_at=confirmed_at,
        )
