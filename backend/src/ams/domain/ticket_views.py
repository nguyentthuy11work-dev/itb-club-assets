"""Ownership-scoped Borrow Ticket projections and query repository contract."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from uuid import UUID

from ams.domain.tickets import TicketMethod, TicketStatus


class TicketPerspective(StrEnum):
    """Actor relationship used to filter a Ticket page."""

    ALL = "ALL"
    BORROWER = "BORROWER"
    CUSTODIAN = "CUSTODIAN"


@dataclass(frozen=True, slots=True)
class TicketListItemView:
    """One ownership-safe Ticket list row."""

    id: UUID
    borrower_user_id: UUID
    borrower_name: str
    source_custodian_user_id: UUID
    source_custodian_name: str
    method: TicketMethod
    status: TicketStatus
    due_date: date | None
    total_quantity: int
    asset_names: tuple[str, ...]
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class TicketItemView:
    """One normalized Ticket item with safe Asset presentation data."""

    id: UUID
    asset_id: UUID
    asset_name: str
    source_custody_id: UUID
    quantity: int


@dataclass(frozen=True, slots=True)
class TicketEvidenceView:
    """Safe evidence metadata that never exposes its private object key."""

    id: UUID
    uploaded_by_user_id: UUID
    evidence_type: str
    content_type: str
    size_bytes: int
    note: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class TicketEventView:
    """Immutable actor-attributed Ticket history row."""

    id: UUID
    actor_user_id: UUID
    actor_name: str
    event_type: str
    from_status: TicketStatus | None
    to_status: TicketStatus
    note: str | None
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class TicketDetailView:
    """Complete Ticket detail visible only to its borrower or custodian."""

    summary: TicketListItemView
    items: tuple[TicketItemView, ...]
    evidence: tuple[TicketEvidenceView, ...]
    events: tuple[TicketEventView, ...]


class ITicketReadRepository(ABC):
    """Load read-optimized Ticket pages and ownership-scoped details."""

    @abstractmethod
    async def list_for_actor(
        self,
        actor_id: UUID,
        *,
        perspective: TicketPerspective,
        status: TicketStatus | None,
        search: str | None,
        asset_id: UUID | None,
        offset: int,
        limit: int,
    ) -> tuple[tuple[TicketListItemView, ...], int]:
        """Return one stable filtered Ticket page for an involved actor."""

    @abstractmethod
    async def get_for_actor(self, ticket_id: UUID, actor_id: UUID) -> TicketDetailView | None:
        """Return detail only when the actor is borrower or source custodian."""
