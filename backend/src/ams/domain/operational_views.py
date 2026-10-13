"""Framework-independent operational history and dashboard read models."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from uuid import UUID


class AssetTransactionKind(StrEnum):
    """Immutable source record represented by one Asset history row."""

    ASSET_CREATED = "ASSET_CREATED"
    CUSTODY_ASSIGNED = "CUSTODY_ASSIGNED"
    CUSTODY_CONFIRMED = "CUSTODY_CONFIRMED"
    TICKET_EVENT = "TICKET_EVENT"


class DashboardTodoKind(StrEnum):
    """Machine-readable work item category."""

    OVERDUE_RETURN = "OVERDUE_RETURN"
    TICKET_APPROVAL = "TICKET_APPROVAL"
    RETURN_CONFIRMATION = "RETURN_CONFIRMATION"
    CUSTODY_CONFIRMATION = "CUSTODY_CONFIRMATION"
    OVERDUE_CUSTODY_CONFIRMATION = "OVERDUE_CUSTODY_CONFIRMATION"


class DashboardAction(StrEnum):
    """UI action identifier without embedded executable behavior."""

    MARK_RETURNED = "MARK_RETURNED"
    REVIEW_TICKET = "REVIEW_TICKET"
    CONFIRM_RETURNED = "CONFIRM_RETURNED"
    CONFIRM_CUSTODY = "CONFIRM_CUSTODY"
    REVIEW_CUSTODY = "REVIEW_CUSTODY"


class DashboardReferenceType(StrEnum):
    """Aggregate type addressed by a dashboard work item."""

    TICKET = "TICKET"
    CUSTODY = "CUSTODY"


@dataclass(frozen=True, slots=True)
class AssetTransactionView:
    """One immutable Asset activity projection."""

    id: str
    asset_id: UUID
    asset_name: str
    kind: AssetTransactionKind
    event_type: str
    quantity: int
    occurred_at: datetime
    actor_user_id: UUID
    actor_name: str
    ticket_id: UUID | None = None
    custody_id: UUID | None = None
    from_user_id: UUID | None = None
    to_user_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class DashboardTodoView:
    """Typed work reference consumed by a client ViewModel."""

    id: str
    kind: DashboardTodoKind
    action: DashboardAction
    reference_type: DashboardReferenceType
    reference_id: UUID
    due_date: date | None = None


@dataclass(frozen=True, slots=True)
class MemberDashboardStats:
    """Actor-scoped operational counters."""

    active_borrow_count: int
    overdue_count: int
    pending_approval_count: int
    awaiting_return_confirmation_count: int
    held_quantity: int
    pending_custody_confirmation_count: int


@dataclass(frozen=True, slots=True)
class MemberDashboardView:
    """Member dashboard projection and typed work queue."""

    stats: MemberDashboardStats
    todos: tuple[DashboardTodoView, ...]


@dataclass(frozen=True, slots=True)
class HrDashboardStats:
    """Club-wide HR operational counters."""

    total_asset_quantity: int
    asset_type_count: int
    temporarily_loaned_quantity: int
    overdue_ticket_count: int
    pending_custody_confirmation_count: int
    overdue_custody_confirmation_count: int


@dataclass(frozen=True, slots=True)
class HrDashboardView:
    """HR dashboard projection and current intervention queue."""

    stats: HrDashboardStats
    todos: tuple[DashboardTodoView, ...]


class IOperationalReadRepository(ABC):
    """Load immutable history and dashboard projections."""

    @abstractmethod
    async def get_asset_scope(self, asset_id: UUID) -> tuple[bool, UUID | None]:
        """Return whether the Asset exists and its Responsible Team classification."""

    @abstractmethod
    async def list_transactions(
        self,
        *,
        asset_id: UUID | None,
        from_time: datetime | None,
        to_time: datetime | None,
        offset: int,
        limit: int,
    ) -> tuple[tuple[AssetTransactionView, ...], int]:
        """Return a stable filtered page derived from immutable source records."""

    @abstractmethod
    async def get_member_dashboard(self, actor_id: UUID, *, today: date) -> MemberDashboardView:
        """Return one actor's counters and work queue."""

    @abstractmethod
    async def get_hr_dashboard(
        self, *, today: date, custody_overdue_before: datetime
    ) -> HrDashboardView:
        """Return club-wide HR counters and intervention work."""
