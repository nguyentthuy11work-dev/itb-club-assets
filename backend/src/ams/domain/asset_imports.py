"""Durable baseline-Asset import preview domain records and repository port."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID

from ams.domain.assets import AssetCondition


class AssetImportError(Exception):
    """Base failure for invalid import preview state or data."""


class AssetImportStatus(StrEnum):
    """Review lifecycle for one uploaded baseline file."""

    PREVIEWED = "PREVIEWED"
    COMPLETED = "COMPLETED"


@dataclass(frozen=True, slots=True)
class BaselineAssetData:
    """Normalized template row ready to create one Asset and pending Custody."""

    name: str
    description: str | None
    source: str
    total_quantity: int
    category_id: UUID
    responsible_team_id: UUID | None
    custodian_user_id: UUID
    condition: AssetCondition
    condition_note: str | None


@dataclass(frozen=True, slots=True)
class AssetImportRow:
    """One reviewed source row with either normalized data or validation errors."""

    row_number: int
    data: BaselineAssetData | None
    errors: tuple[str, ...]

    def __post_init__(self) -> None:
        """Require exactly one valid-data or invalid-errors outcome."""

        if self.row_number < 2:
            raise AssetImportError("Import data row number must follow the header row.")
        if (self.data is None) == (not self.errors):
            raise AssetImportError("Import row must contain data or validation errors.")

    @property
    def valid(self) -> bool:
        """Return whether this row can be persisted after confirmation."""

        return self.data is not None


@dataclass(slots=True)
class AssetImport:
    """Durable reviewed snapshot used by an explicit confirmation command."""

    id: UUID
    created_by_user_id: UUID
    status: AssetImportStatus
    source_filename: str
    source_content_type: str
    source_sha256: str
    rows: tuple[AssetImportRow, ...]
    expires_at: datetime
    created_at: datetime
    confirmed_at: datetime | None = None

    def __post_init__(self) -> None:
        """Validate source identity, counts, timestamps, and lifecycle state."""

        if not self.source_filename.strip() or not self.source_content_type.strip():
            raise AssetImportError("Import source metadata must not be blank.")
        if len(self.source_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in self.source_sha256
        ):
            raise AssetImportError("Import source fingerprint must be lowercase SHA-256.")
        if not self.rows:
            raise AssetImportError("Import preview requires at least one data row.")
        self._assert_utc(self.created_at, "Import creation time")
        self._assert_utc(self.expires_at, "Import expiry")
        if self.expires_at <= self.created_at:
            raise AssetImportError("Import expiry must follow creation time.")
        if self.confirmed_at is not None:
            self._assert_utc(self.confirmed_at, "Import confirmation time")
        if self.status is AssetImportStatus.PREVIEWED and self.confirmed_at is not None:
            raise AssetImportError("Previewed import cannot have confirmation time.")
        if self.status is AssetImportStatus.COMPLETED and self.confirmed_at is None:
            raise AssetImportError("Completed import requires confirmation time.")

    @property
    def valid_row_count(self) -> int:
        """Count rows approved by preview validation."""

        return sum(row.valid for row in self.rows)

    @property
    def invalid_row_count(self) -> int:
        """Count rows rejected by preview validation."""

        return len(self.rows) - self.valid_row_count

    def complete(self, confirmed_at: datetime) -> None:
        """Mark a live reviewed preview as atomically imported."""

        self._assert_utc(confirmed_at, "Import confirmation time")
        if self.status is not AssetImportStatus.PREVIEWED:
            raise AssetImportError("Only a previewed import can be completed.")
        if confirmed_at >= self.expires_at:
            raise AssetImportError("Import preview has expired.")
        if confirmed_at < self.created_at:
            raise AssetImportError("Import confirmation cannot precede creation.")
        self.status = AssetImportStatus.COMPLETED
        self.confirmed_at = confirmed_at

    @staticmethod
    def _assert_utc(value: datetime, label: str) -> None:
        if value.utcoffset() != timedelta(0):
            raise AssetImportError(f"{label} must be timezone-aware UTC.")


class IAssetImportRepository(ABC):
    """Persist and lock durable baseline-import previews."""

    @abstractmethod
    async def add(self, asset_import: AssetImport) -> None:
        """Stage one reviewed preview without committing."""

    @abstractmethod
    async def get_for_update(self, import_id: UUID) -> AssetImport | None:
        """Lock and return one preview for confirmation."""

    @abstractmethod
    async def save(self, asset_import: AssetImport) -> None:
        """Stage preview lifecycle changes without committing."""
