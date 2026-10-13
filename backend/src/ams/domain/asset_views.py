"""Framework-independent Asset and Custody read projections and repository port."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from ams.domain.assets import AssetCondition, CustodyStatus


@dataclass(frozen=True, slots=True)
class HeldAssetView:
    """One current Custody owned by the requesting actor."""

    custody_id: UUID
    asset_id: UUID
    asset_name: str
    status: CustodyStatus
    on_hand_quantity: int
    reserved_quantity: int
    temporarily_loaned_quantity: int
    lendable_quantity: int
    condition: AssetCondition
    assigned_at: datetime
    confirmed_at: datetime | None


@dataclass(frozen=True, slots=True)
class AssetCustodyView:
    """Public current-Custody summary nested under Asset detail."""

    custody_id: UUID
    custodian_user_id: UUID
    status: CustodyStatus
    on_hand_quantity: int
    reserved_quantity: int
    temporarily_loaned_quantity: int
    lendable_quantity: int
    condition: AssetCondition
    condition_note: str | None
    assigned_at: datetime
    confirmed_at: datetime | None
    evidence_count: int


@dataclass(frozen=True, slots=True)
class AssetDetailView:
    """Asset detail with current pending and active physical Custodies."""

    id: UUID
    category_id: UUID
    responsible_team_id: UUID | None
    name: str
    description: str | None
    source: str
    total_quantity: int
    available_quantity: int
    custodies: tuple[AssetCustodyView, ...]


@dataclass(frozen=True, slots=True)
class EligibleCustodyView:
    """Active Custody that can currently satisfy a borrow allocation."""

    source_custody_id: UUID
    lendable_quantity: int


@dataclass(frozen=True, slots=True)
class AssetBrowseView:
    """Public Asset catalog row with borrow-eligible source Custodies."""

    id: UUID
    category_id: UUID
    responsible_team_id: UUID | None
    name: str
    description: str | None
    source: str
    total_quantity: int
    available_quantity: int
    eligible_custodies: tuple[EligibleCustodyView, ...]


class IAssetReadRepository(ABC):
    """Read optimized Asset and Custody projections without aggregate mutation."""

    @abstractmethod
    async def list_held(
        self, actor_id: UUID, *, offset: int, limit: int
    ) -> tuple[tuple[HeldAssetView, ...], int]:
        """Return one stable page of the actor's pending and active Custodies."""

    @abstractmethod
    async def get_detail(self, asset_id: UUID) -> AssetDetailView | None:
        """Return one Asset with current Custody summaries, or ``None``."""

    @abstractmethod
    async def list_assets(
        self,
        *,
        search: str | None,
        category_id: UUID | None,
        team_id: UUID | None,
        offset: int,
        limit: int,
    ) -> tuple[tuple[AssetBrowseView, ...], int]:
        """Return a stably ordered, filtered page of public Asset rows."""

    @abstractmethod
    async def list_for_teams(
        self,
        team_ids: frozenset[UUID],
        *,
        offset: int,
        limit: int,
    ) -> tuple[tuple[AssetBrowseView, ...], int]:
        """Return Assets classified to any Team in the explicit scope."""
